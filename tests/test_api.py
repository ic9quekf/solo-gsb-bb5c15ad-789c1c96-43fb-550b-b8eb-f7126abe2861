"""End-to-end API tests against a temporary SQLite database."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

TRANSFORMATIONS_URL = "/api/v1/transformations"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    with TestClient(app) as test_client:
        yield test_client


def register(client, inputs, outputs):
    """Shorthand for registering a transformation."""
    return client.post(
        TRANSFORMATIONS_URL,
        json={
            "inputs": [{"batch_id": b, "quantity": q} for b, q in inputs],
            "outputs": [{"batch_id": b, "quantity": q} for b, q in outputs],
        },
    )


def recall(client, batch_id, quantity=None):
    params = {} if quantity is None else {"quantity": quantity}
    return client.get(f"/api/v1/recalls/{batch_id}", params=params)


def affected_map(response):
    return {
        entry["batch_id"]: entry["recall_quantity"]
        for entry in response.json()["affected_batches"]
    }


# ---------------------------------------------------------------------------
# Health & basic registration
# ---------------------------------------------------------------------------

def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_create_transformation_success(client):
    response = register(
        client,
        inputs=[("RAW-1", 100)],
        outputs=[("WIP-1", 60), ("WIP-2", 40)],
    )
    assert response.status_code == 201
    body = response.json()
    assert body["id"] == 1
    assert body["inputs"] == [{"batch_id": "RAW-1", "quantity": 100.0}]
    assert {o["batch_id"] for o in body["outputs"]} == {"WIP-1", "WIP-2"}
    assert body["created_at"]


def test_list_transformations(client):
    register(client, [("A", 10)], [("B", 10)])
    register(client, [("B", 10)], [("C", 10)])
    response = client.get(TRANSFORMATIONS_URL)
    assert response.status_code == 200
    body = response.json()
    assert [t["id"] for t in body] == [1, 2]
    assert body[1]["inputs"] == [{"batch_id": "B", "quantity": 10.0}]


def test_persistence_across_connections(client):
    register(client, [("A", 10)], [("B", 10)])
    # A fresh request (new connection) must see the previously stored data.
    assert len(client.get(TRANSFORMATIONS_URL).json()) == 1


# ---------------------------------------------------------------------------
# Write-time validation
# ---------------------------------------------------------------------------

def test_quantity_imbalance_rejected(client):
    response = register(client, [("A", 100)], [("B", 90)])
    assert response.status_code == 422
    assert response.json()["code"] == "quantity_imbalance"


def test_duplicate_batch_rejected(client):
    response = register(client, [("A", 10), ("A", 5)], [("B", 15)])
    assert response.status_code == 422
    assert response.json()["code"] == "duplicate_batch"


def test_self_cycle_rejected(client):
    response = register(client, [("A", 10)], [("A", 10)])
    assert response.status_code == 409
    assert response.json()["code"] == "cycle_detected"


def test_indirect_cycle_rejected(client):
    assert register(client, [("A", 10)], [("B", 10)]).status_code == 201
    assert register(client, [("B", 10)], [("C", 10)]).status_code == 201
    response = register(client, [("C", 10)], [("A", 10)])
    assert response.status_code == 409
    assert response.json()["code"] == "cycle_detected"


def test_cycle_through_multi_input_transformation_rejected(client):
    assert register(client, [("A", 10)], [("B", 10)]).status_code == 201
    # B is upstream of nothing yet, but A is upstream of B: X -> A is fine,
    # whereas B -> X plus this would close a loop once X -> A exists.
    assert register(client, [("X", 5)], [("A", 5)]).status_code == 201
    response = register(client, [("B", 3), ("Y", 3)], [("X", 6)])
    assert response.status_code == 409


def test_late_upstream_registration_allowed(client):
    # Registering an upstream transformation after its consumer is not a cycle.
    assert register(client, [("A", 10)], [("B", 10)]).status_code == 201
    assert register(client, [("RAW", 10)], [("A", 10)]).status_code == 201


def test_request_validation_errors(client):
    # Empty inputs / outputs are rejected by schema validation.
    assert register(client, [], [("B", 1)]).status_code == 422
    assert register(client, [("A", 1)], []).status_code == 422
    # Non-positive quantities are rejected.
    assert register(client, [("A", 0)], [("B", 0)]).status_code == 422
    assert register(client, [("A", -5)], [("B", -5)]).status_code == 422
    # Blank batch ids are rejected.
    assert register(client, [("  ", 1)], [("B", 1)]).status_code == 422


# ---------------------------------------------------------------------------
# Recall propagation
# ---------------------------------------------------------------------------

def test_recall_linear_flow(client):
    register(client, [("X", 100)], [("A", 100)])
    register(client, [("A", 100)], [("B", 50), ("C", 50)])
    response = recall(client, "X", quantity=100)
    assert response.status_code == 200
    assert affected_map(response) == {"A": 100.0, "B": 50.0, "C": 50.0}
    assert response.json()["total_recall_quantity"] == 200.0


def test_recall_diamond_counts_shared_batch_once(client):
    register(client, [("X", 100)], [("A", 100)])
    register(client, [("X", 100)], [("B", 100)])
    register(client, [("A", 100), ("B", 100)], [("C", 200)])
    response = recall(client, "X", quantity=100)
    assert response.status_code == 200
    # C is reached via two paths but must be counted exactly once.
    assert affected_map(response) == {"A": 100.0, "B": 100.0, "C": 200.0}
    assert response.json()["total_recall_quantity"] == 400.0


def test_recall_partial_consumption_recalls_minimum(client):
    register(client, [("X", 100)], [("A", 100)])
    register(client, [("A", 50)], [("B", 50)])  # only half of A is consumed
    response = recall(client, "X", quantity=100)
    assert affected_map(response) == {"A": 100.0, "B": 50.0}


def test_recall_mixed_inputs_spreads_proportionally(client):
    register(client, [("X", 100)], [("A", 100)])
    register(client, [("A", 50), ("Y", 50)], [("B", 100)])
    response = recall(client, "X", quantity=100)
    # Only 50 of the 100 consumed units are contaminated; B gets 50%.
    assert affected_map(response) == {"A": 100.0, "B": 50.0}


def test_recall_respects_topological_not_insertion_order(client):
    register(client, [("A", 100)], [("B", 100)])   # registered first
    register(client, [("X", 100)], [("A", 100)])   # upstream registered later
    response = recall(client, "X", quantity=100)
    assert affected_map(response) == {"A": 100.0, "B": 100.0}


def test_recall_default_quantity_is_produced_total(client):
    register(client, [("X", 80)], [("A", 80)])
    register(client, [("A", 80)], [("B", 80)])
    response = recall(client, "A")
    assert response.status_code == 200
    assert response.json()["contaminated_quantity"] == 80.0
    assert affected_map(response) == {"B": 80.0}


def test_recall_unknown_batch_returns_404(client):
    register(client, [("A", 10)], [("B", 10)])
    response = recall(client, "NOPE", quantity=5)
    assert response.status_code == 404
    assert response.json()["code"] == "batch_not_found"


def test_recall_raw_material_requires_quantity(client):
    register(client, [("X", 50)], [("A", 50)])
    response = recall(client, "X")  # X is never produced -> quantity unknown
    assert response.status_code == 422
    assert response.json()["code"] == "quantity_required"


def test_recall_rejects_non_positive_quantity(client):
    register(client, [("X", 50)], [("A", 50)])
    assert recall(client, "X", quantity=0).status_code == 422
    assert recall(client, "X", quantity=-1).status_code == 422


def test_recall_is_stable_across_calls(client):
    register(client, [("X", 100)], [("A", 60), ("B", 40)])
    register(client, [("A", 60), ("B", 40)], [("C", 100)])
    first = recall(client, "X", quantity=100).json()
    second = recall(client, "X", quantity=100).json()
    assert first == second
    assert [e["batch_id"] for e in first["affected_batches"]] == sorted(
        e["batch_id"] for e in first["affected_batches"]
    )


def test_recall_does_not_propagate_upstream(client):
    register(client, [("X", 100)], [("A", 100)])
    register(client, [("A", 100)], [("B", 100)])
    response = recall(client, "B", quantity=100)
    assert response.json()["affected_batches"] == []
    assert response.json()["total_recall_quantity"] == 0.0
