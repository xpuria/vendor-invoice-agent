"""Human review dashboard for the invoicing agent.

    uv sync --extra dashboard
    uv run streamlit run dashboard/review_app.py

Reads the latest run (data/run_log.json, written by `app.run`) and presents it as a review
queue for a Finance user: a plain-language status, the invoice in human terms, a PDF preview,
and an Approve & Post action for the ones the agent escalated.
"""

from __future__ import annotations

import base64
import sys
from datetime import datetime
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.pipeline import read_run_log  # noqa: E402

st.set_page_config(page_title="Invoice Review", layout="wide")

# Plain-language status per decision: (headline, explanation, severity, needs_review)
STATUS = {
    "AUTO_POST": ("Posted automatically",
                  "Complete and from an approved supplier — posted to Odoo with no action needed.",
                  "ok", False),
    "FLAG_INCOMPLETE": ("Needs review: missing information",
                        "Some required details are missing from the invoice.", "warn", True),
    "FLAG_NEW_VENDOR": ("Needs review: new supplier",
                        "This supplier is not on the Approved Supplier List yet.", "warn", True),
    "FLAG_DUPLICATE": ("Duplicate: already entered",
                       "This invoice number was already processed — not entered twice.", "warn", True),
    "FLAG_DISCREPANCY": ("Needs review: please confirm",
                         "Something on the invoice doesn't match our records.", "warn", True),
    "FLAG_LOW_CONFIDENCE": ("Needs review: unclear invoice",
                            "The reading wasn't confident enough to post automatically.", "warn", True),
}

FIELD_LABELS = {
    "vendor_name": "Supplier", "invoice_number": "Invoice number",
    "invoice_date": "Invoice date", "due_date": "Due date",
    "total_amount": "Amount", "payment_terms": "Payment terms",
}
SYMBOL = {"USD": "$", "EUR": "€", "GBP": "£"}


def money(amount, currency) -> str:
    if amount is None:
        return ":red[not found]"
    sym = SYMBOL.get((currency or "").upper(), "")
    return f"{sym}{amount:,.2f}" + (f" {currency}" if not sym and currency else "")


def _as_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date() if s else None
    except (ValueError, TypeError):
        return None


def field(label: str, value, problem: bool = False) -> str:
    if value in (None, "", []):
        return f"**{label}:** :red[not found]"
    if problem:
        return f"**{label}:** :orange[{value}]"
    return f"**{label}:** {value}"


@st.cache_resource
def _odoo():
    try:
        from app.odoo import XmlRpcOdooClient

        return XmlRpcOdooClient()
    except Exception as e:  # noqa: BLE001
        st.session_state["odoo_error"] = str(e)
        return None


def _pdf_preview(path: str) -> None:
    p = Path(path)
    if not p.exists():
        alt = p.parent.parent / "processed" / p.name
        p = alt if alt.exists() else p
    if not p.exists():
        st.info("Original PDF not available.")
        return
    b64 = base64.b64encode(p.read_bytes()).decode()
    st.markdown(
        f'<iframe src="data:application/pdf;base64,{b64}" width="100%" height="560" '
        'style="border:1px solid #ddd;border-radius:6px;"></iframe>',
        unsafe_allow_html=True,
    )


def _render(r: dict, odoo, state: str | None) -> None:
    headline, explain, severity, needs_review = STATUS.get(
        r["decision"], (r["decision"], "", "warn", True)
    )
    supplier = r.get("matched_supplier") or r.get("vendor_name") or "Unknown supplier"
    amount = money(r.get("total_amount"), r.get("currency"))

    banner = {"ok": st.success, "warn": st.warning}[severity]
    banner(f"{headline}")
    st.caption(explain)
    # The agent's specific reasons, in the plain wording the pipeline already produces.
    for reason in r.get("reasons", []):
        st.write("• " + reason)

    left, right = st.columns([1, 1])
    with left:
        miss = set(r.get("missing_fields", []))
        st.markdown(field("Supplier", supplier))
        st.markdown(field("Invoice number", r.get("invoice_number"),
                          problem="invoice_number" in miss))
        st.markdown(field("Invoice date", r.get("invoice_date"),
                          problem="invoice_date" in miss))
        st.markdown(field("Due date", r.get("due_date"), problem="due_date" in miss))
        st.markdown(f"**Amount:** {amount}")
        st.markdown(field("Payment terms", r.get("payment_terms"),
                          problem="payment_terms" in miss))

        items = r.get("line_items") or []
        if items:
            st.markdown("**Items**")
            st.dataframe(
                [{"Description": it.get("description"), "Qty": it.get("quantity"),
                  "Unit price": it.get("unit_price"), "Amount": it.get("amount")}
                 for it in items],
                hide_index=True, use_container_width=True,
            )
        elif "line_items" in miss:
            st.markdown("**Items:** :red[none found]")

        # Duplicates are informational (already entered) — no posting action.
        if needs_review and r.get("move_id") and r["decision"] != "FLAG_DUPLICATE":
            st.markdown("---")
            if odoo is None:
                st.caption("Turn on 'Connect to Odoo' in the sidebar to edit and post.")
            elif state == "posted":
                st.success("Already posted to Odoo — no further action needed.")
            else:
                st.markdown("**Review and fix details, then post**")
                with st.form(f"edit_{r['source']}"):
                    inv_no = st.text_input("Invoice number",
                                           value=r.get("invoice_number") or "",
                                           placeholder="enter the invoice number")
                    ca, cb = st.columns(2)
                    inv_date = ca.date_input("Invoice date",
                                             value=_as_date(r.get("invoice_date")))
                    due = cb.date_input("Due date", value=_as_date(r.get("due_date")))
                    submitted = st.form_submit_button("Save and post to Odoo", type="primary")
                if submitted:
                    try:
                        odoo.update_bill(r["move_id"], invoice_number=inv_no or None,
                                         invoice_date=inv_date, due_date=due)
                        odoo.approve_and_post(r["move_id"])
                        st.success("Saved your changes and posted to Odoo.")
                        st.rerun()
                    except Exception as e:  # noqa: BLE001
                        st.error(f"Could not post: {e}")

        with st.expander("Technical details"):
            st.caption(
                f"Decision code: {r['decision']} · model: {r.get('extractor_model')} · "
                f"confidence: {r.get('overall_confidence')} · Odoo move id: {r.get('move_id')}"
            )

    with right:
        _pdf_preview(r.get("source_path", ""))


def main() -> None:
    st.title("Invoice Review")
    st.caption("Invoices the agent processed. Approve the ones that need a human check.")

    records = read_run_log()
    if not records:
        st.warning("No invoices yet. Run:  `uv run python -m app.run --real`")
        return

    with st.sidebar:
        st.header("Settings")
        connect = st.toggle("Connect to Odoo", value=False,
                            help="Needed to approve and post invoices.")
    odoo = _odoo() if connect else None
    if connect and odoo is None:
        st.sidebar.error(f"Odoo not reachable: {st.session_state.get('odoo_error', '')}")

    # Live Odoo states (when connected) so a bill that's been posted moves out of the
    # review queue, regardless of what the original run decided.
    states = odoo.bill_states() if odoo is not None else {}

    def needs_action(r: dict) -> bool:
        flagged = STATUS.get(r["decision"], (None,) * 4)[3]
        if not flagged or r["decision"] == "FLAG_DUPLICATE":
            return False  # auto-posted or duplicate: no action
        return states.get(r.get("move_id")) != "posted"  # resolved once posted

    review = [r for r in records if needs_action(r)]
    done = [r for r in records if not needs_action(r)]

    c1, c2 = st.columns(2)
    c1.metric("Need your review", len(review))
    c2.metric("Posted / no action", len(done))
    st.divider()

    def _title(r):
        return (f"{r.get('matched_supplier') or r.get('vendor_name') or 'Unknown'}"
                f"  —  {money(r.get('total_amount'), r.get('currency'))}")

    if review:
        st.subheader("Needs your review")
        for r in review:
            with st.expander(_title(r), expanded=True):
                _render(r, odoo, states.get(r.get("move_id")))

    if done:
        st.subheader("Posted / no action needed")
        for r in done:
            with st.expander(_title(r), expanded=False):
                _render(r, odoo, states.get(r.get("move_id")))


main()
