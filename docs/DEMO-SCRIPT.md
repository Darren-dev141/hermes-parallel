# Demo script — mock session, not recorded

Do not post this. The run below already happened against a local mock model, not a live LLM. A recording still needs a separate go-ahead, a public repo, and a catalog PR that does not exist yet.

Label the recording as a scripted local model. Do not call it a conversation with a model.

Hermes: v0.23.0, commit `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`.
Plugin: `receipts` 0.1.0, Tool Search off so `receipts_verify` is in the eager tool list.
Evidence: `artifacts/session/`.

0–10s. "An agent saying done is not evidence. Receipts records what Hermes actually observed."

10–28s. Show `hermes plugins list` with `receipts` enabled. Show the ledger lines for the exit-0 terminal call: tool name `terminal`, status `ok`, exit `0`, claim `execution_observed`, input and output hashes. "The command output is not in the file."

28–42s. Show the next line: exit `3`, status `error`, claim `failed`. Then a `receipts_verify` line: status `ok`, exit `unknown`, claim `execution_observed`. "Failure stays failure. A call with no exit code is not marked exit 0. Hermes saying ok is not the task succeeding."

42–57s. Show the verify JSON from `artifacts/session/verify-results.json`. First call: chain `intact`, `input_match: true`, `output_match: true`, `proves_task_success: false`. Second call, different output text: `output_match: false`, chain still `intact`. "The check does not rewrite the ledger. Matching hashes and a recorded status are different facts."

57–69s. Show the session-end line: `session_end_observed`, `completed: true`, `turn_exit_reason: text_response(finish_reason=stop)`. "That flag is written at the end of the turn, every turn. It is not proof the whole task worked." Mention `/receipts` printed only this session; the file also holds the earlier one.

69–80s. "Local, no account, no telemetry. The chain catches an edited record. It does not catch someone replacing the file or deleting the tail, unless a tip digest was saved somewhere else. Receipts separates an observed tool call from a claim that the job is done."

Recording gate: legible output, no private data, under 90 seconds. Tagging @NousResearch and submitting the Typeform are a separate decision.
