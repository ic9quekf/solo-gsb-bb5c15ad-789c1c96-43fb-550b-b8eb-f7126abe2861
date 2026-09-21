# 食品批次召回溯源 API

基于 **FastAPI + SQLite** 的食品批次转换登记与召回溯源服务：

- 登记**批次转换**：多个输入批次按数量产出多个新批次（多对多）；
- 写入时校验：**拒绝环路**（转换图必须保持 DAG）与**数量守恒违规**（输入总量必须等于输出总量）；
- **召回查询**：给定受污染批次，沿转换 DAG 向下游传播，计算受影响批次及**最小召回量**；多路径汇合（菱形结构）不重复计数，结果稳定可复现；
- 数据持久化在 SQLite，默认**无需任何第三方账号/服务**。

## 快速开始（Docker Compose，单命令拉起）

```bash
docker compose up --build
```

镜像构建时会安装 Python 依赖；容器启动时自动完成 SQLite 建库建表（幂等）。

- API 地址：<http://localhost:8000>
- 交互式文档（Swagger UI）：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/health>

停止并保留数据：`docker compose down`；同时清除数据：`docker compose down -v`。

## 配置

通过环境变量配置（均有默认值，可直接运行）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PORT` | `8000` | 宿主机映射端口（compose 使用，如 `PORT=9000 docker compose up`） |
| `DATABASE_PATH` | 容器内 `/data/recall.db`，本地运行 `data/recall.db` | SQLite 数据库文件路径 |

SQLite 数据通过名为 `recall-data` 的 volume 持久化（容器内 `/data`）。

## API 一览

所有业务接口前缀为 `/api/v1`。

### 健康检查

```
GET /health
```

响应：`{"status": "ok", "database": "ok"}`

### 登记批次转换

```
POST /api/v1/transformations
Content-Type: application/json

{
  "inputs":  [{"batch_id": "RAW-001", "quantity": 100}],
  "outputs": [{"batch_id": "WIP-001", "quantity": 60},
              {"batch_id": "WIP-002", "quantity": 40}]
}
```

- 成功：`201 Created`，返回带 `id`、`created_at` 的完整转换记录。
- 校验规则（写入时执行）：
  - 输入/输出均至少 1 条，`quantity > 0`，`batch_id` 非空（否则 `422`，FastAPI 请求校验）；
  - 同一侧不允许重复批次（`422`，`code=duplicate_batch`）；
  - **数量守恒**：输入总量必须等于输出总量（`422`，`code=quantity_imbalance`）；
  - **环路拒绝**：批次不得同时出现在同一转换的输入与输出中，且任一输出批次不得已是任一输入批次的上游（`409`，`code=cycle_detected`）。

### 查询已登记转换

```
GET /api/v1/transformations
```

按登记顺序返回全部转换记录。

### 召回查询

```
GET /api/v1/recalls/{batch_id}?quantity=100
```

- `quantity` 可选；省略时默认取该批次的**累计产出总量**。若该批次从未被任何转换产出（原料批次），则必须显式提供，否则返回 `422`（`code=quantity_required`）。
- 批次不存在于任何转换中时返回 `404`（`code=batch_not_found`）。

响应示例：

```json
{
  "batch_id": "RAW-001",
  "contaminated_quantity": 100.0,
  "affected_batches": [
    {"batch_id": "WIP-001", "recall_quantity": 60.0},
    {"batch_id": "WIP-002", "recall_quantity": 40.0}
  ],
  "total_recall_quantity": 100.0
}
```

`affected_batches` 为下游受影响批次（不含污染源本身），按 `batch_id` 排序，结果确定、可复现。

## 召回量计算模型（最小召回量）

- 转换构成以批次为节点、`输入批次 -> 输出批次` 为有向边的 **DAG**（写入时保证无环）。
- 污染量沿 DAG 向下游传播：转换按确定性拓扑序处理（Kahn 算法，同层按转换 id 升序），保证处理某转换时其所有输入批次的污染量已最终确定；每个转换从某输入批次带入的污染量最多为其实际消耗量（`min(消耗量, 该批次累计污染量)`），污染流入按各输出数量占比分摊到输出批次。
- 由于按“批次”而非“路径”累计，**多路径汇合（菱形结构）的批次只计一次**：汇合批次直接汇总其各输入批次的污染流入，不会因多条路径重复计数；某下游批次的召回量 = 实际流入该批次的污染量，即最小召回量（而非整批盲目召回）。
- 数值保留 6 位小数，输出排序固定，多次查询结果一致。

## 本地开发（不用 Docker）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload          # http://localhost:8000
pytest                                  # 运行测试
```

测试通过 `DATABASE_PATH` 指向临时数据库，不会影响本地数据。

## 项目结构

```
app/
  main.py       # FastAPI 应用与路由
  models.py     # Pydantic 请求/响应模型
  services.py   # 转换登记、环路检测、召回传播算法
  db.py         # SQLite 连接与建库（启动时幂等执行）
tests/
  test_api.py   # 端到端 API 测试
Dockerfile
docker-compose.yml
requirements.txt        # 运行时依赖（随镜像安装）
requirements-dev.txt    # 测试依赖
```
