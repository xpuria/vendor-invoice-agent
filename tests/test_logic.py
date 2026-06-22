"""Compliance logic: the contract, the decision rules, supplier matching, and the
race-free duplicate store. All deterministic — no LLM, no tokens."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date

import app.rules as sup
from app.models import (
    Decision, ExtractionResult, InvoiceData, LineItem, ValidationResult,
)
from app.pipeline import SeenStore
from app.rules import MatchResult, SupplierMatch, load_suppliers, match
from app.rules import check_complete, decide


# --- contract -------------------------------------------------------------------------

def test_invoicedata_all_optional_and_roundtrips():
    assert InvoiceData().line_items == []  # partial extraction never crashes
    inv = InvoiceData(vendor_name="Atlassian Pty Ltd", invoice_number="AT-558210",
                      invoice_date=date(2026, 4, 17), total_amount=6875.0,
                      line_items=[LineItem(description="Jira", quantity=1)])
    assert InvoiceData(**inv.model_dump()) == inv


# --- decision rules -------------------------------------------------------------------

def _ext(data, conf=0.95):
    return ExtractionResult(data=data, source_path="x.pdf", extractor_model="test",
                            overall_confidence=conf)


def _complete(**over):
    base = dict(vendor_name="Atlassian Pty Ltd", invoice_number="AT-558210",
                invoice_date=date(2026, 4, 17), total_amount=6875.0, currency="USD",
                line_items=[LineItem(description="Jira", quantity=1, unit_price=3850.0)],
                payment_terms="Net 30", due_date=date(2026, 5, 17))
    base.update(over)
    return InvoiceData(**base)


def _approved(currency="USD"):
    return MatchResult("Atlassian Pty Ltd", "SUP-0003", 97.0, True, "match", currency=currency)


def _unapproved():
    return MatchResult(None, None, 0.0, False, "not on list")


def test_clean_approved_auto_posts():
    assert decide(_ext(_complete()), _approved(), False).decision == Decision.AUTO_POST


def test_missing_due_date_flags_incomplete():        # AWS / Sentry, literal brief reading
    r = decide(_ext(_complete(due_date=None)), _approved(), False)
    assert r.decision == Decision.FLAG_INCOMPLETE and "due_date" in r.missing_fields


def test_missing_invoice_number_flags_incomplete():  # GitHub
    r = decide(_ext(_complete(invoice_number=None)), _approved(), False)
    assert r.decision == Decision.FLAG_INCOMPLETE


def test_unapproved_flags_new_vendor():              # Northwind
    assert decide(_ext(_complete()), _unapproved(), False).decision == Decision.FLAG_NEW_VENDOR


def test_duplicate_flags_and_takes_precedence():     # Atlassian resend
    assert decide(_ext(_complete()), _approved(), True).decision == Decision.FLAG_DUPLICATE
    # duplicate beats incomplete
    assert decide(_ext(_complete(due_date=None)), _approved(), True).decision == \
        Decision.FLAG_DUPLICATE


def test_low_confidence_flags():
    assert decide(_ext(_complete(), conf=0.4), _approved(), False,
                  confidence_threshold=0.7).decision == Decision.FLAG_LOW_CONFIDENCE


def test_currency_discrepancy_flags_for_human():     # invoice "$"->AUD vs master USD
    r = decide(_ext(_complete(currency="AUD")), _approved("USD"), False)
    assert r.decision == Decision.FLAG_DISCREPANCY


def test_matching_currency_auto_posts():
    assert decide(_ext(_complete(currency="USD")), _approved("USD"), False).decision == \
        Decision.AUTO_POST


def test_check_complete_lists_all_missing():
    ok, missing = check_complete(InvoiceData(vendor_name="X"))
    assert not ok and set(missing) == {"invoice_number", "invoice_date", "total_amount",
                                       "line_items", "payment_terms", "due_date"}


# --- supplier matching ----------------------------------------------------------------

def test_load_suppliers_parses_list_with_currency():
    s = load_suppliers()
    names = {x.name for x in s}
    assert {"Atlassian Pty Ltd", "GitHub, Inc."} <= names
    assert not any("northwind" in n.lower() for n in names)   # brief's unapproved example
    assert all(x.currency for x in s)                          # currency master loaded


@dataclass
class _FakeRun:
    output: SupplierMatch


def test_match_maps_verdict(monkeypatch):
    verdict = SupplierMatch(supplier_id="SUP-0003", matched_name="Atlassian Pty Ltd",
                            confidence=0.97)
    monkeypatch.setattr(sup, "_agent", lambda _m: type("A", (), {
        "run_sync": lambda self, _p: _FakeRun(verdict)})())
    r = match("Atlassian Pty Ltd")
    assert r.approved and r.supplier_id == "SUP-0003" and r.currency


def test_match_no_vendor_short_circuits():
    assert match(None).approved is False


# --- duplicate store (atomic, race-free) ----------------------------------------------

def test_claim_is_idempotent(tmp_path):
    s = SeenStore(tmp_path / "seen.db")
    assert s.claim("Atlassian", "AT-1") is False   # first time: not a duplicate
    assert s.claim("Atlassian", "AT-1") is True    # second time: duplicate
    assert s.claim("Atlassian", None) is False     # no number: never a duplicate


def test_claim_race_exactly_one_winner(tmp_path):
    # 50 threads claim the SAME invoice: exactly one wins -> can never post twice.
    s = SeenStore(tmp_path / "seen.db")
    with ThreadPoolExecutor(max_workers=50) as pool:
        results = list(pool.map(lambda _: s.claim("V", "INV-42"), range(50)))
    assert results.count(False) == 1 and results.count(True) == 49
