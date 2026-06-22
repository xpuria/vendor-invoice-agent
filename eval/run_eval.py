"""Field-level extraction accuracy against a hand-labelled golden set.

    uv run python eval/run_eval.py

Runs the (cached) extractor on each sample invoice and compares every field to the expected
value, reporting per-field and overall accuracy. This is both the accuracy report and a
regression check: swap the model in config/models.yaml and re-run to compare.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import app.extract as extract_mod
from app.config import ROOT, models
from app.extract import extract
from app.models import ExtractionResult

GOLDEN = Path(__file__).resolve().parent / "golden_set.json"
INBOX = ROOT / "data" / "inbox"

FIELDS = [
    "vendor_name", "invoice_number", "invoice_date", "due_date",
    "total_amount", "currency", "line_items_count", "payment_terms",
]


def _norm(s) -> str:
    s = str(s or "").strip().lower().replace("—", "-").replace("–", "-")
    return re.sub(r"\s+", " ", s)


def compare_field(field: str, got, expected) -> bool:
    if field == "line_items_count":
        return got == expected
    if field == "total_amount":
        if got is None or expected is None:
            return got == expected
        return abs(float(got) - float(expected)) < 0.01
    if field in ("invoice_date", "due_date"):
        return _norm(got) == _norm(expected)
    if field == "currency":
        return _norm(got) == _norm(expected)
    if field == "payment_terms":
        g, e = _norm(got), _norm(expected)
        if not g and not e:
            return True
        return bool(g) and bool(e) and (e in g or g in e)
    # vendor_name, invoice_number: normalized exact (handles null == null)
    return _norm(got) == _norm(expected)


def _extracted_values(res: ExtractionResult) -> dict:
    d = res.data
    return {
        "vendor_name": d.vendor_name,
        "invoice_number": d.invoice_number,
        "invoice_date": d.invoice_date.isoformat() if d.invoice_date else None,
        "due_date": d.due_date.isoformat() if d.due_date else None,
        "total_amount": d.total_amount,
        "currency": d.currency,
        "line_items_count": len(d.line_items),
        "payment_terms": d.payment_terms,
    }


def main(model: str | None = None) -> None:
    if model:
        # Override the primary model for this run (cache is model-keyed, so no collision).
        base = models()
        extract_mod.models = lambda: {**base, "primary": model}
        print(f"model override: {model}")
    else:
        print(f"model: {models()['primary']}")

    golden = json.loads(GOLDEN.read_text())
    per_field = {f: [0, 0] for f in FIELDS}  # [correct, total]
    mismatches: list[str] = []

    for name, expected in golden.items():
        got = _extracted_values(extract(INBOX / name))
        for f in FIELDS:
            ok = compare_field(f, got[f], expected[f])
            per_field[f][0] += int(ok)
            per_field[f][1] += 1
            if not ok:
                mismatches.append(f"{name:28} {f:16} expected={expected[f]!r} got={got[f]!r}")

    print(f"{'field':18}{'accuracy':>10}")
    total_ok = total = 0
    for f in FIELDS:
        ok, n = per_field[f]
        total_ok += ok
        total += n
        print(f"{f:18}{f'{ok}/{n}':>10}  {100*ok/n:5.1f}%")
    print(f"{'OVERALL':18}{f'{total_ok}/{total}':>10}  {100*total_ok/total:5.1f}%")

    if mismatches:
        print("\nmismatches:")
        for m in mismatches:
            print("  " + m)


if __name__ == "__main__":
    arg = None
    if "--model" in sys.argv:
        arg = sys.argv[sys.argv.index("--model") + 1]
    main(arg)
