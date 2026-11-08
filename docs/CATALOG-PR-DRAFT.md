# Draft only — do not submit

Proposed title: Add Receipts: local tool-execution evidence for Hermes

This file is a draft for a future catalog PR. Nothing here has been opened against NousResearch/hermes-agent. No X post and no Typeform exist from this work.

## Pin

Plugin tree: `receipts/` (`subdir: receipts` if the catalog entry points at a repo that also contains tests).

Version in `plugin.yaml`: `0.1.0`.

`requires_hermes`: `>=0.23.0`. That is the release this tree was installed and run against (tag `v0.23.0`, commit `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`). It is not a claim that older Hermes releases fail or work.

Plugin code pin, full sha, version `0.1.0` tree: `e5d84abf09862c44709ba924c0e4adbf2fc00e1c`.

That commit is the plugin, tests, and the mock-session artifacts. This draft's sha line was added afterward and is not part of the pin. Do not open the catalog PR until the repository is public and someone has decided to enter.

## What it does

Receipts appends a hash-chained ledger of Hermes `post_tool_call` and `on_session_end` observations. `receipts_verify` checks the chain and, separately, whether an independently supplied input or output matches the stored hash. `/receipts` prints the calling session's records and the chain status.

Reported status stays `ok`, `error`, `blocked`, `cancelled`, `timeout`, or `unknown`. An integer `exit_code` is stored only when the result JSON has one. Missing fields stay unknown. `session_end_observed` plus `completed: true` is a lifecycle flag. `proves_task_success` is always false.

## Surfaces

Native `plugin.yaml` and `register(ctx)`. No core overrides.

- hooks: `post_tool_call`, `on_session_end`
- tool: `receipts_verify` (toolset `receipts`)
- slash command: `/receipts`
- storage: `plugin_data_dir("receipts") / ledger.jsonl`

`on_session_end` in v0.23.0 fires at each turn finalization (`agent/turn_finalizer.py`), not only when a session is torn down.

## Rule 13 disclosures

- No network calls, telemetry, credentials, spawned shell commands, or background services.
- Writes one plugin-owned local ledger: correlation ids, an observation timestamp, SHA-256 digests, fixed claim labels, Hermes's reported status, and an integer exit code when present. Not raw tool inputs or outputs, not error message text, not arbitrary claim text.
- Reads that ledger only. Lookups are 32-character lowercase hex ids. No credential stores, environment files, or caller-chosen paths.
- `/receipts` and `receipts_verify` record lookup are limited to the session id Hermes passes in. They fail closed when that id is missing. They do not list another session's tools.
- Hashes are not anonymization, signatures, or independent attestation. A locally writable chain cannot detect a replaced file or a deleted tail unless the caller supplies a `checkpoint_digest` that was stored outside the ledger.
- Coverage is the events Hermes emits. A tool name rejected before dispatch (including a plugin tool deferred by Tool Search, which is on by default) does not create a receipt. The smoke run set `tools.tool_search.enabled: off` so a mock model could call `receipts_verify` by name.
- No Python dependencies. Security scan on Hermes v0.23.0 reported `safe`. Doctor and validate passed; logs are in `artifacts/session/`.

## Checks still required before opening

- A public repository the author controls.
- Publish the repository, then use pin `e5d84abf09862c44709ba924c0e4adbf2fc00e1c` (or a later commit if the plugin code changes; bump `version` in the same change).
- A human decision to enter. This draft is not that decision.
