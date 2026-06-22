"""Load env + models.yaml once, and configure logging."""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def setup_logging(level: str | None = None) -> None:
    """configure timestamped logs to stdout (cloud logging picks these up). level from
    LOG_LEVEL env, default INFO. call once at an entrypoint."""
    logging.basicConfig(
        level=(level or os.getenv("LOG_LEVEL", "INFO")).upper(),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


@lru_cache
def settings() -> dict:
    with open(ROOT / "config" / "models.yaml") as f:
        cfg = yaml.safe_load(f)
    cfg["odoo"] = {
        "url": os.getenv("ODOO_URL", "http://localhost:8069"),
        "db": os.getenv("ODOO_DB", "huma"),
        "user": os.getenv("ODOO_USER", ""),
        "password": os.getenv("ODOO_PASSWORD", ""),
    }
    cfg["gmail"] = {  # imap ingestion; empty => fall back to the folder inbox
        "user": os.getenv("GMAIL_USER", ""),
        "app_password": os.getenv("GMAIL_APP_PASSWORD", ""),
    }
    return cfg


def models() -> dict:
    return settings()["extraction"]


def thresholds() -> dict:
    return settings()["thresholds"]
