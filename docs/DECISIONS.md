# Architecture & Decisions

Why the system is shaped the way it is. Each entry: the decision, the rationale, the
trade-off. These are the talking points for the call.

## Design philosophy

Exactly **one** step is genuinely "AI" — reading messy PDFs into structured fields.
Everything else (orchestration, validation, supplier matching, duplicate detection, the Odoo
writes) is **deterministic code**: testable, predictable, auditable. We reach for an LLM only
where it earns its keep, and never let it make the post-vs-flag compliance decision. This is
the core thesis of the build: *automate the clean path, escalate everything doubtful, and keep
a transparent audit trail of every action.*

## Pipeline shape

```
inbox (folder poller)  ->  pdf filter  ->  EXTRACT (LLM, the only AI step)
   ->  validate (deterministic: complete? approved? duplicate?)
   ->  AUTO_POST  ───────────────► Odoo: create account.move + action_post
   └─  FLAG_*     ───────────────► Odoo: create DRAFT + mail.activity + chatter note
   ->  audit (message_post on every event)  ->  move file to processed/
```

The deterministic validator is the spine. The LLM hands it typed data; the validator alone
decides post vs flag.

## Key decisions

| Area | Decision | Rationale / trade-off |
|---|---|---|
| Package mgmt | `uv` + `pyproject.toml`, extras for adk/dashboard | Reproducible, fast; heavy deps opt-in |
| Agent framework | Google ADK orchestrates (M5); PydanticAI does the typed LLM calls | ADK = "built on ADK" narration layer; PydanticAI = schema-validated outputs with native retries |
| Odoo | Local Docker Odoo 17, XML-RPC | Free, resettable, a real API write |
| Inbox | Mock = folder poller behind an `InboxSource` Protocol | Demoable now; Gmail is a drop-in swap |
| Extraction | `pymupdf4llm` → Markdown → PydanticAI agent → `InvoiceData` | Deterministic text first; typed, model-agnostic |
| Scans / OCR | Tesseract OCR (via PyMuPDF) when no text layer; LLM-vision as last resort | Born-digital uses the text layer; scans are OCR'd to text; vision only if OCR yields nothing |
| Model strategy | Config-driven confidence ladder (primary→escalation) | Cost-aware, provider-agnostic |
| Vendor match | PydanticAI agent vs the loaded supplier list | LLM handles legal-suffix/brand variants; returns a typed verdict with a supplier_id only for the same entity |
| Duplicate key | `(matched_supplier or vendor, invoice_number)` + Odoo `ref` | Two-layer guard, idempotent re-runs |
| Human review | Odoo draft + activity + chatter, plus Streamlit (stretch) | Meets brief; dashboard is enablement signal |

## Sample decision matrix

Derived from reading the actual PDFs + supplier list. This **is** the acceptance spec — the
eval golden set asserts against it.

Confirmed by a full live run (gpt-4o-mini extraction + LLM supplier match against stub Odoo):

| File | Vendor | Inv # | Due date | On approved list | Decision (verified) |
|---|---|---|---|---|---|
| 01_AWS | Amazon Web Services EMEA SARL | EUINGB26-2041785 | none (card on file, PAID) | ✅ SUP-0001 | FLAG_INCOMPLETE |
| 02_GoogleCloud | Google Cloud EMEA Limited | 3920184473 | Net 30 | ✅ SUP-0002 | AUTO_POST |
| 03_Atlassian | Atlassian Pty Ltd | AT-558210 | 2026-05-17 | ✅ SUP-0003 | AUTO_POST |
| 04_Sentry | Functional Software, Inc. (Sentry) | Receipt # 2026-3391 | none (PAID receipt) | ✅ SUP-0004 | FLAG_INCOMPLETE |
| 05_Communere | Communere Ltd | CMN-0091 | Net 14 | ✅ SUP-0005 | AUTO_POST |
| 06_Northwind | Northwind Office Supplies Ltd | 7741 | 2026-05-28 | ❌ not listed | FLAG_NEW_VENDOR (SE) |
| 07_Atlassian_resend | Atlassian Pty Ltd | AT-558210 (dup of #03) | 2026-05-17 | ✅ | FLAG_DUPLICATE |
| 08_GitHub | GitHub, Inc. | **missing** | 2026-06-02 | ✅ SUP-0006 | FLAG_INCOMPLETE |

## Judgment calls (ambiguity in the brief)

1. **All seven fields are strictly mandatory — literal reading.** The brief's "Data to
   extract" table lists due_date as required and states "valid only when all mandatory fields
   are present." So AWS and Sentry (card charge / paid receipt, no due date) **flag as
   incomplete** rather than auto-post. We deliberately do **not** invent an "immediate
   payment => derive due date" exception: it is not in the requirements, and flagging for a
   human is the safe default (a person confirms the card charge in seconds). This is a
   conscious choice to follow the controls exactly.
2. **Receipts vs invoices.** `04_Sentry` is a RECEIPT with id "Receipt #". The extractor maps
   it to `invoice_number` and sets `already_paid=True` (informational, recorded in the audit
   note — never drives the decision).
3. **Vendor-name matching is delegated to an LLM** (not hand-rolled normalization). It handles
   legal-suffix and parenthetical variants ("Functional Software, Inc. (Sentry)" == SUP-0004)
   and only returns a supplier_id for the same legal entity.
4. **Ambiguity → escalate, never guess (currency cross-check).** Some invoices show a bare "$"
   with no currency code (Atlassian is an AU entity, so the LLM may read it as AUD). Rather than
   trust the guess or silently override it, we **cross-check the extracted currency against the
   supplier master** (the Approved Supplier List has a currency per vendor). Agreement →
   auto-post; disagreement → `FLAG_DISCREPANCY` with a precise note for a human. This turns the
   master into a *check*, not a silent correction — a currency mismatch can also signal a wrong
   entity or anomaly, which a human should see. This is the field-level extension of the
   human-in-the-loop principle: the eval target is not 100% extraction, it is 100% *safe
   decisions* — the validator never auto-posts on an uncertain field.

## Scalability

The runner is a thin demo driver; the *unit of work* (`process_one`) is stateless and
idempotent-keyed, which is what actually matters for scale. Concrete properties built in:

- **Concurrency:** `run.py` fans `process_one` across a bounded thread pool. 1000 files/day
  (~0.7/min) is trivial; the same unit of work scales horizontally (one worker per pod) with
  no code change.
- **Race-free exactly-once:** duplicates are caught by an **atomic SQLite `claim()`** (UNIQUE
  insert), not check-then-set — so concurrent workers can never post the same invoice twice
  (proved by `test_claim_race_exactly_one_winner`: 1 winner / 49 duplicates out of 50 threads).
- **No dropped invoices:** transient API failures retry with exponential backoff; exhausted
  files go to a **dead-letter** dir with the traceback, never silently lost.

Production swap (interfaces already in place): folder poller → Pub/Sub/SQS consumer; thread
pool → Cloud Run autoscale (concurrency=N); SQLite → Postgres/Redis; plus the Odoo `ref`
cross-check as a second idempotency layer.

## Tooling rationale — why each choice, and the alternative rejected

**Extraction → PydanticAI.** Typed, schema-validated output in one call, with native
retry-when-the-JSON-doesn't-fit-the-schema, and model-agnostic by id. *Rejected:* the raw
OpenAI SDK (manual JSON parsing, no typed retry); LangChain (heavier, leakier abstractions);
`instructor` (similar, but PydanticAI also gives the agent layer). *DSPy* is the right tool
when you have a labelled set and want to **optimize** prompts against a metric — overkill now,
but a natural next step once the eval golden set grows.

**Why not rely only on OCR for parsing.** OCR (Tesseract) returns raw characters with no
understanding of *which* string is the invoice number vs a date vs the vendor. Invoices vary
wildly in layout; mapping label→field is exactly what an LLM does well and OCR does not. Our
sample invoices are also born-digital (a perfect text layer), so running them through OCR
would be a lossy detour. So OCR is a **fallback for scans**, not the primary path:
text layer → Tesseract OCR (scans) → LLM vision (last resort), with the LLM always doing the
field mapping.

**Why not n8n (or similar workflow tools).** n8n is for automations where the AI is minor and
the value is the **glue** — many SaaS integrations, owned by ops, input and output living in
one low-code platform (e.g. "email arrives → classify → push to CRM/Slack"). This task's value
is *auditable compliance logic*: the decision precedence, the atomic dedup, the audit trail —
fiddly rules that must be unit-tested, code-reviewed, and version-controlled. That is a **code**
story; encoding it in n8n function-nodes is hard to test and diff. The production-grade answer
is hybrid: n8n on the *outside* (ingestion, notifications) calling our tested core as an API.

**Why not LangGraph.** LangGraph shines for complex, stateful, cyclic multi-agent control
flow. Our pipeline is linear and deterministic, so a plain code pipeline is simpler and just
as correct — no graph runtime needed.

**Google ADK + PydanticAI (two layers, not redundant).** ADK is the agent-framework layer
(orchestrates the pipeline as tools, narrates) and the JD signal; PydanticAI is the typed LLM
I/O layer. Each is used for its strength. The ADK agent never makes the post/flag decision —
that stays in `rules.decide`.

**Model: gpt-4.1-mini primary, gpt-4.1 vision/escalation.** Provider is OpenAI (the key we
had); the 4.1 family is newer/stronger than 4o; mini balances cost/quality with a ladder to
the full model when unsure. The model-agnostic config means swapping to Claude (a strong pick
for document extraction) is a one-line change once an Anthropic key exists.

**Vendor match → LLM, not rapidfuzz.** The match must handle legal-suffix, brand, and
parenthetical variants ("Sentry" == "Functional Software, Inc. (Sentry)"); a string-similarity
score misses those. The list load stays deterministic; only the fuzzy judgment is the LLM's,
returned as a typed verdict.

**Odoo → XML-RPC.** Odoo's documented, stable external API; stdlib `xmlrpc.client`; works
against the local Docker instance. *Rejected:* UI automation (fragile); a custom Odoo module
(overkill for the slice). The `OdooClient` interface + stub lets the whole pipeline run and be
tested without a live Odoo.

**Dedup → SQLite atomic claim.** Race-free exactly-once under concurrency (proven by a
50-thread test) plus the Odoo `ref` cross-check. *Rejected:* a JSON file (rewrite-on-write,
racey) and in-memory (lost across runs).

**Infra/DX.** `uv` for fast reproducible installs with extras isolating heavy deps; Streamlit
for the fastest real review UI a non-engineer can use; Gmail via stdlib IMAP (app password, no
OAuth setup) with the Gmail API as the finer-scoped alternative.

**Troubleshooting → four layers.** Timestamped stdout **logs** (per stage; `LOG_LEVEL` env,
Cloud-Logging friendly), a **dead-letter** dir with full tracebacks, `run_log.json` per-invoice
outcomes, and the Odoo **chatter** audit note on every bill.

## Deferred (documented, not built)

Real Gmail inbox (interface defined), Cloud Run deployment, secrets manager, retry queue at
scale, tax/multi-currency posting nuance. Each is a one-line "what I'd do next".
