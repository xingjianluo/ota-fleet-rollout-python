"""HTTP 端到端：错误码/HTTP 状态归类与完整发布-失败-回滚链路。"""
import pytest
from fastapi.testclient import TestClient

import api.routes as routes
from main import app
from ota.app import AppContext
from ota.clock import reset, set_source


class FakeClock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture()
def client(tmp_path):
    clock = FakeClock()
    set_source(clock)
    ctx = AppContext(str(tmp_path), heartbeat_ttl=10.0,
                     stage_timeout=30.0, max_retries=1)
    routes.set_ctx(ctx)
    c = TestClient(app)
    yield c, ctx, clock
    routes.set_ctx(None)
    reset()


def register(c, did, fw="1.0.0"):
    r = c.post("/devices", json={"device_id": did, "model": "A1",
                                 "hw_version": "h1", "fw_version": fw})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def make_firmwares(c):
    r = c.post("/firmwares", json={"fw_id": "fw2", "version": "2.0.0",
                                   "model": "A1", "hw_version": "h1",
                                   "compatible_from": ["1.0.0"]})
    assert r.status_code == 200, r.text
    r = c.post("/firmwares", json={"fw_id": "fw1back", "version": "1.0.0",
                                   "model": "A1", "hw_version": "h1",
                                   "compatible_from": ["2.0.0"],
                                   "allow_downgrade": True})
    assert r.status_code == 200, r.text


def test_error_classification_over_http(client):
    c, _, _ = client
    token = register(c, "d1")

    # 重复注册 -> 409 + 可区分原因
    r = c.post("/devices", json={"device_id": "d1", "model": "A1",
                                 "hw_version": "h1", "fw_version": "1.0.0"})
    assert r.status_code == 409
    assert r.json()["details"]["reason"] == "identical_reregistration"

    # 伪造 token -> 401
    r = c.post("/devices/d1/heartbeat", json={"token": "bogus", "seq": 1})
    assert r.status_code == 401 and r.json()["code"] == "bad_device_token"

    # 未知设备 -> 404
    r = c.post("/devices/ghost/heartbeat", json={"token": token, "seq": 1})
    assert r.status_code == 404 and r.json()["code"] == "unknown_device"

    # 正常心跳
    assert c.post("/devices/d1/heartbeat",
                  json={"token": token, "seq": 1}).status_code == 200
    # 过期心跳 -> 409
    r = c.post("/devices/d1/heartbeat", json={"token": token, "seq": 1})
    assert r.status_code == 409
    assert r.json()["code"] == "stale_heartbeat"


def test_full_rollout_halt_and_rollback_flow(client):
    c, ctx, clock = client
    tokens = {f"d{i}": register(c, f"d{i}") for i in range(3)}
    make_firmwares(c)

    # 型号不匹配的设备混入 -> 422，发布不创建
    bad = register(c, "dX", fw="1.0.0")
    ctx.store.devices["dX"].model = "A2"
    with ctx.store.lock:
        ctx.store.persist()
    r = c.post("/rollouts", json={
        "rollout_id": "r1", "fw_id": "fw2",
        "device_ids": ["d0", "d1", "d2"], "batch_size": 2,
        "failure_rate_threshold": 0.5})
    assert r.status_code == 200, r.text
    r = c.post("/rollouts", json={
        "rollout_id": "rbad", "fw_id": "fw2",
        "device_ids": ["d0", "dX"], "batch_size": 2})
    assert r.status_code == 422
    assert r.json()["details"]["reason"] == "incompatible_members"

    assert c.post("/rollouts/r1/start").status_code == 200

    # d0 成功
    assert c.post("/rollouts/r1/claim/d0",
                  json={"token": tokens["d0"]}).status_code == 200
    for i, st in enumerate(["downloaded", "installed", "rebooted",
                            "confirmed"], start=1):
        rr = c.post("/rollouts/r1/report/d0",
                    json={"token": tokens["d0"], "seq": i, "stage": st})
        assert rr.status_code == 200, rr.text

    # 重连重复认领幂等
    rr = c.post("/rollouts/r1/claim/d0", json={"token": tokens["d0"]})
    assert rr.status_code == 409
    assert rr.json()["details"]["reason"] == "already_terminal"

    # d1 认领后失败 -> 失败率 50% 触发自动停止
    c.post("/rollouts/r1/claim/d1", json={"token": tokens["d1"]})
    rr = c.post("/rollouts/r1/report/d1",
                json={"token": tokens["d1"], "seq": 1,
                      "error": "download failed"})
    assert rr.status_code == 200 and rr.json()["stage"] == "failed"

    s = c.get("/rollouts/r1").json()
    assert s["status"] == "auto_halted"
    assert s["batches"][1]["status"] == "waiting"

    # 终态后迟到上报被拒（422 terminal_stage_locked）
    rr = c.post("/rollouts/r1/report/d1",
                json={"token": tokens["d1"], "seq": 10, "stage": "installed"})
    assert rr.status_code == 422
    assert rr.json()["details"]["reason"] == "terminal_stage_locked"
    # seq 回退的迟到上报 -> 409
    rr = c.post("/rollouts/r1/report/d1",
                json={"token": tokens["d1"], "seq": 0,
                      "stage": "downloaded"})
    assert rr.status_code == 422  # pydantic 校验 seq>0

    # d0 回滚（带幂等键，重放返回同样结果）
    rr = c.post("/rollback/device/d0",
                json={"target_version": "1.0.0", "idem_key": "rb-1"})
    assert rr.status_code == 200 and rr.json()["result"] == "completed"
    rr2 = c.post("/rollback/device/d0",
                 json={"target_version": "1.0.0", "idem_key": "rb-1"})
    assert rr2.json()["duplicate"] is True

    # 审计可追溯
    ev = c.get("/audit?limit=200").json()["events"]
    kinds = {e["kind"] for e in ev}
    assert {"rollout_auto_halted", "stage_transition",
            "rollback_recorded"} <= kinds

    # 设备视图与回滚记录
    assert c.get("/devices/d0").json()["fw_version"] == "1.0.0"
    recs = c.get("/rollback/records").json()["records"]
    assert any(r["device_id"] == "d0" and r["result"] == "completed"
               for r in recs)


def test_watchdog_and_reconcile_http(client):
    c, ctx, clock = client
    tokens = {f"d{i}": register(c, f"d{i}") for i in range(2)}
    make_firmwares(c)
    c.post("/rollouts", json={"rollout_id": "r1", "fw_id": "fw2",
                              "device_ids": ["d0", "d1"], "batch_size": 2,
                              "failure_rate_threshold": 1.0})
    c.post("/rollouts/r1/start")
    c.post("/rollouts/r1/claim/d0", json={"token": tokens["d0"]})
    c.post("/rollouts/r1/report/d0",
           json={"token": tokens["d0"], "seq": 1, "stage": "downloaded"})

    clock.t += 31
    w = c.post("/rollouts/r1/watchdog").json()
    assert w["retried"] == ["d0"]

    rr = c.post("/rollouts/r1/reconcile/d0",
                json={"token": tokens["d0"], "client_seq": 0})
    assert rr.json()["stage"] == "none"
    assert rr.json()["client_is_behind"] is True
    assert rr.json()["accepted_seq"] == 1


def test_shelved_device_failure_report_rejected_over_http(client):
    """搁置后不带 stage 的失败上报走 HTTP 也被明确拒绝，汇总不变。"""
    c, ctx, clock = client
    tokens = {f"d{i}": register(c, f"d{i}") for i in range(2)}
    make_firmwares(c)
    c.post("/rollouts", json={"rollout_id": "r1", "fw_id": "fw2",
                              "device_ids": ["d0", "d1"], "batch_size": 2,
                              "failure_rate_threshold": 1.0})
    c.post("/rollouts/r1/start")
    c.post("/rollouts/r1/claim/d0", json={"token": tokens["d0"]})
    c.post("/rollouts/r1/claim/d1", json={"token": tokens["d1"]})
    for i, st in enumerate(["downloaded", "installed", "rebooted",
                            "confirmed"], start=1):
        c.post("/rollouts/r1/report/d0",
               json={"token": tokens["d0"], "seq": i, "stage": st})
    # d1：两轮沉默超时 -> 补救用尽 -> 搁置
    c.post("/rollouts/r1/report/d1",
           json={"token": tokens["d1"], "seq": 1, "stage": "downloaded"})
    clock.t += 31
    assert c.post("/rollouts/r1/watchdog").json()["retried"] == ["d1"]
    clock.t += 31
    assert c.post("/rollouts/r1/watchdog").json()["shelved"] == ["d1"]

    before = c.get("/rollouts/r1").json()
    assert before["stall"]["shelved"] == 1

    # 只报失败、不带 stage、seq 更大 -> 422 device_shelved
    r = c.post("/rollouts/r1/report/d1",
               json={"token": tokens["d1"], "seq": 99, "error": "late"})
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "stage_transition_rejected"
    assert body["details"]["reason"] == "device_shelved"

    after = c.get("/rollouts/r1").json()
    assert after["stall"] == before["stall"]
    assert after["batches"] == before["batches"]
