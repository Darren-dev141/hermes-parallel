"""Receipts plugin for Hermes Agent.

Hooks observe tool calls and session-end events. They persist hashes and
explicit status fields. They do not persist raw payloads, and they do not
turn a missing field into a success.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .command import format_receipts
from .ledger import Ledger
from .observe import session_body, tool_body
from .schemas import VERIFY_SCHEMA
from .verify_api import verify_tool

logger = logging.getLogger("receipts")

__all__ = ["register"]


def default_ledger_path() -> Path:
    """Profile-scoped data directory Hermes keeps across plugin updates."""
    from plugins.plugin_storage import plugin_data_dir

    return Path(plugin_data_dir("receipts")) / "ledger.jsonl"


def default_ledger() -> Ledger:
    return Ledger(default_ledger_path())


def register(ctx, ledger_factory=None):
    """Register the verify tool, /receipts, and the two observation hooks."""
    factory = ledger_factory or default_ledger

    def receipts_verify(args, **kwargs):
        session_id = kwargs.get("session_id") if isinstance(kwargs.get("session_id"), str) else ""
        try:
            return verify_tool(factory(), args if isinstance(args, dict) else {}, session_id=session_id)
        except Exception:
            logger.exception("receipts_verify failed")
            return (
                '{"schema":"hermes.receipts.verify.v1","proves_task_success":false,'
                '"error":"verify_failed"}'
            )

    def post_tool_call(tool_name=None, args=None, result=None, task_id=None, duration_ms=None, **kwargs):
        try:
            payload = dict(kwargs)
            payload["tool_name"] = tool_name
            payload["args"] = args
            payload["result"] = result
            payload["task_id"] = task_id
            payload["duration_ms"] = duration_ms
            factory().append(tool_body(**payload))
        except Exception:
            logger.exception("post_tool_call receipt was not written")
        return None

    def on_session_end(
        session_id=None,
        completed=None,
        interrupted=None,
        model=None,
        platform=None,
        **kwargs,
    ):
        try:
            payload = dict(kwargs)
            payload["session_id"] = session_id
            payload["completed"] = completed
            payload["interrupted"] = interrupted
            payload["model"] = model
            payload["platform"] = platform
            factory().append(session_body(**payload))
        except Exception:
            logger.exception("on_session_end receipt was not written")
        return None

    def receipts_command(raw_args, **kwargs):
        session_id = kwargs.get("session_id") if isinstance(kwargs.get("session_id"), str) else ""
        try:
            return format_receipts(factory(), session_id, raw_args or "")
        except Exception:
            logger.exception("receipts command failed")
            return (
                "receipts: could not read the ledger.\n"
                "proves_task_success: false\n"
            )

    ctx.register_tool(
        name="receipts_verify",
        toolset="receipts",
        schema=VERIFY_SCHEMA,
        handler=receipts_verify,
    )
    ctx.register_hook("post_tool_call", post_tool_call)
    ctx.register_hook("on_session_end", on_session_end)
    ctx.register_command(
        "receipts",
        handler=receipts_command,
        description=(
            "Show this session's receipt chain. A lifecycle flag is not proof "
            "the task succeeded."
        ),
        args_hint="[receipt-id]",
    )
