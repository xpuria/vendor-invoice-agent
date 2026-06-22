"""Wiring: pipeline routing (stub Odoo + fake extractor), retry/dead-letter, the PDF
filter, and the eval scorer. Deterministic — no tokens."""

import sys
from datetime import date
from pathlib import Path

import pytest

from app.inbox import is_pdf, keep_pdfs
from app.models import ExtractionResult, InvoiceData, LineItem
from app.odoo import StubOdooClient
from app.pipeline import process_one, with_retry
from app.pipeline import SeenStore
from app.rules import MatchResult

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "inbox"


# --- pipeline routing -----------------------------------------------------------------

def _fake_extractor(data):
    return lambda _p: ExtractionResult(data=data, source_path=str(_p),
                                       extractor_model="fake", overall_confidence=0.95)


def _complete(**over):
    base = dict(vendor_name="Atlassian Pty Ltd", invoice_number="AT-558210",
                invoice_date=date(2026, 4, 17), total_amount=6875.0, currency="USD",
                line_items=[LineItem(description="Jira", quantity=1, unit_price=3850.0)],
                payment_terms="Net 30", due_date=date(2026, 5, 17))
    base.update(over)
    return InvoiceData(**base)


def _approved(_n=None):
    return MatchResult("Atlassian Pty Ltd", "SUP-0003", 97.0, True, "match", currency="USD")


def _unapproved(_n=None):
    return MatchResult(None, None, 0.0, False, "not on list")


def test_clean_approved_creates_and_posts(tmp_path):
    odoo, store = StubOdooClient(), SeenStore(tmp_path / "s.db")
    out = process_one(tmp_path / "x.pdf", odoo, store, extractor=_fake_extractor(_complete()),
                      matcher=_approved, move_processed=False)
    assert out.result.decision.value == "AUTO_POST" and out.bill.posted is True
    kinds = [c[0] for c in odoo.calls]
    assert "create_bill" in kinds and "add_activity" not in kinds  # clean post, no review


def test_unapproved_creates_draft_and_activity(tmp_path):
    odoo, store = StubOdooClient(), SeenStore(tmp_path / "s.db")
    out = process_one(tmp_path / "x.pdf", odoo, store,
                      extractor=_fake_extractor(_complete(vendor_name="Northwind")),
                      matcher=_unapproved, move_processed=False)
    assert out.result.decision.value == "FLAG_NEW_VENDOR" and out.bill.posted is False
    assert "add_activity" in [c[0] for c in odoo.calls]


def test_second_identical_invoice_is_duplicate(tmp_path):
    odoo, store = StubOdooClient(), SeenStore(tmp_path / "s.db")
    ext = _fake_extractor(_complete())
    process_one(tmp_path / "a.pdf", odoo, store, extractor=ext, matcher=_approved,
                move_processed=False)
    out2 = process_one(tmp_path / "b.pdf", odoo, store, extractor=ext, matcher=_approved,
                       move_processed=False)
    assert out2.result.decision.value == "FLAG_DUPLICATE"


# --- retry / dead-letter --------------------------------------------------------------

def test_retry_succeeds_after_transient_failures():
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] < 3:
            raise RuntimeError("429")
        return "ok"

    assert with_retry(flaky, attempts=3, sleep=lambda _: None) == "ok"


def test_retry_exhausts_and_reraises():
    with pytest.raises(RuntimeError):
        with_retry(lambda: (_ for _ in ()).throw(RuntimeError("boom")),
                   attempts=2, sleep=lambda _: None)


# --- pdf filter -----------------------------------------------------------------------

def test_pdf_filter(tmp_path):
    txt = tmp_path / "note.txt"
    txt.write_text("nope")
    aws = SAMPLES / "01_AWS.pdf"
    assert is_pdf(aws) and not is_pdf(txt)
    assert keep_pdfs([aws, txt]) == [aws]


def test_pipeline_logs_each_stage(caplog, tmp_path):
    import logging
    odoo, store = StubOdooClient(), SeenStore(tmp_path / "s.db")
    with caplog.at_level(logging.INFO, logger="app.pipeline"):
        process_one(tmp_path / "x.pdf", odoo, store,
                    extractor=_fake_extractor(_complete()), matcher=_approved,
                    move_processed=False)
    msgs = " ".join(r.message for r in caplog.records)
    for stage in ("received", "extracted", "matched", "decision", "odoo"):
        assert stage in msgs


# --- OCR for scanned PDFs (deterministic, no tokens) ----------------------------------

def test_ocr_recovers_text_from_scanned_pdf(tmp_path):
    import fitz

    from app.extract import has_text_layer, ocr_pdf

    born_digital = SAMPLES / "01_AWS.pdf"
    assert has_text_layer(born_digital)  # the samples are born-digital

    # Build an image-only PDF (no text layer) by rasterising a sample page.
    src = fitz.open(str(born_digital))
    pix = src[0].get_pixmap(dpi=200)
    scan = fitz.open()
    page = scan.new_page(width=pix.width, height=pix.height)
    page.insert_image(page.rect, pixmap=pix)
    scanned = tmp_path / "scan.pdf"
    scan.save(str(scanned)); scan.close(); src.close()

    assert not has_text_layer(scanned)           # detected as a scan
    text = ocr_pdf(scanned)                       # OCR recovers the content
    assert "amazon" in text.lower() and "invoice" in text.lower()


# --- Gmail integration correctness — verified against a fake IMAP server ---------------
# Proves the code drives IMAP correctly, so it works with valid credentials.

def test_gmail_inbox_fetches_parses_saves_and_marks_seen(monkeypatch, tmp_path):
    import imaplib
    from email.message import EmailMessage

    import app.inbox as inbox

    # a real multipart email carrying a real PDF attachment
    msg = EmailMessage()
    msg["From"] = "AWS Billing <billing@aws.com>"
    msg["Subject"] = "Your April invoice"
    msg.set_content("see attached")
    msg.add_attachment((SAMPLES / "01_AWS.pdf").read_bytes(),
                       maintype="application", subtype="pdf", filename="01_AWS.pdf")
    raw = msg.as_bytes()

    class FakeIMAP:
        def __init__(self, host): self.host, self.stored, self.creds = host, [], None
        def login(self, u, p): self.creds = (u, p); return ("OK", [b""])
        def select(self, mbox): return ("OK", [b"1"])
        def search(self, charset, *crit): return ("OK", [b"1"])
        def fetch(self, num, parts): return ("OK", [(b"1 (RFC822", raw), b")"])
        def store(self, num, flag, val): self.stored.append((num, flag, val)); return ("OK", [b""])
        def logout(self): return ("BYE", [b""])

    fake = FakeIMAP("imap.gmail.com")
    monkeypatch.setattr(imaplib, "IMAP4_SSL", lambda host: fake)
    monkeypatch.setattr(inbox, "settings",
                        lambda: {"gmail": {"user": "me@gmail.com", "app_password": "app-pw"}})

    emails = inbox.GmailInbox(save_dir=tmp_path).fetch()

    assert fake.creds == ("me@gmail.com", "app-pw")          # credentials are used to log in
    assert len(emails) == 1
    e = emails[0]
    assert e.sender == "AWS Billing <billing@aws.com>"
    assert e.subject == "Your April invoice"
    assert len(e.attachments) == 1 and e.attachments[0].exists()
    assert e.attachments[0].read_bytes()[:5] == b"%PDF-"      # the PDF was saved intact
    assert fake.stored == [(b"1", "+FLAGS", "\\Seen")]        # message marked read


# --- eval scorer ----------------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))
from run_eval import compare_field  # noqa: E402


def test_eval_scorer():
    assert compare_field("total_amount", 3072.241, 3072.24)
    assert not compare_field("total_amount", 3072.24, 5340.0)
    assert compare_field("currency", "usd", "USD")
    # em-dash vs hyphen must not be a mismatch
    assert compare_field("payment_terms", "Paid — Visa 4421", "Paid - Visa 4421")
    assert compare_field("vendor_name", "Atlassian Pty Ltd", "atlassian pty ltd")
    assert not compare_field("line_items_count", 0, 4)
