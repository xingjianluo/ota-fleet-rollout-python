"""冒烟测试：验证骨架可导入、应用可装配。"""
from fastapi.testclient import TestClient

from main import app
from ota.app import AppContext


def test_smoke(tmp_path):
    ctx = AppContext(str(tmp_path))
    assert ctx.store is not None
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
