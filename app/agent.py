"""Google ADK orchestration layer (M5).

An ADK LlmAgent sequences the four-stage pipeline by calling deterministic tools. The agent
narrates and orchestrates; it never makes the post/flag decision — that stays in
`validate.decide`, called inside the tool. This is the honest "built on Google ADK" layer:
the agent drives, but compliance logic remains testable deterministic code.

Data flows between tools via ADK session state (JSON), so the agent shuttles nothing itself.

    uv run python -m app.agent data/inbox/03_Atlassian.pdf
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import InMemoryRunner
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from app import rules
from app.config import models, setup_logging, thresholds
from app.extract import extract as default_extract
from app.models import ExtractionResult, ValidationResult
from app.odoo import OdooClient, StubOdooClient, post_or_flag
from app.pipeline import SeenStore, with_retry
from app.rules import decide

APP_NAME = "odoo-invoice-agent"

# runtime deps the tools need (odoo client + store), set by build_runner so the tools don't
# depend on the llm layer
_RT: dict = {"odoo": None, "store": None}


def _litellm_model() -> str:
    # config uses pydantic-ai ids ("openai-chat:..."); litellm wants "openai/..."
    return "openai/" + models()["primary"].split(":", 1)[-1]


# tools (deterministic). they're async because they run inside adk's loop, but the pipeline
# uses run_sync — so we offload the blocking work to a worker thread with its own loop.

async def _blocking(fn):
    def wrapper():
        asyncio.set_event_loop(asyncio.new_event_loop())
        return fn()

    return await asyncio.to_thread(wrapper)


async def extract_invoice(file_path: str, tool_context: ToolContext) -> dict:
    """Extract the vendor-bill fields from the PDF at file_path. Call this first."""
    extraction = await _blocking(lambda: with_retry(lambda: default_extract(file_path)))
    tool_context.state["extraction"] = extraction.model_dump_json()
    d = extraction.data
    return {
        "vendor_name": d.vendor_name,
        "invoice_number": d.invoice_number,
        "total_amount": d.total_amount,
        "overall_confidence": extraction.overall_confidence,
    }


def _validate(extraction: ExtractionResult) -> ValidationResult:
    data = extraction.data
    supplier = rules.match(data.vendor_name)
    store: SeenStore = _RT["store"]
    odoo: OdooClient = _RT["odoo"]
    is_dup = store.claim(data.vendor_name, data.invoice_number) or odoo.bill_exists(
        0, data.invoice_number
    )
    return decide(extraction, supplier, is_dup, thresholds()["confidence_escalate"])


async def validate_invoice(tool_context: ToolContext) -> dict:
    """Validate the extracted invoice against finance rules and decide post vs flag.
    Call this after extract_invoice."""
    extraction = ExtractionResult.model_validate_json(tool_context.state["extraction"])
    result = await _blocking(lambda: _validate(extraction))
    tool_context.state["decision"] = result.model_dump_json()
    return {"decision": result.decision.value, "reasons": result.reasons}


async def record_in_odoo(tool_context: ToolContext) -> dict:
    """Create the vendor bill in Odoo (post if clean+approved, else draft+flag) and write
    the audit trail. Call this last, after validate_invoice."""
    extraction = ExtractionResult.model_validate_json(tool_context.state["extraction"])
    result = ValidationResult.model_validate_json(tool_context.state["decision"])
    bill = await _blocking(lambda: post_or_flag(_RT["odoo"], extraction, result))
    return {"move_id": bill.move_id, "posted": bill.posted,
            "decision": result.decision.value}


def build_agent() -> LlmAgent:
    return LlmAgent(
        name="invoice_agent",
        model=LiteLlm(model=_litellm_model()),
        description="Processes one vendor invoice PDF end to end.",
        instruction=(
            "You process exactly one vendor invoice PDF. Given a file path, call the tools "
            "in this exact order: (1) extract_invoice with the file path, (2) validate_invoice, "
            "(3) record_in_odoo. Do NOT decide whether to post or flag yourself — the tools "
            "compute that. After record_in_odoo, reply with one line: the decision and the "
            "Odoo move id."
        ),
        tools=[extract_invoice, validate_invoice, record_in_odoo],
    )


def build_runner(odoo: OdooClient | None = None, store: SeenStore | None = None) -> InMemoryRunner:
    _RT["odoo"] = odoo if odoo is not None else StubOdooClient()
    _RT["store"] = store if store is not None else SeenStore()
    return InMemoryRunner(agent=build_agent(), app_name=APP_NAME)


async def process_invoice(path: str, runner: InMemoryRunner, user_id: str = "demo") -> str:
    session_id = Path(path).name
    await runner.session_service.create_session(
        app_name=APP_NAME, user_id=user_id, session_id=session_id
    )
    msg = types.Content(role="user", parts=[types.Part(text=f"Process the invoice at {path}")])
    final = ""
    async for event in runner.run_async(
        user_id=user_id, session_id=session_id, new_message=msg
    ):
        if event.is_final_response() and event.content and event.content.parts:
            final = "".join(p.text or "" for p in event.content.parts)
    return final


def main(path: str) -> None:
    setup_logging()
    runner = build_runner(store=SeenStore(Path("data") / "seen_adk.db"))
    out = asyncio.run(process_invoice(path, runner))
    print(out)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/inbox/03_Atlassian.pdf")
