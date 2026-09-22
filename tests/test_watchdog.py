"""看门狗：阶段超时重试/搁置、心跳离线、重连增量补齐、乱序迟到收敛。"""

from __future__ import annotations

import pytest

from ota.errors import (
    DuplicateReport,
    InvalidStageTransition,
    StaleReport,
)
from ota.models import DeviceStatus, JobStage


def _active_job(harness, did="dev1", stage_timeout=100.0, max_attempts=2,
                hb_timeout=100.0):
    harness.reg(did)
    harness.make_fw("fw-new", "2.0.0")
    rel = harness.svc.create_release({
        "target_fw_id": "fw-new", "batch_size": 1,
        "failure_threshold": 1.0, "stage_timeout": stage_timeout,
        "heartbeat_timeout": hb_timeout, "max_stage_attempts": max_attempts,
    })["release"]["release_id"]
    harness.svc.start_release(rel)
    harness.svc.plan_next_batch(rel)
    jid = harness.claim(did)["job"]["job_id"]
    return rel, jid


def test_heartbeat_timeout_marks_offline(harness):
    rel, jid = _active_job(harness, hb_timeout=50.0)
    harness.hb("dev1", 1)
    out = harness.svc.sweep()
    assert out["offline"] == []
    harness.clock.advance(51)
    out = harness.svc.sweep()
    assert out["offline"] == ["dev1"]
    dev = harness.svc.get_device("dev1")["device"]
    assert dev["status"] == DeviceStatus.OFFLINE.value
    harness.log("心跳超时 -> 离线", sweep=out)


def test_stage_timeout_retries_then_suspends(harness):
    """长时间无进展：先重试（不计数为失败），耗尽后搁置，且不阻塞批次统计。"""
    rel, jid = _active_job(harness, stage_timeout=100.0, max_attempts=2)
    # DOWNLOADING 中，无任何上报
    harness.clock.advance(101)
    out = harness.svc.sweep()
    assert out["retried"] == [jid]
    job = harness.svc.store.jobs[jid]
    assert job.attempts == 1 and job.stage is JobStage.DOWNLOADING
    harness.clock.advance(101)
    out = harness.svc.sweep()
    assert out["suspended"] == [jid]
    job = harness.svc.store.jobs[jid]
    assert job.stage is JobStage.SUSPENDED
    assert job.terminal_reason == "watchdog_stage_timeout:downloading"
    harness.log("看门狗搁置", reason=job.terminal_reason)
    # 搁置不计入 confirmed，但计入失败率统计口径；批次可继续解释
    stats = harness.svc.get_release(rel)["stats"]
    assert stats["suspended"] == 1 and stats["confirmed"] == 0


def test_reconnect_resumes_suspended_without_double_count(harness):
    """掉电搁置的设备重连后补齐进度：恢复执行，之前的终态计数必须被冲销。"""
    rel, jid = _active_job(harness, stage_timeout=100.0, max_attempts=1)
    harness.clock.advance(200)
    harness.svc.sweep()
    assert harness.svc.store.jobs[jid].stage is JobStage.SUSPENDED
    stats = harness.svc.get_release(rel)["stats"]
    assert stats["suspended"] == 1
    # 重连心跳：搁置任务恢复
    r = harness.hb("dev1", 7)
    assert r["resumed_jobs"] == [jid]
    job = harness.svc.store.jobs[jid]
    assert job.stage is JobStage.DOWNLOADING and job.counted is False
    assert job.last_report_seq == -1
    stats = harness.svc.get_release(rel)["stats"]
    assert stats["suspended"] == 0 and stats["in_flight"] == 1
    harness.log("重连补齐：搁置计数冲销，任务恢复", stage=job.stage.value)
    # 继续完成升级，只成功一次
    harness.drive_success("dev1", jid, seq0=8)
    stats = harness.svc.get_release(rel)["stats"]
    assert stats["confirmed"] == 1 and stats["suspended"] == 0


def test_offline_progress_not_double_counted(harness):
    """离线期间设备的进度在重连后增量上报，不重复计数、不丢失。"""
    rel, jid = _active_job(harness, stage_timeout=50.0, hb_timeout=50.0, max_attempts=1)
    # 设备在线后掉电（先有一次心跳，离线判定与重连恢复才成立）
    harness.hb("dev1", 1)
    harness.clock.advance(60)
    harness.svc.sweep()  # 标记离线并搁置 DOWNLOADING 任务
    r = harness.hb("dev1", 5)
    assert r["resumed_jobs"] == [jid]
    for i, st in enumerate(["downloaded", "installing", "installed",
                            "rebooting", "confirmed"], start=1):
        harness.report("dev1", jid, st, i)
    stats = harness.svc.get_release(rel)["stats"]
    assert stats["confirmed"] == 1
    assert harness.svc.get_device("dev1")["device"]["current_fw_version"] == "2.0.0"
    harness.log("离线后补齐完成", stats=stats)


def test_out_of_order_reports_converge(harness):
    """乱序/迟到上报：序号小的一律丢弃，状态只由最大序号决定。"""
    rel, jid = _active_job(harness)
    harness.report("dev1", jid, "downloaded", 1)
    harness.report("dev1", jid, "installing", 2)
    # 迟到的旧进度（seq=1 语义）：拒绝
    with pytest.raises(StaleReport) as ei:
        harness.report("dev1", jid, "downloaded", 1)
    assert ei.value.reason == "seq_regressed"
    # 乱序跳阶段（seq=3 直接 confirmed）：阶段非法，拒绝且不迁移、不消费 seq
    with pytest.raises(InvalidStageTransition) as ei:
        harness.report("dev1", jid, "confirmed", 3)
    assert ei.value.reason == "non_adjacent_or_backward"
    job = harness.svc.store.jobs[jid]
    # seq=5 被拒后 last_report_seq 保持 2（已接受最大值），阶段停在 installing
    assert job.last_report_seq == 2
    assert job.stage is JobStage.INSTALLING
    harness.log("乱序收敛", last_seq=job.last_report_seq, stage=job.stage.value)


def test_duplicate_report_idempotent_no_double_count(harness):
    rel, jid = _active_job(harness)
    harness.report("dev1", jid, "downloaded", 1)
    with pytest.raises(DuplicateReport) as ei:
        harness.report("dev1", jid, "downloaded", 1)
    assert ei.value.reason == "seq_duplicate"
    job = harness.svc.store.jobs[jid]
    assert job.stage_seq == 2  # claim(1) + downloaded(2)，重复不增加
    harness.log("重复上报幂等", stage_seq=job.stage_seq)


def test_repeated_same_stage_retried_and_eventually_suspended(harness):
    """反复上报同一阶段：消耗重试，达到上限由看门狗搁置，不卡死批次。"""
    rel, jid = _active_job(harness, stage_timeout=100.0, max_attempts=2)
    harness.clock.advance(101)
    with pytest.raises(InvalidStageTransition) as ei:
        harness.report("dev1", jid, "downloading", 1)
    assert ei.value.reason == "same_stage_repeated_retry"
    job = harness.svc.store.jobs[jid]
    assert job.attempts == 1
    harness.clock.advance(101)
    harness.svc.sweep()
    assert harness.svc.store.jobs[jid].stage is JobStage.SUSPENDED


def test_suspended_device_does_not_block_other_batches(harness):
    """一台设备被搁置不会卡死发布：其他设备照常成功，发布状态可解释。"""
    for d in ("dev1", "dev2"):
        harness.reg(d)
    harness.make_fw("fw-new", "2.0.0")
    rel = harness.svc.create_release({"target_fw_id": "fw-new", "batch_size": 2,
                                      "failure_threshold": 1.0,
                                      "stage_timeout": 100.0,
                                      "max_stage_attempts": 1})["release"]["release_id"]
    harness.svc.start_release(rel)
    harness.svc.plan_next_batch(rel)
    j1 = harness.claim("dev1")["job"]["job_id"]
    j2 = harness.claim("dev2")["job"]["job_id"]
    harness.clock.advance(200)
    harness.svc.sweep()  # dev1、dev2 都搁置
    assert harness.svc.store.jobs[j1].stage is JobStage.SUSPENDED
    # dev2 重连后完成
    harness.hb("dev2", 1)
    harness.drive_success("dev2", j2, seq0=10)
    stats = harness.svc.get_release(rel)["stats"]
    assert stats["suspended"] == 1 and stats["confirmed"] == 1
    harness.log("搁置不阻塞批次", stats=stats)
