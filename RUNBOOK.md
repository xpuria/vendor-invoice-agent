# Runbook — Odoo Invoicing Agent

How to operate the service, triage failures, and roll back. The guiding rule: the agent
auto-posts only what is clean and approved; everything doubtful is flagged for a human and
nothing is ever silently dropped.

## Operate

Start from scratch:

```bash
uv sync --extra adk --extra dashboard
docker compose up -d
uv run python scripts/odoo_setup.py     # create DB + install Accounting (idempotent)
uv run python scripts/odoo_check.py     # confirm XML-RPC login
```

Process the inbox:

```bash
uv run python -m app.run --real         # real Odoo; omit --real for the stub
uv run streamlit run dashboard/review_app.py   # human review surface
```

Configuration:
- `config/models.yaml` — model tiers + thresholds. Change a value, no code change.
- `.env` — `OPENAI_API_KEY`, `ODOO_*`. Never committed.

State on disk (all under `data/`):
- `seen.db` — SQLite duplicate store (atomic claim).
- `run_log.json` — the latest run's outcomes (the dashboard reads this).
- `dead_letter/` — files that failed all retries, with a `.error.txt` traceback.
- `.cache/extractions/` — cached extractions, keyed by file hash + model.

## What to watch

- **Logs** — timestamped, per-stage, to stdout (received → extracted → matched → decision →
  odoo). Set `LOG_LEVEL=DEBUG` in `.env` for more detail. In Cloud Run these land in Cloud
  Logging; locally they print to the console.
- `data/dead_letter/` non-empty — something failed processing. Triage below.
- Distribution of decisions in the run output — a spike in `FLAG_*` may signal a model or
  Odoo problem, not bad invoices.
- Odoo: vendor bills in `draft` with an open `mail.activity` are the human review queue.

Four troubleshooting layers exist: the **logs** above, the **dead-letter** tracebacks,
`data/run_log.json` (per-invoice outcomes), and the Odoo **chatter** audit note on each bill.

## Triage

**Files in `data/dead_letter/`.** Read the `<name>.error.txt`. Common causes:
- LLM API rate limit / 5xx — already retried with backoff; if persistent, the provider is
  down. Re-drive once healthy: move the PDF back into `data/inbox/` and re-run.
- Malformed/corrupt PDF — inspect manually; if unreadable, route to a human.
- Odoo unreachable — bring Odoo up (`docker compose up -d`), then re-drive the file.
Re-driving is safe: the duplicate guard prevents double-posting.

**Everything flags as duplicate.** Expected if the bills already exist in Odoo (the `ref`
guard) or `seen.db` already has the keys. This is correct idempotency on a re-run, not a bug.
For a clean demo: `uv run python scripts/odoo_reset.py` and delete `data/seen*.db`.

**Odoo auth fails.** Check `.env` `ODOO_*`; run `scripts/odoo_check.py`. The local Docker
login is `admin` / `admin`, db `huma`.

**An approved invoice was flagged (e.g. currency discrepancy).** Working as designed — the
agent escalated genuine ambiguity. A human resolves it in the dashboard (fill/confirm the
field, then Approve & Post).

**Extraction looks wrong.** Re-run the eval to quantify: `uv run python eval/run_eval.py`.
Compare models with `--model`. The validator fails safe — a bad field flags, never mis-posts.

## Rollback

- **A bill was posted in error.** In Odoo: reset it to draft (`button_draft`) and correct or
  cancel it. `scripts/odoo_reset.py` clears all vendor bills (dev/demo only — never prod).
- **A bad model change.** Revert `config/models.yaml`. The cache is keyed by model, so the
  previous model's cached results are intact — no stale collision.
- **A bad deploy (prod).** Cloud Run: redeploy the previous revision. The service is
  stateless; the dedup store and Odoo are the only state.

## Safety guarantees (why this is low-risk to operate)

- **Never double-posts** — atomic SQLite `claim()` + Odoo `ref` cross-check. Proven by a
  50-thread race test.
- **Never auto-posts on doubt** — missing field, unknown vendor, low confidence, or a
  master-data discrepancy all route to a human.
- **Never drops an invoice** — failures go to the dead-letter queue with a traceback.

## Ownership and on-call

Before this ships, name an owner — if no one owns it, it does not ship.

Incident severities:
- SEV-high: a bill posted with wrong data, or an invoice silently lost (should be impossible
  given the guarantees above — if it happens, that is a real bug).
- SEV-med: dead-letter backlog growing (provider or Odoo outage).
- SEV-low: higher-than-usual flag rate (review the model / a vendor's invoice format).

First response: check `data/dead_letter/`, check Odoo is up (`scripts/odoo_check.py`), check
the LLM provider status. Re-driving dead-lettered files is always safe.

## Scaling

See `docs/DECISIONS.md` (Scalability). Short version: the unit of work (`process_one`) is
stateless and idempotent-keyed, so production is queue-in → autoscaled stateless workers →
Postgres/Redis dedup store, with the same retries and dead-letter.
