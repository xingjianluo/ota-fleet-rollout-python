"""定局设备保护：搁置/完成/回滚/失败后，后续失败上报不得覆盖已定结论。

覆盖：
- 搁置设备再发只报失败（seq 更大）被 device_shelved 拒绝，
  状态、搁置原因、发布汇总（shelved 计数/原因、批次失败/成功）保持不变；
- confirmed / rolled_back 设备再报失败被 terminal_stage_locked 拒绝；
- failed 设备重发同一条失败（同 seq）幂等、不重复计数，更大 seq 再报失败
  被拒绝；seq 回退按 seq_regression 拒绝；
- 多台设备并发上报失败 + 停滞巡检同时发生：定局结论只落一次，
  汇总计数不跳变；被拒绝的上报不影响批次容量与后续批次推进。
"""
import threading

import pytest

from ota.app import AppContext
from ota.clock import reset, set_source
from ota.errors import StageTransitionError, StaleReportError
from ota.models import BatchStatus, RolloutStatus, UpdateStage


class FakeClock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture()
def ctx(tmp_path):
    clock = FakeClock()
    set_source(clock)
    c = AppContext(str(tmp_path), stage_timeout=30.0, max_retries=1,
                   stall_window=60.0)
    tokens = {}
    for i in range(4):
        tokens[f"d{i}"] = c.devices.register(
            f"d{i}", "A1", "h1", "1.0.0")["token"]
    c.firmwares.create("fw2", "2.0.0", "A1", "h1", ["1.0.0"])
    # 回滚目标：允许 2.0.0 -> 1.0.0
    c.firmwares.create("fw1-back", "1.0.0", "A1", "h1", ["2.0.0"],
                       allow_downgrade=True)
    # 阈值 1.0：单台搁置/失败（失败率 0.5）不会触发自动停止
    c.rollouts.create("r1", "fw2", ["d0", "d1", "d2", "d3"],
                      batch_size=2, failure_rate_threshold=1.0)
    c.rollouts.start("r1")
    yield c, clock, tokens
    reset()


def claim(c, tokens, d):
    c.rollouts.claim("r1", d, tokens[d])


def confirm(c, tokens, d, base=1):
    claim(c, tokens, d)
    for i, st in enumerate(["downloaded", "installed", "rebooted",
                            "confirmed"], start=base):
        c.progress.report(d, tokens[d], "r1", st, i)


def shelve(c, tokens, clock, d):
    """让设备经历 停滞 -> 补救 -> 再停滞 -> 搁置。"""
    claim(c, tokens, d)
    c.progress.report(d, tokens[d], "r1", "downloaded", 1)
    for k in range(2, 9):
        clock.t += 10
        c.progress.report(d, tokens[d], "r1", "downloaded", k)
    assert c.progress.watchdog("r1")["retried"] == [d]
    c.progress.report(d, tokens[d], "r1", "downloaded", 20)
    for k in range(21, 28):
        clock.t += 10
        c.progress.report(d, tokens[d], "r1", "downloaded", k)
    assert c.progress.watchdog("r1")["shelved"] == [d]


def test_shelved_late_failure_report_rejected(ctx):
    """搁置设备再发只报失败、seq 更大：拒绝，状态/原因/汇总保持原样。"""
    c, clock, tokens = ctx
    confirm(c, tokens, "d0")
    shelve(c, tokens, clock, "d1")

    state = c.store.update_states[("d1", "r1")]
    assert state.stage == UpdateStage.SHELVED
    shelved_reason = state.last_error
    before = c.rollouts.summary("r1")
    assert before["stall"]["shelved"] == 1
    assert before["stall"]["reasons"]["shelved"]["d1"] == shelved_reason
    assert before["batches"][0]["failed"] == 1
    assert before["batches"][0]["succeeded"] == 1

    # 不带阶段、只报失败、seq 比已接受的更大（27 -> 30/99）
    for late_seq in (28, 30, 99):
        with pytest.raises(StageTransitionError) as ei:
            c.progress.report_failure("d1", tokens["d1"], "r1", late_seq,
                                      "late_boom")
        assert ei.value.details["reason"] == "device_shelved"
        assert ei.value.details["stage"] == "shelved"
        assert ei.value.details["shelved_reason"] == shelved_reason

    # 一切保持原样
    state = c.store.update_states[("d1", "r1")]
    assert state.stage == UpdateStage.SHELVED
    assert state.last_error == shelved_reason
    assert state.stage_seq == 27
    after = c.rollouts.summary("r1")
    assert after["stall"] == before["stall"]
    assert after["batches"] == before["batches"]

    # 审计：每次拒绝都可追溯，且没有任何 device_failed 事件
    kinds = [e.kind for e in c.audit.all()]
    assert kinds.count("progress_rejected") == 3
    rejected = [e for e in c.audit.all()
                if e.kind == "progress_rejected"]
    assert all(e.detail["reason"] == "device_shelved" for e in rejected)
    assert all(e.detail.get("via") == "failure_report" for e in rejected)
    assert "device_failed" not in kinds


def test_confirmed_late_failure_report_rejected(ctx):
    """正常完成的设备再报失败：拒绝，版本与成功计数不变。"""
    c, _, tokens = ctx
    confirm(c, tokens, "d0")

    with pytest.raises(StageTransitionError) as ei:
        c.progress.report_failure("d0", tokens["d0"], "r1", 99, "late")
    assert ei.value.details["reason"] == "terminal_stage_locked"
    assert ei.value.details["stage"] == "confirmed"

    st = c.store.update_states[("d0", "r1")]
    assert st.stage == UpdateStage.CONFIRMED and st.stage_seq == 4
    assert c.store.devices["d0"].fw_version == "2.0.0"
    b = c.store.rollouts["r1"].batches[0]
    assert b.succeeded == ["d0"] and b.failed == []


def test_rolled_back_late_failure_report_rejected(ctx):
    """整批回滚后的设备再报失败：拒绝，保持 ROLLED_BACK。"""
    c, _, tokens = ctx
    confirm(c, tokens, "d0")
    confirm(c, tokens, "d1", base=10)
    res = c.rollbacks.rollback_batch("r1", 0, "1.0.0", idem_key="b1")
    assert res["result"] == "completed"
    b = c.store.rollouts["r1"].batches[0]
    assert set(b.failed) == {"d0", "d1"}

    for d in ("d0", "d1"):
        with pytest.raises(StageTransitionError) as ei:
            c.progress.report_failure(d, tokens[d], "r1", 500, "late")
        assert ei.value.details["reason"] == "terminal_stage_locked"
        assert ei.value.details["stage"] == "rolled_back"
        assert c.store.update_states[(d, "r1")].stage \
            == UpdateStage.ROLLED_BACK

    # 汇总没有被后来上报改写
    after = c.rollouts.summary("r1")
    assert set(after["stall"]["reasons"]["shelved"]) == set()
    assert after["batches"][0]["failed"] == 2
    assert c.store.devices["d0"].fw_version == "1.0.0"


def test_failed_device_replay_idempotent_and_new_seq_rejected(ctx):
    """已失败设备：同 seq 失败重发幂等不重复计数；更大 seq 再报失败拒绝。"""
    c, _, tokens = ctx
    claim(c, tokens, "d0")
    claim(c, tokens, "d1")
    c.progress.report("d0", tokens["d0"], "r1", "downloaded", 1)
    v = c.progress.report_failure("d0", tokens["d0"], "r1", 2, "boom")
    assert v["classification"] == "failed" and v["duplicate"] is False

    # 同一条内容（同 seq）重发：幂等，不当作新结果
    v2 = c.progress.report_failure("d0", tokens["d0"], "r1", 2, "boom")
    assert v2["duplicate"] is True
    assert v2["stage"] == "failed" and v2["seq"] == 2

    b = c.store.rollouts["r1"].batches[0]
    assert b.failed == ["d0"] and b.failed.count("d0") == 1
    fail_events = [e for e in c.audit.all() if e.kind == "device_failed"]
    assert len(fail_events) == 1  # 重放不重复写审计

    # 更大 seq 的新失败上报：终态锁定，拒绝
    with pytest.raises(StageTransitionError) as ei:
        c.progress.report_failure("d0", tokens["d0"], "r1", 3, "again")
    assert ei.value.details["reason"] == "terminal_stage_locked"
    st = c.store.update_states[("d0", "r1")]
    assert st.stage == UpdateStage.FAILED and st.stage_seq == 2
    assert st.last_error == "boom"  # 原因也不被覆盖

    # seq 回退：按乱序拒绝
    with pytest.raises(StaleReportError) as ei:
        c.progress.report_failure("d0", tokens["d0"], "r1", 1, "old")
    assert ei.value.details["reason"] == "seq_regression"
    assert c.store.rollouts["r1"].batches[0].failed == ["d0"]


def test_concurrent_failure_reports_on_terminal_shelved_once(ctx):
    """失败上报与搁置巡检并发：搁置结论只落一次，后来失败全被拒，计数不跳。"""
    c, clock, tokens = ctx
    confirm(c, tokens, "d0")
    # d1 已处于“补救后再次停滞”窗口边缘，巡检将把它搁置
    claim(c, tokens, "d1")
    c.progress.report("d1", tokens["d1"], "r1", "downloaded", 1)
    for k in range(2, 9):
        clock.t += 10
        c.progress.report("d1", tokens["d1"], "r1", "downloaded", k)
    c.progress.watchdog("r1")  # 第一次：补救
    c.progress.report("d1", tokens["d1"], "r1", "downloaded", 20)
    for k in range(21, 28):
        clock.t += 10
        c.progress.report("d1", tokens["d1"], "r1", "downloaded", k)

    barrier = threading.Barrier(9)
    outcomes = []

    def watchdog_worker():
        barrier.wait()
        outcomes.append(("watchdog", c.progress.watchdog("r1")))

    def failure_worker(seq):
        barrier.wait()
        try:
            c.progress.report_failure("d1", tokens["d1"], "r1", seq,
                                      "concurrent_boom")
            outcomes.append((f"fail-{seq}", "accepted"))
        except Exception as exc:  # noqa: BLE001
            outcomes.append((f"fail-{seq}",
                             getattr(exc, "code", str(exc)),
                             exc.details.get("reason")))

    threads = [threading.Thread(target=watchdog_worker)]
    for seq in range(28, 36):  # 8 个并发失败上报，seq 都更大
        threads.append(threading.Thread(target=failure_worker, args=(seq,)))
    [t.start() for t in threads]
    [t.join() for t in threads]

    # 无论调度如何：d1 最终必须是 SHELVED，不能被改成 failed
    st = c.store.update_states[("d1", "r1")]
    assert st.stage == UpdateStage.SHELVED
    assert "stalled_same_stage" in st.last_error

    # 巡检恰好搁置一次
    shelved_events = [e for e in c.audit.all()
                      if e.kind == "watchdog_shelved"]
    assert len(shelved_events) == 1
    failed_events = [e for e in c.audit.all() if e.kind == "device_failed"]
    assert failed_events == []

    # 汇总稳定：shelved=1，批次失败只有 d1（搁置计入），成功只有 d0
    summary = c.rollouts.summary("r1")
    assert summary["stall"]["shelved"] == 1
    b = summary["batches"][0]
    assert b["failed"] == 1 and b["succeeded"] == 1

    # 批次照常收敛，后续批次推进；被拒上报不占容量
    r = c.store.rollouts["r1"]
    assert r.batches[0].status == BatchStatus.DONE
    assert r.batches[1].status == BatchStatus.ACTIVE
    claim(c, tokens, "d2")
    claim(c, tokens, "d3")
    for d in ("d2", "d3"):
        for i, stg in enumerate(["downloaded", "installed", "rebooted",
                                 "confirmed"], start=1):
            c.progress.report(d, tokens[d], "r1", stg, i)
    assert r.status == RolloutStatus.COMPLETED


def test_concurrent_failure_and_failure_on_active_device_once(ctx):
    """非定局设备并发多条失败上报：只有第一条生效，其余按终态锁定拒绝。"""
    c, _, tokens = ctx
    claim(c, tokens, "d0")
    claim(c, tokens, "d1")
    c.progress.report("d0", tokens["d0"], "r1", "downloaded", 1)

    barrier = threading.Barrier(8)
    accepted, rejected = [], []

    def worker(seq):
        barrier.wait()
        try:
            v = c.progress.report_failure("d0", tokens["d0"], "r1", seq,
                                          "boom")
            accepted.append((seq, v["duplicate"]))
        except Exception as exc:  # noqa: BLE001
            rejected.append((seq, exc.details.get("reason")))

    threads = [threading.Thread(target=worker, args=(s,))
               for s in range(2, 10)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    st = c.store.update_states[("d0", "r1")]
    assert st.stage == UpdateStage.FAILED
    # 恰好一个新结果被接受（seq 最小的那条），计数只落一次
    new_accepted = [a for a in accepted if a[1] is False]
    assert len(new_accepted) == 1
    b = c.store.rollouts["r1"].batches[0]
    assert b.failed == ["d0"] and b.failed.count("d0") == 1
    assert len([e for e in c.audit.all() if e.kind == "device_failed"]) == 1
    # 其余 7 条全部被明确拒绝：seq 较小的迟到包 -> seq_regression；
    # 撞上 FAILED 终态的 -> terminal_stage_locked，两种原因可区分
    assert len(rejected) == 7
    assert {reason for _, reason in rejected} <= {
        "seq_regression", "terminal_stage_locked"}
