"""Delete all vendor bills (account.move, in_invoice) so the demo run is repeatable.

    uv run python scripts/odoo_reset.py
"""

from __future__ import annotations

import xmlrpc.client

from app.odoo import XmlRpcOdooClient


def main() -> None:
    c = XmlRpcOdooClient()
    ids = c._kw("account.move", "search", [["move_type", "=", "in_invoice"]])
    if not ids:
        print("no vendor bills to delete")
        return
    # Anything not already draft must be reset before it can be unlinked.
    not_draft = c._kw("account.move", "search",
                      [["move_type", "=", "in_invoice"], ["state", "!=", "draft"]])
    if not_draft:
        # button_draft returns None, and Odoo's XML-RPC server can't serialize a None
        # response (it commits the reset-to-draft first, then fails serializing) — so the
        # state change DID happen; the marshalling Fault is cosmetic and safe to ignore.
        try:
            c._kw("account.move", "button_draft", not_draft)
        except xmlrpc.client.Fault as e:
            if "cannot marshal None" not in str(e):
                raise
    c._kw("account.move", "unlink", ids)
    left = c._kw("account.move", "search_count", [["move_type", "=", "in_invoice"]])
    print(f"deleted {len(ids)} vendor bill(s); remaining: {left}")


if __name__ == "__main__":
    main()
