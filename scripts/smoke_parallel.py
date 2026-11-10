#!/usr/bin/env python3
"""Mock-model demo of Hermes Parallel retraction.

Not a live model. The mock calls parallel_investigate, parallel_retract,
parallel_report, and parallel_order_check. Hermes dispatches those tools and
the receipts plugin records the calls.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HERMES_SRC = Path(os.environ.get("HERMES_SRC", "/tmp/hermes-src"))
HERMES_BIN = Path(os.environ.get("HERMES_BIN", str(HERMES_SRC / ".venv" / "bin" / "hermes")))
PORT = int(os.environ.get("RECEIPTS_MOCK_PORT", "43119"))
OUT = ROOT / "artifacts" / "parallel"
HOME = ROOT / ".hermes-home-parallel"

QUESTION = "What is the supplier total?"


def _count(messages: list) -> int:
    return sum(1 for message in messages if isinstance(message, dict) and message.get("role") == "tool")


def _call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": f"chatcmpl-{call_id}",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }],
            },
        }],
    }


def _final() -> dict:
    return {
        "id": "chatcmpl-final",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "finish_reason": "stop",
            "message": {
                "role": "assistant",
                "content": "The total was rebuilt after the bad quote was withdrawn. This sentence is the mock, not evidence.",
            },
        }],
    }


def _completion(messages: list) -> dict:
    step = _count(messages)
    if step == 0:
        return _call("call_investigate", "parallel_investigate", {"question": QUESTION})
    if step == 1:
        return _call("call_retract", "parallel_retract", {"key": "price_quote", "value": 25, "reason": "rival check was right"})
    if step == 2:
        return _call("call_report", "parallel_report", {})
    if step == 3:
        return _call("call_order", "parallel_order_check", {})
    return _final()


def _sse(payload: dict) -> bytes:
    message = payload["choices"][0]["message"]
    if message.get("tool_calls"):
        call = message["tool_calls"][0]
        chunks = [
            {"choices": [{"index": 0, "finish_reason": None, "delta": {"role": "assistant", "tool_calls": [{
                "index": 0, "id": call["id"], "type": "function",
                "function": {"name": call["function"]["name"], "arguments": call["function"]["arguments"]},
            }]}}]},
            {"choices": [{"index": 0, "finish_reason": "tool_calls", "delta": {}}]},
        ]
    else:
        chunks = [
            {"choices": [{"index": 0, "finish_reason": None, "delta": {"role": "assistant", "content": message["content"]}}]},
            {"choices": [{"index": 0, "finish_reason": "stop", "delta": {}}]},
        ]
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    return (body + "data: [DONE]\n\n").encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def _json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw.decode()) if raw else {}

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/").endswith("/models"):
            self._send(200, b'{"object":"list","data":[{"id":"mock-receipts"}]}', "application/json")
            return
        self._send(404, b"{}", "application/json")

    def do_POST(self) -> None:  # noqa: N802
        payload = self._json()
        messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
        record = {"step": _count(messages), "tools": []}
        for message in messages:
            if not isinstance(message, dict):
                continue
            if message.get("role") == "assistant":
                calls = message.get("tool_calls") or []
                if calls:
                    record["tools"].append(calls[0].get("function", {}).get("name"))
            if message.get("role") == "tool" and isinstance(message.get("content"), str):
                record.setdefault("results", []).append(message["content"][:2500])
        OUT.mkdir(parents=True, exist_ok=True)
        with (OUT / "mock-index.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._send(404, b"{}", "application/json")
            return
        completion = _completion(messages)
        if payload.get("stream"):
            self._send(200, _sse(completion), "text/event-stream")
            return
        self._send(200, json.dumps(completion).encode(), "application/json")


def _run(cmd: list[str], env: dict, log_name: str) -> int:
    print("+", " ".join(cmd), flush=True)
    result = subprocess.run(cmd, env=env, text=True, capture_output=True)
    (OUT / log_name).write_text(
        f"$ {' '.join(cmd)}\nexit={result.returncode}\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}\n",
        encoding="utf-8",
    )
    print(result.stdout)
    if result.returncode:
        print(result.stderr, file=sys.stderr)
    return result.returncode


def main() -> int:
    if not HERMES_BIN.exists():
        print(f"missing {HERMES_BIN}", file=sys.stderr)
        return 2
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    subprocess.run([str(HERMES_BIN), "gateway", "stop"], env={**os.environ, "HERMES_HOME": str(HOME)}, capture_output=True)
    if HOME.exists():
        shutil.rmtree(HOME)
    plugins = HOME / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "parallel-chain").symlink_to(ROOT / "parallel_chain")
    (plugins / "hermes-parallel").symlink_to(ROOT / "hermes_parallel")
    (HOME / "config.yaml").write_text(textwrap.dedent(f"""\
        _config_version: 12
        model:
          default: mock-receipts
          provider: custom
          base_url: http://127.0.0.1:{PORT}/v1
          api_key: local-mock
          api_mode: chat_completions
          context_length: 64000
        plugins:
          enabled:
            - parallel-chain
            - hermes-parallel
        platform_toolsets:
          cli:
            - hermes-cli
            - parallel-chain
            - hermes-parallel
        memory:
          memory_enabled: false
          user_profile_enabled: false
        compression:
          enabled: false
        auxiliary:
          title_generation:
            enabled: false
        agent:
          max_turns: 8
        tools:
          tool_search:
            enabled: "off"
    """), encoding="utf-8")
    env = os.environ.copy()
    env["HERMES_HOME"] = str(HOME)
    env["OPENAI_API_KEY"] = "local-mock"
    env.pop("OPENROUTER_API_KEY", None)
    doctor_r = _run([str(HERMES_BIN), "plugins", "doctor", str(ROOT / "parallel_chain"), "--ci"], env, "doctor-parallel-chain.log")
    doctor_p = _run([str(HERMES_BIN), "plugins", "doctor", str(ROOT / "hermes_parallel"), "--ci"], env, "doctor-parallel.log")
    listed = _run([str(HERMES_BIN), "plugins", "list", "--enabled", "--plain"], env, "plugins-list.log")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.2)
    oneshot = _run([str(HERMES_BIN), "-z", "Investigate the supplier total, then correct a bad quote."], env, "oneshot.log")
    server.shutdown()
    subprocess.run([str(HERMES_BIN), "gateway", "stop"], env=env, capture_output=True)
    ledger = HOME / "plugin-data" / "parallel-chain" / "ledger.jsonl"
    evidence = HOME / "plugin-data" / "hermes-parallel" / "evidence.jsonl"
    if ledger.exists():
        shutil.copy(ledger, OUT / "ledger.jsonl")
    if evidence.exists():
        shutil.copy(evidence, OUT / "evidence.jsonl")
    summary = {
        "label": "mock-model retraction demo, not a live LLM",
        "doctor_parallel_chain": doctor_r,
        "doctor_parallel": doctor_p,
        "plugins_list": listed,
        "oneshot_exit": oneshot,
        "ledger": ledger.exists(),
        "evidence": evidence.exists(),
    }
    if evidence.exists():
        rows = [json.loads(line) for line in evidence.read_text().splitlines() if line.strip()]
        summary["evidence_tail"] = [
            {"key": row["key"], "status": row["status"], "value": row["value"]}
            for row in rows
        ]
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    ok = doctor_r == 0 and doctor_p == 0 and oneshot == 0 and evidence.exists()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
