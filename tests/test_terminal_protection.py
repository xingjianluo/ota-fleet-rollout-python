"""已定局设备的后续上报保护：搁置/完成/回滚/失败后再来失败上报不得改写定局。

覆盖：
- 搁置设备再发“只报失败、不带阶段”的上报（seq 更大）被明确拒绝，
  状态、搁置原因、发布汇总搁置计数全部保持不变；
- confirmed / rolled_back / failed 设备的失败上报保护与幂等重放；
- 被拒绝的上报不影响批次容量与后续批次推进；
- 失败上报与停滞巡检（搁置/补救）并发时，终局与汇总计数只落一次；
- 经 HTTP 的“搁置 -> 只报失败”链路同样被拒。
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
    c.firmwares.create("fw1-back", "1.0.0", "A1", "h1", ["2.0.0"],
                       allow_downgrade=True)
    # 阈值 1.0：单台失败/搁置即 100% 时会自动停止（既有行为），
    # 相关用例需要继续推进时显式 resume
    c.rollouts.create("r1", "fw2", ["d0", "d1", "d2", "d3"],
                      batch_size=2, failure_rate_threshold=1.0)
    c.rollouts.start("r1")
    yield c, clock, tokens
    reset()


def claim(c, tokens, d):
    c.rollouts.claim("r1", d, tokens[d])


def shelve_device(c, clock, tokens, d, *, start_seq=1):
    """让设备停滞 -> 补救 -> 再停滞 -> 搁置。"""
    claim(c, tokens, d)
    c.progress.report(d, tokens[d], "r1", "downloaded", start_seq)
    for k in range(1, 8):
        clock.t += 10
        c.progress.report(d, tokens[d], "r1", "downloaded", start_seq + k)
    assert c.progress.watchdog("r1")["retried"] == [d]
    c.progress.report(d, tokens[d], "r1", "downloaded", start_seq + 20)
    for k in range(1, 8):
        clock.t += 10
        c.progress.report(d, tokens[d], "r1", "downloaded",
                          start_seq + 20 + k)
    assert c.progress.watchdog("r1")["shelved"] == [d]
    st = c.store.update_states[(d, "r1")]
    assert st.stage == UpdateStage.SHELVED
    return st


def test_shelved_failure_report_rejected_state_and_summary_unchanged(ctx):
    """搁置设备再发更大 seq 的只失败上报：拒绝，定局与汇总保持原样。"""
    c, clock, tokens = ctx
    claim(c, tokens, "d1")  # 同批另一台，先不收敛
    st = shelve_device(c, clock, tokens, "d0")
    shelved_reason = st.last_error
    accepted_seq = st.stage_seq

    # 搁置结论先体现在汇总里
    stall_before = c.rollouts.summary("r1")["stall"]
    assert stall_before["shelved"] == 1
    assert "d0" in stall_before["reasons"]["shelved"]
    batch = c.store.rollouts["r1"].batches[0]
    failed_before, succeeded_before = list(batch.failed), list(batch.succeeded)

    # 再发一条“只报失败、不带阶段”的上报（编号更大）
    with pytest.raises(StageTransitionError) as ei:
        c.progress.report_failure("d0", tokens["d0"], "r1",
                                  accepted_seq + 100, "late failure")
    assert ei.value.details["reason"] == "device_shelved"
    assert ei.value.details["shelved_reason"] == shelved_reason

    # 状态、搁置原因、游标一律不动
    st = c.store.update_states[("d0", "r1")]
    assert st.stage == UpdateStage.SHELVED
    assert st.last_error == shelved_reason
    assert st.stage_seq == accepted_seq

    # 汇总搁置计数与原因不消失
    stall_after = c.rollouts.summary("r1")["stall"]
    assert stall_after == stall_before

    # 批次计数不跳变
    assert batch.failed == failed_before
    assert batch.succeeded == succeeded_before

    # 拒绝留痕，且没有产生新的 device_failed
    kinds = [e.kind for e in c.audit.all()]
    rejects = [e for e in c.audit.all()
               if e.kind == "progress_rejected"
               and e.detail.get("device_id") == "d0"
               and e.detail.get("reason") == "device_shelved"
               and e.detail.get("input_stage") == "failed"]
    assert rejects
    assert kinds.count("device_failed") == 0


def test_shelved_failure_report_same_or_lower_seq_also_rejected(ctx):
    """搁置设备无论 seq 大小，失败上报都被拒（不再依赖 seq 判定）。"""
    c, clock, tokens = ctx
    st = shelve_device(c, clock, tokens, "d0")
    for seq in (st.stage_seq, st.stage_seq - 1, 1, st.stage_seq + 5):
        with pytest.raises(StageTransitionError) as ei:
            c.progress.report_failure("d0", tokens["d0"], "r1", seq, "x")
        assert ei.value.details["reason"] == "device_shelved", seq
    assert c.store.update_states[("d0", "r1")].stage == UpdateStage.SHELVED


def test_confirmed_then_failure_report_rejected(ctx):
    """正常完成的设备再来失败上报：终态锁定，不丢成功计数。"""
    c, _, tokens = ctx
    claim(c, tokens, "d0")
    for i, stg in enumerate(["downloaded", "installed", "rebooted",
                             "confirmed"], start=1):
        c.progress.report("d0", tokens["d0"], "r1", stg, i)

    with pytest.raises(StageTransitionError) as ei:
        c.progress.report_failure("d0", tokens["d0"], "r1", 99, "late")
    assert ei.value.details["reason"] == "terminal_stage_locked"
    assert ei.value.details["stage"] == "confirmed"

    st = c.store.update_states[("d0", "r1")]
    assert st.stage == UpdateStage.CONFIRMED and st.stage_seq == 4
    b = c.store.rollouts["r1"].batches[0]
    assert b.succeeded == ["d0"] and b.failed == []
    assert c.store.devices["d0"].fw_version == "2.0.0"  # 提交不被回滚


def test_rolled_back_then_failure_report_rejected(ctx):
    """整批/单设备回滚后再来失败上报：终态锁定，回滚结论保留。"""
    c, _, tokens = ctx
    claim(c, tokens, "d0")
    for i, stg in enumerate(["downloaded", "installed", "rebooted",
                             "confirmed"], start=1):
        c.progress.report("d0", tokens["d0"], "r1", stg, i)
    res = c.rollbacks.rollback_device("d0", "1.0.0", idem_key="rb1")
    assert res["result"] == "completed"
    assert c.store.update_states[("d0", "r1")].stage == \
        UpdateStage.ROLLED_BACK

    with pytest.raises(StageTransitionError) as ei:
        c.progress.report_failure("d0", tokens["d0"], "r1", 50, "boom")
    assert ei.value.details["reason"] == "terminal_stage_locked"
    st = c.store.update_states[("d0", "r1")]
    assert st.stage == UpdateStage.ROLLED_BACK
    assert c.store.devices["d0"].fw_version == "1.0.0"
    b = c.store.rollouts["r1"].batches[0]
    assert b.failed == ["d0"]  # 回滚计入失败且只计一次


def test_failed_replay_is_idempotent_not_recounted(ctx):
    """已定局 FAILED：重发同一条失败（相同 seq）幂等；更大 seq 被拒。"""
    c, _, tokens = ctx
    claim(c, tokens, "d0")
    c.progress.report("d0", tokens["d0"], "r1", "downloaded", 1)
    v = c.progress.report_failure("d0", tokens["d0"], "r1", 2, "boom")
    assert v["classification"] == "failed" and v["duplicate"] is False

    # 相同内容（相同 seq）重发：幂等返回，不重复落结果
    v2 = c.progress.report_failure("d0", tokens["d0"], "r1", 2, "boom")
    assert v2["duplicate"] is True
    assert v2["stage"] == "failed" and v2["seq"] == 2

    failed_events = [e for e in c.audit.all() if e.kind == "device_failed"]
    assert len(failed_events) == 1
    b = c.store.rollouts["r1"].batches[0]
    assert b.failed == ["d0"] and b.succeeded == []

    # 编号更大的新失败上报也不能当成新结果
    with pytest.raises(StageTransitionError) as ei:
        c.progress.report_failure("d0", tokens["d0"], "r1", 3, "again")
    assert ei.value.details["reason"] == "terminal_stage_locked"
    assert len([e for e in c.audit.all()
                if e.kind == "device_failed"]) == 1
    assert b.failed == ["d0"]


def test_non_terminal_stale_failure_seq_rejected(ctx):
    """非终态设备回退/重复 seq 的失败上报仍按 seq_regression 拒绝。"""
    c, _, tokens = ctx
    claim(c, tokens, "d0")
    c.progress.report("d0", tokens["d0"], "r1", "installed", 5)
    with pytest.raises(StaleReportError) as ei:
        c.progress.report_failure("d0", tokens["d0"], "r1", 5, "dup")
    assert ei.value.details["reason"] == "seq_regression"
    with pytest.raises(StaleReportError) as ei:
        c.progress.report_failure("d0", tokens["d0"], "r1", 4, "old")
    assert ei.value.details["reason"] == "seq_regression"
    st = c.store.update_states[("d0", "r1")]
    assert st.stage == UpdateStage.INSTALLED and st.stage_seq == 5


def test_rejected_failure_does_not_touch_capacity_or_advance(ctx):
    """被拒失败上报不占用容量、不改变批次推进节奏。"""
    c, clock, tokens = ctx
    st = shelve_device(c, clock, tokens, "d0")
    r = c.store.rollouts["r1"]
    # 单台搁置即 100% 失败率，阈值 1.0 下发布先自动停止（既有行为）
    assert r.status == RolloutStatus.AUTO_HALTED

    # 自动停止状态下，迟到失败上报同样被拒，状态/计数不变
    with pytest.raises(StageTransitionError):
        c.progress.report_failure("d0", tokens["d0"], "r1",
                                  st.stage_seq + 1, "late")
    assert r.status == RolloutStatus.AUTO_HALTED
    assert r.batches[0].failed == ["d0"]
    assert r.batches[1].status == BatchStatus.WAITING

    # 人工 resume 后 d1 正常完成 -> 批次0 收敛、批次1 开放
    c.rollouts.resume("r1")
    claim(c, tokens, "d1")
    for i, stg in enumerate(["downloaded", "installed", "rebooted",
                             "confirmed"], start=1):
        c.progress.report("d1", tokens["d1"], "r1", stg, i)
    assert r.batches[0].status == BatchStatus.DONE
    assert r.batches[1].status == BatchStatus.ACTIVE
    assert r.batches[0].succeeded == ["d1"]

    # d0 再发迟到失败上报仍被拒，批次0/1 状态与计数不变
    with pytest.raises(StageTransitionError):
        c.progress.report_failure("d0", tokens["d0"], "r1",
                                  st.stage_seq + 2, "late")
    assert r.batches[0].status == BatchStatus.DONE
    assert r.batches[1].status == BatchStatus.ACTIVE
    assert r.batches[0].failed == ["d0"]

    # 后续批次照常认领推进，被拒上报没有凭空占用槽位
    claim(c, tokens, "d2")
    assert r.batches[1].claimed == ["d2"]


def test_concurrent_failure_reports_on_shelved_device(ctx):
    """多线程同时给搁置设备发失败上报：全部拒绝，搁置结论与计数只落一次。"""
    c, clock, tokens = ctx
    st = shelve_device(c, clock, tokens, "d0")
    accepted_seq = st.stage_seq
    errors = []

    def worker(seq):
        try:
            c.progress.report_failure("d0", tokens["d0"], "r1", seq, "race")
        except Exception as exc:  # noqa: BLE001
            errors.append(getattr(exc, "code", str(exc)))

    # 20 个编号更大的并发失败上报
    seqs = [accepted_seq + k for k in range(1, 21)]
    threads = [threading.Thread(target=worker, args=(s,)) for s in seqs]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert len(errors) == 20
    assert set(errors) == {"stage_transition_rejected"}
    st = c.store.update_states[("d0", "r1")]
    assert st.stage == UpdateStage.SHELVED
    # 游标停留在搁置前最后接受的编号，失败上报没有推进它
    assert st.stage_seq == accepted_seq
    assert st.last_error  # 搁置原因仍在
    assert not [e for e in c.audit.all() if e.kind == "device_failed"]
    stall = c.rollouts.summary("r1")["stall"]
    assert stall["shelved"] == 1
    b = c.store.rollouts["r1"].batches[0]
    assert b.failed == ["d0"]


def test_concurrent_failure_report_vs_watchdog_shelve(ctx, monkeypatch):
    """失败上报与停滞巡检并发：设备最终要么 FAILED 要么 SHELVED，只落一次。

    用 Barrier 让两类线程在临界区入口处同时竞争，重复多轮消除调度偶然性。
    """
    import threading as _t

    c, clock, tokens = ctx
    results = {"failed": 0, "shelved_rejected": 0, "other": 0}
    res_lock = _t.Lock()

    def make_stalled():
        claim(c, tokens, "d0")
        c.progress.report("d0", tokens["d0"], "r1", "downloaded", 1)
        for k in range(1, 8):
            clock.t += 10
            c.progress.report("d0", tokens["d0"], "r1", "downloaded",
                              1 + k)
        # 第一轮巡检应补救；第二轮停滞就绪后进入竞争窗口
        c.progress.watchdog("r1")
        c.progress.report("d0", tokens["d0"], "r1", "downloaded", 20)
        for k in range(1, 8):
            clock.t += 10
            c.progress.report("d0", tokens["d0"], "r1", "downloaded",
                              20 + k)

    make_stalled()
    barrier = _t.Barrier(16)

    def failure_worker():
        barrier.wait()
        try:
            c.progress.report_failure("d0", tokens["d0"], "r1", 99, "race")
            with res_lock:
                results["failed"] += 1
        except StageTransitionError as exc:
            with res_lock:
                if exc.details.get("reason") == "device_shelved":
                    results["shelved_rejected"] += 1
                else:
                    results["other"] += 1
        except Exception:  # noqa: BLE001
            with res_lock:
                results["other"] += 1

    def watchdog_worker():
        barrier.wait()
        c.progress.watchdog("r1")

    threads = [_t.Thread(target=failure_worker) for _ in range(15)]
    threads.append(_t.Thread(target=watchdog_worker))
    [t.start() for t in threads]
    [t.join() for t in threads]

    st = c.store.update_states[("d0", "r1")]
    b = c.store.rollouts["r1"].batches[0]
    if st.stage == UpdateStage.SHELVED:
        # 巡检先赢：15 个失败上报全部被 device_shelved 拒绝
        assert results["failed"] == 0
        assert results["shelved_rejected"] == 15
        assert b.failed == ["d0"]
        stall = c.rollouts.summary("r1")["stall"]
        assert stall["shelved"] == 1 and "d0" in stall["reasons"]["shelved"]
    else:
        # 失败上报先赢：恰好一个 FAILED，巡检不再改动已终态设备
        assert st.stage == UpdateStage.FAILED
        assert results["failed"] == 1
        assert results["shelved_rejected"] == 0
        assert len([e for e in c.audit.all()
                    if e.kind == "device_failed"]) == 1
        assert b.failed == ["d0"]
    assert results["other"] == 0
    # 无论谁先赢，批次计数都稳定且不含重复
    assert b.succeeded == [] and b.failed.count("d0") == 1


def test_concurrent_failure_and_watchdog_across_devices_counts_once(ctx):
    """多设备并发：部分搁置、部分失败，汇总计数稳定不跳变。"""
    import threading as _t

    c, clock, tokens = ctx

    def stalled(d, base):
        claim(c, tokens, d)
        c.progress.report(d, tokens[d], "r1", "downloaded", base)
        for k in range(1, 8):
            clock.t += 10
            c.progress.report(d, tokens[d], "r1", "downloaded", base + k)

    claim(c, tokens, "d0")  # 批次0: d0(将确认), d1(竞争)
    stalled("d1", 1)
    # d1 第一轮停滞 -> 补救，再停滞到竞争窗口（max_retries=1 -> 将搁置）
    c.progress.watchdog("r1")
    c.progress.report("d1", tokens["d1"], "r1", "downloaded", 20)
    for k in range(1, 8):
        clock.t += 10
        c.progress.report("d1", tokens["d1"], "r1", "downloaded", 20 + k)
    for i, stg in enumerate(["downloaded", "installed", "rebooted",
                             "confirmed"], start=1):
        c.progress.report("d0", tokens["d0"], "r1", stg, i)

    barrier = _t.Barrier(2)
    errs = []

    def fail_d1():
        barrier.wait()
        try:
            c.progress.report_failure("d1", tokens["d1"], "r1", 99, "x")
        except Exception as exc:  # noqa: BLE001
            errs.append(getattr(exc, "code", str(exc)))

    def scan():
        barrier.wait()
        c.progress.watchdog("r1")

    t1 = _t.Thread(target=fail_d1)
    t2 = _t.Thread(target=scan)
    t1.start(); t2.start(); t1.join(); t2.join()

    st = c.store.update_states[("d1", "r1")]
    b = c.store.rollouts["r1"].batches[0]
    assert st.stage in (UpdateStage.FAILED, UpdateStage.SHELVED)
    # 再跑巡检也不会翻案
    c.progress.watchdog("r1")
    assert c.store.update_states[("d1", "r1")].stage == st.stage
    assert b.succeeded == ["d0"] and b.failed == ["d1"]
    # 批次0 已收敛 -> 批次1 开放；汇总数字自洽
    assert b.status == BatchStatus.DONE
    assert c.store.rollouts["r1"].batches[1].status == BatchStatus.ACTIVE
    stall = c.rollouts.summary("r1")["stall"]
    if st.stage == UpdateStage.SHELVED:
        assert stall["shelved"] == 1
        assert errs == ["stage_transition_rejected"]
    else:
        assert stall["shelved"] == 0 and errs == []


def test_http_shelved_device_failure_report_rejected(tmp_path):
    """HTTP 端到端：搁置后不带 stage 的失败上报返回 422 device_shelved。"""
    from fastapi.testclient import TestClient

    from api import routes
    from main import app

    clock = FakeClock()
    set_source(clock)
    c = AppContext(str(tmp_path), stage_timeout=30.0, max_retries=1,
                   stall_window=60.0)
    routes.set_ctx(c)
    try:
        client = TestClient(app)
        tok = client.post("/devices", json={
            "device_id": "d0", "model": "A1", "hw_version": "h1",
            "fw_version": "1.0.0"}).json()["token"]
        client.post("/firmwares", json={"fw_id": "fw2", "version": "2.0.0",
                                        "model": "A1", "hw_version": "h1",
                                        "compatible_from": ["1.0.0"]})
        client.post("/rollouts", json={"rollout_id": "r1", "fw_id": "fw2",
                                       "device_ids": ["d0"], "batch_size": 1,
                                       "failure_rate_threshold": 1.0})
        client.post("/rollouts/r1/start")
        client.post("/rollouts/r1/claim/d0", json={"token": tok})
        for seq in range(1, 9):
            clock.t += 10
            client.post("/rollouts/r1/report/d0",
                        json={"token": tok, "seq": seq,
                              "stage": "downloaded"})
        client.post("/rollouts/r1/watchdog")  # 补救
        for seq in range(20, 28):
            clock.t += 10
            client.post("/rollouts/r1/report/d0",
                        json={"token": tok, "seq": seq,
                              "stage": "downloaded"})
        w = client.post("/rollouts/r1/watchdog").json()  # 搁置
        assert w["shelved"] == ["d0"]

        # 只报失败、不带 stage，编号更大
        r = client.post("/rollouts/r1/report/d0",
                        json={"token": tok, "seq": 999, "error": "late fail"})
        assert r.status_code == 422
        assert r.json()["details"]["reason"] == "device_shelved"

        s = client.get("/rollouts/r1").json()
        assert s["stall"]["shelved"] == 1
        assert "d0" in s["stall"]["reasons"]["shelved"]
    finally:
        routes.set_ctx(None)
        reset()
