"""Pydantic request/response schemas."""
from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

BatchId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]


class BatchQuantity(BaseModel):
    batch_id: BatchId = Field(examples=["RAW-001"])
    quantity: float = Field(gt=0, examples=[100.0])


class TransformationCreate(BaseModel):
    """A transformation consumes input batches and produces output batches."""

    inputs: list[BatchQuantity] = Field(min_length=1)
    outputs: list[BatchQuantity] = Field(min_length=1)


class Transformation(BaseModel):
    id: int
    inputs: list[BatchQuantity]
    outputs: list[BatchQuantity]
    created_at: str


class RecallEntry(BaseModel):
    batch_id: str
    recall_quantity: float


class RecallReport(BaseModel):
    batch_id: str
    contaminated_quantity: float
    affected_batches: list[RecallEntry]
    total_recall_quantity: float
