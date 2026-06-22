"""De-risk Odoo first: authenticate over XML-RPC and print the server version + uid.

    uv run python scripts/odoo_check.py

Reads ODOO_URL / ODOO_DB / ODOO_USER / ODOO_PASSWORD from .env.
"""

from __future__ import annotations

import sys
import xmlrpc.client

from app.config import settings


def main() -> int:
    o = settings()["odoo"]
    print(f"url={o['url']} db={o['db']} user={o['user']}")
    common = xmlrpc.client.ServerProxy(f"{o['url']}/xmlrpc/2/common")
    try:
        version = common.version()
    except Exception as e:  # noqa: BLE001
        print(f"FAILED to reach Odoo at {o['url']}: {e}")
        return 1
    print(f"server version: {version.get('server_version')}")

    uid = common.authenticate(o["db"], o["user"], o["password"], {})
    if not uid:
        print("AUTH FAILED — check ODOO_DB / ODOO_USER / ODOO_PASSWORD in .env")
        return 1
    print(f"authenticated OK, uid={uid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
