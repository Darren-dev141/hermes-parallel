"""Turn Hermes hook payloads into receipt bodies.

Missing status stays ``unknown``. A missing exit code stays unknown. Nothing
in here treats a missing error as success, and lifecycle flags are recorded
only on session-end observations.
"""

from __future__ import annotations

import hashlib
import json
import math
import re

from .ledger import SCHEMA, canonical_json

_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_FAILURE = {"error", "blocked", "cancelled", "timeout"}


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json_compatible(value) -> bool:
    if value is None or isinstance(value, str) or isinstance(value, bool):
        return True
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_json_compatible(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _json_compatible(item) for key, item in value.items())
    return False


def hash_input(args):
    """SHA-256 of canonical JSON, or None when the arguments were not observed."""
    if args is None or not _json_compatible(args):
        return None
    try:
        return _sha256_text(canonical_json(args))
    except (TypeError, ValueError):
        return None


def hash_output(result):
    """SHA-256 of the raw result string. Non-strings stay unknown."""
    if not isinstance(result, str):
        return None
    return _sha256_text(result)


def classify_tool(status: str, exit_code: int | None) -> tuple[str, bool]:
    """Return ``(claim, signal_consistent)`` from explicit signals only."""
    nonzero = exit_code is not None and exit_code != 0
    zero = exit_code == 0
    recognized_failure = status in _FAILURE
    if status == "ok" and nonzero:
        return "failed", False
    if recognized_failure and zero:
        return "unknown", False
    if status not in _FAILURE and status not in {"ok", "unknown"} and nonzero:
        return "failed", False
    if status not in _FAILURE and status not in {"ok", "unknown"} and zero:
        return "unknown", False
    if nonzero or recognized_failure:
        return "failed", True
    if status == "ok" or zero:
        return "execution_observed", True
    return "unknown", True


def _text(value, limit: int) -> str | None:
    if not isinstance(value, str) or value == "":
        return None
    if len(value) > limit or any(ord(char) < 32 for char in value):
        return None
    return value


def _token(value) -> str | None:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        return None
    return value


def _status(value) -> str:
    token = _token(value)
    return token if token is not None else "unknown"


def _bool(value):
    if isinstance(value, bool):
        return value
    return None


def _duration(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _result_object(result):
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def _exit(result) -> tuple[int | None, bool]:
    obj = _result_object(result)
    if obj is None or "exit_code" not in obj:
        return None, False
    value = obj["exit_code"]
    if isinstance(value, bool) or not isinstance(value, int):
        return None, False
    return value, True


def tool_body(**kwargs) -> dict:
    exit_code, known = _exit(kwargs.get("result"))
    status = _status(kwargs.get("status"))
    claim, consistent = classify_tool(status, exit_code if known else None)
    return {
        "schema": SCHEMA,
        "kind": "tool_call",
        "session_id": _text(kwargs.get("session_id"), 256),
        "conversation_id": _text(kwargs.get("conversation_id"), 256),
        "task_id": _text(kwargs.get("task_id"), 256),
        "turn_id": _text(kwargs.get("turn_id"), 256),
        "tool_call_id": _text(kwargs.get("tool_call_id"), 256),
        "tool_name": _text(kwargs.get("tool_name"), 200),
        "input_sha256": hash_input(kwargs.get("args")),
        "output_sha256": hash_output(kwargs.get("result")),
        "duration_ms": _duration(kwargs.get("duration_ms")),
        "reported_status": status,
        "reported_exit_code": exit_code if known else None,
        "exit_code_known": known,
        "reported_error_type": _token(kwargs.get("error_type")),
        "claim": claim,
        "signal_consistent": consistent,
        "completed": None,
        "interrupted": None,
        "lifecycle_failed": None,
        "turn_exit_reason": None,
        "model": None,
        "platform": None,
    }


def session_body(**kwargs) -> dict:
    return {
        "schema": SCHEMA,
        "kind": "session_end",
        "session_id": _text(kwargs.get("session_id"), 256),
        "conversation_id": _text(kwargs.get("conversation_id"), 256),
        "task_id": _text(kwargs.get("task_id"), 256),
        "turn_id": _text(kwargs.get("turn_id"), 256),
        "tool_call_id": None,
        "tool_name": None,
        "input_sha256": None,
        "output_sha256": None,
        "duration_ms": None,
        "reported_status": "unknown",
        "reported_exit_code": None,
        "exit_code_known": False,
        "reported_error_type": None,
        "claim": "session_end_observed",
        "signal_consistent": True,
        "completed": _bool(kwargs["completed"]) if "completed" in kwargs else None,
        "interrupted": _bool(kwargs["interrupted"]) if "interrupted" in kwargs else None,
        "lifecycle_failed": _bool(kwargs["failed"]) if "failed" in kwargs else None,
        # Hermes sends codes such as text_response(finish_reason=stop). Keep a
        # short single-line code and drop anything that could carry payload text.
        "turn_exit_reason": _text(kwargs["turn_exit_reason"], 120) if "turn_exit_reason" in kwargs else None,
        "model": _text(kwargs["model"], 200) if "model" in kwargs else None,
        "platform": _text(kwargs["platform"], 200) if "platform" in kwargs else None,
    }
