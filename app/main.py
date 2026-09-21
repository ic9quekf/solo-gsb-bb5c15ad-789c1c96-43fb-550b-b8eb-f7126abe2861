"""FastAPI 入口：转换写入、召回查询、健康检查。"""

import sqlite3
from contextlib import asynccontextmanager
from typing import AsyncIterator, List

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import config, database, schemas, service
from .errors import ServiceError


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # 容器数据卷首次为空时在此自动建库；已存在则为幂等空操作。
    database.init_db()
    yield


app = FastAPI(
    title="食品批次召回溯源 API",
    description=(
        "登记批次转换（多个输入批次按数量产出多个新批次），"
        "写入时拒绝环路及数量守恒违规；"
        "给定受污染批次，沿转换 DAG 传播受影响批次与最小召回量。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# 统一错误响应
# ---------------------------------------------------------------------------


@app.exception_handler(ServiceError)
async def service_error_handler(_request: Request, exc: ServiceError) -> JSONResponse:
    body = {"error": {"code": exc.code, "message": exc.message}}
    if exc.details is not None:
        body["error"]["details"] = exc.details
    return JSONResponse(status_code=exc.status_code, content=body)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "message": "请求参数校验失败",
                "details": exc.errors(),
            }
        },
    )


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------


@app.get("/health", response_model=schemas.HealthResponse, tags=["health"])
def health(conn: sqlite3.Connection = Depends(database.get_conn)) -> schemas.HealthResponse:
    """存活/就绪探针：顺带执行一次 SQLite 查询确认数据库可用。"""
    conn.execute("SELECT 1").fetchone()
    return schemas.HealthResponse(status="ok", database=config.DB_PATH)


# ---------------------------------------------------------------------------
# 批次
# ---------------------------------------------------------------------------


@app.post("/api/batches", response_model=schemas.BatchResponse, status_code=201, tags=["batches"])
def create_batch(
    payload: schemas.CreateBatchRequest,
    conn: sqlite3.Connection = Depends(database.get_conn),
) -> schemas.BatchResponse:
    """登记原始批次（原料入库）。"""
    data = service.create_batch(conn, payload.id, payload.quantity)
    return schemas.BatchResponse(**data)


@app.get("/api/batches", response_model=List[schemas.BatchResponse], tags=["batches"])
def list_batches(conn: sqlite3.Connection = Depends(database.get_conn)):
    return service.list_batches(conn)


@app.get("/api/batches/{batch_id}", response_model=schemas.BatchResponse, tags=["batches"])
def get_batch(batch_id: str, conn: sqlite3.Connection = Depends(database.get_conn)):
    return service.get_batch(conn, batch_id)


# ---------------------------------------------------------------------------
# 转换
# ---------------------------------------------------------------------------


@app.post(
    "/api/transformations",
    response_model=schemas.TransformationResponse,
    status_code=201,
    tags=["transformations"],
)
def create_transformation(
    payload: schemas.CreateTransformationRequest,
    conn: sqlite3.Connection = Depends(database.get_conn),
) -> schemas.TransformationResponse:
    """登记一次批次转换：多个输入批次按数量产出多个新批次。

    写入时校验：输入存在、产出 ID 全新、数量守恒、累计消耗不超额、无环路。
    """
    inputs = [(i.batch_id, i.quantity) for i in payload.inputs]
    outputs = [(o.batch_id, o.quantity) for o in payload.outputs]
    data = service.create_transformation(conn, inputs, outputs, payload.id)
    return schemas.TransformationResponse(**data)


@app.get(
    "/api/transformations/{txn_id}",
    response_model=schemas.TransformationResponse,
    tags=["transformations"],
)
def get_transformation(txn_id: str, conn: sqlite3.Connection = Depends(database.get_conn)):
    return service.get_transformation(conn, txn_id)


# ---------------------------------------------------------------------------
# 召回查询
# ---------------------------------------------------------------------------


@app.post("/api/recall", response_model=schemas.RecallResponse, tags=["recall"])
def recall(
    payload: schemas.RecallRequest,
    conn: sqlite3.Connection = Depends(database.get_conn),
) -> schemas.RecallResponse:
    """给定受污染批次，前向传播受影响批次与最小召回量。"""
    data = service.recall(conn, payload.contaminated_batch_id, payload.fraction)
    return schemas.RecallResponse(**data)
