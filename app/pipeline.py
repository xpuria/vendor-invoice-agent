"""The deterministic spine. Calls each stage in order; the LLM never makes the post/flag
decision — `validate.decide` does."""

from __future__ import annotations

import json
import logging
import random
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TypeVar

from app import rules
from app.config import ROOT, thresholds
from app.extract import extract as default_extract
from app.models import ExtractionResult, ValidationResult
from app.odoo import BillRef, OdooClient, post_or_flag
from app.rules import decide

log = logging.getLogger(__name__)

T = TypeVar("T")


# duplicate store — sqlite, concurrency-safe. "check seen() then remember()" is a race; the
# fix is an atomic claim (one insert guarded by a unique constraint) so exactly one worker
# wins and we never double-post.

def _key(vendor: str | None, invoice_number: str | None) -> str:
    return f"{(vendor or '').strip().lower()}::{(invoice_number or '').strip().lower()}"


class SeenStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else ROOT / "data" / "seen.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")  # concurrent readers + one writer
        self._conn.execute("CREATE TABLE IF NOT EXISTS seen ("
                           " key TEXT PRIMARY KEY, vendor TEXT, invoice_number TEXT)")
        self._conn.commit()
        self._lock = threading.Lock()

    def claim(self, vendor: str | None, invoice_number: str | None) -> bool:
        """Atomically record the invoice. Return True if it was ALREADY present (duplicate).
        No invoice number => cannot dedup => not-a-duplicate (fails completeness on its own)."""
        if not invoice_number:
            return False
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO seen(key, vendor, invoice_number) VALUES(?,?,?)",
                (_key(vendor, invoice_number), vendor, invoice_number))
            self._conn.commit()
            return cur.rowcount == 0  # 0 rows inserted => key already existed => duplicate

    def seen(self, vendor: str | None, invoice_number: str | None) -> bool:
        if not invoice_number:
            return False
        cur = self._conn.execute("SELECT 1 FROM seen WHERE key=?",
                                 (_key(vendor, invoice_number),))
        return cur.fetchone() is not None

    def remember(self, vendor: str | None, invoice_number: str | None) -> None:
        self.claim(vendor, invoice_number)

    def close(self) -> None:
        self._conn.close()


def with_retry(fn: Callable[[], T], *, attempts: int = 3, base_delay: float = 1.0,
               max_delay: float = 20.0, sleep: Callable[[float], None] = time.sleep) -> T:
    """Exponential-backoff retry for transient LLM/API failures (rate limits, 5xx). After
    the last attempt the exception propagates so the caller can dead-letter the file."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - transient API errors are broad
            last = e
            if i == attempts - 1:
                break
            sleep(min(max_delay, base_delay * 2**i) + random.uniform(0, base_delay))
    assert last is not None
    raise last


@dataclass
class Outcome:
    source: Path
    extraction: ExtractionResult
    result: ValidationResult
    bill: BillRef


def process_one(
    attachment: str | Path,
    odoo: OdooClient,
    store: SeenStore,
    *,
    extractor: Callable[..., ExtractionResult] = default_extract,
    matcher: Callable[..., rules.MatchResult] = rules.match,
    move_processed: bool = True,
) -> Outcome:
    attachment = Path(attachment)
    name = attachment.name
    log.info("received %s", name)
    # retry transient api failures; if it still fails it propagates so the runner can
    # dead-letter the file instead of dropping it
    extraction = with_retry(lambda: extractor(attachment))
    data = extraction.data
    log.info("extracted %s model=%s confidence=%s ocr=%s vision=%s", name,
             extraction.extractor_model, extraction.overall_confidence,
             extraction.used_ocr, extraction.used_fallback)

    supplier = matcher(data.vendor_name)
    log.info("matched %s vendor=%r supplier=%r score=%s", name, data.vendor_name,
             supplier.matched_name, supplier.score)
    # atomic claim: records the invoice and tells us if it was already seen — race-free, so
    # the same invoice can never be posted twice
    is_dup = store.claim(data.vendor_name, data.invoice_number) or odoo.bill_exists(
        0, data.invoice_number
    )
    result = decide(extraction, supplier, is_dup, thresholds()["confidence_escalate"])
    log.info("decision %s = %s | %s", name, result.decision.value, " ".join(result.reasons))

    bill = post_or_flag(odoo, extraction, result)
    log.info("odoo %s move_id=%s posted=%s", name, bill.move_id, bill.posted)

    if move_processed:
        dest = ROOT / "data" / "processed" / attachment.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(attachment), str(dest))

    return Outcome(attachment, extraction, result, bill)


# run log — json snapshot of each run, read by the dashboard
RUN_LOG = ROOT / "data" / "run_log.json"


def _to_record(o: Outcome) -> dict:
    e, r, d = o.extraction, o.result, o.extraction.data
    return {
        "source": Path(o.source).name, "source_path": str(o.source),
        "decision": r.decision.value, "reasons": r.reasons,
        "vendor_name": d.vendor_name, "matched_supplier": r.matched_supplier,
        "vendor_approved": r.vendor_approved, "match_score": r.match_score,
        "invoice_number": d.invoice_number,
        "invoice_date": d.invoice_date.isoformat() if d.invoice_date else None,
        "due_date": d.due_date.isoformat() if d.due_date else None,
        "total_amount": d.total_amount, "currency": d.currency,
        "payment_terms": d.payment_terms,
        "line_items": [li.model_dump() for li in d.line_items],
        "missing_fields": r.missing_fields, "extractor_model": e.extractor_model,
        "overall_confidence": e.overall_confidence,
        "move_id": o.bill.move_id, "posted": o.bill.posted,
    }


def write_run_log(outcomes: list[Outcome]) -> None:
    RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
    RUN_LOG.write_text(json.dumps([_to_record(o) for o in outcomes], indent=2))


def read_run_log() -> list[dict]:
    return json.loads(RUN_LOG.read_text()) if RUN_LOG.exists() else []
