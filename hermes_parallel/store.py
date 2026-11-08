"""Append-only evidence log. Each new event is anchored in the receipts chain."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from .engine import EVENT_KEYS, digest_of

_LOCKS: dict[str, threading.Lock] = {}
_GUARD = threading.Lock()


def _lock(path: Path) -> threading.Lock:
    key = str(path)
    with _GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def _body(event: dict) -> dict:
    return {key: event[key] for key in EVENT_KEYS if key != "digest"}


class EvidenceLog:
    def __init__(self, path: Path, *, ledger=None, session_id: str):
        self.path = path
        self.ledger = ledger
        self.session_id = session_id

    def save(self, graph) -> None:
        if graph.run.session_id != self.session_id:
            raise ValueError("refusing to write another session into this log")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _lock(self.path):
            rows = _read(self.path)
            known = {row["id"] for row in rows}
            prev = rows[-1]["digest"] if rows else None
            fresh = [event for event in graph.run.events if event["id"] not in known]
            with self.path.open("a", encoding="utf-8") as handle:
                for event in fresh:
                    if self.ledger is not None and not event.get("receipt_id"):
                        event["receipt_id"] = _anchor(self.ledger, self.session_id, event)
                    event["prev_digest"] = prev
                    event["digest"] = digest_of(_body(event))
                    handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
                    prev = event["digest"]

    def load_events(self) -> list[dict]:
        rows = _read(self.path)
        prev = None
        for index, event in enumerate(rows, 1):
            if digest_of(_body(event)) != event.get("digest"):
                raise ValueError(f"evidence digest mismatch at line {index}")
            if event.get("prev_digest") != prev:
                raise ValueError(f"evidence chain break at line {index}")
            prev = event["digest"]
        return [event for event in rows if event.get("session_id") == self.session_id]


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _observe():
    try:
        from receipts.observe import hash_input, tool_body
        return hash_input, tool_body
    except ImportError:
        import sys
        module = sys.modules.get("hermes_plugins.receipts.observe")
        if module is None:
            return None
        return module.hash_input, module.tool_body


def _anchor(ledger, session_id: str, event: dict) -> str | None:
    payload = {key: event[key] for key in EVENT_KEYS if key not in {"digest", "receipt_id", "prev_digest"}}
    observed = _observe()
    if observed is None:
        return None
    hash_input, tool_body = observed
    record = ledger.append(tool_body(
        tool_name="parallel_evidence",
        args=payload,
        result=json.dumps({"key": event["key"], "status": event["status"]}, sort_keys=True),
        session_id=session_id,
        status="ok",
        duration_ms=0,
    ))
    if record["input_sha256"] != hash_input(payload):
        raise RuntimeError("receipt anchor does not match the evidence payload")
    return record["id"]


def verify_anchors(events: list[dict], ledger) -> list[str]:
    observed = _observe()
    if observed is None:
        return [event["key"] for event in events]
    hash_input = observed[0]
    bad = []
    for event in events:
        receipt_id = event.get("receipt_id")
        record = ledger.get(receipt_id) if receipt_id else None
        payload = {key: event[key] for key in EVENT_KEYS if key not in {"digest", "receipt_id", "prev_digest"}}
        if record is None or record.get("input_sha256") != hash_input(payload):
            bad.append(event["key"])
    return bad
