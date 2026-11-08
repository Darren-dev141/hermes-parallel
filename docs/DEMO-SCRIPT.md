# Demo script — under 90 seconds, not recorded

Do not post this. The run already happened against a local mock, not a live model. `artifacts/parallel/` is that transcript.

Hermes v0.23.0. Both plugins enabled. Tool Search off so the tools are callable by name.

`on_session_end` fires at the end of every turn in this release, not only when the chat session ends. Do not say "when the session ends" and point at that hook.

0–12s. "My agents agreed on an answer built on a bad number. The total looked supported. The quote underneath it was wrong."

12–28s. Show the evidence history: four investigations in parallel, quantity independently checked at 10, rival quote 25, the quote that was used still 40. Subtotal 400, total 440, both `supported`. "Supported means the arithmetic followed the quote. It does not mean the quote was true."

28–48s. Correct `price_quote` to 25. Show the retraction result: `retracted` is subtotal and total, `recomputed` is those two, `kept` is quantity, tax, and the rival quote. New total 275, status `supported`. "The bad number took its conclusions with it. The checks that did not use it stayed."

48–68s. Show the order check. Total is 440 then 440, not order-sensitive. `first_price` is 40 then 25, order-sensitive. "Same evidence, different arrival order. We flag the conclusion that moved, and we leave the one that did not."

68–85s. "The history is still there: 440 was retracted, not deleted. The receipts chain anchors the events. None of this proves the supplier question is finished. It shows which claims the evidence still supports."

Recording, tagging, and the catalog form are a separate decision.
