"""Behavior tests for the receipts plugin.

Expected hashes are computed with the stdlib, not the plugin, so a broken
canonicalization cannot satisfy itself.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _clock_factory(stamp="2026-11-08T01:28:00.000Z"):
    def clock():
        return stamp

    return clock


def _id_factory():
    state = {"n": 0}

    def nxt():
        state["n"] += 1
        return f"{state['n']:032x}"

    return nxt


class HashContractTest(unittest.TestCase):
    def test_input_hash_is_canonical_utf8_sha256(self):
        from parallel_chain.observe import hash_input

        args = {"b": 1, "a": [2, {"d": 1, "c": "café"}]}
        self.assertEqual(hash_input(args), _sha256_text(_canonical(args)))

    def test_input_key_order_does_not_change_hash(self):
        from parallel_chain.observe import hash_input

        self.assertEqual(hash_input({"b": 1, "a": 2}), hash_input({"a": 2, "b": 1}))

    def test_missing_args_stay_unknown(self):
        from parallel_chain.observe import hash_input

        self.assertIsNone(hash_input(None))

    def test_unserializable_input_stays_unknown(self):
        from parallel_chain.observe import hash_input

        self.assertIsNone(hash_input({"blob": b"nope"}))
        self.assertIsNone(hash_input({"n": float("nan")}))

    def test_output_hash_is_the_raw_string_not_reparsed_json(self):
        from parallel_chain.observe import hash_output

        raw = '{"b":1,"a":2}'
        reordered = '{"a":2,"b":1}'
        self.assertEqual(hash_output(raw), _sha256_text(raw))
        self.assertNotEqual(hash_output(raw), hash_output(reordered))

    def test_non_string_output_stays_unknown(self):
        from parallel_chain.observe import hash_output

        self.assertIsNone(hash_output({"exit_code": 0}))
        self.assertIsNone(hash_output(None))


class ClassifyTest(unittest.TestCase):
    def _claim(self, status, exit_code):
        from parallel_chain.observe import classify_tool

        return classify_tool(status, exit_code)

    def test_table(self):
        cases = [
            ("unknown", None, "unknown", True),
            ("unknown", 0, "execution_observed", True),
            ("unknown", 3, "failed", True),
            ("ok", None, "execution_observed", True),
            ("ok", 0, "execution_observed", True),
            ("ok", 3, "failed", False),
            ("error", None, "failed", True),
            ("error", 3, "failed", True),
            ("error", 0, "unknown", False),
            ("blocked", None, "failed", True),
            ("cancelled", None, "failed", True),
            ("timeout", None, "failed", True),
            ("weird", None, "unknown", True),
            ("weird", 0, "unknown", False),
            ("weird", 2, "failed", False),
        ]
        for status, exit_code, claim, consistent in cases:
            with self.subTest(status=status, exit_code=exit_code):
                self.assertEqual(self._claim(status, exit_code), (claim, consistent))


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ledger.jsonl"
        from parallel_chain.ledger import Ledger

        self.ledger = Ledger(self.path, clock=_clock_factory(), id_factory=_id_factory())

    def tearDown(self):
        self.tmp.cleanup()

    def _tool_body(self, **overrides):
        from parallel_chain.observe import tool_body

        kwargs = {
            "tool_name": "terminal",
            "args": {"command": "true"},
            "result": json.dumps({"output": "", "exit_code": 0, "error": None}),
            "task_id": "task-1",
            "duration_ms": 12,
            "session_id": "sess-a",
            "status": "ok",
        }
        kwargs.update(overrides)
        return tool_body(**kwargs)

    def test_first_record_links_to_null_and_digest_matches_body(self):
        from parallel_chain.ledger import RECORD_KEYS, digest_of

        record = self.ledger.append(self._tool_body())
        self.assertEqual(record["prev_digest"], None)
        self.assertEqual(record["id"], f"{1:032x}")
        self.assertEqual(record["observed_at"], "2026-11-08T01:28:00.000Z")
        self.assertEqual(set(record), RECORD_KEYS)
        body = {k: v for k, v in record.items() if k != "digest"}
        self.assertEqual(record["digest"], digest_of(body))
        self.assertEqual(record["digest"], _sha256_text(_canonical(body)))
        self.assertEqual(record["claim"], "execution_observed")
        self.assertEqual(record["reported_exit_code"], 0)
        self.assertTrue(record["exit_code_known"])

    def test_second_record_chains_to_the_first(self):
        first = self.ledger.append(self._tool_body())
        second = self.ledger.append(self._tool_body(result=json.dumps({"exit_code": 3}), status="error"))
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(second["prev_digest"], first["digest"])
        self.assertEqual(second["claim"], "failed")
        report = self.ledger.verify()
        self.assertEqual(report["chain"]["status"], "intact")
        self.assertEqual(report["chain"]["tip"], second["digest"])
        self.assertFalse(report["tail_deletion_detectable"])
        self.assertFalse(report["proves_task_success"])
        self.assertFalse(report["independent_attestation"])
        self.assertIn("tail", report["limit"].lower())

    def test_tampered_field_breaks_the_chain(self):
        self.ledger.append(self._tool_body())
        lines = self.path.read_text(encoding="utf-8").splitlines()
        obj = json.loads(lines[0])
        obj["tool_name"] = "not-the-tool"
        lines[0] = json.dumps(obj, sort_keys=True, separators=(",", ":"))
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        report = self.ledger.verify()
        self.assertEqual(report["chain"]["status"], "broken")
        self.assertEqual(report["chain"]["broken_at"], 1)

    def test_recomputed_tail_needs_a_checkpoint_to_be_caught(self):
        from parallel_chain.ledger import digest_of

        first = self.ledger.append(self._tool_body())
        self.ledger.append(self._tool_body(tool_name="other"))
        original_tip = self.ledger.verify()["chain"]["tip"]
        lines = self.path.read_text(encoding="utf-8").splitlines()
        last = json.loads(lines[-1])
        last["tool_name"] = "rewritten"
        body = {k: v for k, v in last.items() if k != "digest"}
        last["digest"] = digest_of(body)
        lines[-1] = _canonical(last)
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        naked = self.ledger.verify()
        self.assertEqual(naked["chain"]["status"], "intact")
        anchored = self.ledger.verify(checkpoint=original_tip)
        self.assertTrue(anchored["tail_deletion_detectable"])
        self.assertFalse(anchored["checkpoint"]["matches_tip"])
        still = self.ledger.verify(checkpoint=first["digest"])
        self.assertFalse(still["checkpoint"]["matches_tip"])

    def test_checkpoint_matches_real_tip(self):
        self.ledger.append(self._tool_body())
        tip = self.ledger.verify()["chain"]["tip"]
        report = self.ledger.verify(checkpoint=tip)
        self.assertTrue(report["checkpoint"]["matches_tip"])
        self.assertTrue(report["tail_deletion_detectable"])

    def test_corrupt_line_is_unreadable_and_append_refuses(self):
        from parallel_chain.ledger import LedgerCorrupt

        self.ledger.append(self._tool_body())
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("{not-json\n")
        report = self.ledger.verify()
        self.assertEqual(report["chain"]["status"], "unreadable")
        before = self.path.read_bytes()
        with self.assertRaises(LedgerCorrupt):
            self.ledger.append(self._tool_body())
        self.assertEqual(self.path.read_bytes(), before)

    def test_secret_text_is_not_stored(self):
        secret = "s3cr3t-token-do-not-store"
        self.ledger.append(
            self._tool_body(
                args={"api_key": secret, "command": "echo hidden"},
                result=json.dumps({"output": secret, "exit_code": 0}),
                error_message=f"failed with {secret}",
                user_message=secret,
            )
        )
        blob = self.path.read_text(encoding="utf-8")
        self.assertNotIn(secret, blob)
        self.assertIn(_sha256_text(_canonical({"api_key": secret, "command": "echo hidden"})), blob)
        self.assertNotIn("echo hidden", blob)

    def test_bool_and_string_exit_codes_stay_unknown(self):
        record = self.ledger.append(
            self._tool_body(result=json.dumps({"exit_code": True}), status=None)
        )
        self.assertFalse(record["exit_code_known"])
        self.assertIsNone(record["reported_exit_code"])
        self.assertEqual(record["claim"], "unknown")
        record = self.ledger.append(
            self._tool_body(result=json.dumps({"exit_code": "0"}), status=None)
        )
        self.assertFalse(record["exit_code_known"])
        self.assertEqual(record["claim"], "unknown")

    def test_error_key_without_status_or_exit_is_unknown(self):
        secret = "boom-secret-message"
        record = self.ledger.append(
            self._tool_body(result=json.dumps({"error": secret}), status=None)
        )
        self.assertEqual(record["claim"], "unknown")
        self.assertEqual(record["reported_status"], "unknown")
        self.assertNotIn(secret, self.path.read_text(encoding="utf-8"))

    def test_unsafe_status_text_is_not_stored(self):
        secret = "status-secret-value"
        record = self.ledger.append(self._tool_body(status=f"failed {secret}"))
        self.assertEqual(record["reported_status"], "unknown")
        self.assertNotIn(secret, self.path.read_text(encoding="utf-8"))

    def test_explicit_zero_without_status_is_execution_observed(self):
        record = self.ledger.append(
            self._tool_body(result=json.dumps({"exit_code": 0}), status=None)
        )
        self.assertEqual(record["claim"], "execution_observed")
        self.assertEqual(record["reported_status"], "unknown")
        self.assertEqual(record["reported_exit_code"], 0)

    def test_lifecycle_flags_on_a_tool_call_are_not_copied(self):
        record = self.ledger.append(self._tool_body(completed=True, interrupted=False, failed=False))
        self.assertIsNone(record["completed"])
        self.assertIsNone(record["interrupted"])
        self.assertIsNone(record["lifecycle_failed"])

    def test_non_string_result_can_still_expose_an_integer_exit(self):
        record = self.ledger.append(
            self._tool_body(result={"exit_code": 4, "output": "s3cr3t-token-do-not-store"}, status=None)
        )
        self.assertEqual(record["reported_exit_code"], 4)
        self.assertEqual(record["claim"], "failed")
        self.assertIsNone(record["output_sha256"])
        self.assertNotIn("s3cr3t-token-do-not-store", self.path.read_text(encoding="utf-8"))

    def test_missing_lookup_and_hostile_ids(self):
        self.ledger.append(self._tool_body())
        self.assertIsNone(self.ledger.get(f"{9:032x}"))
        for hostile in ("../ledger.jsonl", "..\\ledger.jsonl", "a" * 31, "A" * 32, "not an id", ""):
            self.assertIsNone(self.ledger.get(hostile))
        marker = Path(self.tmp.name) / "other.txt"
        marker.write_text("secret-marker-from-other-file", encoding="utf-8")
        self.assertIsNone(self.ledger.get("../other.txt"))
        self.assertNotIn("secret-marker-from-other-file", json.dumps(self.ledger.verify()))

    def test_concurrent_appends_keep_a_single_intact_chain(self):
        from parallel_chain.ledger import Ledger

        ledger = Ledger(self.path)
        errors = []

        def worker(i):
            try:
                ledger.append(self._tool_body(tool_name=f"tool-{i}", session_id="sess-a"))
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        report = ledger.verify()
        self.assertEqual(report["chain"]["status"], "intact")
        self.assertEqual(len(ledger.records()), 12)
        ids = [row["id"] for row in ledger.records()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_empty_ledger_verifies(self):
        report = self.ledger.verify()
        self.assertEqual(report["chain"]["status"], "intact")
        self.assertIsNone(report["chain"]["tip"])
        self.assertEqual(report["chain"]["records"], 0)
        self.assertFalse(self.ledger.verify(checkpoint="ab" * 32)["checkpoint"]["matches_tip"])


class SessionEndTest(unittest.TestCase):
    def test_repeated_ends_are_distinct_and_do_not_invent_flags(self):
        from parallel_chain.ledger import Ledger
        from parallel_chain.observe import session_body

        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.jsonl", clock=_clock_factory(), id_factory=_id_factory())
            first = ledger.append(
                session_body(
                    session_id="sess-a",
                    completed=True,
                    interrupted=False,
                    failed=False,
                    turn_exit_reason="completed",
                    model="mock/local",
                    platform="cli",
                    task_id="task-1",
                    turn_id="turn-1",
                    user_message="please do not store this prompt",
                )
            )
            second = ledger.append(session_body(session_id="sess-a"))
            self.assertEqual(first["kind"], "session_end")
            self.assertEqual(first["claim"], "session_end_observed")
            self.assertTrue(first["completed"])
            self.assertFalse(first["lifecycle_failed"])
            self.assertEqual(first["turn_exit_reason"], "completed")
            self.assertEqual(first["model"], "mock/local")
            self.assertIsNone(second["completed"])
            self.assertIsNone(second["interrupted"])
            self.assertIsNone(second["lifecycle_failed"])
            self.assertNotEqual(first["id"], second["id"])
            blob = (Path(tmp) / "ledger.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("please do not store this prompt", blob)
            self.assertNotIn("succeeded", first["claim"])


class CommandAndVerifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ledger.jsonl"
        from parallel_chain.ledger import Ledger

        self.ledger = Ledger(self.path, clock=_clock_factory(), id_factory=_id_factory())
        from parallel_chain.observe import tool_body

        self.a = self.ledger.append(
            tool_body(
                tool_name="terminal",
                args={"command": "true"},
                result=json.dumps({"output": "ok-out", "exit_code": 0}),
                session_id="sess-a",
                status="ok",
                duration_ms=5,
            )
        )
        self.b = self.ledger.append(
            tool_body(
                tool_name="foreign_tool",
                args={"command": "other-session"},
                result=json.dumps({"exit_code": 1}),
                session_id="sess-b",
                status="error",
                duration_ms=5,
            )
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_slash_without_session_fails_closed(self):
        from parallel_chain.command import format_receipts

        text = format_receipts(self.ledger, "", "")
        self.assertIn("session id", text)
        self.assertNotIn("foreign_tool", text)
        self.assertNotIn("terminal", text)
        self.assertNotIn(self.a["id"], text)

    def test_slash_lists_only_the_calling_session_and_checks_the_chain(self):
        from parallel_chain.command import format_receipts

        before = self.path.read_bytes()
        text = format_receipts(self.ledger, "sess-a", "")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIn(self.a["id"], text)
        self.assertIn("terminal", text)
        self.assertIn("execution_observed", text)
        self.assertIn(self.a["input_sha256"], text)
        self.assertIn(self.a["output_sha256"], text)
        self.assertIn("chain: intact", text)
        self.assertIn("proves_task_success: false", text)
        self.assertNotIn("foreign_tool", text)
        self.assertNotIn(self.b["id"], text)
        self.assertNotIn("other-session", text)

    def test_slash_does_not_accept_a_session_override(self):
        from parallel_chain.command import format_receipts

        text = format_receipts(self.ledger, "sess-a", "session sess-b")
        self.assertNotIn("foreign_tool", text)
        self.assertNotIn(self.b["id"], text)
        self.assertIn("usage:", text)

    def test_other_sessions_receipt_is_not_found(self):
        from parallel_chain.command import format_receipts

        text = format_receipts(self.ledger, "sess-a", self.b["id"])
        self.assertIn("not found", text)
        self.assertNotIn("foreign_tool", text)

    def test_verify_tool_separates_integrity_payload_and_outcome(self):
        from parallel_chain.verify_api import verify_tool

        raw = json.dumps({"output": "ok-out", "exit_code": 0})
        before = self.path.read_bytes()
        report = json.loads(
            verify_tool(
                self.ledger,
                {
                    "receipt_id": self.a["id"],
                    "input_json": json.dumps({"command": "true"}),
                    "output_text": raw,
                },
                session_id="sess-a",
            )
        )
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(report["chain"]["status"], "intact")
        self.assertTrue(report["record"]["found"])
        self.assertTrue(report["record"]["digest_ok"])
        self.assertTrue(report["record"]["input_match"])
        self.assertTrue(report["record"]["output_match"])
        self.assertTrue(report["record"]["claim_rederived_ok"])
        self.assertEqual(report["record"]["claim"], "execution_observed")
        self.assertFalse(report["proves_task_success"])

        mismatch = json.loads(
            verify_tool(
                self.ledger,
                {"receipt_id": self.a["id"], "output_text": "different"},
                session_id="sess-a",
            )
        )
        self.assertEqual(mismatch["chain"]["status"], "intact")
        self.assertFalse(mismatch["record"]["output_match"])
        self.assertIsNone(mismatch["record"]["input_match"])

    def test_verify_lookup_is_session_scoped_and_rejects_paths(self):
        from parallel_chain.verify_api import verify_tool

        other = json.loads(
            verify_tool(self.ledger, {"receipt_id": self.b["id"]}, session_id="sess-a")
        )
        self.assertFalse(other["record"]["found"])
        self.assertNotIn("foreign_tool", json.dumps(other))
        hostile = json.loads(
            verify_tool(self.ledger, {"receipt_id": "../ledger.jsonl"}, session_id="sess-a")
        )
        self.assertFalse(hostile["record"]["found"])
        self.assertTrue(hostile["record"]["invalid_id"])
        self.assertNotIn("foreign_tool", json.dumps(hostile))
        no_session = json.loads(verify_tool(self.ledger, {"receipt_id": self.a["id"]}, session_id=""))
        self.assertFalse(no_session["record"]["found"])
        self.assertNotIn("terminal", json.dumps(no_session))

    def test_supplied_digest_mismatch_does_not_rewrite_the_ledger(self):
        from parallel_chain.verify_api import verify_tool

        before = self.path.read_bytes()
        report = json.loads(
            verify_tool(
                self.ledger,
                {"receipt_id": self.a["id"], "output_sha256": "ab" * 32},
                session_id="sess-a",
            )
        )
        self.assertFalse(report["record"]["output_match"])
        self.assertEqual(self.path.read_bytes(), before)


class RegisterTest(unittest.TestCase):
    def test_register_wires_the_documented_surfaces(self):
        from parallel_chain import register

        class Ctx:
            def __init__(self):
                self.tools = []
                self.hooks = []
                self.commands = []

            def register_tool(self, **kwargs):
                self.tools.append(kwargs)

            def register_hook(self, name, callback, **kwargs):
                self.hooks.append((name, callback))

            def register_command(self, name, handler, **kwargs):
                self.commands.append((name, handler, kwargs))

        ctx = Ctx()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"

            def factory():
                from parallel_chain.ledger import Ledger

                return Ledger(path, clock=_clock_factory(), id_factory=_id_factory())

            register(ctx, ledger_factory=factory)
            self.assertEqual([tool["name"] for tool in ctx.tools], ["chain_verify"])
            self.assertEqual([name for name, _cb in ctx.hooks], ["post_tool_call", "on_session_end"])
            self.assertEqual(ctx.commands[0][0], "chain")
            for _name, callback in ctx.hooks:
                self.assertIn("kwargs", callback.__code__.co_varnames)
            post = dict(ctx.hooks)["post_tool_call"]
            end = dict(ctx.hooks)["on_session_end"]
            post(
                tool_name="terminal",
                args={"command": "true"},
                result=json.dumps({"exit_code": 0}),
                task_id="task-1",
                duration_ms=3,
                session_id="sess-a",
                status="ok",
            )
            end(session_id="sess-a", completed=True, interrupted=False, model="mock", platform="cli")
            # A second end with no flags must not invent them, and must not raise.
            end(session_id="sess-a")
            post(tool_name="terminal", args={"command": object()}, result=object(), status=None)
            from parallel_chain.ledger import Ledger

            rows = Ledger(path).records()
            self.assertEqual([row["kind"] for row in rows], ["tool_call", "session_end", "session_end", "tool_call"])
            self.assertIsNone(rows[2]["completed"])
            self.assertEqual(rows[3]["claim"], "unknown")
            self.assertIsNone(rows[3]["input_sha256"])
            command = ctx.commands[0][1]
            text = command("", session_id="sess-a")
            self.assertIn("chain: intact", text)
            self.assertIn("session_end_observed", text)
            refused = command("", session_id="")
            self.assertIn("session id", refused)


if __name__ == "__main__":
    unittest.main()
