"""核心业务逻辑：批次登记、转换写入校验、召回溯源传播。

模型约定
--------
* 每个批次在 ``batches`` 中只有一条记录，数量为该批次的初始/当前总数量。
* 一次转换（transformation）消耗若干既有批次（inputs），产出若干新批次
  （outputs）。outputs 的批次 ID 必须是全新的——因此批次-转换构成有向无环图：
  边为 input 批次 → transformation → output 批次。
* 写入时强制：
  1) 输入批次存在、且历史累计消耗量 + 本次消耗量不超过批次数量
     （防止同一批被超额领用，这是跨转换层面的数量守恒）；
  2) 本次转换的输入数量之和与产出数量之和相等（转换内部数量守恒）；
  3) 新边并入已有图后无环路（深度防御：DAG 不变式）。
* 召回传播采用质量分摊：
  受污染质量先按各输入在该转换中的领用比例汇入转换，
  再按各产出数量占比分摊到产出批次；每个批次只计算一次（memoize），
  因而菱形路径在汇合处天然合并、绝不重复计数。
"""

import sqlite3
import uuid
from collections import deque
from typing import Dict, List, Optional, Tuple

from . import config
from .errors import ServiceError

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _near_equal(a: float, b: float) -> bool:
    """相对误差不超过 QUANTITY_EPS 的数量相等判断。"""
    return abs(a - b) <= config.QUANTITY_EPS * max(1.0, abs(a), abs(b))


def _q(value: float) -> float:
    """稳定的输出数量：保留固定小数位，消除浮点尾差导致的结果抖动。"""
    return round(float(value), config.QUANTITY_ROUND)


def _not_found(message: str) -> ServiceError:
    return ServiceError(404, "not_found", message)


def _conflict(message: str) -> ServiceError:
    return ServiceError(409, "conflict", message)


def _unprocessable(message: str, details=None) -> ServiceError:
    return ServiceError(422, "unprocessable_entity", message, details)


# ---------------------------------------------------------------------------
# 批次
# ---------------------------------------------------------------------------


def _get_batch_row(conn: sqlite3.Connection, batch_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT id, quantity, produced_by_txn, created_at FROM batches WHERE id = ?",
        (batch_id,),
    ).fetchone()


def _require_batch(conn: sqlite3.Connection, batch_id: str) -> sqlite3.Row:
    row = _get_batch_row(conn, batch_id)
    if row is None:
        raise _not_found(f"批次不存在: {batch_id}")
    return row


def create_batch(conn: sqlite3.Connection, batch_id: str, quantity: float) -> dict:
    """登记一个原始（未由任何转换产出的）批次。"""
    try:
        conn.execute(
            "INSERT INTO batches (id, quantity, produced_by_txn) VALUES (?, ?, NULL)",
            (batch_id, float(quantity)),
        )
    except sqlite3.IntegrityError as exc:
        raise _conflict(f"批次 ID 已存在: {batch_id}") from exc
    return row_to_batch(_require_batch(conn, batch_id))


def get_batch(conn: sqlite3.Connection, batch_id: str) -> dict:
    return row_to_batch(_require_batch(conn, batch_id))


def list_batches(conn: sqlite3.Connection) -> List[dict]:
    rows = conn.execute(
        "SELECT id, quantity, produced_by_txn, created_at FROM batches ORDER BY id"
    ).fetchall()
    return [row_to_batch(r) for r in rows]


def row_to_batch(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "quantity": row["quantity"],
        "produced_by_txn": row["produced_by_txn"],
        "created_at": row["created_at"],
    }


# ---------------------------------------------------------------------------
# 转换
# ---------------------------------------------------------------------------


def create_transformation(
    conn: sqlite3.Connection,
    inputs: List[Tuple[str, float]],
    outputs: List[Tuple[str, float]],
    txn_id: Optional[str] = None,
) -> dict:
    """原子地校验并写入一次批次转换。"""
    input_ids = [bid for bid, _ in inputs]
    output_ids = [bid for bid, _ in outputs]
    in_total = sum(q for _, q in inputs)
    out_total = sum(q for _, q in outputs)

    # 同请求内的结构校验 --------------------------------------------------
    if len(set(input_ids)) != len(input_ids):
        raise _unprocessable("inputs 中存在重复批次")
    if len(set(output_ids)) != len(output_ids):
        raise _unprocessable("outputs 中存在重复批次")
    overlap = set(input_ids) & set(output_ids)
    if overlap:
        raise _unprocessable(
            "同一批次不能同时作为一次转换的输入和输出（将形成自环）",
            {"batch_ids": sorted(overlap)},
        )

    # 1) 转换内部数量守恒
    if not _near_equal(in_total, out_total):
        raise _unprocessable(
            "数量守恒违规：输入总数量必须等于产出总数量",
            {
                "input_total": _q(in_total),
                "output_total": _q(out_total),
                "difference": _q(in_total - out_total),
            },
        )

    txn_id = txn_id or uuid.uuid4().hex

    from .database import transaction

    with transaction(conn):
        # 2) 输入批次必须存在
        placeholders = ",".join("?" for _ in input_ids)
        rows = conn.execute(
            f"SELECT id, quantity FROM batches WHERE id IN ({placeholders})",
            input_ids,
        ).fetchall()
        existing = {r["id"]: r["quantity"] for r in rows}
        missing = sorted(set(input_ids) - existing.keys())
        if missing:
            raise _not_found(f"输入批次不存在: {', '.join(missing)}")

        # 3) 产出批次必须是全新 ID
        out_placeholders = ",".join("?" for _ in output_ids)
        dup_out = [
            r["id"]
            for r in conn.execute(
                f"SELECT id FROM batches WHERE id IN ({out_placeholders})",
                output_ids,
            ).fetchall()
        ]
        if dup_out:
            raise _conflict(
                f"产出批次 ID 已存在: {', '.join(sorted(dup_out))}；"
                "转换只能产出新批次"
            )

        # 4) 跨转换数量守恒：任一输入批次的累计消耗量不得超过其数量
        used_rows = conn.execute(
            f"""
            SELECT batch_id, COALESCE(SUM(quantity), 0) AS used
            FROM transformation_items
            WHERE role = 'input' AND batch_id IN ({placeholders})
            GROUP BY batch_id
            """,
            input_ids,
        ).fetchall()
        used: Dict[str, float] = {r["batch_id"]: r["used"] for r in used_rows}
        over = []
        for bid, qty in inputs:
            projected = used.get(bid, 0.0) + qty
            # 允许落在容差内的微小尾差，超出（即使只是一点点）也拒绝
            if projected > existing[bid] and not _near_equal(projected, existing[bid]):
                over.append(
                    {
                        "batch_id": bid,
                        "batch_quantity": _q(existing[bid]),
                        "already_consumed": _q(used.get(bid, 0.0)),
                        "requested": _q(qty),
                    }
                )
        if over:
            raise _unprocessable(
                "数量守恒违规：输入批次累计消耗量超过其批次数量",
                {"batches": over},
            )

        # 5) DAG 校验：把本次新边并入已有边后必须仍无环
        _ensure_acyclic(conn, inputs, outputs)

        # 6) 写入转换
        try:
            conn.execute("INSERT INTO transformations (id) VALUES (?)", (txn_id,))
        except sqlite3.IntegrityError as exc:
            raise _conflict(f"转换 ID 已存在: {txn_id}") from exc

        # 7) 写入产出批次（produced_by_txn 指向本次转换）
        conn.executemany(
            "INSERT INTO batches (id, quantity, produced_by_txn) VALUES (?, ?, ?)",
            [(bid, float(qty), txn_id) for bid, qty in outputs],
        )

        # 8) 写入转换明细
        conn.executemany(
            "INSERT INTO transformation_items (txn_id, batch_id, role, quantity, ord)"
            " VALUES (?, ?, 'input', ?, ?)",
            [
                (txn_id, bid, float(qty), idx)
                for idx, (bid, qty) in enumerate(inputs)
            ],
        )
        conn.executemany(
            "INSERT INTO transformation_items (txn_id, batch_id, role, quantity, ord)"
            " VALUES (?, ?, 'output', ?, ?)",
            [
                (txn_id, bid, float(qty), idx)
                for idx, (bid, qty) in enumerate(outputs)
            ],
        )

    return get_transformation(conn, txn_id)


def _ensure_acyclic(
    conn: sqlite3.Connection,
    inputs: List[Tuple[str, float]],
    outputs: List[Tuple[str, float]],
) -> None:
    """在“已有批次 DAG + 本次新边”上做 Kahn 拓扑排序，存在环路则拒绝。"""
    # 已有边：转换输入批次 -> 该转换的产出批次
    edges = conn.execute(
        """
        SELECT i.batch_id AS src, o.batch_id AS dst
        FROM transformation_items i
        JOIN transformation_items o
          ON i.txn_id = o.txn_id AND o.role = 'output'
        WHERE i.role = 'input'
        """
    ).fetchall()

    adj: Dict[str, set] = {}
    indeg: Dict[str, int] = {}
    nodes = {bid for bid, _ in inputs} | {bid for bid, _ in outputs}

    def add_node(n: str) -> None:
        indeg.setdefault(n, 0)
        adj.setdefault(n, set())

    for n in nodes:
        add_node(n)
    for e in edges:
        s, d = e["src"], e["dst"]
        add_node(s)
        add_node(d)
        if d not in adj[s]:
            adj[s].add(d)
            indeg[d] += 1
    # 本次新增边：每个 input -> 每个 output
    for src, _ in inputs:
        for dst, _ in outputs:
            if dst not in adj[src]:
                adj[src].add(dst)
                indeg[dst] += 1

    queue = deque(sorted(n for n, deg in indeg.items() if deg == 0))
    visited = 0
    while queue:
        n = queue.popleft()
        visited += 1
        for d in sorted(adj[n]):
            indeg[d] -= 1
            if indeg[d] == 0:
                queue.append(d)

    if visited != len(indeg):
        cyclic = sorted(n for n, deg in indeg.items() if deg > 0)
        raise _unprocessable(
            "环路拒绝：本次转换会在批次转换图中形成环路",
            {"nodes_in_cycle": cyclic},
        )


def get_transformation(conn: sqlite3.Connection, txn_id: str) -> dict:
    trow = conn.execute(
        "SELECT id, created_at FROM transformations WHERE id = ?", (txn_id,)
    ).fetchone()
    if trow is None:
        raise _not_found(f"转换不存在: {txn_id}")

    items = conn.execute(
        "SELECT batch_id, role, quantity FROM transformation_items"
        " WHERE txn_id = ? ORDER BY role, ord, batch_id",
        (txn_id,),
    ).fetchall()
    return {
        "id": trow["id"],
        "created_at": trow["created_at"],
        "inputs": [
            {"batch_id": r["batch_id"], "quantity": r["quantity"]}
            for r in items
            if r["role"] == "input"
        ],
        "outputs": [
            {"batch_id": r["batch_id"], "quantity": r["quantity"]}
            for r in items
            if r["role"] == "output"
        ],
    }


# ---------------------------------------------------------------------------
# 召回溯源
# ---------------------------------------------------------------------------


def recall(
    conn: sqlite3.Connection, contaminated_batch_id: str, fraction: float = 1.0
) -> dict:
    """沿转换 DAG 前向传播污染，返回受影响批次及最小召回量。"""
    batch_row = _get_batch_row(conn, contaminated_batch_id)
    if batch_row is None:
        raise _not_found(f"受污染批次不存在: {contaminated_batch_id}")

    quantities: Dict[str, float] = {
        r["id"]: r["quantity"]
        for r in conn.execute("SELECT id, quantity FROM batches").fetchall()
    }

    # 载入全部转换，构造：
    #   producer[batch] = 产出它的转换；consumes[batch] = 消耗它的转换
    txn_ids = [
        r["id"] for r in conn.execute("SELECT id FROM transformations ORDER BY id").fetchall()
    ]
    txn_inputs: Dict[str, List[Tuple[str, float]]] = {}
    txn_outputs: Dict[str, List[Tuple[str, float]]] = {}
    producer: Dict[str, str] = {}
    consumes: Dict[str, List[str]] = {}
    for tid in txn_ids:
        rows = conn.execute(
            "SELECT batch_id, role, quantity FROM transformation_items"
            " WHERE txn_id = ? ORDER BY role, ord, batch_id",
            (tid,),
        ).fetchall()
        ins = [(r["batch_id"], r["quantity"]) for r in rows if r["role"] == "input"]
        outs = [(r["batch_id"], r["quantity"]) for r in rows if r["role"] == "output"]
        txn_inputs[tid] = ins
        txn_outputs[tid] = outs
        for bid, _ in outs:
            producer[bid] = tid
        for bid, _ in ins:
            consumes.setdefault(bid, []).append(tid)

    source = contaminated_batch_id
    source_mass = quantities[source] * fraction

    # 每个批次的“受污染数量”记忆化计算：汇合点只算一次 → 多路径不重复计数。
    memo: Dict[str, float] = {}
    in_progress: set = set()

    def contaminated(bid: str) -> float:
        if bid in memo:
            return memo[bid]
        if bid in in_progress:  # 理论不可达（写入时已拒绝环路），防御性处理
            raise ServiceError(500, "dag_cycle", "检测到意外的转换环路")
        if bid == source:
            memo[bid] = source_mass
            return source_mass
        tid = producer.get(bid)
        if tid is None:
            memo[bid] = 0.0
            return 0.0

        in_progress.add(bid)
        total_out = sum(q for _, q in txn_outputs[tid])
        incoming = 0.0
        for in_bid, consumed_qty in txn_inputs[tid]:
            in_total = quantities[in_bid]
            incoming += (
                contaminated(in_bid) * (consumed_qty / in_total) if in_total > 0 else 0.0
            )
        in_progress.discard(bid)

        own_qty = dict(txn_outputs[tid])[bid]
        result = incoming * (own_qty / total_out) if total_out > 0 else 0.0
        memo[bid] = result
        return result

    # 前向可达集（含受污染批次本身），同时记录转换跳数
    distance: Dict[str, int] = {source: 0}
    queue = deque([source])
    while queue:
        bid = queue.popleft()
        for tid in consumes.get(bid, []):
            for out_bid, _ in txn_outputs[tid]:
                if out_bid not in distance:
                    distance[out_bid] = distance[bid] + 1
                    queue.append(out_bid)

    affected = []
    for bid in sorted(distance, key=lambda b: (distance[b], b)):
        amount = _q(contaminated(bid))
        if amount > 0:  # 四舍五入后仍为 0 的极微量批次不计入召回
            affected.append(
                {
                    "batch_id": bid,
                    "distance": distance[bid],
                    "recall_quantity": amount,
                    "total_quantity": _q(quantities[bid]),
                }
            )

    return {
        "contaminated_batch_id": source,
        "fraction": fraction,
        "affected_count": len(affected),
        "total_recall_quantity": _q(sum(a["recall_quantity"] for a in affected)),
        "affected_batches": affected,
    }
