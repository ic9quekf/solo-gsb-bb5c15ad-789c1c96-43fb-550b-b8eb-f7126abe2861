"""Domain logic: transformation registration, cycle detection, recall propagation.

Graph model
-----------
Every transformation contributes a directed edge ``input_batch -> output_batch``
for each of its inputs/outputs. Batches are nodes, so the registered
transformations form a DAG that is validated on every write.

Recall propagation ("minimum recall quantity")
----------------------------------------------
Contamination flows forward through the DAG:

* transformations are processed in a deterministic topological order
  (Kahn's algorithm, ties broken by ascending transformation id), so the
  contaminated quantity of every batch is final before its consumers run;
* a transformation draws from each input batch at most the quantity it
  actually consumes (``min(consumed, contaminated total of the batch)``);
* the contaminated inflow is distributed over the outputs proportionally to
  their produced quantities (total input quantity == total output quantity
  is enforced at write time).

Because quantities are tracked per batch rather than per path, batches reached
through several paths (diamonds) are counted exactly once: a join batch simply
sums the contaminated inflow of its direct inputs. The recall quantity of a
batch is the amount of contaminated material that actually flowed into it --
the minimum that must be recalled, rather than recalling whole batches blindly.
"""
from __future__ import annotations

import heapq
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

from .models import TransformationCreate

EPSILON = 1e-9
ROUND_DIGITS = 6


class DomainError(Exception):
    """Business-rule violation mapped to an HTTP response by the app layer."""

    def __init__(self, code: str, message: str, status_code: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass
class TransformationRecord:
    id: int
    inputs: dict[str, float] = field(default_factory=dict)
    outputs: dict[str, float] = field(default_factory=dict)
    created_at: str = ""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_transformations(conn: sqlite3.Connection) -> list[TransformationRecord]:
    rows = conn.execute(
        "SELECT id, created_at FROM transformations ORDER BY id"
    ).fetchall()
    records = {
        row["id"]: TransformationRecord(id=row["id"], created_at=row["created_at"])
        for row in rows
    }
    for row in conn.execute(
        "SELECT transformation_id, batch_id, quantity FROM transformation_inputs ORDER BY id"
    ):
        records[row["transformation_id"]].inputs[row["batch_id"]] = row["quantity"]
    for row in conn.execute(
        "SELECT transformation_id, batch_id, quantity FROM transformation_outputs ORDER BY id"
    ):
        records[row["transformation_id"]].outputs[row["batch_id"]] = row["quantity"]
    return [records[row["id"]] for row in rows]


# ---------------------------------------------------------------------------
# Transformation registration
# ---------------------------------------------------------------------------

def create_transformation(
    conn: sqlite3.Connection, payload: TransformationCreate
) -> TransformationRecord:
    inputs = _to_quantity_map(payload.inputs, side="inputs")
    outputs = _to_quantity_map(payload.outputs, side="outputs")

    overlap = sorted(set(inputs) & set(outputs))
    if overlap:
        raise DomainError(
            "cycle_detected",
            f"Batch(es) {overlap} appear as both input and output of the same "
            "transformation, which is a self-cycle.",
            409,
        )

    total_in = sum(inputs.values())
    total_out = sum(outputs.values())
    if abs(total_in - total_out) > EPSILON:
        raise DomainError(
            "quantity_imbalance",
            f"Quantity conservation violated: inputs sum to {total_in} but "
            f"outputs sum to {total_out}.",
            422,
        )

    existing = load_transformations(conn)
    edges = _build_batch_edges(existing)
    if _reachable(edges, starts=set(outputs), targets=set(inputs)):
        raise DomainError(
            "cycle_detected",
            "Registering this transformation would create a cycle: an output "
            "batch is already upstream of an input batch.",
            409,
        )

    cursor = conn.execute("INSERT INTO transformations DEFAULT VALUES")
    transformation_id = cursor.lastrowid
    conn.executemany(
        "INSERT INTO transformation_inputs (transformation_id, batch_id, quantity)"
        " VALUES (?, ?, ?)",
        [(transformation_id, batch, qty) for batch, qty in inputs.items()],
    )
    conn.executemany(
        "INSERT INTO transformation_outputs (transformation_id, batch_id, quantity)"
        " VALUES (?, ?, ?)",
        [(transformation_id, batch, qty) for batch, qty in outputs.items()],
    )
    created_at = conn.execute(
        "SELECT created_at FROM transformations WHERE id = ?", (transformation_id,)
    ).fetchone()["created_at"]
    return TransformationRecord(
        id=transformation_id, inputs=inputs, outputs=outputs, created_at=created_at
    )


def _to_quantity_map(items, side: str) -> dict[str, float]:
    quantities: dict[str, float] = {}
    for item in items:
        if item.batch_id in quantities:
            raise DomainError(
                "duplicate_batch",
                f"Batch '{item.batch_id}' is listed more than once in {side}.",
                422,
            )
        quantities[item.batch_id] = item.quantity
    return quantities


def _build_batch_edges(
    records: list[TransformationRecord],
) -> dict[str, set[str]]:
    edges: dict[str, set[str]] = defaultdict(set)
    for record in records:
        for source in record.inputs:
            edges[source].update(record.outputs)
    return edges


def _reachable(
    edges: dict[str, set[str]], starts: set[str], targets: set[str]
) -> bool:
    """True if any node in ``targets`` is reachable from ``starts``."""
    stack = list(starts)
    seen = set(stack)
    while stack:
        node = stack.pop()
        if node in targets:
            return True
        for nxt in edges.get(node, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return False


# ---------------------------------------------------------------------------
# Recall computation
# ---------------------------------------------------------------------------

def compute_recall(
    conn: sqlite3.Connection, batch_id: str, quantity: float | None
) -> dict:
    records = load_transformations(conn)

    produced: dict[str, float] = defaultdict(float)
    involved: set[str] = set()
    for record in records:
        for batch, qty in record.outputs.items():
            produced[batch] += qty
        involved.update(record.outputs)
        involved.update(record.inputs)

    if batch_id not in involved:
        raise DomainError(
            "batch_not_found",
            f"Batch '{batch_id}' does not appear in any transformation.",
            404,
        )

    if quantity is None:
        quantity = produced.get(batch_id, 0.0)
        if quantity <= 0:
            raise DomainError(
                "quantity_required",
                f"Batch '{batch_id}' is never produced by a transformation, so "
                "its quantity is unknown; provide the 'quantity' parameter.",
                422,
            )

    contaminated: dict[str, float] = defaultdict(float)
    contaminated[batch_id] = quantity

    for record in _topological_order(records):
        total_in = sum(record.inputs.values())
        contaminated_in = 0.0
        for batch, consumed in record.inputs.items():
            contaminated_in += min(consumed, contaminated.get(batch, 0.0))
        if contaminated_in <= 0:
            continue
        for out_batch, out_qty in record.outputs.items():
            contaminated[out_batch] += contaminated_in * out_qty / total_in

    affected = [
        {"batch_id": batch, "recall_quantity": round(qty, ROUND_DIGITS)}
        for batch, qty in contaminated.items()
        if batch != batch_id and qty > EPSILON
    ]
    affected.sort(key=lambda entry: entry["batch_id"])
    return {
        "batch_id": batch_id,
        "contaminated_quantity": quantity,
        "affected_batches": affected,
        "total_recall_quantity": round(
            sum(entry["recall_quantity"] for entry in affected), ROUND_DIGITS
        ),
    }


def _topological_order(
    records: list[TransformationRecord],
) -> list[TransformationRecord]:
    """Kahn's algorithm over the transformation DAG.

    Ties are broken by ascending transformation id so the processing order --
    and therefore the contaminated-quantity allocation -- is deterministic.
    """
    producers: dict[str, list[int]] = defaultdict(list)
    for record in records:
        for batch in record.outputs:
            producers[batch].append(record.id)

    by_id = {record.id: record for record in records}
    indegree = {record.id: 0 for record in records}
    dependents: dict[int, set[int]] = defaultdict(set)
    for record in records:
        upstream: set[int] = set()
        for batch in record.inputs:
            upstream.update(producers.get(batch, ()))
        upstream.discard(record.id)  # defensive; self-cycles are rejected on write
        indegree[record.id] = len(upstream)
        for upstream_id in upstream:
            dependents[upstream_id].add(record.id)

    heap = [rid for rid, degree in indegree.items() if degree == 0]
    heapq.heapify(heap)
    order: list[TransformationRecord] = []
    while heap:
        rid = heapq.heappop(heap)
        order.append(by_id[rid])
        for dependent_id in dependents[rid]:
            indegree[dependent_id] -= 1
            if indegree[dependent_id] == 0:
                heapq.heappush(heap, dependent_id)

    if len(order) != len(records):
        raise DomainError(
            "cycle_detected",
            "Stored transformations contain a cycle; cannot compute recall.",
            409,
        )
    return order
