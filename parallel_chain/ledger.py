"""Append-only receipt ledger.

The chain detects edits to records that remain in the file. It does not, by
itself, detect deletion of the tail or replacement of the whole file. That
takes a checkpoint digest stored somewhere this ledger does not control.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "hermes.parallel_chain.v1"
RECORD_KEYS = frozenset(
    {
        "schema",
        "id",
        "kind",
        "observed_at",
        "session_id",
        "conversation_id",
        "task_id",
        "turn_id",
        "tool_call_id",
        "tool_name",
        "input_sha256",
        "output_sha256",
        "duration_ms",
        "reported_status",
        "reported_exit_code",
        "exit_code_known",
        "reported_error_type",
        "claim",
        "signal_consistent",
        "completed",
        "interrupted",
        "lifecycle_failed",
        "turn_exit_reason",
        "model",
        "platform",
        "prev_digest",
        "digest",
    }
)
BODY_KEYS = RECORD_KEYS - {"id", "observed_at", "prev_digest", "digest"}
ID_RE = re.compile(r"^[0-9a-f]{32}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
LIMIT = (
    "Without a checkpoint digest kept outside this ledger, deleting the tail "
    "or replacing the file still verifies as intact."
)

_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class LedgerError(Exception):
    """Base error for ledger writes."""


class LedgerCorrupt(LedgerError):
    """The file on disk is not a readable receipt chain."""

    def __init__(self, message: str, line: int | None = None):
        super().__init__(message)
        self.line = line


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest_of(body: dict) -> str:
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def default_clock() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def default_id() -> str:
    return uuid.uuid4().hex


def _thread_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock


def _parse(text: str) -> list[dict]:
    if text == "":
        return []
    records = []
    for index, line in enumerate(text.splitlines(), 1):
        if line.strip() == "":
            raise LedgerCorrupt(f"blank line at {index}", index)
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LedgerCorrupt(f"unreadable JSON at line {index}", index) from exc
        if not isinstance(obj, dict):
            raise LedgerCorrupt(f"line {index} is not an object", index)
        records.append(obj)
    if text and not text.endswith("\n"):
        # A missing trailing newline is still one complete JSON line.
        pass
    return records


def inspect_chain(records: list[dict]) -> dict:
    prev = None
    for index, obj in enumerate(records, 1):
        if set(obj) != RECORD_KEYS:
            return _broken(index, "closed schema mismatch", len(records))
        if obj.get("schema") != SCHEMA:
            return _broken(index, "schema mismatch", len(records))
        digest = obj.get("digest")
        if not isinstance(digest, str) or not HASH_RE.fullmatch(digest):
            return _broken(index, "digest mismatch", len(records))
        prev_digest = obj.get("prev_digest")
        if prev_digest is not None and (
            not isinstance(prev_digest, str) or not HASH_RE.fullmatch(prev_digest)
        ):
            return _broken(index, "prev mismatch", len(records))
        body = {key: value for key, value in obj.items() if key != "digest"}
        if digest_of(body) != digest:
            return _broken(index, "digest mismatch", len(records))
        if prev_digest != prev:
            return _broken(index, "prev mismatch", len(records))
        prev = digest
    return {
        "status": "intact",
        "detail": "ok",
        "broken_at": None,
        "tip": prev,
        "records": len(records),
    }


def _broken(index: int, detail: str, count: int) -> dict:
    return {
        "status": "broken",
        "detail": detail,
        "broken_at": index,
        "tip": None,
        "records": count,
    }


def verification_report(chain: dict, *, checkpoint: str | None, supplied: bool) -> dict:
    tip = chain.get("tip")
    if not supplied:
        matches = None
    else:
        matches = isinstance(checkpoint, str) and tip is not None and checkpoint == tip
    return {
        "schema": "hermes.parallel_chain.verify.v1",
        "proves_task_success": False,
        "independent_attestation": False,
        "tail_deletion_detectable": supplied,
        "limit": LIMIT,
        "chain": chain,
        "checkpoint": {"supplied": supplied, "matches_tip": matches},
    }


class Ledger:
    def __init__(self, path: str | Path, *, clock=None, id_factory=None):
        self.path = Path(path)
        self.clock = clock or default_clock
        self.id_factory = id_factory or default_id

    def append(self, body: dict) -> dict:
        missing = BODY_KEYS - body.keys()
        extra = set(body) - BODY_KEYS
        if missing or extra:
            raise ValueError("receipt body does not match the closed schema")
        clean = {key: body[key] for key in BODY_KEYS}
        new_id = self.id_factory()
        if not isinstance(new_id, str) or not ID_RE.fullmatch(new_id):
            raise ValueError("refusing to persist a receipt id that is not 32 lowercase hex characters")
        stamp = self.clock()
        if not isinstance(stamp, str) or not STAMP_RE.fullmatch(stamp):
            raise ValueError("refusing to persist a timestamp that is not UTC millisecond form")
        clean["id"] = new_id
        clean["observed_at"] = stamp

        def write(text: str):
            records = _parse(text)
            chain = inspect_chain(records)
            if chain["status"] != "intact":
                raise LedgerCorrupt(chain["detail"], chain["broken_at"])
            clean["prev_digest"] = records[-1]["digest"] if records else None
            clean["digest"] = digest_of({key: value for key, value in clean.items() if key != "digest"})
            line = canonical_json(clean) + "\n"
            prefix = ""
            if text and not text.endswith("\n"):
                prefix = "\n"
            return clean, prefix + line

        self._mutate(write)
        return clean

    def records(self) -> list[dict]:
        return _parse(self._read())

    def get(self, receipt_id: str) -> dict | None:
        if not isinstance(receipt_id, str) or not ID_RE.fullmatch(receipt_id):
            return None
        try:
            rows = self.records()
        except LedgerCorrupt:
            return None
        for row in rows:
            if row.get("id") == receipt_id:
                return row
        return None

    def verify(self, checkpoint: str | None = None) -> dict:
        supplied = checkpoint is not None
        try:
            records = self.records()
        except LedgerCorrupt as exc:
            chain = {
                "status": "unreadable",
                "detail": str(exc),
                "broken_at": exc.line,
                "tip": None,
                "records": 0,
            }
            return verification_report(chain, checkpoint=checkpoint, supplied=supplied)
        return verification_report(inspect_chain(records), checkpoint=checkpoint, supplied=supplied)

    def _read(self) -> str:
        if not self.path.exists():
            return ""

        def read(text: str):
            return text, None

        return self._with_lock(read, create=False)

    def _mutate(self, fn) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._with_lock(fn, create=True)

    def _with_lock(self, fn, *, create: bool):
        lock = _thread_lock(self.path)
        with lock:
            if not self.path.exists() and not create:
                text = ""
                result, _extra = fn(text)
                return result
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                os.chmod(self.path, 0o600)
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
                chunks = []
                while True:
                    piece = os.read(fd, 1024 * 1024)
                    if not piece:
                        break
                    chunks.append(piece)
                try:
                    text = b"".join(chunks).decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise LedgerCorrupt("ledger is not valid UTF-8", 1) from exc
                result, extra = fn(text)
                if extra is not None:
                    os.lseek(fd, 0, os.SEEK_END)
                    os.write(fd, extra.encode("utf-8"))
                    os.fsync(fd)
                return result
            finally:
                try:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(fd)
