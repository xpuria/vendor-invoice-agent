"""Odoo integration.

`OdooClient` is the interface the pipeline codes against. `StubOdooClient` records the calls
it would make (so the whole spine runs without a live Odoo). `XmlRpcOdooClient` implements the
same interface against a real Odoo via XML-RPC, mapping our InvoiceData onto account.move
(move_type='in_invoice'): find/create the partner, create the bill (post only on AUTO_POST,
else leave draft + a mail.activity for review), and message_post the audit trail on chatter.
`post_or_flag` turns a validation decision into those writes.
"""

from __future__ import annotations

import re
import xmlrpc.client
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Protocol

from app.config import settings
from app.models import ExtractionResult, InvoiceData, ValidationResult


@dataclass
class BillRef:
    move_id: int
    posted: bool


class OdooClient(Protocol):
    def find_or_create_partner(self, name: str) -> int: ...
    def create_bill(self, data, partner_id: int, post: bool) -> BillRef: ...
    def bill_exists(self, partner_id: int, invoice_number: str | None) -> bool: ...
    def find_bill_by_ref(self, invoice_number: str | None) -> int | None: ...
    def add_activity(self, move_id: int, summary: str, note: str) -> None: ...
    def audit(self, move_id: int, message: str) -> None: ...


# in-memory stub
@dataclass
class StubOdooClient:
    """In-memory fake. Records every call for assertions and demo printing."""

    calls: list[tuple] = field(default_factory=list)
    _partners: dict[str, int] = field(default_factory=dict)
    _bills_by_ref: dict[str, int] = field(default_factory=dict)
    _next_id: int = 1000
    verbose: bool = False

    def _log(self, *call):
        self.calls.append(call)
        if self.verbose:
            print("  [odoo]", *call)

    def find_or_create_partner(self, name: str) -> int:
        if name not in self._partners:
            self._next_id += 1
            self._partners[name] = self._next_id
            self._log("create_partner", name, self._partners[name])
        return self._partners[name]

    def create_bill(self, data, partner_id: int, post: bool) -> BillRef:
        self._next_id += 1
        ref = BillRef(self._next_id, posted=post)
        if data.invoice_number:
            self._bills_by_ref[data.invoice_number] = ref.move_id
        self._log("create_bill", data.invoice_number, partner_id, f"post={post}", ref.move_id)
        return ref

    def bill_exists(self, partner_id: int, invoice_number: str | None) -> bool:
        return False  # the seen store is the duplicate guard for the stub

    def find_bill_by_ref(self, invoice_number: str | None) -> int | None:
        return self._bills_by_ref.get(invoice_number) if invoice_number else None

    def add_activity(self, move_id: int, summary: str, note: str) -> None:
        self._log("add_activity", move_id, summary)

    def audit(self, move_id: int, message: str) -> None:
        self._log("audit", move_id, message)


# real xml-rpc client
class XmlRpcOdooClient:
    def __init__(self):
        o = settings()["odoo"]
        self.db, self.user, self.pw = o["db"], o["user"], o["password"]
        self.common = xmlrpc.client.ServerProxy(f"{o['url']}/xmlrpc/2/common", allow_none=True)
        self.models = xmlrpc.client.ServerProxy(f"{o['url']}/xmlrpc/2/object", allow_none=True)
        self.uid = self.common.authenticate(self.db, self.user, self.pw, {})
        if not self.uid:
            raise RuntimeError("Odoo authentication failed — check .env ODOO_* values")

    def _kw(self, model: str, method: str, *args, **kw):
        return self.models.execute_kw(self.db, self.uid, self.pw, model, method, list(args), kw)

    def find_or_create_partner(self, name: str) -> int:
        found = self._kw("res.partner", "search", [["name", "=", name]], limit=1)
        if found:
            return found[0]
        return self._kw("res.partner", "create", {"name": name, "supplier_rank": 1})

    def bill_exists(self, partner_id: int, invoice_number: str | None) -> bool:
        return self.find_bill_by_ref(invoice_number) is not None

    def find_bill_by_ref(self, invoice_number: str | None) -> int | None:
        if not invoice_number:
            return None
        domain = [["move_type", "=", "in_invoice"], ["ref", "=", invoice_number]]
        found = self._kw("account.move", "search", domain, limit=1)
        return found[0] if found else None

    def create_bill(self, data: InvoiceData, partner_id: int, post: bool) -> BillRef:
        lines = [
            (0, 0, {"name": li.description, "quantity": li.quantity or 1,
                    "price_unit": li.unit_price or 0})
            for li in data.line_items
        ] or [(0, 0, {"name": "(no line items extracted)", "quantity": 1,
                      "price_unit": data.total_amount or 0})]
        vals = {"move_type": "in_invoice", "partner_id": partner_id,
                "invoice_line_ids": lines}
        if data.invoice_number:
            vals["ref"] = data.invoice_number  # duplicate guard key in odoo
        if data.invoice_date:
            vals["invoice_date"] = str(data.invoice_date)
        if data.due_date:
            vals["invoice_date_due"] = str(data.due_date)
        move_id = self._kw("account.move", "create", vals)
        if post:
            self._kw("account.move", "action_post", [move_id])
        return BillRef(move_id=move_id, posted=post)

    def add_activity(self, move_id: int, summary: str, note: str) -> None:
        model_id = self._kw("ir.model", "search", [["model", "=", "account.move"]], limit=1)[0]
        self._kw("mail.activity", "create", {
            "res_model_id": model_id, "res_id": move_id,
            "activity_type_id": _todo_activity_type(self),
            "summary": summary, "note": note, "user_id": self.uid,
        })

    def audit(self, move_id: int, message: str) -> None:
        self._kw("account.move", "message_post", [move_id], body=message)

    # read/actions used by the review dashboard

    def get_state(self, move_id: int) -> str | None:
        rows = self._kw("account.move", "read", [move_id], fields=["state"])
        return rows[0]["state"] if rows else None

    def bill_states(self) -> dict[int, str]:
        """Live state of every vendor bill, in one call: {move_id: 'draft'|'posted'|...}."""
        rows = self._kw("account.move", "search_read",
                        [["move_type", "=", "in_invoice"]], fields=["state"])
        return {row["id"]: row["state"] for row in rows}

    def update_bill(self, move_id: int, *, invoice_number=None, invoice_date=None,
                    due_date=None) -> None:
        """Apply a human's corrections to a draft bill (e.g. fill a missing invoice number)."""
        vals: dict = {}
        if invoice_number:
            vals["ref"] = invoice_number
        if invoice_date:
            vals["invoice_date"] = str(invoice_date)
        if due_date:
            vals["invoice_date_due"] = str(due_date)
        if vals:
            self._kw("account.move", "write", [move_id], vals)

    def approve_and_post(self, move_id: int) -> str:
        """Human approves a flagged draft -> post it. Returns the new state."""
        self._kw("account.move", "action_post", [move_id])
        return self.get_state(move_id)

    def chatter(self, move_id: int) -> list[str]:
        msgs = self._kw("mail.message", "search_read", [["model", "=", "account.move"],
                        ["res_id", "=", move_id]], fields=["body"], order="id")
        return [re.sub(r"<[^>]+>", "", m["body"] or "").strip() for m in msgs]


@lru_cache
def _todo_activity_type(client: "XmlRpcOdooClient") -> int:
    # "to do" is the default activity type shipped with mail
    found = client._kw("mail.activity.type", "search", [["name", "=", "To Do"]], limit=1)
    return found[0] if found else 1


# turn a decision into odoo writes
def post_or_flag(odoo: OdooClient, extraction: ExtractionResult,
                 result: ValidationResult) -> BillRef:
    """Turn a validation decision into Odoo writes + a chatter audit trail."""
    data = extraction.data

    def _audit_msg():
        return (f"[agent] decision={result.decision.value} "
                f"model={extraction.extractor_model} "
                f"confidence={extraction.overall_confidence} "
                f"approved={result.vendor_approved} | {' '.join(result.reasons)}")

    # duplicate: don't create a second bill — attach the flag to the existing one
    if result.is_duplicate:
        existing = odoo.find_bill_by_ref(data.invoice_number)
        if existing is not None:
            odoo.add_activity(existing, "Duplicate invoice received", " ".join(result.reasons))
            odoo.audit(existing, _audit_msg())
            return BillRef(existing, posted=False)

    partner_id = odoo.find_or_create_partner(data.vendor_name or "Unknown vendor")
    auto = result.is_auto_post
    bill = odoo.create_bill(data, partner_id, post=auto)
    if not auto:
        odoo.add_activity(bill.move_id, summary=f"Review needed: {result.decision.value}",
                          note=" ".join(result.reasons))
    odoo.audit(bill.move_id, message=_audit_msg())
    return bill
