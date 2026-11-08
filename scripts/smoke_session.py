#!/usr/bin/env python3
"""Host-driven Hermes session against a local mock model.

This is not a live model conversation. A stdlib HTTP server answers
OpenAI-style chat completions with a fixed tool sequence. Hermes itself
discovers the plugin, dispatches tools, and fires post_tool_call and
on_session_end. After the process exits, this script calls /receipts through
Hermes's own slash-command dispatcher, because `hermes -z` has no prompt.

Requires the Hermes v0.23.0 checkout installed at /tmp/hermes-src/.venv
(see README). Writes logs and the ledger under artifacts/session/.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HERMES_SRC = Path(os.environ.get("HERMES_SRC", "/tmp/hermes-src"))
HERMES_BIN = Path(os.environ.get("HERMES_BIN", str(HERMES_SRC / ".venv" / "bin" / "hermes")))
HERMES_PYTHON = Path(os.environ.get("HERMES_PYTHON", str(HERMES_SRC / ".venv" / "bin" / "python")))
PORT = int(os.environ.get("RECEIPTS_MOCK_PORT", "43117"))
OUT = ROOT / "artifacts" / "session"
HOME = ROOT / ".hermes-home"

ZERO_CMD = "python3 -c \"print('receipts-zero')\""
FAIL_CMD = "python3 -c \"import sys; print('receipts-nonzero'); sys.exit(3)\""


def _tool_result_count(messages: list) -> int:
    return sum(1 for message in messages if isinstance(message, dict) and message.get("role") == "tool")


def _zero_receipt() -> dict | None:
    path = HOME / "plugin-data" / "receipts" / "ledger.jsonl"
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("tool_name") == "terminal" and row.get("reported_exit_code") == 0:
            return row
    return None


def _completion(messages: list) -> dict:
    step = _tool_result_count(messages)
    if step == 0:
        return _tool_call("call_zero", "terminal", {"command": ZERO_CMD, "timeout": 30})
    if step == 1:
        return _tool_call("call_fail", "terminal", {"command": FAIL_CMD, "timeout": 30})
    receipt = _zero_receipt()
    receipt_id = receipt["id"] if receipt else "0" * 32
    exact_output = '{"output": "receipts-zero", "exit_code": 0, "error": null}'
    if step == 2:
        return _tool_call(
            "call_verify_match",
            "receipts_verify",
            {
                "receipt_id": receipt_id,
                "output_text": exact_output,
                "input_json": json.dumps({"command": ZERO_CMD, "timeout": 30}),
            },
        )
    if step == 3:
        return _tool_call(
            "call_verify_mismatch",
            "receipts_verify",
            {"receipt_id": receipt_id, "output_text": "not-the-output"},
        )
    return {
        "id": "chatcmpl-final",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "mock-session-finished. This text is the mock, not evidence.",
                },
                "finish_reason": "stop",
            }
        ],
    }


def _tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": f"chatcmpl-{call_id}",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }


def _sse(payload: dict) -> bytes:
    choice = payload["choices"][0]
    message = choice["message"]
    chunks = []
    if message.get("tool_calls"):
        call = message["tool_calls"][0]
        chunks.append(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": call["id"],
                                    "type": "function",
                                    "function": {
                                        "name": call["function"]["name"],
                                        "arguments": call["function"]["arguments"],
                                    },
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ]
            }
        )
        finish = "tool_calls"
    else:
        chunks.append(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": message.get("content") or ""},
                        "finish_reason": None,
                    }
                ]
            }
        )
        finish = "stop"
    chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    return (body + "data: [DONE]\n\n").encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    log_path = OUT / "mock-requests.jsonl"

    def log_message(self, fmt: str, *args) -> None:
        return

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _write(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/").endswith("/models"):
            body = json.dumps({"object": "list", "data": [{"id": "mock-receipts"}]}).encode()
            self._write(200, body, "application/json")
            return
        self._write(404, b"{}", "application/json")

    def do_POST(self) -> None:  # noqa: N802
        payload = self._read_json()
        messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
        record = {
            "path": self.path,
            "stream": bool(payload.get("stream")),
            "roles": [
                {
                    "role": message.get("role"),
                    "tool": ((message.get("tool_calls") or [{}])[0].get("function") or {}).get("name")
                    if isinstance(message, dict) else None,
                    "tool_result": (message.get("content") or "")[:400]
                    if isinstance(message, dict) and message.get("role") == "tool" else None,
                }
                for message in messages
                if isinstance(message, dict)
            ],
        }
        OUT.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._write(404, b"{\"error\":\"unsupported\"}", "application/json")
            return
        completion = _completion(messages)
        if payload.get("stream"):
            body = _sse(completion)
            self._write(200, body, "text/event-stream")
            return
        self._write(200, json.dumps(completion).encode("utf-8"), "application/json")


def _run(cmd: list[str], env: dict, log_name: str) -> int:
    print("+", " ".join(cmd), flush=True)
    result = subprocess.run(cmd, env=env, text=True, capture_output=True)
    (OUT / log_name).write_text(
        f"$ {' '.join(cmd)}\nexit={result.returncode}\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}\n",
        encoding="utf-8",
    )
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    return result.returncode


def _stop_gateway() -> None:
    """hermes -z on v0.23 talks to a profile gateway. An old one keeps the previous config."""
    if not HERMES_BIN.exists():
        return
    subprocess.run(
        [str(HERMES_BIN), "gateway", "stop"],
        env={**os.environ, "HERMES_HOME": str(HOME)},
        text=True,
        capture_output=True,
    )
    subprocess.run(["pkill", "-f", f"gateway run --quiet --idle-exit"], check=False)


def _write_home() -> None:
    _stop_gateway()
    if HOME.exists():
        shutil.rmtree(HOME)
    plugin_dir = HOME / "plugins" / "receipts"
    plugin_dir.parent.mkdir(parents=True)
    plugin_dir.symlink_to(ROOT / "receipts")
    config = textwrap.dedent(
        f"""\
        _config_version: 12
        model:
          default: mock-receipts
          provider: custom
          base_url: http://127.0.0.1:{PORT}/v1
          api_key: local-mock
          api_mode: chat_completions
          context_length: 64000
        toolsets:
          - terminal
          - receipts
        platform_toolsets:
          cli:
            - hermes-cli
            - receipts
        plugins:
          enabled:
            - receipts
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
        # Plugin tools are deferred behind tool_search by default. Turn that off
        # so this deterministic mock can call receipts_verify by name.
        tools:
          tool_search:
            enabled: "off"
        """
    )
    (HOME / "config.yaml").write_text(config, encoding="utf-8")
    (HOME / ".env").write_text("OPENAI_API_KEY=local-mock\n", encoding="utf-8")


def _slash(env: dict) -> str:
    code = textwrap.dedent(
        """\
        import json
        import os
        from pathlib import Path
        from hermes_cli.plugin_command_dispatch import invoke_plugin_command
        from hermes_cli.plugins import get_plugin_commands

        home = Path(os.environ["HERMES_HOME"])
        ledger_path = home / "plugin-data" / "receipts" / "ledger.jsonl"
        rows = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        session_ids = [row.get("session_id") for row in rows if isinstance(row.get("session_id"), str)]
        session_id = session_ids[-1] if session_ids else ""
        handler = get_plugin_commands()["receipts"]["handler"]
        text = invoke_plugin_command(
            handler, "", session_id=session_id, platform="cli", session_key="smoke", source=None,
        )
        focused = ""
        tool_rows = [row for row in rows if row.get("session_id") == session_id and row.get("kind") == "tool_call"]
        if tool_rows:
            focused = invoke_plugin_command(
                handler, tool_rows[0]["id"], session_id=session_id, platform="cli",
                session_key="smoke", source=None,
            )
        missing = invoke_plugin_command(
            handler, "", session_id="", platform="cli", session_key="smoke", source=None,
        )
        Path(os.environ["SMOKE_OUT"]).write_text(
            json.dumps({"session_id": session_id, "list": text, "focus": focused, "no_session": missing}, indent=2),
            encoding="utf-8",
        )
        print(text)
        """
    )
    env = dict(env)
    env["SMOKE_OUT"] = str(OUT / "slash.json")
    result = subprocess.run(
        [str(HERMES_PYTHON), "-c", code],
        env=env,
        text=True,
        capture_output=True,
    )
    (OUT / "slash-invoke.log").write_text(
        f"exit={result.returncode}\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}\n",
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise SystemExit(f"slash dispatch failed:\n{result.stderr}")
    return result.stdout


def main() -> int:
    if not HERMES_BIN.exists():
        print(f"hermes binary not found at {HERMES_BIN}", file=sys.stderr)
        return 2
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True, exist_ok=True)
    _write_home()
    env = os.environ.copy()
    env["HERMES_HOME"] = str(HOME)
    env["HOME"] = str(HOME / "unix-home")
    (HOME / "unix-home").mkdir(parents=True, exist_ok=True)
    env.pop("OPENROUTER_API_KEY", None)
    env.pop("ANTHROPIC_API_KEY", None)
    env["OPENAI_API_KEY"] = "local-mock"

    # First launch persists plugin toolset keys. A cold gateway resolves tools
    # before discovery finishes and would omit receipts_verify.
    _run([str(HERMES_BIN), "plugins", "list", "--enabled", "--plain"], env, "plugins-list.log")
    doctor = _run([str(HERMES_BIN), "plugins", "doctor", str(ROOT / "receipts"), "--ci"], env, "doctor.log")
    validate = _run(
        [str(HERMES_BIN), "plugins", "validate", str(ROOT / "receipts"), "--install-deps"],
        env,
        "validate.log",
    )

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    import threading

    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.2)

    prompt = (
        "Run the receipts smoke. Call the terminal tool when asked. "
        "Do not claim the task succeeded."
    )
    # The first gateway process can snapshot its tool list before user plugins
    # finish registering. A second one-shot reuses the warm gateway.
    warmup = _run([str(HERMES_BIN), "-z", prompt], env, "oneshot-warmup.log")
    oneshot = _run([str(HERMES_BIN), "-z", prompt], env, "oneshot.log")
    server.shutdown()
    _stop_gateway()

    ledger = HOME / "plugin-data" / "receipts" / "ledger.jsonl"
    if ledger.exists():
        shutil.copy(ledger, OUT / "ledger.jsonl")
    else:
        (OUT / "ledger-missing.txt").write_text(str(ledger), encoding="utf-8")

    slash_text = ""
    if ledger.exists():
        slash_text = _slash(env)

    summary = {
        "hermes_bin": str(HERMES_BIN),
        "doctor_exit": doctor,
        "validate_exit": validate,
        "warmup_exit": warmup,
        "oneshot_exit": oneshot,
        "ledger_exists": ledger.exists(),
        "ledger_bytes": ledger.stat().st_size if ledger.exists() else 0,
        "slash_preview": slash_text[:1500],
        "label": "mock-model host-driven session, not a live LLM",
    }
    if ledger.exists():
        rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
        summary["records"] = [
            {
                "kind": row.get("kind"),
                "tool_name": row.get("tool_name"),
                "claim": row.get("claim"),
                "reported_status": row.get("reported_status"),
                "reported_exit_code": row.get("reported_exit_code"),
                "exit_code_known": row.get("exit_code_known"),
                "session_id": row.get("session_id"),
            }
            for row in rows
        ]
        blob = ledger.read_text(encoding="utf-8")
        summary["stored_raw_marker"] = "receipts-zero" in blob or "receipts-nonzero" in blob
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0 if doctor == 0 and validate == 0 and oneshot == 0 and ledger.exists() else 1


if __name__ == "__main__":
    raise SystemExit(main())
