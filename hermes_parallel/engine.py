"""Evidence graph for one parallel investigation.

Statuses are only the ones the run can actually justify: proposed, computed,
independently_checked, supported, unknown, and retracted. A retracted node
was supported or computed until a dependency changed. Nodes that do not
depend on the changed result are left as they were.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from dataclasses import dataclass, field

SCHEMA = "hermes.parallel.v1"
STATUSES = {
    "proposed",
    "computed",
    "independently_checked",
    "supported",
    "unknown",
    "retracted",
}
USABLE = {"computed", "independently_checked", "supported"}
EVENT_KEYS = (
    "schema",
    "id",
    "run_id",
    "session_id",
    "seq",
    "key",
    "kind",
    "status",
    "statement",
    "value",
    "depends_on",
    "worker",
    "arrival_index",
    "check_value",
    "check_agreed",
    "corrected_from",
    "retracted_because",
    "generation",
    "receipt_id",
    "prev_digest",
    "digest",
)


def digest_of(body: dict) -> str:
    payload = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _blank(**overrides) -> dict:
    event = {
        "schema": SCHEMA,
        "id": uuid.uuid4().hex,
        "run_id": None,
        "session_id": None,
        "seq": 0,
        "key": "",
        "kind": "investigation",
        "status": "unknown",
        "statement": "",
        "value": None,
        "depends_on": [],
        "worker": None,
        "arrival_index": None,
        "check_value": None,
        "check_agreed": None,
        "corrected_from": None,
        "retracted_because": None,
        "generation": 1,
        "receipt_id": None,
        "prev_digest": None,
        "digest": "",
    }
    event.update(overrides)
    return event


@dataclass
class Retraction:
    corrected: str
    old_value: object
    new_value: object
    retracted: list[str]
    recomputed: list[str]
    kept: list[str]


@dataclass
class OrderFinding:
    key: str
    values: list
    order_sensitive: bool


@dataclass
class Run:
    run_id: str
    session_id: str
    question: str
    events: list[dict] = field(default_factory=list)

    def current(self, key: str) -> dict | None:
        found = [event for event in self.events if event["key"] == key]
        return found[-1] if found else None

    def currents(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for event in self.events:
            out[event["key"]] = event
        return out


class Graph:
    def __init__(self, *, session_id: str, question: str, run_id: str | None = None):
        self.run = Run(run_id=run_id or uuid.uuid4().hex, session_id=session_id, question=question)
        self._arrival = 0
        self._lock = threading.Lock()

    def _append(self, event: dict) -> dict:
        with self._lock:
            body = {key: event[key] for key in EVENT_KEYS if key != "digest"}
            body["prev_digest"] = self.run.events[-1]["digest"] if self.run.events else None
            body["seq"] = len(self.run.events) + 1
            body["run_id"] = self.run.run_id
            body["session_id"] = self.run.session_id
            if set(body) != set(EVENT_KEYS) - {"digest"}:
                raise ValueError("evidence event is missing a field")
            body["digest"] = digest_of(body)
            self.run.events.append(body)
            return body

    def propose(self, key: str, statement: str, *, kind: str = "investigation", depends_on: list[str] | None = None) -> dict:
        return self._append(_blank(
            key=key, kind=kind, status="proposed", statement=statement,
            depends_on=list(depends_on or []),
        ))

    def compute(self, key: str, value: int, *, worker: str, statement: str, arrival_index: int | None = None) -> dict:
        with self._lock:
            if arrival_index is None:
                arrival_index = self._arrival
                self._arrival += 1
            else:
                self._arrival = max(self._arrival, arrival_index + 1)
        previous = self.current(key)
        generation = 1 if previous is None else previous["generation"]
        return self._append(_blank(
            key=key, kind="investigation", status="computed", statement=statement,
            value=value, worker=worker, arrival_index=arrival_index, generation=generation,
        ))

    def check(self, key: str, value: int, *, worker: str) -> dict:
        current = self.current(key)
        if current is None or current["value"] is None:
            return self._append(_blank(
                key=key, kind="investigation", status="unknown", statement=f"check of {key}",
                check_value=value, check_agreed=None, worker=worker,
            ))
        agreed = current["value"] == value
        status = "independently_checked" if agreed else current["status"]
        return self._append(_blank(
            key=key,
            kind="investigation",
            status=status,
            statement=current["statement"],
            value=current["value"],
            worker=worker,
            arrival_index=current["arrival_index"],
            check_value=value,
            check_agreed=agreed,
            generation=current["generation"],
            depends_on=list(current["depends_on"]),
        ))

    def conclude(self, key: str, statement: str, depends_on: list[str], formula) -> dict:
        values = {}
        for dep in depends_on:
            node = self.current(dep)
            if node is None or node["status"] not in USABLE or not isinstance(node["value"], int):
                return self._append(_blank(
                    key=key, kind="conclusion", status="unknown", statement=statement,
                    depends_on=list(depends_on),
                ))
            values[dep] = node["value"]
        value = formula(values)
        if not isinstance(value, int):
            raise ValueError("conclusions in this slice are integers")
        return self._append(_blank(
            key=key, kind="conclusion", status="supported", statement=statement,
            value=value, depends_on=list(depends_on),
        ))

    def current(self, key: str) -> dict | None:
        return self.run.current(key)

    def dependents(self, key: str) -> list[str]:
        """Keys whose current dependency list reaches `key`, not including `key`."""
        nodes = self.run.currents()
        changed = True
        hit = {key}
        while changed:
            changed = False
            for name, node in nodes.items():
                if name in hit:
                    continue
                if any(dep in hit for dep in node["depends_on"]):
                    hit.add(name)
                    changed = True
        hit.discard(key)
        return sorted(hit)

    def correct(self, key: str, new_value: int, *, reason: str, formulas: dict) -> Retraction:
        current = self.current(key)
        if current is None:
            raise KeyError(key)
        old = current["value"]
        downstream = self.dependents(key)
        kept = sorted(name for name in self.run.currents() if name != key and name not in downstream)
        self._append(_blank(
            key=key,
            kind="investigation",
            status="computed",
            statement=current["statement"],
            value=new_value,
            worker=current["worker"] or "correction",
            arrival_index=current["arrival_index"],
            corrected_from=old,
            generation=current["generation"] + 1,
        ))
        recomputed: list[str] = []
        retracted: list[str] = []
        # Recompute in dependency order: a node is ready when its deps are not still waiting.
        pending = list(downstream)
        guard = 0
        while pending and guard < 20:
            guard += 1
            progress = False
            for name in list(pending):
                node = self.current(name)
                if node is None:
                    pending.remove(name)
                    continue
                if any(dep in pending for dep in node["depends_on"]):
                    continue
                self._append(_blank(
                    key=name,
                    kind=node["kind"],
                    status="retracted",
                    statement=node["statement"],
                    value=node["value"],
                    depends_on=list(node["depends_on"]),
                    retracted_because=key,
                    generation=node["generation"],
                ))
                retracted.append(name)
                formula = formulas.get(name)
                if formula is not None and node["kind"] == "conclusion":
                    self.conclude(name, node["statement"], list(node["depends_on"]), formula)
                    recomputed.append(name)
                pending.remove(name)
                progress = True
            if not progress:
                break
        return Retraction(
            corrected=key,
            old_value=old,
            new_value=new_value,
            retracted=retracted,
            recomputed=recomputed,
            kept=kept,
        )


SUPPLIER_FORMULAS = {
    "subtotal": lambda values: values["price_quote"] * values["quantity"],
    "total": lambda values: values["subtotal"] * (100 + values["tax_percent"]) // 100,
}


def supplier_question(question: str) -> bool:
    text = question.lower()
    return "supplier" in text or "total" in text


def run_supplier(graph: Graph, *, arrival: list[str] | None = None) -> None:
    """Four competing reads. The quote is the number the total is built on.

    `arrival` forces compute order for replay. None runs the workers on threads
    and records completion order.
    """
    specs = {
        "price_quote": (40, "quote-desk", "Supplier quote, unverified"),
        "rival_price": (25, "rival-desk", "Competing quote, not used in the total"),
        "quantity": (10, "warehouse", "Units on the order"),
        "tax_percent": (10, "tax-table", "Tax percent"),
    }
    graph.propose("price_quote", specs["price_quote"][2])
    graph.propose("rival_price", specs["rival_price"][2])
    graph.propose("quantity", specs["quantity"][2])
    graph.propose("tax_percent", specs["tax_percent"][2])
    graph.propose("subtotal", "price times quantity", kind="conclusion", depends_on=["price_quote", "quantity"])
    graph.propose("total", "subtotal plus tax", kind="conclusion", depends_on=["subtotal", "tax_percent"])

    def compute_one(key: str, index: int | None) -> None:
        value, worker, statement = specs[key]
        graph.compute(key, value, worker=worker, statement=statement, arrival_index=index)

    if arrival is None:
        threads = [threading.Thread(target=compute_one, args=(key, None)) for key in specs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    else:
        for index, key in enumerate(arrival):
            compute_one(key, index)

    graph.check("quantity", 10, worker="warehouse-replay")
    graph.check("price_quote", 25, worker="rival-desk")
    graph.conclude("subtotal", "price times quantity", ["price_quote", "quantity"], SUPPLIER_FORMULAS["subtotal"])
    graph.conclude("total", "subtotal plus tax", ["subtotal", "tax_percent"], SUPPLIER_FORMULAS["total"])


def first_price(events: list[dict]) -> int | None:
    prices = [
        event for event in events
        if event["key"] in {"price_quote", "rival_price"}
        and event["status"] == "computed"
        and event["corrected_from"] is None
        and isinstance(event["arrival_index"], int)
    ]
    if not prices:
        return None
    prices.sort(key=lambda event: (event["arrival_index"], event["seq"]))
    return prices[0]["value"]


def _latest_usable(events: list[dict], key: str) -> int | None:
    found = [
        event for event in events
        if event["key"] == key and event["status"] in USABLE and isinstance(event["value"], int)
    ]
    return found[-1]["value"] if found else None


def formula_total(events: list[dict]) -> int | None:
    """Price times quantity plus tax, from the latest usable values. Arrival order is ignored."""
    price = _latest_usable(events, "price_quote")
    quantity = _latest_usable(events, "quantity")
    tax = _latest_usable(events, "tax_percent")
    if price is None or quantity is None or tax is None:
        return None
    return price * quantity * (100 + tax) // 100


def replay_supplier(session_id: str, arrival: list[str]) -> Graph:
    graph = Graph(session_id=session_id, question="replay")
    run_supplier(graph, arrival=arrival)
    return graph


def order_sensitivity(session_id: str, orders: list[list[str]]) -> list[OrderFinding]:
    totals = []
    firsts = []
    for order in orders:
        graph = replay_supplier(session_id, order)
        totals.append(formula_total(graph.run.events))
        firsts.append(first_price(graph.run.events))
    return [
        OrderFinding("total", totals, len(set(totals)) > 1),
        OrderFinding("first_price", firsts, len(set(firsts)) > 1),
    ]


def report(graph: Graph) -> dict:
    nodes = graph.run.currents()
    supported = sorted(key for key, node in nodes.items() if node["status"] == "supported")
    rejected = sorted(
        key for key, node in nodes.items()
        if key == "rival_price" or node.get("check_agreed") is False
    )
    unresolved = sorted(key for key, node in nodes.items() if node["status"] in {"unknown", "proposed"})
    retracted = sorted(key for key, node in nodes.items() if node["status"] == "retracted")
    return {
        "schema": "hermes.parallel.report.v1",
        "question": graph.run.question,
        "run_id": graph.run.run_id,
        "proves_task_success": False,
        "supported": [{"key": key, "value": nodes[key]["value"], "statement": nodes[key]["statement"]} for key in supported],
        "rejected_alternatives": [
            {"key": key, "value": nodes[key]["value"], "check_value": nodes[key]["check_value"], "statement": nodes[key]["statement"]}
            for key in rejected
        ],
        "unresolved": unresolved,
        "retracted": retracted,
        "history": [
            {"seq": event["seq"], "key": event["key"], "status": event["status"], "value": event["value"], "depends_on": event["depends_on"]}
            for event in graph.run.events
        ],
    }
