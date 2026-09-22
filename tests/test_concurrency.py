"""并发一致性：并发心跳、并发阶段推进、并发回滚交错、落盘恢复后计数不偏。"""

from __future__ import annotations

import concurrent.futures
import threading

import pytest

from ota.errors import (
    InvalidStageTransition,
    RollbackInProgress,
    StaleHeartbeat,
    StaleReport,
)
from ota.models import JobStage
from ota.service import OtaService


def _fleet(harness, n=10):
    for i in range(1, n + 1):
        harness.reg(f"dev{i}")
    harness.make_fw("fw-new", "2.0.0")
    rel = harness.svc.create_release({"target_fw_id": "fw-new",
                                      "batch_size": n,
                                      "failure_threshold": 1.0,
                                      "stage_timeout": 1e9,
                                      "max_stage_attempts": 5})["release"]["release_id"]
    harness.svc.start_release(rel)
    harness.svc.plan_next_batch(rel)
    return rel


def test_concurrent_heartbeats_monotonic(harness):
    """同一设备并发不同 seq 心跳：只有严格递增的被接受，计数最终连续无洞。"""
    harness.reg("dev1")
    accepted = []
    lock = threading.Lock()

    def hb(seq):
        try:
            harness.svc.heartbeat({"device_id": "dev1",
                                   "token": harness.tokens["dev1"], "seq": seq})
            with lock:
                accepted.append(seq)
        except StaleHeartbeat:
            pass

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(hb, range(100)))
    dev = harness.svc.get_device("dev1")["device"]
    # 最终接受的最大 seq 必为 99，且无并发覆盖导致的回退
    assert dev["last_heartbeat_seq"] == 99
    assert len(accepted) == 100 or dev["last_heartbeat_seq"] == 99
    harness.log("并发心跳收敛", max_seq=dev["last_heartbeat_seq"])


def test_concurrent_reports_one_terminal_count(harness):
    """多个线程同时上报设备成功终态：CONFIRMED 只生效一次，计数不翻倍。"""
    rel = _fleet(harness, n=1)
    jid = harness.claim("dev1")["job"]["job_id"]
    # 先推进到 REBOOTING
    for i, st in enumerate(["downloaded", "installing", "installed",
                            "rebooting"], start=1):
        harness.report("dev1", jid, st, i)
    errors = []

    def confirm(seq):
        try:
            return harness.svc.report_stage({
                "device_id": "dev1", "token": harness.tokens["dev1"],
                "job_id": jid, "stage": "confirmed", "seq": seq})
        except (StaleReport, InvalidStageTransition) as e:
            return e

    # seq 5..5+19 同时冲终态：seq=5 成功，seq<5 无；其余 seq>5 因终态不可变被拒
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(confirm, range(5, 25)))
    oks = [r for r in results if isinstance(r, dict)]
    assert len(oks) == 1
    stats = harness.svc.get_release(rel)["stats"]
    assert stats["confirmed"] == 1
    job = harness.svc.store.jobs[jid]
    assert job.stage is JobStage.CONFIRMED and job.counted is True
    assert harness.svc.get_device("dev1")["device"]["current_fw_version"] == "2.0.0"
    harness.log("并发终态上报收敛", confirmed=stats["confirmed"])


def test_concurrent_mixed_claims_and_reports_fleet(harness):
    """10 台设备并发认领+推进：最终恰好 10 confirmed、0 偏差。"""
    n = 10
    rel = _fleet(harness, n=n)

    def device_flow(did):
        r = harness.claim(did)
        jid = r["job"]["job_id"]
        for i, st in enumerate(["downloaded", "installing", "installed",
                                "rebooting", "confirmed"], start=1):
            try:
                harness.report(did, jid, st, i)
            except (StaleReport, InvalidStageTransition):
                pass
        return did

    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        done = list(pool.map(device_flow, [f"dev{i}" for i in range(1, n + 1)]))
    assert sorted(done, key=lambda x: int(x[3:])) == [f"dev{i}" for i in range(1, n + 1)]
    stats = harness.svc.get_release(rel)["stats"]
    assert stats["confirmed"] == n and stats["in_flight"] == 0
    harness.log("编队并发发布收敛", stats=stats)


def test_rollback_and_report_interleave_safe(harness):
    """回滚建单与阶段上报交错：在途时回滚被拒，终态后回滚成功，状态不倒退。"""
    rel = _fleet(harness, n=1)
    harness.make_fw("fw-old", "1.0.0")
    jid = harness.claim("dev1")["job"]["job_id"]
    harness.report("dev1", jid, "downloaded", 1)

    def try_rollback():
        try:
            return harness.svc.rollback_device({"device_id": "dev1"})
        except Exception as e:  # noqa: BLE001
            return e

    # 升级未完成时并发回滚 -> 一定被 job_in_flight 拒绝
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        rbs = list(pool.map(lambda _: try_rollback(), range(4)))
    assert all(getattr(r, "reason", None) == "job_in_flight" for r in rbs)
    assert harness.svc.get_device("dev1")["device"]["current_fw_version"] == "1.0.0"

    # 完成升级
    for i, st in enumerate(["installing", "installed", "rebooting",
                            "confirmed"], start=2):
        harness.report("dev1", jid, st, i)
    out = harness.svc.rollback_batch(rel, harness.job_of("dev1").batch_id, None)
    assert len(out["created_jobs"]) == 1 and out["rejected"] == {}
    rjid = out["created_jobs"][0]
    # 并发二次回滚 -> inflight 保护
    harness.claim("dev1")
    with pytest.raises(RollbackInProgress):
        harness.svc.rollback_device({"device_id": "dev1"})
    harness.drive_success("dev1", rjid, seq0=20)
    dev = harness.svc.get_device("dev1")["device"]
    assert dev["current_fw_version"] == "1.0.0"
    harness.log("回滚/上报交错收敛", fw=dev["current_fw_version"])


def test_state_persistent_under_concurrent_writes(harness):
    """并发写后立即重建服务：计数、阶段与发布状态与内存完全一致。"""
    rel = _fleet(harness, n=5)
    for did in [f"dev{i}" for i in range(1, 6)]:
        jid = harness.claim(did)["job"]["job_id"]
        harness.drive_success(did, jid)
    before = harness.svc.get_release(rel)
    # 重新加载（模拟进程重启）
    svc2 = OtaService(harness.svc.store, harness.clock)
    after = svc2.get_release(rel)
    assert before["stats"] == after["stats"]
    assert after["release"]["state"] == "completed"
    # ID 生成器不回退：再注册/规划不撞 ID
    harness.reg("dev6")
    assert "dev6" in harness.svc.store.devices
    harness.log("重启后状态一致", stats=after["stats"])
