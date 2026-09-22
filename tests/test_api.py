"""HTTP 端到端：验证路由、错误码/原因码映射与可复现流程。"""

from __future__ import annotations


def test_http_happy_path_and_error_mapping(client):
    c = client
    r = c.post("/devices/register", json={
        "device_id": "d1", "model": "M1", "hw_version": "h1",
        "current_fw_version": "1.0.0"})
    assert r.status_code == 200
    token = r.json()["token"]

    # 重复注册 -> 409 + 可区分 reason
    r = c.post("/devices/register", json={
        "device_id": "d1", "model": "M1", "hw_version": "h1",
        "current_fw_version": "1.0.0"})
    assert r.status_code == 409
    assert r.json()["error"]["reason"] == "identity_conflict"

    # 心跳
    assert c.post("/devices/heartbeat",
                  json={"device_id": "d1", "token": token, "seq": 1}).status_code == 200
    r = c.post("/devices/heartbeat",
               json={"device_id": "d1", "token": "nope", "seq": 2})
    assert r.status_code == 401 and r.json()["error"]["reason"] == "bad_token"

    # 固件 -> 发布 -> 批次 -> 认领 -> 全流程
    c.post("/firmwares", json={"fw_id": "fw2", "version": "2.0.0",
                               "model": "M1", "hw_version": "h1", "checksum": "x"})
    rel = c.post("/releases", json={"target_fw_id": "fw2", "batch_size": 1,
                                    "failure_threshold": 1.0}).json()["release"]["release_id"]
    c.post(f"/releases/{rel}/start")
    c.post(f"/releases/{rel}/batches/plan")
    j = c.post("/jobs/claim", json={"device_id": "d1", "token": token}).json()["job"]
    jid = j["job_id"]
    for i, st in enumerate(["downloaded", "installing", "installed",
                            "rebooting", "confirmed"], start=1):
        rr = c.post("/jobs/report", json={"device_id": "d1", "token": token,
                                          "job_id": jid, "stage": st, "seq": i})
        assert rr.status_code == 200, (st, rr.text)
    info = c.get(f"/releases/{rel}").json()
    assert info["release"]["state"] == "completed"
    assert info["stats"]["confirmed"] == 1

    # 审计可追溯
    events = c.get("/audit", params={"ref": jid}).json()["events"]
    assert events and all({"ts", "kind", "ref", "message"} <= set(e) for e in events)


def test_http_duplicate_report_is_idempotent_200(client):
    c = client
    r = c.post("/devices/register", json={
        "device_id": "d9", "model": "M1", "hw_version": "h1",
        "current_fw_version": "1.0.0"})
    token = r.json()["token"]
    c.post("/firmwares", json={"fw_id": "fw2", "version": "2.0.0",
                               "model": "M1", "hw_version": "h1", "checksum": "x"})
    rel = c.post("/releases", json={"target_fw_id": "fw2"}).json()["release"]["release_id"]
    c.post(f"/releases/{rel}/start")
    c.post(f"/releases/{rel}/batches/plan")
    jid = c.post("/jobs/claim",
                 json={"device_id": "d9", "token": token}).json()["job"]["job_id"]
    body = {"device_id": "d9", "token": token, "job_id": jid,
            "stage": "downloaded", "seq": 1}
    assert c.post("/jobs/report", json=body).status_code == 200
    # 完全重复：幂等 200 且原因码可区分
    r = c.post("/jobs/report", json=body)
    assert r.status_code == 200 and r.json()["error"]["reason"] == "seq_duplicate"
    # 迟到：409
    body["seq"] = 0
    r = c.post("/jobs/report", json=body)
    assert r.status_code == 409 and r.json()["error"]["reason"] == "seq_regressed"
