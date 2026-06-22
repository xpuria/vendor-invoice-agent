"""Extraction — the only genuinely 'AI' step.

Flow: pymupdf4llm renders the PDF to Markdown (deterministic, free). For a scanned PDF with
no text layer, we OCR it with Tesseract (via PyMuPDF) and use that text; only if OCR still
yields nothing do we fall back to sending the raw PDF to a vision model. A PydanticAI agent
turns the text/image into a schema-validated InvoiceData, with a confidence ladder that
escalates to a stronger model when unsure. Results are cached by file hash so tests and the
eval harness never burn tokens.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import fitz  # pymupdf
import pymupdf4llm
from pydantic import BaseModel, Field
from pydantic_ai import Agent, BinaryContent

from app.config import ROOT, models, thresholds
from app.models import ExtractionResult, FieldConfidence, InvoiceData

_CACHE = ROOT / ".cache" / "extractions"

# where tesseract language data lives (homebrew / linux defaults)
_TESSDATA_CANDIDATES = [
    os.getenv("TESSDATA_PREFIX"), "/opt/homebrew/share/tessdata",
    "/usr/local/share/tessdata", "/usr/share/tesseract-ocr/5/tessdata",
    "/usr/share/tessdata",
]


def _tessdata() -> str | None:
    return next((p for p in _TESSDATA_CANDIDATES if p and Path(p).exists()), None)


def has_text_layer(path: Path) -> bool:
    """True if the PDF carries a real (born-digital) text layer — vs a scan/image."""
    doc = fitz.open(str(path))
    try:
        return any(page.get_text("text").strip() for page in doc)
    finally:
        doc.close()


def ocr_pdf(path: Path) -> str:
    """Render each page and OCR it with Tesseract. Used when there is no text layer."""
    td = _tessdata()
    doc = fitz.open(str(path))
    try:
        return "\n".join(
            page.get_text(textpage=page.get_textpage_ocr(full=True, dpi=200, tessdata=td))
            for page in doc
        )
    finally:
        doc.close()

_INSTRUCTIONS = (
    "Extract the vendor-bill fields from the invoice text or image. Map whatever label the "
    "document uses to the schema field (e.g. 'Receipt #' or 'Invoice No.' -> invoice_number; "
    "'Billing date' or 'Date paid' -> invoice_date). "
    "vendor_name: the full legal entity name including any suffix (SARL, Ltd, Inc, Pty), even "
    "if it wraps across lines in the header. "
    "currency: return the 3-letter ISO 4217 code (USD, EUR, GBP), never a symbol. "
    "total_amount: the final total due INCLUDING tax/VAT, not the subtotal. "
    "Use null for anything genuinely not present — never guess or fabricate a value. Set "
    "already_paid=true only if the document clearly says PAID or is a receipt. Report "
    "overall_confidence in [0,1] and list any unsure fields in low_confidence_fields."
)


class _LLMExtraction(BaseModel):
    """What the LLM returns in one call: the data plus its own confidence self-report."""

    invoice: InvoiceData
    overall_confidence: float = Field(ge=0, le=1)
    low_confidence_fields: list[str] = Field(default_factory=list)


def _agent(model: str) -> Agent:
    return Agent(model, output_type=_LLMExtraction, instructions=_INSTRUCTIONS)


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _to_result(out: _LLMExtraction, path: Path, model: str, used_fallback: bool,
               used_ocr: bool = False) -> ExtractionResult:
    confs = [FieldConfidence(field=f, confidence=0.3) for f in out.low_confidence_fields]
    return ExtractionResult(
        data=out.invoice,
        confidences=confs,
        source_path=str(path),
        extractor_model=model,
        used_ocr=used_ocr,
        used_fallback=used_fallback,
        overall_confidence=out.overall_confidence,
    )


def _call(model: str, text: str, path: Path, use_vision: bool) -> _LLMExtraction:
    if use_vision:
        user = [
            "Extract the invoice fields from this PDF.",
            BinaryContent(data=path.read_bytes(), media_type="application/pdf"),
        ]
    else:
        user = f"Invoice document (Markdown):\n\n{text}"
    return _agent(model).run_sync(user).output


def extract(path: str | Path, use_cache: bool = True) -> ExtractionResult:
    path = Path(path)
    # cache key includes the primary model so models don't collide (lets the eval compare them)
    model_tag = re.sub(r"[^a-zA-Z0-9]", "_", models()["primary"])
    cache_file = _CACHE / f"{_file_hash(path)}_{model_tag}.json"
    if use_cache and cache_file.exists():
        return ExtractionResult.model_validate_json(cache_file.read_text())

    # pymupdf4llm renders the text layer, ocr-ing image pages with tesseract automatically
    native_text = has_text_layer(path)
    md = pymupdf4llm.to_markdown(str(path))
    used_ocr = False
    if not native_text:  # scanned pdf — any text we got came from ocr
        if len(md.strip()) < 50:  # belt-and-braces: ocr explicitly if pymupdf4llm came up dry
            try:
                ocr_md = ocr_pdf(path)
            except Exception:  # noqa: BLE001 - OCR unavailable => let vision handle it
                ocr_md = ""
            if len(ocr_md.strip()) >= 50:
                md = ocr_md
        used_ocr = len(md.strip()) >= 50

    use_vision = len(md.strip()) < 50  # still nothing after ocr => llm vision
    model = models()["fallback"] if use_vision else models()["primary"]

    out = _call(model, md, path, use_vision)
    result = _to_result(out, path, model, use_vision, used_ocr)

    # confidence ladder: escalate to a stronger model if the first pass is unsure
    if result.overall_confidence is not None and result.overall_confidence < thresholds()["confidence_escalate"]:
        esc = models()["escalation"]
        out = _call(esc, md, path, use_vision)
        result = _to_result(out, path, esc, use_vision, used_ocr)

    if use_cache:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(result.model_dump_json(indent=2))
    return result
