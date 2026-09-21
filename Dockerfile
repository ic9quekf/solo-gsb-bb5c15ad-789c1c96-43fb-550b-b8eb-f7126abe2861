FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TRACE_DB_PATH=/data/trace.db \
    PORT=8000

WORKDIR /app

# 先装依赖，充分利用镜像层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY app ./app
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

# 随镜像完成一次 SQLite 建库（/app/data/trace.db，已存在则幂等无操作）。
# 运行时若使用 /data 命名卷，entrypoint 会在启动时再次幂等建库。
RUN python -m app.init_db

# 容器内默认数据库放在挂载卷，保证数据持久
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD python -c "import os,sqlite3,sys; \
        c=sqlite3.connect(os.environ.get('TRACE_DB_PATH','/data/trace.db')); \
        c.execute('SELECT 1'); c.close()" || exit 1

ENTRYPOINT ["./entrypoint.sh"]
