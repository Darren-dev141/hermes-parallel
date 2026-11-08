# Receipts

A Hermes Agent plugin that writes a local, hash-chained ledger of tool calls and turn-end observations, then checks that ledger for tampering.

A receipt shows what Hermes recorded. It does not prove that a task succeeded.

Tested against Hermes Agent **v0.23.0** (`2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`) on Python 3.14.10. Older releases were not tested. `requires_hermes` is `>=0.23.0` for that reason.

## What a receipt is

Each line of `ledger.jsonl` is one observation:

- schema `hermes.receipts.v1` and an opaque 32-hex id
- session, task, turn, and tool-call ids when Hermes supplied them
- UTC observation time (when the hook ran, not a clock inside the tool)
- tool name
- SHA-256 of the canonical tool arguments, and SHA-256 of the raw result string
- Hermes's reported status (`ok`, `error`, `blocked`, `cancelled`, `timeout`, or `unknown`)
- an integer `exit_code` only when the result JSON has an integer `exit_code`
- a fixed claim: `execution_observed`, `failed`, `unknown`, or `session_end_observed`
- the previous record's digest, and this record's digest

Raw arguments, raw results, error messages, and user text are not stored. A hash is not anonymization: a short or guessable value can be checked by hashing candidates. Do not treat the ledger as a place that has forgotten a secret that was passed to a tool.

## What it can and cannot prove

| Question | Answer |
| --- | --- |
| Was this record edited in place? | Yes, if you still have the file. A changed field breaks the digest. |
| Does an input or output you supply match what was hashed? | Yes. `receipts_verify` reports that separately from chain integrity and from the recorded status. |
| Did the tool process exit 0, or did Hermes mark the call failed? | Only when that field was actually present. A missing status stays `unknown`. A missing exit code stays unknown. A JSON `"error"` key is not treated as failure: the terminal tool includes `"error": null` on success. |
| Did the user's task succeed? | No. `proves_task_success` is always false. `completed: true` on a session-end receipt is a Hermes lifecycle flag. |
| Is the file the complete history? | No, unless you kept a tip digest outside the ledger and pass it as `checkpoint_digest`. Deleting the tail, or replacing the whole file, still verifies as intact. This is not a signature and not an independent attestation. |

Claims:

- `execution_observed` — Hermes reported status `ok`, or the result carried integer exit code 0, and those signals do not contradict each other.
- `failed` — status was `error`, `blocked`, `cancelled`, or `timeout`, or the exit code was a non-zero integer, without a contradictory `ok` plus exit 0.
- `unknown` — there is no decisive signal, or the signals disagree (status `ok` with a non-zero exit is recorded as `failed` with `signal_consistent: false`; status `error` with exit 0 is `unknown`).
- `session_end_observed` — `on_session_end` ran. It does not grade the turn.

In a normal Hermes v0.23.0 session, `post_tool_call` always receives a status, so a live tool receipt is `execution_observed` or `failed`, not `unknown`. `unknown` is what we store when status is absent or not a recognized token and there is no decisive exit code. Unit tests cover that. The live smoke's `receipts_verify` calls are `execution_observed` with status `ok` and **exit unknown**: Hermes said the dispatch was ok, and the result JSON had no `exit_code`. That is not a task-success flag.

## Coverage

Hooks, and only the events Hermes emits:

- `post_tool_call` after a dispatched tool returns, including results Hermes marks `error` or `blocked`.
- `on_session_end` from `agent/turn_finalizer.py` at **each turn finalization**, not at a unique session boundary. The v0.23.0 payload includes `completed`, `failed`, `interrupted`, `turn_exit_reason`, `model`, and `platform`. Omitted fields stay null. A reduced CLI-exit shape is stored the same way.
- A tool name Hermes rejects before dispatch does not create a receipt. With Tool Search left on its default, plugin tools are deferred behind `tool_search` / `tool_call`, and a direct `receipts_verify` call is rejected as "does not exist" with no ledger line. The smoke turns Tool Search off so the mock can call the tool by name. A real model can either do that or call the tool through the bridge.

`/receipts` reads the calling session only. It fails closed when the host does not pass a session id. It does not accept a session id from the command text, and it does not fall back to some other session's latest receipt. `receipts_verify` uses the same rule for record lookup. Chain status describes the whole file (a tampered line anywhere breaks it) without printing other sessions' tools.

The ledger lives in Hermes's profile-scoped plugin data directory (`plugin_data_dir("receipts") / ledger.jsonl`), not in the plugin install tree. It is append-only, locked across threads and processes, and not deleted by this plugin. Deleting or replacing that file is the user's action, and it is also the hole in an unanchored chain.

## Layout

```
receipts/           plugin (plugin.yaml + register)
  __init__.py       Hermes register(ctx)
  ledger.py         hash chain
  observe.py        hook payload -> receipt body
  verify_api.py     receipts_verify
  command.py        /receipts
  schemas.py
tests/test_receipts.py
scripts/smoke_session.py
artifacts/          unit-test log and the mock-model session
```

No third-party Python packages. The plugin does not make network calls, spawn shells, start background services, or read credential files.

## Run the tests

```bash
python3 -m unittest tests.test_receipts -v
```

32 tests. They cover canonical hashes, tampered fields, a recomputed tail that still looks intact until a checkpoint is supplied, corrupt lines, secrets absent from the file, unknown and non-zero exits, session isolation, hostile ids, concurrent appends, and repeated session-end events that do not invent missing flags.

## Install into Hermes

Hermes v0.23.0 needs Python 3.14. Its `pyproject.toml` pins `browser-harness` to Pillow 12.3.0 and Hermes itself to Pillow 13.0.0; `uv sync` applies the override in `[tool.uv]`. A plain `uv pip install` of the git URL does not, and fails.

```bash
uv python install 3.14
git clone --depth 1 --branch v0.23.0 https://github.com/NousResearch/hermes-agent.git
cd hermes-agent
uv sync --python 3.14 --frozen --no-dev
```

Point a profile at the plugin. `HERMES_HOME` below is a disposable directory; use your real profile if you want the plugin in an existing one.

```bash
export HERMES_HOME="$HOME/.hermes"
mkdir -p "$HERMES_HOME/plugins"
ln -s /absolute/path/to/this/repo/receipts "$HERMES_HOME/plugins/receipts"
```

In `$HERMES_HOME/config.yaml`:

```yaml
plugins:
  enabled:
    - receipts
```

If Tool Search is on (the default), the model will not see `receipts_verify` in the eager tool list. Either leave it to `tool_search` / `tool_call`, or set:

```yaml
tools:
  tool_search:
    enabled: "off"
```

Check the plugin the way the catalog does:

```bash
hermes plugins doctor /absolute/path/to/this/repo/receipts --ci
hermes plugins validate /absolute/path/to/this/repo/receipts --install-deps
```

Both passed on v0.23.0. Doctor: discovery, manifest, import, registration, 1 tool, 2 hooks. Validate: manifest, `requires_hermes`, load, declared tools and hooks, security scan **safe**, no core override. Validate also notes the plugin is eligible for `plugins.isolation: host`. Logs from this machine are in `artifacts/session/doctor.log` and `artifacts/session/validate.log`.

## Use it

In a Hermes chat session:

```
/receipts
/receipts <32-hex-id>
```

`hermes -z` has no slash prompt. The smoke calls the same dispatcher Hermes uses (`hermes_cli.plugin_command_dispatch.invoke_plugin_command`) with the session id from the ledger.

The model calls `receipts_verify` with any of:

- `receipt_id` — this session only
- `input_json` — JSON text of the arguments; hashed in canonical form (sorted keys, UTF-8, no extra whitespace)
- `output_text` — the exact result string; hashed as raw UTF-8, not re-parsed
- `input_sha256` / `output_sha256` — digests you computed yourself
- `checkpoint_digest` — a tip you copied out of the ledger earlier and stored somewhere else

The tool returns JSON. Chain integrity, payload match, and the recorded claim are separate fields. It does not rewrite the ledger. The verify call itself is then recorded by `post_tool_call`, so the chain grows after the answer you just read.

## Mock-model session (not a live LLM)

`scripts/smoke_session.py` installs nothing from a paid API. It serves a fixed OpenAI-style chat completion on `127.0.0.1:43117` and runs `hermes -z` twice against it. The script is labeled as a mock. It is not a conversation with a model.

What the second session recorded, and what `/receipts` printed for that session only:

- `terminal` exit 0, status `ok`, claim `execution_observed`
- `terminal` exit 3, status `error`, claim `failed`
- `receipts_verify` with the real output: `input_match: true`, `output_match: true`, chain `intact`, `proves_task_success: false`
- `receipts_verify` with a different output: `output_match: false`, chain still `intact` (the check does not edit the ledger)
- `session_end_observed` with `completed: true`, `lifecycle_failed: false`, `turn_exit_reason: text_response(finish_reason=stop)`

The file also contains the first session. `/receipts` did not list it. `receipts-zero` and `receipts-nonzero` are not in the ledger.

```bash
# after the Hermes venv exists at /tmp/hermes-src/.venv
python3 scripts/smoke_session.py
```

Evidence from that run: `artifacts/session/ledger.jsonl`, `artifacts/session/verify-results.json`, `artifacts/session/summary.json`, `artifacts/session/slash.json`, `artifacts/unit-tests.txt`.

## Before a hackathon entry

Not done, and not started from this tree:

- No catalog pull request, no public pin, no X post, no Typeform. Those need a separate go-ahead.
- `requires_hermes` is the release that was installed and tested, not a search for the oldest release that still loads.
- There is no external checkpoint store. Passing `checkpoint_digest` works; nothing here copies the tip anywhere trustworthy.
- Tool Search's default hides the verify tool from the eager list.
- `on_session_end` is per turn. A demo that says "when the session ends" is describing the wrong event.

## License

MIT. See `LICENSE`.
