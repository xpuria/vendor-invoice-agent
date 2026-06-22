"""The data contract.

`InvoiceData` is the fixed shape every extractor must produce, regardless of which
LLM/provider read the PDF. Keeping this schema stable is what makes the rest of the
pipeline model-agnostic: swap the model, the contract is unchanged.
"""

from __future__ import annotations

from datetime import date
from enum import Enum

from pydantic import BaseModel, Field


class LineItem(BaseModel):
    description: str
    quantity: float | None = None
    unit_price: float | None = None
    amount: float | None = None


class InvoiceData(BaseModel):
    """Fields the brief requires us to capture from every vendor bill."""

    vendor_name: str | None = None
    invoice_number: str | None = None
    invoice_date: date | None = None
    total_amount: float | None = None
    currency: str | None = None
    line_items: list[LineItem] = Field(default_factory=list)
    payment_terms: str | None = None  # e.g. "Net 30", "Charged to card", "Paid"
    due_date: date | None = None
    # informational only (recorded in the audit trail, never drives post/flag)
    document_type: str | None = None  # "invoice" | "receipt" | "tax invoice" ...
    already_paid: bool | None = None  # true when the doc says paid / is a receipt


class FieldConfidence(BaseModel):
    field: str
    confidence: float  # 0..1, as reported by the extractor


class ExtractionResult(BaseModel):
    data: InvoiceData
    confidences: list[FieldConfidence] = Field(default_factory=list)
    source_path: str
    extractor_model: str  # which model produced this — recorded for the audit trail
    used_ocr: bool = False  # true when the text came from tesseract ocr (a scanned pdf)
    used_fallback: bool = False  # true when we fell back to llm vision on a scan
    overall_confidence: float | None = None

    def confidence_of(self, field: str) -> float | None:
        for fc in self.confidences:
            if fc.field == field:
                return fc.confidence
        return None


class Decision(str, Enum):
    AUTO_POST = "AUTO_POST"
    FLAG_INCOMPLETE = "FLAG_INCOMPLETE"
    FLAG_NEW_VENDOR = "FLAG_NEW_VENDOR"
    FLAG_DUPLICATE = "FLAG_DUPLICATE"
    FLAG_LOW_CONFIDENCE = "FLAG_LOW_CONFIDENCE"
    FLAG_DISCREPANCY = "FLAG_DISCREPANCY"  # extracted field conflicts with supplier master


class ValidationResult(BaseModel):
    is_complete: bool
    missing_fields: list[str] = Field(default_factory=list)
    vendor_approved: bool = False
    matched_supplier: str | None = None
    match_score: float | None = None
    is_duplicate: bool = False
    decision: Decision
    reasons: list[str] = Field(default_factory=list)

    @property
    def is_auto_post(self) -> bool:
        return self.decision == Decision.AUTO_POST
