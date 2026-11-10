"""chain_verify: integrity, payload agreement, and recorded outcome, kept apart."""

from __future__ import annotations

import json

from .ledger import HASH_RE, ID_RE, canonical_json, digest_of
from .observe import classify_tool, hash_input, hash_output


def verify_tool(ledger, args: dict, *, session_id: str) -> str:
    if not isinstance(args, dict):
        args = {}
    supplied = "checkpoint_digest" in args and args.get("checkpoint_digest") is not None
    checkpoint = args.get("checkpoint_digest") if supplied else None
    report = ledger.verify(checkpoint=checkpoint)
    if "receipt_id" in args and args.get("receipt_id") is not None:
        report["record"] = _lookup(ledger, args, session_id=session_id)
    else:
        report["record"] = None
    return canonical_json(report)


def claim_rederived_ok(row: dict) -> bool:
    if row.get("kind") == "session_end":
        return row.get("claim") == "session_end_observed" and row.get("signal_consistent") is True
    if row.get("kind") != "tool_call":
        return False
    exit_code = row.get("reported_exit_code") if row.get("exit_code_known") else None
    claim, consistent = classify_tool(row.get("reported_status") or "unknown", exit_code)
    return claim == row.get("claim") and consistent == row.get("signal_consistent")


def _lookup(ledger, args: dict, *, session_id: str) -> dict:
    receipt_id = args.get("receipt_id")
    if not isinstance(session_id, str) or session_id.strip() == "":
        return {"found": False, "invalid_id": False}
    if not isinstance(receipt_id, str) or not ID_RE.fullmatch(receipt_id):
        return {"found": False, "invalid_id": True}
    row = ledger.get(receipt_id)
    if row is None or row.get("session_id") != session_id:
        return {"found": False, "invalid_id": False}
    body = {key: value for key, value in row.items() if key != "digest"}
    return {
        "found": True,
        "invalid_id": False,
        "id": row["id"],
        "in_session": True,
        "digest_ok": digest_of(body) == row.get("digest"),
        "claim_rederived_ok": claim_rederived_ok(row),
        "claim": row.get("claim"),
        "reported_status": row.get("reported_status"),
        "reported_exit_code": row.get("reported_exit_code"),
        "exit_code_known": row.get("exit_code_known"),
        "input_match": _input_match(row, args),
        "output_match": _output_match(row, args),
        "signal_consistent": row.get("signal_consistent"),
    }


def _input_match(row: dict, args: dict):
    checks = []
    if args.get("input_sha256") is not None and "input_sha256" in args:
        supplied = args["input_sha256"]
        recorded = row.get("input_sha256")
        checks.append(
            isinstance(supplied, str) and recorded is not None and supplied.lower() == recorded
        )
    if args.get("input_json") is not None and "input_json" in args:
        raw = args["input_json"]
        if not isinstance(raw, str):
            checks.append(False)
        else:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                checks.append(False)
            else:
                digest = hash_input(parsed)
                checks.append(digest is not None and digest == row.get("input_sha256"))
    if not checks:
        return None
    return all(checks)


def _output_match(row: dict, args: dict):
    checks = []
    if args.get("output_sha256") is not None and "output_sha256" in args:
        supplied = args["output_sha256"]
        recorded = row.get("output_sha256")
        checks.append(
            isinstance(supplied, str)
            and isinstance(recorded, str)
            and HASH_RE.fullmatch(supplied.lower()) is not None
            and supplied.lower() == recorded
        )
    if args.get("output_text") is not None and "output_text" in args:
        text = args["output_text"]
        if not isinstance(text, str):
            checks.append(False)
        else:
            checks.append(hash_output(text) == row.get("output_sha256"))
    if not checks:
        return None
    return all(checks)
