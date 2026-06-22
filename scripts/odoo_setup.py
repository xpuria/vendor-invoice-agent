"""One-shot Odoo provisioning: create the database and install Accounting.

Idempotent — safe to re-run. After `docker compose up -d`, run:

    uv run python scripts/odoo_setup.py

Uses ODOO_URL / ODOO_DB / ODOO_USER / ODOO_PASSWORD from .env, plus ODOO_MASTER_PW
(the Odoo db-manager master password; defaults to 'admin' for the local Docker image).
"""

from __future__ import annotations

import os
import xmlrpc.client

from app.config import settings


def main() -> None:
    o = settings()["odoo"]
    url, db = o["url"], o["db"]
    user, pw = o["user"] or "admin", o["password"] or "admin"
    master = os.getenv("ODOO_MASTER_PW", "admin")

    db_rpc = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/db")
    if db not in db_rpc.list():
        print(f"creating database {db!r} ...")
        db_rpc.create_database(master, db, False, "en_US", pw, user, "GB")
    else:
        print(f"database {db!r} already exists")

    common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
    uid = common.authenticate(db, user, pw, {})
    models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")

    def kw(model, method, *a, **k):
        return models.execute_kw(db, uid, pw, model, method, list(a), k)

    mod = kw("ir.module.module", "search_read", [["name", "=", "account"]],
             fields=["state"])
    if mod and mod[0]["state"] != "installed":
        print("installing Accounting (account) module ...")
        kw("ir.module.module", "button_immediate_install", [mod[0]["id"]])
    print("account module:", kw("ir.module.module", "search_read",
                                 [["name", "=", "account"]], fields=["state"])[0]["state"])
    print(f"done. login user={user!r} password={pw!r} db={db!r}")


if __name__ == "__main__":
    main()
