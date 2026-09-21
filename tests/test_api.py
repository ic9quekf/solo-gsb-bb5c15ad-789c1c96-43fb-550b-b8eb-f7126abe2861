"""端到端行为测试：健康检查、转换写入、守恒/环路拒绝、召回传播与去重。"""


def _batch(client, bid, qty):
    r = client.post("/api/batches", json={"id": bid, "quantity": qty})
    assert r.status_code == 201, r.text
    return r.json()


def _xform(client, inputs, outputs, **extra):
    body = {"inputs": [{"batch_id": b, "quantity": q} for b, q in inputs],
            "outputs": [{"batch_id": b, "quantity": q} for b, q in outputs]}
    body.update(extra)
    return client.post("/api/transformations", json=body)


def _recall(client, bid, fraction=1.0):
    return client.post("/api/recall", json={
        "contaminated_batch_id": bid, "fraction": fraction})


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------


def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["database"].endswith("test.db")


# ---------------------------------------------------------------------------
# 批次登记
# ---------------------------------------------------------------------------


def test_create_and_get_batch_persists(client):
    _batch(client, "RM-1", 100)
    r = client.get("/api/batches/RM-1")
    assert r.status_code == 200
    assert r.json()["quantity"] == 100
    assert r.json()["produced_by_txn"] is None


def test_duplicate_batch_rejected(client):
    _batch(client, "RM-1", 100)
    r = client.post("/api/batches", json={"id": "RM-1", "quantity": 5})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"


def test_batch_validation_negative_quantity(client):
    r = client.post("/api/batches", json={"id": "RM-1", "quantity": 0})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


# ---------------------------------------------------------------------------
# 转换写入
# ---------------------------------------------------------------------------


def test_simple_split_transformation(client):
    _batch(client, "RM-1", 100)
    r = _xform(client, [("RM-1", 60)], [("P-A", 40), ("P-B", 20)], id="X1")
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["id"] == "X1"
    assert [i["batch_id"] for i in data["inputs"]] == ["RM-1"]
    assert [o["batch_id"] for o in data["outputs"]] == ["P-A", "P-B"]

    # 产出批次可查询，produced_by_txn 指向本次转换
    pa = client.get("/api/batches/P-A").json()
    assert pa["produced_by_txn"] == "X1"


def test_quantity_conservation_violation_rejected(client):
    _batch(client, "RM-1", 100)
    r = _xform(client, [("RM-1", 60)], [("P-A", 50), ("P-B", 20)])
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "unprocessable_entity"
    assert err["details"]["input_total"] == 60
    assert err["details"]["output_total"] == 70


def test_missing_input_batch_rejected(client):
    r = _xform(client, [("GHOST", 10)], [("P-A", 10)])
    assert r.status_code == 404
    assert "GHOST" in r.json()["error"]["message"]


def test_output_id_must_be_new(client):
    _batch(client, "RM-1", 100)
    r = _xform(client, [("RM-1", 50)], [("RM-1", 50)])
    # RM-1 既已存在又出现在输出 -> 冲突（同时也是自环场景）
    assert r.status_code in (409, 422)


def test_overconsumption_rejected_across_transformations(client):
    _batch(client, "RM-1", 100)
    assert _xform(client, [("RM-1", 60)], [("P-A", 60)]).status_code == 201
    # 只剩 40，再消耗 50 -> 超额
    r = _xform(client, [("RM-1", 50)], [("P-B", 50)])
    assert r.status_code == 422
    assert r.json()["error"]["details"]["batches"][0]["batch_id"] == "RM-1"
    # 被拒后数据未被污染：仍可合法消耗剩余 40
    assert _xform(client, [("RM-1", 40)], [("P-B", 40)]).status_code == 201


def test_self_loop_same_input_output_rejected(client):
    _batch(client, "A", 10)
    r = _xform(client, [("A", 10)], [("A", 10)])
    assert r.status_code in (409, 422)


def test_input_output_overlap_rejected(client):
    """同一批次同时作为输入和输出（自环企图）必须被拒绝。"""
    _batch(client, "A", 10)
    r = client.post("/api/transformations", json={
        "inputs": [{"batch_id": "A", "quantity": 10}],
        "outputs": [
            {"batch_id": "B", "quantity": 5},
            {"batch_id": "A", "quantity": 5},
        ],
    })
    assert r.status_code in (409, 422)


def test_duplicate_entries_rejected(client):
    _batch(client, "A", 10)
    r = client.post("/api/transformations", json={
        "inputs": [
            {"batch_id": "A", "quantity": 5},
            {"batch_id": "A", "quantity": 5},
        ],
        "outputs": [{"batch_id": "B", "quantity": 10}],
    })
    assert r.status_code == 422


def test_empty_inputs_outputs_rejected(client):
    r = client.post("/api/transformations", json={"inputs": [], "outputs": []})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 召回传播
# ---------------------------------------------------------------------------


def test_recall_linear_chain(client):
    _batch(client, "RM", 100)
    assert _xform(client, [("RM", 100)], [("S1", 100)]).status_code == 201
    assert _xform(client, [("S1", 100)], [("F1", 60), ("F2", 40)]).status_code == 201

    r = _recall(client, "RM")
    assert r.status_code == 200
    data = r.json()
    assert data["affected_count"] == 4
    q = {b["batch_id"]: b["recall_quantity"] for b in data["affected_batches"]}
    assert q == {"RM": 100, "S1": 100, "F1": 60, "F2": 40}
    assert data["total_recall_quantity"] == 300
    # 稳定排序：按跳数、再按批次 ID
    assert [b["batch_id"] for b in data["affected_batches"]] == [
        "RM", "S1", "F1", "F2"]
    dist = {b["batch_id"]: b["distance"] for b in data["affected_batches"]}
    assert dist == {"RM": 0, "S1": 1, "F1": 2, "F2": 2}


def test_recall_partial_consumption_uses_input_share(client):
    _batch(client, "RM", 100)
    # 只消耗 RM 的 25%
    assert _xform(client, [("RM", 25)], [("F", 25)]).status_code == 201
    data = _recall(client, "RM").json()
    q = {b["batch_id"]: b["recall_quantity"] for b in data["affected_batches"]}
    assert q["RM"] == 100  # 源批次本身整批召回
    assert q["F"] == 25   # 仅 25% 的污染质量流入下游


def test_recall_fraction(client):
    _batch(client, "RM", 100)
    assert _xform(client, [("RM", 100)], [("F", 100)]).status_code == 201
    data = _recall(client, "RM", fraction=0.1).json()
    q = {b["batch_id"]: b["recall_quantity"] for b in data["affected_batches"]}
    assert q["RM"] == 10
    assert q["F"] == 10
    assert data["total_recall_quantity"] == 20


def test_recall_diamond_merged_without_double_count(client):
    """菱形汇合：两条污染路径在最终批次合并，不得重复计数。

    RM(100) -> S1(50), S2(50) -> F(100)
    S1、S2 各带 50 污染质量，汇入同一转换后 F 恰好为 100（不是 200）。
    """
    _batch(client, "RM", 100)
    assert _xform(client, [("RM", 100)], [("S1", 50), ("S2", 50)]).status_code == 201
    assert _xform(client, [("S1", 50), ("S2", 50)], [("F", 100)]).status_code == 201

    data = _recall(client, "RM").json()
    q = {b["batch_id"]: b["recall_quantity"] for b in data["affected_batches"]}
    assert q == {"RM": 100, "S1": 50, "S2": 50, "F": 100}
    assert data["total_recall_quantity"] == 300


def test_recall_diamond_partial_ratios(client):
    """非全量、非对称菱形，验证按比例分摊与汇合不翻倍。

    RM(100) --50--> S1(50); --50--> S2(50)（另 50 留在 RM，属源头召回）
    S1 全量 50 与 S2 中 20 汇合 -> F1(40), F2(30)
    汇入污染 = 50*(50/50) + 50*(20/50) = 70；F1 得 40，F2 得 30。
    """
    _batch(client, "RM", 100)
    assert _xform(client, [("RM", 100)], [("S1", 50), ("S2", 50)]).status_code == 201
    assert _xform(client, [("S1", 50), ("S2", 20)], [("F1", 40), ("F2", 30)]).status_code == 201

    data = _recall(client, "RM").json()
    q = {b["batch_id"]: b["recall_quantity"] for b in data["affected_batches"]}
    assert q["RM"] == 100
    assert q["S1"] == 50
    assert q["S2"] == 50
    assert q["F1"] == 40
    assert q["F2"] == 30


def test_recall_unrelated_batch_not_affected(client):
    _batch(client, "RM-BAD", 10)
    _batch(client, "RM-OK", 10)
    assert _xform(client, [("RM-OK", 10)], [("OK-F", 10)]).status_code == 201
    data = _recall(client, "RM-BAD").json()
    assert [b["batch_id"] for b in data["affected_batches"]] == ["RM-BAD"]
    assert data["total_recall_quantity"] == 10


def test_recall_stable_results_repeated(client):
    _batch(client, "RM", 100)
    assert _xform(client, [("RM", 50)], [("B", 25), ("A", 25)]).status_code == 201
    first = _recall(client, "RM").json()
    second = _recall(client, "RM").json()
    assert first == second
    # 同跳数内按批次 ID 排序
    assert [b["batch_id"] for b in first["affected_batches"]] == ["RM", "A", "B"]


def test_recall_unknown_batch_404(client):
    r = _recall(client, "NOPE")
    assert r.status_code == 404


def test_persistence_across_connections(client, tmp_path):
    """通过新连接重新打开数据库，验证写入确实持久化。"""
    _batch(client, "RM", 10)
    assert _xform(client, [("RM", 10)], [("F", 10)]).status_code == 201

    import os
    from app import config
    from app.database import connect
    from app.service import recall

    conn = connect(os.path.join(tmp_path, "test.db"))
    try:
        data = recall(conn, "RM")
        assert {b["batch_id"] for b in data["affected_batches"]} == {"RM", "F"}
        assert config.DB_PATH.endswith("test.db")
    finally:
        conn.close()
