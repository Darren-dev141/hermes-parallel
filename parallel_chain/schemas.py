"""Tool schema shown to the model. The description is the contract."""

VERIFY_SCHEMA = {
    "name": "chain_verify",
    "description": (
        "Check this profile's receipts ledger and, optionally, one receipt from "
        "the calling session. Reports three separate facts: whether the records "
        "still on disk hash-chain together, whether an independently supplied "
        "input or output matches the stored hash, and what status was recorded "
        "(ok, failed, or unknown). A match does not prove the user's task "
        "succeeded. Unless checkpoint_digest is a tip you stored outside the "
        "ledger, a deleted tail or a replaced file still looks intact. "
        "Raw tool payloads are not stored; only hashes are."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "receipt_id": {
                "type": "string",
                "description": "32-character lowercase hex id copied from /chain.",
            },
            "input_json": {
                "type": "string",
                "description": (
                    "JSON text of the tool arguments. It is parsed and hashed in "
                    "canonical form (sorted keys, UTF-8). Do not pass a guess."
                ),
            },
            "output_text": {
                "type": "string",
                "description": (
                    "The exact tool result string. Hashed as raw UTF-8, not "
                    "re-parsed or re-ordered."
                ),
            },
            "input_sha256": {
                "type": "string",
                "description": "Lowercase SHA-256 hex of the canonical tool arguments.",
            },
            "output_sha256": {
                "type": "string",
                "description": "Lowercase SHA-256 hex of the raw tool result string.",
            },
            "checkpoint_digest": {
                "type": "string",
                "description": (
                    "Tip digest previously copied out of the ledger. This is the "
                    "only way the check can see tail deletion or full replacement."
                ),
            },
        },
    },
}
