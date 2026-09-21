#!/bin/sh
# 容器入口：幂等建库后启动服务。
set -e

echo "Initializing SQLite database at ${TRACE_DB_PATH} ..."
python -m app.init_db

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
