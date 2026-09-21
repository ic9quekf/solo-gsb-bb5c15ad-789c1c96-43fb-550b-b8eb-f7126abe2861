"""FastAPI application: routes, error mapping, startup initialisation."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import db
from .models import (
    BatchQuantity,
    RecallReport,
    Transformation,
    TransformationCreate,
)
from .services import (
    DomainError,
    TransformationRecord,
    compute_recall,
    create_transformation,
    load_transformations,
)

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()  # create the SQLite file and schema on startup
    yield


app = FastAPI(
    title="Food Batch Recall Traceability API",
    version="1.0.0",
    description=(
        "Register batch transformations (many inputs -> many outputs with "
        "quantity conservation, cycle-free) and trace the minimum recall "
        "quantities caused by a contaminated batch."
    ),
    lifespan=lifespan,
)


@app.exception_handler(DomainError)
async def domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.code, "detail": exc.message},
    )


def _record_to_schema(record: TransformationRecord) -> Transformation:
    return Transformation(
        id=record.id,
        inputs=[
            BatchQuantity(batch_id=batch, quantity=qty)
            for batch, qty in record.inputs.items()
        ],
        outputs=[
            BatchQuantity(batch_id=batch, quantity=qty)
            for batch, qty in record.outputs.items()
        ],
        created_at=record.created_at,
    )


@app.get("/")
def root() -> dict:
    return {
        "service": "food-batch-recall-api",
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/health",
        "api": API_PREFIX,
    }


@app.get("/health")
def health() -> dict:
    with db.connect() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok", "database": "ok"}


@app.post(
    f"{API_PREFIX}/transformations",
    response_model=Transformation,
    status_code=201,
    summary="Register a batch transformation",
)
def post_transformation(payload: TransformationCreate) -> Transformation:
    with db.write_transaction() as conn:
        record = create_transformation(conn, payload)
    return _record_to_schema(record)


@app.get(
    f"{API_PREFIX}/transformations",
    response_model=list[Transformation],
    summary="List registered transformations",
)
def get_transformations() -> list[Transformation]:
    with db.connect() as conn:
        records = load_transformations(conn)
    return [_record_to_schema(record) for record in records]


@app.get(
    f"{API_PREFIX}/recalls/{{batch_id}}",
    response_model=RecallReport,
    summary="Compute affected batches and minimum recall quantities",
)
def get_recall(
    batch_id: str,
    quantity: float | None = Query(
        default=None,
        gt=0,
        description=(
            "Contaminated quantity of the batch. Defaults to the total "
            "produced quantity of the batch."
        ),
    ),
) -> dict:
    with db.connect() as conn:
        return compute_recall(conn, batch_id, quantity)
