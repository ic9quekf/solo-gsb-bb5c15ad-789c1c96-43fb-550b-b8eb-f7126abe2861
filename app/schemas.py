"""Pydantic 请求/响应模型。"""

from typing import List, Optional

from pydantic import BaseModel, Field, constr

# 批次/转换 ID：非空字符串，首尾不允许空白，长度 1..128。
BatchId = constr(strip_whitespace=False, min_length=1, max_length=128)
TxnId = constr(strip_whitespace=False, min_length=1, max_length=128)


class CreateBatchRequest(BaseModel):
    id: BatchId
    quantity: float = Field(gt=0, description="批次初始数量（重量/件数等统一单位），必须 > 0")


class BatchResponse(BaseModel):
    id: str
    quantity: float
    produced_by_txn: Optional[str] = None
    created_at: str


class FlowItem(BaseModel):
    batch_id: BatchId
    quantity: float = Field(gt=0, description="该批次在本次转换中的数量，必须 > 0")


class CreateTransformationRequest(BaseModel):
    id: Optional[TxnId] = Field(default=None, description="可选转换 ID，缺省自动生成 UUID")
    inputs: List[FlowItem] = Field(min_length=1)
    outputs: List[FlowItem] = Field(min_length=1)


class FlowItemOut(BaseModel):
    batch_id: str
    quantity: float


class TransformationResponse(BaseModel):
    id: str
    inputs: List[FlowItemOut]
    outputs: List[FlowItemOut]
    created_at: str


class RecallRequest(BaseModel):
    contaminated_batch_id: BatchId
    # 受污染数量占该批次现有数量的比例，(0, 1]；缺省 1 表示整批受污染。
    fraction: float = Field(default=1.0, gt=0, le=1.0)


class AffectedBatch(BaseModel):
    batch_id: str
    distance: int = Field(description="距受污染批次的转换跳数，0 即受污染批次本身")
    recall_quantity: float = Field(description="最小召回数量")
    total_quantity: float = Field(description="批次当前总数量")


class RecallResponse(BaseModel):
    contaminated_batch_id: str
    fraction: float
    affected_count: int
    total_recall_quantity: float
    affected_batches: List[AffectedBatch]


class HealthResponse(BaseModel):
    status: str
    database: str
