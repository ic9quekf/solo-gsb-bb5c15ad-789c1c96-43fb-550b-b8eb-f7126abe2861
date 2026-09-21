"""运行配置。

所有配置均可通过环境变量覆盖，默认值无需任何第三方账号即可运行。
"""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# SQLite 数据库文件路径；容器内默认 /data/trace.db（由 docker-compose 挂载命名卷）。
DB_PATH = os.getenv("TRACE_DB_PATH", str(PROJECT_ROOT / "data" / "trace.db"))

# 数量守恒比较的相对容差与结果保留小数位。
QUANTITY_EPS = 1e-9
QUANTITY_ROUND = 9
