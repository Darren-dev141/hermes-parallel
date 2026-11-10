"""Session-scoped /chain text. Reads the ledger and does not write it."""

from __future__ import annotations

from .ledger import ID_RE, LedgerCorrupt


def format_receipts(ledger, session_id: str, raw_args: str = "") -> str:
    if not isinstance(session_id, str) or session_id.strip() == "":
        return (
            "chain: host did not supply a session id. "
            "Refusing to pick a session.\n"
            "proves_task_success: false\n"
        )
    report = ledger.verify()
    lines = [
        f"chain session={session_id}",
        "proves_task_success: false",
        f"chain: {report['chain']['status']}",
        "checkpoint: none (deleting the tail or replacing the file is not detectable)",
        report["limit"],
    ]
    args = (raw_args or "").strip()
    focus = None
    if args:
        if ID_RE.fullmatch(args):
            focus = args
        else:
            lines.append("usage: /chain [record-id]")
    try:
        rows = [row for row in ledger.records() if row.get("session_id") == session_id]
    except LedgerCorrupt:
        rows = []
        lines.append("ledger unreadable")
    if focus is not None:
        match = next((row for row in rows if row.get("id") == focus), None)
        if match is None:
            lines.append("not found in this session")
        else:
            lines.append(f"focus: {match['id']}")
    lines.append(f"session_records: {len(rows)}")
    for row in rows:
        lines.extend(_format_row(row))
    return "\n".join(lines) + "\n"


def _show(value) -> str:
    if value is None:
        return "unknown"
    if value is True:
        return "true"
    if value is False:
        return "false"
    return "unknown"


def _format_row(row: dict) -> list[str]:
    exit_display = row["reported_exit_code"] if row.get("exit_code_known") else "unknown"
    lines = [
        (
            f"- id={row['id']} kind={row['kind']} tool={row['tool_name']} "
            f"claim={row['claim']} status={row['reported_status']} exit={exit_display}"
        ),
        f"  observed_at={row['observed_at']}",
        f"  input_sha256={row['input_sha256']}",
        f"  output_sha256={row['output_sha256']}",
    ]
    if row.get("kind") == "session_end":
        lines.append(
            "  lifecycle completed={completed} interrupted={interrupted} "
            "lifecycle_failed={failed} turn_exit_reason={reason} "
            "(lifecycle flags are not proof the task succeeded)".format(
                completed=_show(row.get("completed")),
                interrupted=_show(row.get("interrupted")),
                failed=_show(row.get("lifecycle_failed")),
                reason=_show(row.get("turn_exit_reason")) if row.get("turn_exit_reason") is None else row.get("turn_exit_reason"),
            )
        )
    return lines
