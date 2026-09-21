"""pytest 共享夹具：每个测试使用独立的临时 SQLite 数据库。"""

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("TRACE_DB_PATH", str(db_file))

    # config 在导入时读取环境变量：清掉已缓存的 app.* 模块后重新导入，
    # 使整套应用指向本测试的临时数据库。
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]

    from app.database import init_db
    from app.main import app

    init_db(str(db_file))
    with TestClient(app) as c:
        yield c
