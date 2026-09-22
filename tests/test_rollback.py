"""回滚规则：目标必须存在且兼容、在途设备不可回滚、不可静默半更新、并发防重。"""

from __future__ import annotations

import concurrent.futures

import pytest

from ota.errors import (
    RollbackInProgress,
    RollbackNotAllowed,
    RollbackTargetMissing,
)


def _upgraded(harness, device_id="dev1"):
    harness.reg(device_id)
    harness.make_fw("fw-old", "1.0.0")
    harness.make_fw("fw-new", "2.0.0")
    rel = harness.svc.create_release({
        "target_fw_id": "fw-new", "batch_size": 1,
        "failure_threshold": 1.0, "stage_timeout": 100.0,
        "heartbeat_timeout": 100.0,
    })["release"]["release_id"]
    harness.svc.start_release(rel)
    harness.svc.plan_next_batch(rel)
    jid = harness.claim(device_id)["job"]["job_id"]
    harness.drive_success(device_id, jid)
    return rel, jid


def test_successful_device_rollback_restores_previous_version(harness):
    _upgraded(harness)
    assert harness.svc.get_device("dev1")["device"]["current_fw_version"] == "2.0.0"
    rb = harness.svc.rollback_device({"device_id": "dev1"})["job"]
    assert rb["direction"] == "rollback" and rb["stage"] == "pending"
    assert rb["target_fw_id"] == "fw-old"
    rjid = rb["job_id"]
    harness.claim("dev1")
    harness.drive_success("dev1", rjid, seq0=10)
    dev = harness.svc.get_device("dev1")
    assert dev["device"]["current_fw_version"] == "1.0.0"
    final = [j for j in dev["jobs"] if j["job_id"] == rjid][0]
    assert final["stage"] == "rolled_back"
    # 可追溯：原任务 superseded_by 指向回滚任务
    prior = [j for j in dev["jobs"] if j["stage"] == "confirmed"][0]
    assert prior["superseded_by"] == rjid
    harness.log("单设备回滚成功", fw=dev["device"]["current_fw_version"],
                prior=prior["job_id"], rolled=rjid)


def test_rollback_rejected_while_job_in_flight(harness):
    """升级进行中不允许回滚：避免留下半更新设备。"""
    harness.reg("dev1")
    harness.make_fw("fw-old", "1.0.0")
    harness.make_fw("fw-new", "2.0.0")
    rel = harness.svc.create_release({"target_fw_id": "fw-new",
                                      "batch_size": 1,
                                      "failure_threshold": 1.0})["release"]["release_id"]
    harness.svc.start_release(rel)
    harness.svc.plan_next_batch(rel)
    jid = harness.claim("dev1")["job"]["job_id"]
    harness.report("dev1", jid, "downloaded", 1)
    with pytest.raises(RollbackNotAllowed) as ei:
        harness.svc.rollback_device({"device_id": "dev1"})
    assert ei.value.reason == "job_in_flight"
    # 状态未受污染：仍停在 downloaded
    job = harness.svc.store.jobs[jid]
    assert job.stage.value == "downloaded"
    rejected = [e for e in harness.svc.audit_log("dev1")
                if e["kind"] == "rollback_rejected"]
    assert rejected
    harness.log("在途回滚被拒绝", reason=ei.value.reason, stage=job.stage.value)


def test_rollback_requires_existing_target(harness):
    _upgraded(harness)
    # 固件库中删除旧固件 -> 目标不存在，必须拒绝并记录
    harness.svc.store.firmwares["fw-old"].yanked = True
    with pytest.raises(RollbackTargetMissing) as ei:
        harness.svc.rollback_device({"device_id": "dev1"})
    assert ei.value.reason == "previous_fw_missing"
    assert harness.svc.get_device("dev1")["device"]["current_fw_version"] == "2.0.0"


def test_rollback_target_must_be_previous_version(harness):
    _upgraded(harness)
    harness.make_fw("fw-older", "0.9.0")
    with pytest.raises(RollbackNotAllowed) as ei:
        harness.svc.rollback_device({"device_id": "dev1", "target_fw_id": "fw-older"})
    assert ei.value.reason == "target_not_previous"
    harness.log("回滚到非升级前版本被拒绝", reason=ei.value.reason)


def test_no_history_no_rollback(harness):
    harness.reg("dev1")
    harness.make_fw("fw-old", "1.0.0")
    with pytest.raises(RollbackNotAllowed) as ei:
        harness.svc.rollback_device({"device_id": "dev1", "target_fw_id": "fw-old"})
    assert ei.value.reason == "no_history"


def test_concurrent_rollback_only_one_job(harness):
    _upgraded(harness)
    barrier = __import__("threading").Barrier(8)

    def rb(_):
        barrier.wait()
        try:
            return harness.svc.rollback_device({"device_id": "dev1"})
        except RollbackInProgress as e:
            return e

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(rb, range(8)))
    created = [r for r in results if isinstance(r, dict)]
    rejected = [r for r in results if isinstance(r, RollbackInProgress)]
    assert len(created) == 1
    assert all(r.reason == "rollback_inflight" for r in rejected)
    rb_jobs = [j for j in harness.svc.store.jobs.values()
               if j.direction.value == "rollback"]
    assert len(rb_jobs) == 1  # 不重复建单
    harness.log("并发回滚收敛", created=1, rejected=len(rejected))


def test_batch_rollback_classifies_each_device(harness):
    """整批回滚：成功设备正常建单；在途设备被拒绝且原因可区分；不半更新。"""
    # dev1 升级成功可回滚；dev2 升级失败（终态，未上 2.0.0），需显式目标
    for d in ("dev1", "dev2", "dev3"):
        harness.reg(d)
    harness.make_fw("fw-old", "1.0.0")
    harness.make_fw("fw-new", "2.0.0")
    rel = harness.svc.create_release({"target_fw_id": "fw-new",
                                      "batch_size": 3,
                                      "failure_threshold": 1.0})["release"]["release_id"]
    harness.svc.start_release(rel)
    harness.svc.plan_next_batch(rel)
    j1 = harness.claim("dev1")["job"]["job_id"]
    j2 = harness.claim("dev2")["job"]["job_id"]
    harness.drive_success("dev1", j1)
    harness.report("dev2", j2, "downloaded", 1, failed=True, reason="hw_fail")
    # dev3 保持 PENDING
    batch_id = [b.batch_id for b in
                harness.svc.store.batches.values()][0]
    out = harness.svc.rollback_batch(rel, batch_id, target_fw_id=None)
    # dev1 成功；dev2/ dev3 拒绝（dev2 当前仍 1.0.0 非降级；dev3 在途 PENDING）
    assert len(out["created_jobs"]) == 1
    assert out["rejected"]["dev3"] == "job_in_flight"
    assert out["rejected"]["dev2"] == "nothing_to_rollback"
    harness.log("整批回滚归类", rejected=out["rejected"])
    # dev3 的 PENDING 任务原封不动（无半更新）
    j3 = harness.job_of("dev3")
    assert j3.stage.value == "pending"
    # dev2 固件版本保持 1.0.0
    assert harness.svc.get_device("dev2")["device"]["current_fw_version"] == "1.0.0"


def test_batch_rollback_unknown_target_rejected(harness):
    _upgraded(harness)
    batch_id = next(iter(harness.svc.store.batches))
    with pytest.raises(RollbackTargetMissing) as ei:
        harness.svc.rollback_batch("rel-1", batch_id, target_fw_id="ghost")
    assert ei.value.reason == "target_fw_missing"


def test_rollback_job_watchdog_suspend_then_reconnect_recovers(harness):
    """回滚下载中掉电：看门狗搁置（释放回滚互斥），重连后可重新回滚/恢复成功。"""
    from ota.models import JobStage

    # 自定义发布参数：阶段超时 100、只允许 1 次尝试，便于确定性搁置
    harness.reg("dev1")
    harness.make_fw("fw-old", "1.0.0")
    harness.make_fw("fw-new", "2.0.0")
    rel = harness.svc.create_release({
        "target_fw_id": "fw-new", "batch_size": 1, "failure_threshold": 1.0,
        "stage_timeout": 100.0, "heartbeat_timeout": 100.0,
        "max_stage_attempts": 1,
    })["release"]["release_id"]
    harness.svc.start_release(rel)
    harness.svc.plan_next_batch(rel)
    up = harness.claim("dev1")["job"]["job_id"]
    harness.drive_success("dev1", up)
    rjid = harness.svc.rollback_device({"device_id": "dev1"})["job"]["job_id"]
    harness.claim("dev1")
    # 回滚 DOWNLOADING 阶段设备掉电，时钟超过阶段超时且无重试额度
    harness.clock.advance(200)
    out = harness.svc.sweep()
    assert out["suspended"] == [rjid]
    job = harness.svc.store.jobs[rjid]
    assert job.stage is JobStage.SUSPENDED
    # 设备仍处于 2.0.0（旧固件未切换，绝无半更新）
    assert harness.svc.get_device("dev1")["device"]["current_fw_version"] == "2.0.0"
    # 搁置后设备重连：回滚任务恢复 DOWNLOADING，随后走完整流水线
    harness.hb("dev1", 50)
    assert harness.svc.store.jobs[rjid].stage is JobStage.DOWNLOADING
    harness.drive_success("dev1", rjid, seq0=51)
    dev = harness.svc.get_device("dev1")
    assert dev["device"]["current_fw_version"] == "1.0.0"
    final = [j for j in dev["jobs"] if j["job_id"] == rjid][0]
    assert final["stage"] == "rolled_back"
    harness.log("回滚掉电->搁置->重连恢复成功", fw=dev["device"]["current_fw_version"])
