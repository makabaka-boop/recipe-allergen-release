import os
import sys
import tempfile
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="allergen-test-")
_DB = Path(_TMP) / "test.db"
os.environ["ALLERGEN_DB"] = str(_DB)
os.environ["ALLERGEN_TODAY"] = "2026-10-02"  # 固定"今天"，过期批次可复现

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import seed  # noqa: E402
from app.db import new_conn  # noqa: E402


@pytest.fixture
def fresh_db():
    """每个测试一个全新的种子库。"""
    seed.seed(reset=True)
    conn = new_conn()
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def client(fresh_db):
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def ids_by_code(conn, *codes, table="materials", code_col="code"):
    out = {}
    for code in codes:
        out[code] = conn.execute(
            f"SELECT id FROM {table} WHERE {code_col}=?", (code,)
        ).fetchone()["id"]
    return out
