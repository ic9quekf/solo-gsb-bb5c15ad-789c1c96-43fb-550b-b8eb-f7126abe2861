# 食品批次召回溯源 API

基于 **Python + FastAPI + SQLite** 的食品批次转换登记与污染召回溯源服务。

- 登记批次转换：**多个输入批次按数量产出多个新批次**（拆分 / 合并均可）。
- 写入时强制校验：**拒绝环路**（转换图必须为 DAG）、**拒绝数量守恒违规**
  （转换内投入总量必须等于产出总量；任一输入批次的历史累计消耗量不得超过其批次数量）。
- 召回查询：给定一个受污染批次，沿转换 DAG **前向传播**，返回所有受影响批次及其
  **最小召回量**；多条路径在汇合处**合并计算、不重复计数**，结果顺序稳定。
- SQLite 单文件持久化，**无需任何第三方账号 / 外部数据库**，`docker compose up` 一条命令拉起。

---

## 目录结构

```
.
├── app/
│   ├── main.py          # FastAPI 路由与统一错误处理
│   ├── service.py       # 核心逻辑：写入校验、DAG 环路检测、召回传播
│   ├── database.py      # SQLite 连接、建表脚本、事务
│   ├── schemas.py       # Pydantic 请求/响应模型（请求校验）
│   ├── config.py        # 环境变量配置
│   ├── errors.py        # 业务错误类型
│   └── init_db.py       # 命令行建库：python -m app.init_db
├── tests/               # pytest 端到端测试
├── Dockerfile           # 安装依赖并随镜像完成 SQLite 建库
├── docker-compose.yml   # 单命令拉起（含数据卷持久化）
├── entrypoint.sh        # 容器入口：幂等建库后启动 uvicorn
└── requirements.txt
```

## 一键启动（推荐）

前置条件：已安装 Docker 与 Docker Compose v2。

```bash
docker compose up --build
```

启动后：

| 项目 | 地址 |
| --- | --- |
| API 服务地址 | <http://localhost:8000> |
| 健康检查 | <http://localhost:8000/health> |
| Swagger 交互文档 | <http://localhost:8000/docs> |
| ReDoc 文档 | <http://localhost:8000/redoc> |
| OpenAPI Schema | <http://localhost:8000/openapi.json> |

数据持久化在 Docker 命名卷 `trace-data`（容器内 `/data/trace.db`）。
停止服务：`docker compose down`（加 `-v` 会同时删除数据卷）。

### 配置项

均为环境变量，均有默认值，**默认配置不需要任何账号**：

| 环境变量 | 默认值（本地） | 默认值（容器） | 说明 |
| --- | --- | --- | --- |
| `TRACE_DB_PATH` | `./data/trace.db` | `/data/trace.db` | SQLite 数据库文件路径，目录自动创建 |
| `PORT` | `8000`（uvicorn 默认） | `8000` | 容器内监听端口 |
| `HOST_PORT` | — | `8000` | 宿主机映射端口，仅 compose 使用；如 `HOST_PORT=9000 docker compose up` |

## 本地开发运行（不用 Docker）

```bash
pip install -r requirements.txt
python -m app.init_db                    # 幂等建库
uvicorn app.main:app --reload --port 8000
```

运行测试：

```bash
pip install pytest httpx
python -m pytest -q
```

---

## 数据模型与规则

### 批次（Batch）

每个批次有唯一 `id` 和正数 `quantity`（统一计量单位，如 kg）。
批次分两种来源：

1. **原始批次**：`POST /api/batches` 登记入库，`produced_by_txn = null`；
2. **产出批次**：在某次转换的 `outputs` 中首次出现，系统自动创建，
   `produced_by_txn` 指向该转换。

### 转换（Transformation）

边的方向为 **输入批次 → 转换 → 产出批次**。约束：

1. `inputs` / `outputs` 至少各 1 项，每项数量必须为正数；
2. 同一次转换内输入、输出批次均不可重复，且输入与输出不可有交集（禁止自环）；
3. **所有输入批次必须已存在；产出批次 ID 必须是全新的**
   —— 批次只能被一次转换“创造”，保证流向天然无环；
4. **数量守恒（转换内）**：`sum(input 数量) == sum(output 数量)`，
   允许 `1e-9` 相对浮点容差；
5. **数量守恒（跨转换）**：一个批次被多次转换消耗时，
   累计消耗数量不得超过其批次数量（即不允许把同一批货重复领用）；
6. **DAG 不变式**：服务端把本次新边并入已有批次图后做 Kahn 拓扑排序，
   一旦形成环路立即拒绝（深度防御）。

以上校验与写入在单个 `BEGIN IMMEDIATE` 事务内原子完成，校验失败不会留下脏数据。

### 召回传播语义

`POST /api/recall` 的请求体：

```json
{ "contaminated_batch_id": "RM", "fraction": 1.0 }
```

- `fraction` 取值 `(0, 1]`，表示该批次现有数量中受污染的比例，默认 `1.0`（整批污染）。
- 从受污染批次出发沿 DAG **前向**传播（不追溯其上游兄弟批次）。
- **质量分摊模型**：
  - 对某次转换，先把各输入批次携带的受污染质量按“本次消耗量 ÷ 输入批次总量”汇入转换；
  - 再按各产出批次的数量占比分摊到产出。
  - 当某批次只有一部分被转换消耗时，只有相应比例的污染流向下游，其余部分仍在该批次召回量内。
- **不重复计数**：每个批次的受污染数量采用记忆化计算，菱形拓扑的两条路径在汇合转换处
  先合并污染总量再分摊，最终批次绝不会被翻倍。
- 返回结果按 `(转换跳数 distance, 批次 ID)` **稳定排序**；数量保留 9 位小数。

最小召回量示例（100 原料 → S1/S2 各 50 → 合并为 F 100，RM 整批污染）：

| 批次 | distance | 最小召回量 |
| --- | --- | --- |
| RM | 0 | 100 |
| S1 | 1 | 50 |
| S2 | 1 | 50 |
| F | 2 | 100（两条路径合并，而非 200） |

---

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查（执行 SQLite `SELECT 1`） |
| `POST` | `/api/batches` | 登记原始批次 |
| `GET` | `/api/batches` | 列出全部批次（按 ID 排序） |
| `GET` | `/api/batches/{batch_id}` | 查询单个批次 |
| `POST` | `/api/transformations` | 登记一次批次转换 |
| `GET` | `/api/transformations/{id}` | 查询一次转换 |
| `POST` | `/api/recall` | 召回查询 |

所有错误体使用统一结构：

```json
{ "error": { "code": "unprocessable_entity", "message": "...", "details": { } } }
```

| HTTP 状态码 | code | 触发场景 |
| --- | --- | --- |
| 422 | `validation_error` | 请求体字段缺失 / 数量 ≤ 0 / 列表为空等 |
| 404 | `not_found` | 输入批次或受污染批次不存在 |
| 409 | `conflict` | 批次 / 产出 ID 重复 |
| 422 | `unprocessable_entity` | 数量守恒违规、累计超额消耗、自环 / 环路、重复条目 |

## 使用示例（curl）

```bash
# 1) 入库两个原料批次
curl -X POST localhost:8000/api/batches -H 'Content-Type: application/json' \
  -d '{"id":"RM-001","quantity":100}'
curl -X POST localhost:8000/api/batches -H 'Content-Type: application/json' \
  -d '{"id":"RM-002","quantity":20}'

# 2) 转换：RM-001 消耗 60，拆成两个新批次（总量必须相等）
curl -X POST localhost:8000/api/transformations -H 'Content-Type: application/json' -d '{
  "id": "T-20260921-01",
  "inputs":  [{"batch_id":"RM-001","quantity":60}],
  "outputs": [{"batch_id":"PK-A","quantity":40},{"batch_id":"PK-B","quantity":20}]
}'
# 守恒违规会被拒绝（60 != 70）：
curl -i -X POST localhost:8000/api/transformations -H 'Content-Type: application/json' -d '{
  "inputs":  [{"batch_id":"RM-001","quantity":60}],
  "outputs": [{"batch_id":"PK-C","quantity":50},{"batch_id":"PK-D","quantity":20}]
}'

# 3) 查询受污染批次的召回清单
curl -X POST localhost:8000/api/recall -H 'Content-Type: application/json' \
  -d '{"contaminated_batch_id":"RM-001"}'

# 仅 10% 受污染
curl -X POST localhost:8000/api/recall -H 'Content-Type: application/json' \
  -d '{"contaminated_batch_id":"RM-001","fraction":0.1}'

# 4) 健康检查
curl localhost:8000/health
```
