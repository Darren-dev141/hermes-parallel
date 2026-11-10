# Draft only — do not submit

Proposed title: Add Hermes Parallel: auditable fan-out on a receipts ledger

Nothing here has been opened against NousResearch/hermes-agent. No X post and no Typeform exist from this work. Paper citations in the research notes were not checked and are not part of this description.

## What it does

Two plugins.

`parallel-chain` (directory `parallel_chain/`) appends a hash-chained ledger of `post_tool_call` and `on_session_end` observations. The catalog keys `receipts` and `hermes-receipts` are already taken, and `/receipts` is already used, so this ledger's id is `parallel-chain`, its tool is `chain_verify`, and its command is `/chain`. `on_session_end` in Hermes v0.23.0 fires at each turn finalization, not only when a chat session ends. `chain_verify` checks the chain and, separately, an independently supplied input or output. `proves_task_success` is always false. Missing status stays unknown. A missing exit code stays unknown.

Hermes Parallel (`hermes-parallel`, directory `hermes_parallel/`) fans a question out into parallel investigations and records an evidence log on that receipts chain. Each item has a status: proposed, computed, independently_checked, supported, unknown, or retracted, plus the keys it depends on. Correcting a value retracts and recomputes only the dependents. An order check replays two arrival orders and flags a conclusion whose value changes. `/investigate` and `/parallel` are the commands. The supplier-total case is the scripted demo: a supported total of 440 built on a quote of 40, then retracted to 275 when the quote is corrected to 25. Quantity and tax are kept.

## Surfaces

Native `plugin.yaml` and `register(ctx)`. No core overrides. No `delegate_task` call. v0.23 can fan out with `delegate_task`'s `tasks` array, and a top-level call does not join child model loops in the same turn, so the investigations in this slice are local threads.

parallel-chain: hooks `post_tool_call`, `on_session_end`; tool `chain_verify`; command `/chain`; storage `plugin_data_dir("parallel-chain")/ledger.jsonl`.

Hermes Parallel: tools `parallel_investigate`, `parallel_retract`, `parallel_report`, `parallel_order_check`; commands `/investigate`, `/parallel`; storage `plugin_data_dir("hermes-parallel")/evidence.jsonl`, with each new event anchored into the receipts ledger.

## Rule 13 disclosures

- No network calls, telemetry, credentials, spawned shells, or background services.
- Receipts writes hashes, ids, timestamps, reported status, and an integer exit code when present. Not raw tool payloads.
- Hermes Parallel writes the evidence values the demo needs (the quote, the quantity, the tax, the totals) and their dependency keys. It does not write arbitrary model prose. Those events are hashed into the receipts ledger.
- Both plugins read only their own records, scoped to the session id Hermes passes. They fail closed when that id is missing.
- A local hash chain does not detect a replaced file or a deleted tail without a checkpoint stored outside the ledger.
- Tool Search, left on, hides plugin tools. A direct call that Hermes rejects before dispatch creates no receipt. The mock demo sets `tools.tool_search.enabled: off`.
- No Python dependencies.

## Pin

`requires_hermes` is `>=0.23.0` because that is the release these plugins were run against (tag `v0.23.0`, commit `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`). Older releases were not tested.

Do not fill a catalog sha until the plugin code is public and someone decides to enter. The previous pin `e5d84abf09862c44709ba924c0e4adbf2fc00e1c` is only the receipts plugin, before Hermes Parallel.
