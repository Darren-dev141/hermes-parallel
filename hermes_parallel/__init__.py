"""Hermes Parallel: auditable fan-out on top of the receipts ledger.

Investigations run as local threads. Hermes v0.23 can also fan out through
delegate_task, but that call starts full child model loops and, outside a
joined one-shot intercept, does not return their results in the same turn.
This slice does not pretend those children ran.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .engine import (
    SUPPLIER_FORMULAS,
    Graph,
    order_sensitivity,
    report,
    run_supplier,
    supplier_question,
)
from .store import EvidenceLog

logger = logging.getLogger("hermes_parallel")

INVESTIGATE_SCHEMA = {
    "name": "parallel_investigate",
    "description": (
        "Split a question into competing investigations, run them in parallel, "
        "and record each result on the receipts hash chain with a status: "
        "proposed, computed, independently_checked, supported, or unknown. "
        "A supported total is not proof the question is answered. "
        "The supplier demo builds a total on a quote that a rival check disagrees with."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "question": {
                "type": "string",
                "description": "The question to fan out. The supplier-total case is the scripted demo.",
            },
        },
        "required": ["question"],
    },
}

RETRACT_SCHEMA = {
    "name": "parallel_retract",
    "description": (
        "Correct one computed value. Every conclusion that depends on it is "
        "retracted and recomputed. Conclusions that do not depend on it are kept."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "key": {"type": "string", "description": "Evidence key to correct, such as price_quote."},
            "value": {"type": "integer", "description": "Replacement integer value."},
            "reason": {"type": "string", "description": "Why the previous value is withdrawn."},
        },
        "required": ["key", "value"],
    },
}

REPORT_SCHEMA = {
    "name": "parallel_report",
    "description": (
        "Supported claims, rejected alternatives, unresolved questions, and the "
        "replayable event history for this session. Does not prove task success."
    ),
    "parameters": {"type": "object", "additionalProperties": False, "properties": {}},
}

ORDER_SCHEMA = {
    "name": "parallel_order_check",
    "description": (
        "Replay the supplier evidence in two arrival orders. Flag a conclusion "
        "only when its value changes because of order. The formula total should "
        "not. The first-arriving price should."
    ),
    "parameters": {"type": "object", "additionalProperties": False, "properties": {}},
}


def _session(kwargs: dict) -> str:
    session_id = kwargs.get("session_id")
    return session_id if isinstance(session_id, str) else ""


def _refuse() -> str:
    return json.dumps({
        "schema": "hermes.parallel.report.v1",
        "proves_task_success": False,
        "error": "host did not supply a session id",
    })


def _ledger():
    try:
        import receipts
        return receipts.default_ledger()
    except ImportError:
        import sys
        module = sys.modules.get("hermes_plugins.receipts")
        if module is None or not hasattr(module, "default_ledger"):
            return None
        return module.default_ledger()


def _evidence_path() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir
        return Path(plugin_data_dir("hermes-parallel")) / "evidence.jsonl"
    except Exception:
        return Path.home() / ".hermes" / "plugin-data" / "hermes-parallel" / "evidence.jsonl"


_RUNS: dict[str, Graph] = {}


def _load(session_id: str, path: Path | None = None) -> Graph | None:
    if session_id in _RUNS:
        return _RUNS[session_id]
    log = EvidenceLog(path or _evidence_path(), session_id=session_id)
    events = log.load_events()
    if not events:
        return None
    graph = Graph(session_id=session_id, question="", run_id=events[-1]["run_id"])
    graph.run.events = events
    _RUNS[session_id] = graph
    return graph


def investigate(question: str, *, session_id: str, path: Path | None = None, ledger=None) -> dict:
    if not session_id.strip():
        raise ValueError("session id required")
    graph = Graph(session_id=session_id, question=question)
    if supplier_question(question):
        run_supplier(graph)
    else:
        graph.propose("reading_a", f"Proposed reading of: {question}")
        graph.propose("reading_b", f"Competing reading of: {question}")
        graph.propose(
            "answer",
            "No value was computed for this question, so the answer stays unknown.",
            kind="conclusion",
            depends_on=["reading_a", "reading_b"],
        )
    log = EvidenceLog(path or _evidence_path(), ledger=ledger if ledger is not None else _ledger(), session_id=session_id)
    log.save(graph)
    _RUNS[session_id] = graph
    body = report(graph)
    body["note"] = (
        "Investigations ran as local threads. delegate_task was not called: "
        "v0.23 top-level delegation does not join child model loops in this turn."
    )
    return body


def retract(key: str, value: int, *, reason: str, session_id: str, path: Path | None = None, ledger=None) -> dict:
    graph = _load(session_id, path)
    if graph is None:
        raise KeyError("no investigation in this session")
    result = graph.correct(key, value, reason=reason, formulas=SUPPLIER_FORMULAS)
    log = EvidenceLog(path or _evidence_path(), ledger=ledger if ledger is not None else _ledger(), session_id=session_id)
    log.save(graph)
    return {
        "schema": "hermes.parallel.retraction.v1",
        "proves_task_success": False,
        "corrected": result.corrected,
        "old_value": result.old_value,
        "new_value": result.new_value,
        "retracted": result.retracted,
        "recomputed": result.recomputed,
        "kept": result.kept,
        "total": None if graph.current("total") is None else graph.current("total")["value"],
        "total_status": None if graph.current("total") is None else graph.current("total")["status"],
    }


def order_check(*, session_id: str) -> dict:
    if not session_id.strip():
        raise ValueError("session id required")
    findings = order_sensitivity(session_id, [
        ["price_quote", "rival_price", "quantity", "tax_percent"],
        ["rival_price", "price_quote", "quantity", "tax_percent"],
    ])
    return {
        "schema": "hermes.parallel.order.v1",
        "proves_task_success": False,
        "findings": [
            {"key": item.key, "values": item.values, "order_sensitive": item.order_sensitive}
            for item in findings
        ],
    }


def render_report(body: dict) -> str:
    lines = [
        f"parallel question: {body.get('question', '')}",
        "proves_task_success: false",
        "supported:",
    ]
    for item in body.get("supported") or []:
        lines.append(f"  {item['key']}={item['value']}  {item['statement']}")
    if not body.get("supported"):
        lines.append("  (none)")
    lines.append("rejected alternatives:")
    for item in body.get("rejected_alternatives") or []:
        lines.append(f"  {item['key']} value={item['value']} check={item['check_value']}")
    lines.append("unresolved: " + (", ".join(body.get("unresolved") or []) or "(none)"))
    lines.append("retracted: " + (", ".join(body.get("retracted") or []) or "(none)"))
    return "\n".join(lines) + "\n"


def register(ctx, *, evidence_path=None, ledger_factory=None):
    path = evidence_path
    factory = ledger_factory

    def _path() -> Path:
        return Path(path) if path is not None else _evidence_path()

    def _led():
        return factory() if factory is not None else _ledger()

    def parallel_investigate(args, **kwargs):
        session_id = _session(kwargs)
        if not session_id.strip():
            return _refuse()
        question = args.get("question") if isinstance(args, dict) else ""
        if not isinstance(question, str) or not question.strip():
            return json.dumps({"error": "question required", "proves_task_success": False})
        try:
            body = investigate(question, session_id=session_id, path=_path(), ledger=_led())
        except Exception:
            logger.exception("parallel_investigate failed")
            return json.dumps({"error": "investigate_failed", "proves_task_success": False})
        return json.dumps(body, sort_keys=True)

    def parallel_retract(args, **kwargs):
        session_id = _session(kwargs)
        if not session_id.strip():
            return _refuse()
        if not isinstance(args, dict):
            return json.dumps({"error": "bad args", "proves_task_success": False})
        key = args.get("key")
        value = args.get("value")
        if not isinstance(key, str) or isinstance(value, bool) or not isinstance(value, int):
            return json.dumps({"error": "key and integer value required", "proves_task_success": False})
        try:
            body = retract(
                key, value, reason=str(args.get("reason") or "corrected"),
                session_id=session_id, path=_path(), ledger=_led(),
            )
        except KeyError:
            return json.dumps({"error": "unknown key", "proves_task_success": False})
        except Exception:
            logger.exception("parallel_retract failed")
            return json.dumps({"error": "retract_failed", "proves_task_success": False})
        return json.dumps(body, sort_keys=True)

    def parallel_report(args, **kwargs):
        session_id = _session(kwargs)
        if not session_id.strip():
            return _refuse()
        graph = _load(session_id, _path())
        if graph is None:
            return json.dumps({"error": "no investigation", "proves_task_success": False})
        return json.dumps(report(graph), sort_keys=True)

    def parallel_order_check(args, **kwargs):
        session_id = _session(kwargs)
        if not session_id.strip():
            return _refuse()
        return json.dumps(order_check(session_id=session_id), sort_keys=True)

    def investigate_command(raw_args, **kwargs):
        session_id = _session(kwargs)
        if not session_id.strip():
            return "parallel: host did not supply a session id. Refusing to pick a session.\n"
        question = (raw_args or "").strip() or "What is the supplier total?"
        body = investigate(question, session_id=session_id, path=_path(), ledger=_led())
        return render_report(body)

    def parallel_command(raw_args, **kwargs):
        session_id = _session(kwargs)
        if not session_id.strip():
            return "parallel: host did not supply a session id. Refusing to pick a session.\n"
        parts = (raw_args or "").strip().split()
        if not parts or parts[0] == "report":
            graph = _load(session_id, _path())
            if graph is None:
                return "parallel: no investigation in this session.\n"
            return render_report(report(graph))
        if parts[0] == "retract" and len(parts) >= 3:
            try:
                body = retract(parts[1], int(parts[2]), reason="corrected from /parallel retract", session_id=session_id, path=_path(), ledger=_led())
            except (KeyError, ValueError):
                return "usage: /parallel retract <key> <integer>\n"
            kept = ", ".join(body["kept"]) or "(none)"
            recomputed = ", ".join(body["recomputed"]) or "(none)"
            return (
                f"retracted {', '.join(body['retracted']) or '(none)'}\n"
                f"recomputed {recomputed}\n"
                f"kept {kept}\n"
                f"total={body['total']} status={body['total_status']}\n"
                "proves_task_success: false\n"
            )
        if parts[0] == "order":
            body = order_check(session_id=session_id)
            lines = ["order check:", "proves_task_success: false"]
            for item in body["findings"]:
                flag = "order-sensitive" if item["order_sensitive"] else "stable"
                lines.append(f"  {item['key']}: {item['values']} {flag}")
            return "\n".join(lines) + "\n"
        return "usage: /parallel report | /parallel retract <key> <integer> | /parallel order\n"

    ctx.register_tool(name="parallel_investigate", toolset="hermes-parallel", schema=INVESTIGATE_SCHEMA, handler=parallel_investigate)
    ctx.register_tool(name="parallel_retract", toolset="hermes-parallel", schema=RETRACT_SCHEMA, handler=parallel_retract)
    ctx.register_tool(name="parallel_report", toolset="hermes-parallel", schema=REPORT_SCHEMA, handler=parallel_report)
    ctx.register_tool(name="parallel_order_check", toolset="hermes-parallel", schema=ORDER_SCHEMA, handler=parallel_order_check)
    ctx.register_command("investigate", handler=investigate_command, description="Fan out a question into competing investigations.", args_hint="<question>")
    ctx.register_command("parallel", handler=parallel_command, description="Report, retract, or check order sensitivity.", args_hint="report|retract|order")
