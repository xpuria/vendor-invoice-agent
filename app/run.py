"""End-to-end runner: poll the mock inbox, process every PDF concurrently, summarise.

    uv run python -m app.run [concurrency]

The folder poller is a stand-in for a queue; `process_one` is a self-contained, idempotent
unit of work, so we fan it out across a bounded thread pool. At 1000 files/day this is
trivially enough; the same unit of work scales horizontally (one worker per process/pod)
without code changes. Failures are routed to a dead-letter directory, never dropped.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from app.config import ROOT, setup_logging
from app.inbox import FolderInbox
from app.odoo import StubOdooClient
from app.pipeline import Outcome, SeenStore, process_one

DEAD_LETTER = ROOT / "data" / "dead_letter"
log = logging.getLogger(__name__)


def _thread_init() -> None:
    # run_sync needs an event loop in its thread; worker threads have none
    asyncio.set_event_loop(asyncio.new_event_loop())


def _dead_letter(path: Path, err: Exception) -> None:
    DEAD_LETTER.mkdir(parents=True, exist_ok=True)
    (DEAD_LETTER / f"{path.name}.error.txt").write_text(
        f"{type(err).__name__}: {err}\n\n{traceback.format_exc()}"
    )


def main(concurrency: int = 8, move_processed: bool = False, real: bool = False,
         gmail: bool = False) -> None:
    setup_logging()
    if real:
        from app.odoo import XmlRpcOdooClient

        odoo = XmlRpcOdooClient()
        concurrency = 1  # one shared xml-rpc serverproxy isn't thread-safe; prod pools it
        print("Using REAL Odoo (XML-RPC), sequential")
    else:
        odoo = StubOdooClient()
    demo_store = ROOT / "data" / "seen_demo.db"
    demo_store.unlink(missing_ok=True)
    store = SeenStore(demo_store)

    if gmail:
        from app.inbox import GmailInbox

        source = GmailInbox()
        print("Using REAL Gmail inbox (IMAP)")
    else:
        source = FolderInbox()
    attachments = [a for e in source.fetch() for a in e.attachments]
    print(f"Inbox: {len(attachments)} PDF(s), concurrency={concurrency}")

    outcomes: list[Outcome] = []
    failed: list[str] = []
    with ThreadPoolExecutor(max_workers=concurrency, initializer=_thread_init) as pool:
        futures = {
            pool.submit(process_one, a, odoo, store, move_processed=move_processed): a
            for a in attachments
        }
        for fut in as_completed(futures):
            att = futures[fut]
            try:
                outcomes.append(fut.result())
            except Exception as e:  # noqa: BLE001
                _dead_letter(att, e)
                failed.append(att.name)
                log.exception("dead-letter %s", att.name)

    from app.pipeline import write_run_log
    write_run_log(sorted(outcomes, key=lambda o: o.source.name))

    for o in sorted(outcomes, key=lambda o: o.source.name):
        print(f"{o.source.name}: {o.result.decision.value}  |  {' '.join(o.result.reasons)}")

    posted = sum(1 for o in outcomes if o.result.is_auto_post)
    print(
        f"\nProcessed {len(outcomes)}/{len(attachments)} "
        f"({posted} auto-posted, {len(outcomes) - posted} flagged, {len(failed)} dead-lettered)"
    )
    store.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    nums = [a for a in args if a.isdigit()]
    main(concurrency=int(nums[0]) if nums else 8,
         real="--real" in args, gmail="--gmail" in args)
