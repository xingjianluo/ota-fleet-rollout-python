"""灰度发布：匹配校验、升级/降级路径、暂停继续、失败率熔断、并发认领。"""

from __future__ import annotations

import concurrent.futures

import pytest

from ota.errors import (
    BatchExhausted,
    FirmwareNotFound,
    IncompatibleFirmware,
    InvalidUpgradePath,
    NoEligibleDevice,
    ReleaseStateConflict,
)
from ota.models import ReleaseState


def _prepare(harness, n=3, fw_version="2.0.0", threshold=0.5,
             batch_size=None, allow_downgrade=False, from_versions=None):
    for i in range(1, n + 1):
        harness.reg(f"dev{i}")
    harness.make_fw("fw-new", fw_version)
    return harness.svc.create_release({
        "target_fw_id": "fw-new", "batch_size": batch_size or n,
        "failure_threshold": threshold,
        "stage_timeout": 100.0, "heartbeat_timeout": 100.0,
        "max_stage_attempts": 2,
        "allow_downgrade": allow_downgrade,
        "allowed_from_versions": from_versions or [],
    })["release"]["release_id"]


def test_firmware_compatibility_filtering(harness):
    """型号/硬件不匹配、已在目标版本、允许名单外的设备不得纳入发布。"""
    harness.reg("ok", model="M1", hw="h1")
    harness.reg("wrong-model", model="M2", hw="h1")
    harness.reg("wrong-hw", model="M1", hw="h9")
    harness.reg("already", model="M1", hw="h1", fw="2.0.0")
    harness.make_fw("fw-new", "2.0.0")
    rel = harness.svc.create_release({"target_fw_id": "fw-new",
                                      "batch_size": 10})["release"]["release_id"]
    harness.svc.start_release(rel)
    plan = harness.svc.plan_next_batch(rel)
    ids = plan["batch"]["device_ids"]
    assert ids == ["ok"], ids
    reasons = plan["skipped"]
    assert reasons["wrong-model"] == "model_mismatch"
    assert reasons["wrong-hw"] == "hw_mismatch"
    assert reasons["already"] == "already_on_target"
    harness.log("兼容性筛选结果", included=ids, skipped=reasons)


def test_downgrade_path_must_be_explicit(harness):
    harness.reg("d1", fw="2.0.0")
    harness.make_fw("fw-old", "1.0.0")
    rel = harness.svc.create_release({"target_fw_id": "fw-old",
                                      "batch_size": 5,
                                      "allow_downgrade": False})["release"]["release_id"]
    harness.svc.start_release(rel)
    with pytest.raises(NoEligibleDevice) as ei:
        harness.svc.plan_next_batch(rel)
    assert ei.value.reason == "no_candidate"
    audit = [e for e in harness.svc.audit_log("d1")
             if e["details"].get("reason") == "downgrade_not_allowed"]
    assert audit, "降级被拒绝必须留审计"
    harness.log("降级未授权被拒绝", audit=audit[0]["message"])
    # 显式允许降级后可纳入
    rel2 = harness.svc.create_release({"target_fw_id": "fw-old",
                                       "batch_size": 5,
                                       "allow_downgrade": True})["release"]["release_id"]
    harness.svc.start_release(rel2)
    plan = harness.svc.plan_next_batch(rel2)
    assert plan["batch"]["device_ids"] == ["d1"]


def test_allowed_from_versions_gate(harness):
    harness.reg("d1", fw="1.0.0")
    harness.reg("d2", fw="1.5.0")
    harness.make_fw("fw-new", "2.0.0")
    rel = harness.svc.create_release({
        "target_fw_id": "fw-new", "batch_size": 5,
        "allowed_from_versions": ["1.0.0"],
    })["release"]["release_id"]
    harness.svc.start_release(rel)
    plan = harness.svc.plan_next_batch(rel)
    assert plan["batch"]["device_ids"] == ["d1"]
    assert plan["skipped"]["d2"] == "from_version_not_allowed"


def test_target_firmware_must_exist(harness):
    with pytest.raises(FirmwareNotFound) as ei:
        harness.svc.create_release({"target_fw_id": "ghost"})
    assert ei.value.reason == "target_fw_missing"


def test_pause_blocks_new_batches_and_resume(harness):
    rel = _prepare(harness, n=2, batch_size=1)
    harness.svc.start_release(rel)
    b1 = harness.svc.plan_next_batch(rel)["batch"]["batch_id"]
    harness.svc.pause_release(rel)
    with pytest.raises(ReleaseStateConflict) as ei:
        harness.svc.plan_next_batch(rel)
    assert ei.value.reason == "cannot_plan_from_paused"
    # 已下发批次仍可继续推进（暂停不影响在途设备收敛）
    jid = harness.claim("dev1")["job"]["job_id"]
    harness.drive_success("dev1", jid)
    harness.svc.resume_release(rel)
    b2 = harness.svc.plan_next_batch(rel)
    assert b2["batch"]["device_ids"] == ["dev2"]
    harness.log("暂停继续行为", b1=b1, b2=b2["batch"]["batch_id"])


def test_failure_threshold_halts_following_batches(harness):
    """批次容量=2，阈值=0.5：首个设备失败即自动停止，其余 PENDING 被搁置。"""
    rel = _prepare(harness, n=2, threshold=0.5, batch_size=2)
    harness.svc.start_release(rel)
    harness.svc.plan_next_batch(rel)
    j1 = harness.claim("dev1")["job"]["job_id"]
    j2 = harness.claim("dev2")["job"]["job_id"]
    harness.report("dev1", j1, "downloaded", 1, failed=True, reason="flash_error")
    info = harness.svc.get_release(rel)
    assert info["release"]["state"] == ReleaseState.THRESHOLD_HALTED.value
    assert "failure_rate" in (info["release"]["halt_reason"] or "")
    # 第二个在途设备继续收敛（已下载→安装→…→确认）
    harness.drive_success("dev2", j2)
    # 熔断态不可直接继续（必须人工介入），也不能再规划
    with pytest.raises(ReleaseStateConflict):
        harness.svc.resume_release(rel)
    with pytest.raises(ReleaseStateConflict):
        harness.svc.plan_next_batch(rel)
    # 已下发设备最终状态可解释：一失败一成功
    stages = {j["device_id"]: j["stage"] for j in info["batches"][0] and
              harness.svc.get_batch(info["batches"][0]["batch_id"])["jobs"]}
    assert stages == {"dev1": "failed", "dev2": "confirmed"}
    harness.log("阈值熔断后最终状态", stages=stages,
                halt=info["release"]["halt_reason"])


def test_threshold_halt_suspends_unclaimed_jobs(harness):
    """两个批次：第一批失败触发熔断，第二批未认领设备被确定性搁置。"""
    rel = _prepare(harness, n=3, threshold=1.0, batch_size=1)
    harness.svc.start_release(rel)
    harness.svc.plan_next_batch(rel)  # bat1: dev1
    j1 = harness.claim("dev1")["job"]["job_id"]
    harness.svc.plan_next_batch(rel)  # bat2: dev2
    harness.report("dev1", j1, "downloaded", 1, failed=True, reason="x")
    state = harness.svc.get_release(rel)
    assert state["release"]["state"] == ReleaseState.THRESHOLD_HALTED.value
    # dev2 未认领 -> SUSPENDED；dev3 尚未纳入任何批次（不再规划）
    dev2_jobs = [j for j in harness.svc.store.jobs.values()
                 if j.device_id == "dev2"]
    assert dev2_jobs and dev2_jobs[0].stage.value == "suspended"
    assert not any(j.device_id == "dev3" for j in harness.svc.store.jobs.values())


def test_concurrent_claim_same_device_single_effect(harness):
    """同一设备并发认领：只有一次 PENDING->DOWNLOADING 生效，其余幂等。"""
    rel = _prepare(harness, n=1, batch_size=1)
    harness.svc.start_release(rel)
    harness.svc.plan_next_batch(rel)

    def claim():
        return harness.svc.claim_job({"device_id": "dev1",
                                      "token": harness.tokens["dev1"]})

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: claim(), range(16)))
    job = harness.svc.store.jobs  # 只有一个任务
    assert len(job) == 1
    only = next(iter(job.values()))
    assert only.stage.value == "downloading"
    assert only.stage_seq == 1
    dup_flags = [r["duplicate"] for r in results]
    assert dup_flags.count(False) == 1 and dup_flags.count(True) == 15
    # 批次计数无偏差
    batch = harness.svc.get_batch(only.batch_id)["batch"]
    assert batch["claimed"] == ["dev1"]
    harness.log("并发认领收敛", stage_seq=only.stage_seq, dup=dup_flags)


def test_concurrent_claim_batch_each_device_once(harness):
    """整批并发认领：每台设备恰好一次，claimed 与任务迁移严格对齐。"""
    rel = _prepare(harness, n=8, batch_size=8)
    harness.svc.start_release(rel)
    harness.svc.plan_next_batch(rel)
    barrier = __import__("threading").Barrier(8)

    def claim(dev):
        barrier.wait()
        return dev, harness.svc.claim_job(
            {"device_id": dev, "token": harness.tokens[dev]})

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(claim, [f"dev{i}" for i in range(1, 9)]))
    for did in [f"dev{i}" for i in range(1, 9)]:
        j = harness.job_of(did)
        assert j.stage.value == "downloading" and j.stage_seq == 1
    rel_info = harness.svc.get_release(rel)
    assert rel_info["stats"]["in_flight"] == 8
    harness.log("整批并发认领完成", in_flight=rel_info["stats"]["in_flight"])


def test_release_completes_when_all_terminal(harness):
    rel = _prepare(harness, n=2, batch_size=2)
    harness.svc.start_release(rel)
    harness.svc.plan_next_batch(rel)
    for did in ["dev1", "dev2"]:
        jid = harness.claim(did)["job"]["job_id"]
        harness.drive_success(did, jid)
    info = harness.svc.get_release(rel)
    assert info["release"]["state"] == ReleaseState.COMPLETED.value
    assert info["stats"]["confirmed"] == 2
