"""Retraction and order-sensitivity for Hermes Parallel."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hermes_parallel.engine import Graph, first_price, formula_total, order_sensitivity, report, run_supplier
from hermes_parallel.store import EvidenceLog, verify_anchors
from parallel_chain.ledger import Ledger


def _supplier(arrival=None) -> Graph:
    graph = Graph(session_id="sess-a", question="What is the supplier total?")
    run_supplier(graph, arrival=arrival or ["price_quote", "rival_price", "quantity", "tax_percent"])
    return graph


class RetractionTest(unittest.TestCase):
    def test_bad_quote_supports_a_total_until_it_is_corrected(self):
        graph = _supplier()
        self.assertEqual(graph.current("price_quote")["status"], "computed")
        self.assertFalse(graph.current("price_quote")["check_agreed"])
        self.assertEqual(graph.current("price_quote")["check_value"], 25)
        self.assertEqual(graph.current("quantity")["status"], "independently_checked")
        self.assertEqual(graph.current("total")["status"], "supported")
        self.assertEqual(graph.current("total")["value"], 440)
        self.assertEqual(graph.current("subtotal")["value"], 400)

    def test_retraction_recomputes_only_dependents(self):
        graph = _supplier()
        quantity_ids = [event["id"] for event in graph.run.events if event["key"] == "quantity"]
        tax_generation = graph.current("tax_percent")["generation"]
        result = graph.correct("price_quote", 25, reason="stale quote", formulas=__import__(
            "hermes_parallel.engine", fromlist=["SUPPLIER_FORMULAS"]
        ).SUPPLIER_FORMULAS)
        self.assertEqual(result.old_value, 40)
        self.assertEqual(result.new_value, 25)
        self.assertEqual(result.retracted, ["subtotal", "total"])
        self.assertEqual(result.recomputed, ["subtotal", "total"])
        self.assertIn("quantity", result.kept)
        self.assertIn("tax_percent", result.kept)
        self.assertIn("rival_price", result.kept)
        self.assertNotIn("subtotal", result.kept)
        self.assertEqual([event["id"] for event in graph.run.events if event["key"] == "quantity"], quantity_ids)
        self.assertEqual(graph.current("tax_percent")["generation"], tax_generation)
        self.assertEqual(graph.current("subtotal")["value"], 250)
        self.assertEqual(graph.current("subtotal")["status"], "supported")
        self.assertEqual(graph.current("total")["value"], 275)
        self.assertEqual(graph.current("total")["status"], "supported")
        history = [event["status"] for event in graph.run.events if event["key"] == "total"]
        self.assertEqual(history, ["proposed", "supported", "retracted", "supported"])

    def test_report_lists_supported_rejected_and_history(self):
        body = report(_supplier())
        self.assertFalse(body["proves_task_success"])
        self.assertEqual(body["supported"][0]["key"], "subtotal")
        self.assertIn("total", {item["key"] for item in body["supported"]})
        self.assertIn("rival_price", {item["key"] for item in body["rejected_alternatives"]})
        self.assertTrue(body["history"])


class OrderTest(unittest.TestCase):
    def test_formula_is_stable_and_first_price_is_not(self):
        quote_first = ["price_quote", "rival_price", "quantity", "tax_percent"]
        rival_first = ["rival_price", "price_quote", "quantity", "tax_percent"]
        early = _supplier(quote_first)
        late = _supplier(rival_first)
        self.assertEqual(first_price(early.run.events), 40)
        self.assertEqual(first_price(late.run.events), 25)
        self.assertEqual(formula_total(early.run.events), 440)
        self.assertEqual(formula_total(late.run.events), 440)
        findings = {item.key: item for item in order_sensitivity("sess-a", [quote_first, rival_first])}
        self.assertTrue(findings["first_price"].order_sensitive)
        self.assertEqual(findings["first_price"].values, [40, 25])
        self.assertFalse(findings["total"].order_sensitive)
        self.assertEqual(findings["total"].values, [440, 440])


class AnchorTest(unittest.TestCase):
    def test_events_anchor_into_the_receipts_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.jsonl")
            path = Path(tmp) / "evidence.jsonl"
            from hermes_parallel import investigate, retract
            body = investigate(
                "What is the supplier total?",
                session_id="sess-a",
                path=path,
                ledger=ledger,
            )
            self.assertEqual(body["supported"][-1]["value"], 440)
            self.assertIn("delegate_task was not called", body["note"])
            secret = "do-not-store-quote-text"
            self.assertNotIn(secret, (Path(tmp) / "ledger.jsonl").read_text(encoding="utf-8"))
            events = EvidenceLog(path, ledger=ledger, session_id="sess-a").load_events()
            self.assertEqual(verify_anchors(events, ledger), [])
            retraction = retract("price_quote", 25, reason="stale", session_id="sess-a", path=path, ledger=ledger)
            self.assertEqual(retraction["kept"], ["quantity", "rival_price", "tax_percent"])
            self.assertEqual(retraction["total"], 275)
            self.assertFalse(retraction["proves_task_success"])
            snapshot = path.read_text(encoding="utf-8")
            flipped = json.loads(snapshot.splitlines()[0])
            flipped["value"] = 999
            lines = snapshot.splitlines()
            lines[0] = json.dumps(flipped)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                EvidenceLog(path, session_id="sess-a").load_events()

    def test_command_fails_closed_without_a_session(self):
        from hermes_parallel import register

        class Ctx:
            def __init__(self):
                self.tools = []
                self.commands = []

            def register_tool(self, **kwargs):
                self.tools.append(kwargs["name"])

            def register_command(self, name, handler, **kwargs):
                self.commands.append((name, handler))

        ctx = Ctx()
        register(ctx)
        self.assertEqual(ctx.tools, [
            "parallel_investigate", "parallel_retract", "parallel_report", "parallel_order_check",
        ])
        text = ctx.commands[0][1]("supplier total", session_id="")
        self.assertIn("session id", text)


if __name__ == "__main__":
    unittest.main()
