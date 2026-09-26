"""停滞识别与自动续跑：观察窗口、有限补救、搁置、批次收敛、幂等与并发。"""
import pytest

from ota.app import AppContext
from ota.clock import reset, set_source
from ota.errors import StageTransitionError
from ota.models import BatchStatus, RolloutStatus, StallState, UpdateStage


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture()
def ctx(tmp_path):
    clock = FakeClock()
    set_source(clock)
    c = AppContext(str(tmp_path), stage_timeout=300.0, max_retries=1,
                   stall_window=10.0, remedy_grace=5.0)
    tokens = {}
    for i in range(6):
        tokens[f"d{i}"] = c.devices.register(
            f"d{i}", "A1", "h1", "1.0.0")["token"]
    c.firmwares.create("fw2", "2.0.0", "A1", "h1", ["1.0.0"])
    # 阈值 1.0：仅搁置/失败不触发自动停止，批次可正常收敛推进
    c.rollouts.create("r1", "fw2", ["d0", "d1", "d2", "d3", "d4", "d5"],
                      batch_size=3, failure_rate_threshold=0.6)
    c.rollouts.start("r1")
    yield c, clock, tokens
    reset()


def claim(c, tokens, d, rid="r1"):
    c.rollouts.claim(rid, d, tokens[d])

def test_stuck_same_stage_then_shelved_and_batch_converges(ctx):
    """设备反复上报同一阶段（seq 新鲜）仍被判停滞；搁置后批次继续收敛。"""
    c, clock, tokens = ctx
    for d in ["d0", "d1", "d2"]:
        claim(c, tokens, d)
    # d0 进入 downloaded 后不断重复同阶段、seq 持续增长
    c.progress.report("d0", tokens["d0"], "r1", "downloaded", 1)
    clock.t += 1
    v = c.progress.report("d0", tokens["d0"], "r1", "downloaded", 2)
    assert v["report_class"] == "same_stage_within_window"
    assert v["stall_state"] == "idle"

    # 超过观察窗口：新鲜上报也被标为 observing（不移动停滞锚点）
    clock.t += 10
    v = c.progress.report("d0", tokens["d0"], "r1", "downloaded", 3)
    assert v["report_class"] == "same_stage_observing"
    st = c.store.update_states[("d0", "r1")]
    assert st.stall_state == StallState.OBSERVING

    # d1/d2 在巡检前一刻才上报 downloaded（新鲜，不进观察），
    # 同时保证 d0 搁置时批次失败率分母为 3
    c.progress.report("d1", tokens["d1"], "r1", "downloaded", 1)
    c.progress.report("d2", tokens["d2"], "r1", "downloaded", 1)
    # d1 立刻走完，保证停滞窗口内它不被误判
    for i, stg in enumerate(["installed", "rebooted", "confirmed"], start=2):
        c.progress.report("d1", tokens["d1"], "r1", stg, i)
    # 巡检：窗口内重复扫描幂等，不重复补救
    assert c.progress.stall_sweep("r1") == {"observing": [], "remedied": [],
                                            "shelved": []}
    clock.t += 6  # 过 remedy_grace
    w = c.progress.stall_sweep("r1")
    assert w["remedied"] == ["d0"] and w["shelved"] == []
    st = c.store.update_states[("d0", "r1")]
    assert st.stage == UpdateStage.NONE and st.retries == 1
    assert st.stall_state == StallState.REMEDIATING

    # 设备被补救后重新下载，却又卡住：再来一个观察窗口 -> 搁置
    v = c.progress.report("d0", tokens["d0"], "r1", "downloaded", 4)
    assert v["report_class"] == "remedy_resumed"
    clock.t += 11
    w = c.progress.stall_sweep("r1")
    assert w["shelved"] == ["d0"]
    st = c.store.update_states[("d0", "r1")]
    assert st.stage == UpdateStage.SHELVED
    assert st.last_error == "shelved_stalled_after_remedies"

    # 已搁置不得静默回归：推进被拒（shelved_locked），同阶段重放显式标注
    with pytest.raises(StageTransitionError) as ei:
        c.progress.report("d0", tokens["d0"], "r1", "installed", 5)
    assert ei.value.details["reason"] == "shelved_locked"
    v = c.progress.report("d0", tokens["d0"], "r1", "shelved", 4)
    assert v["report_class"] == "shelved_duplicate" and v["duplicate"] is True

    # 搁置设备借失败上报改态同样被拒（不得静默脱离搁置）
    with pytest.raises(StageTransitionError) as ei:
        c.progress.report_failure("d0", tokens["d0"], "r1", 99, "boom")
    assert ei.value.details["reason"] == "shelved_locked"

    # d1/d2 正常走完：批次 0 在 d0 搁置 + d1/d2 成功后推进到 DONE，
    # 批次 1 开放；最终整次发布 completed（搁置不阻塞）
    for i, stg in enumerate(["installed", "rebooted", "confirmed"], start=2):
        c.progress.report("d2", tokens["d2"], "r1", stg, i)
    r = c.store.rollouts["r1"]
    assert r.status == RolloutStatus.IN_PROGRESS  # 1 搁置 1/2 失败 < 阈值
    assert r.batches[0].status == BatchStatus.DONE
    assert r.batches[0].failed == ["d0"]
    assert r.batches[0].succeeded == ["d1", "d2"]
    assert r.batches[1].status == BatchStatus.ACTIVE

    for d in ["d3", "d4", "d5"]:
        claim(c, tokens, d)
        for i, stg in enumerate(["downloaded", "installed", "rebooted",
                                 "confirmed"], start=1):
            c.progress.report(d, tokens[d], "r1", stg, i)
    r = c.store.rollouts["r1"]
    assert r.batches[1].status == BatchStatus.DONE
    assert r.status == RolloutStatus.COMPLETED

    # 汇总一眼可见停滞分布
    summ = c.rollouts.summary("r1")["stall"]
    assert summ["shelved"]["count"] == 1
    assert summ["shelved"]["devices"][0]["device_id"] == "d0"
    assert summ["observing"]["count"] == 0
    assert summ["remediating"]["count"] == 0

def test_repeated_and_concurrent_sweep_idempotent(ctx):
    """重复/并发停滞巡检：补救次数、搁置、批次推进都恰好一次。"""
    import threading

    c, clock, tokens = ctx
    for d in ["d0", "d1", "d2"]:
        claim(c, tokens, d)
    c.progress.report("d0", tokens["d0"], "r1", "downloaded", 1)
    # 其余两台直接成功，保证搁置时不会因失败率阈值自动停止
    for d in ["d1", "d2"]:
        c.progress.report(d, tokens[d], "r1", "downloaded", 1)
        for i, stg in enumerate(["installed", "rebooted", "confirmed"],
                                start=2):
            c.progress.report(d, tokens[d], "r1", stg, i)

    clock.t += 11  # 超过观察窗口

    def parallel_sweep(n=8):
        results, barrier = [], threading.Barrier(n)

        def worker():
            barrier.wait()
            results.append(c.progress.stall_sweep("r1"))

        threads = [threading.Thread(target=worker) for _ in range(n)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        return results

    # 第 1 轮并发：恰好一个线程标记 observing，其余无动作
    r1 = parallel_sweep()
    assert sum(bool(x["observing"]) for x in r1) == 1
    assert all(not x["remedied"] and not x["shelved"] for x in r1)
    st = c.store.update_states[("d0", "r1")]
    assert st.stall_state == StallState.OBSERVING and st.retries == 0

    clock.t += 6  # 过 remedy_grace
    # 第 2 轮并发：恰好一次补救，retries 只增到 1
    r2 = parallel_sweep()
    assert sum(x["remedied"] == ["d0"] for x in r2) == 1
    st = c.store.update_states[("d0", "r1")]
    assert st.stage == UpdateStage.NONE and st.retries == 1
    assert st.stall_state == StallState.REMEDIATING
    # 再扫没有重复补救
    assert c.progress.stall_sweep("r1")["remedied"] == []

    clock.t += 11  # 补救后仍未续跑，超过一个观察窗口
    # 第 3 轮并发：恰好一次搁置
    r3 = parallel_sweep()
    assert sum(x["shelved"] == ["d0"] for x in r3) == 1
    st = c.store.update_states[("d0", "r1")]
    assert st.stage == UpdateStage.SHELVED and st.retries == 1
    assert st.last_error == "shelved_stalled_after_remedies"

    kinds = [e.kind for e in c.audit.all()]
    assert kinds.count("stall_observed") == 1
    assert kinds.count("stall_remedy") == 1
    assert kinds.count("stall_shelved") == 1

    # 批次只推进一次：DONE，且下一批只开放一次（后续再扫不重复动作）
    for _ in range(3):
        assert c.progress.stall_sweep("r1") == {"observing": [],
                                                "remedied": [],
                                                "shelved": []}
    r = c.store.rollouts["r1"]
    assert r.batches[0].status == BatchStatus.DONE
    assert r.batches[1].status == BatchStatus.ACTIVE
    opened = [e for e in c.audit.all() if e.kind == "batch_opened"
              and e.detail.get("batch_index") == 1]
    assert len(opened) == 1

def test_same_stage_report_classification(ctx):
    """同阶段新 seq 上报按观察窗口给出可区分反馈；过期上报单独分类。"""
    from ota.errors import StaleReportError

    c, clock, tokens = ctx
    claim(c, tokens, "d0")
    c.progress.report("d0", tokens["d0"], "r1", "downloaded", 10)

    # seq 回退：过期/重放，独立分类（与停滞无关）
    with pytest.raises(StaleReportError) as ei:
        c.progress.report("d0", tokens["d0"], "r1", "downloaded", 9)
    assert ei.value.details["reason"] == "seq_regression"

    # seq 相同、阶段相同：幂等重复
    v = c.progress.report("d0", tokens["d0"], "r1", "downloaded", 10)
    assert v["report_class"] == "same_stage_duplicate" and v["duplicate"] is True

    clock.t += 5
    v = c.progress.report("d0", tokens["d0"], "r1", "downloaded", 11)
    assert v["report_class"] == "same_stage_within_window"

    clock.t += 6  # 累计 11 > window 10
    v = c.progress.report("d0", tokens["d0"], "r1", "downloaded", 12)
    assert v["report_class"] == "same_stage_observing"

    # 补救后再回到同阶段：remedying 分类
    clock.t += 6
    c.progress.stall_sweep("r1")  # 触发补救 -> NONE
    v = c.progress.report("d0", tokens["d0"], "r1", "downloaded", 13)
    assert v["report_class"] == "remedy_resumed"
    v = c.progress.report("d0", tokens["d0"], "r1", "downloaded", 14)
    assert v["report_class"] == "same_stage_remedying"

    # 真正推进：分类回到 progress，停滞簿记清空
    v = c.progress.report("d0", tokens["d0"], "r1", "installed", 15)
    assert v["report_class"] == "progress"
    assert v["stall_state"] == "idle"

def test_watchdog_and_stall_share_remedy_budget(ctx):
    """看门狗补救与停滞补救共享 retries：合计不超过上限，搁置只一次。"""
    c, clock, tokens = ctx
    claim(c, tokens, "d0")
    c.progress.report("d0", tokens["d0"], "r1", "downloaded", 1)

    # 看门狗先触发（stage_timeout=300 是 fixture 配置；这里单独造短超时服务）
    c.progress.stage_timeout = 30.0
    clock.t += 31
    w = c.progress.watchdog("r1")
    assert w["retried"] == ["d0"]
    st = c.store.update_states[("d0", "r1")]
    assert st.retries == 1 and st.stall_state == StallState.REMEDIATING

    # 设备重新下载后又卡住：停滞巡检不得再给补救，应直接搁置
    c.progress.report("d0", tokens["d0"], "r1", "downloaded", 2)
    clock.t += 100
    w = c.progress.stall_sweep("r1")
    assert w["remedied"] == [] and w["shelved"] == ["d0"]
    st = c.store.update_states[("d0", "r1")]
    assert st.retries == 1 and st.stage == UpdateStage.SHELVED

    # 再看门狗也不会重复处理
    assert c.progress.watchdog("r1") == {"retried": [], "shelved": []}

def test_silent_after_remedy_eventually_shelved(ctx):
    """设备补救回 NONE 后彻底不再上报：停滞巡检继续兜底直至搁置。"""
    c, clock, tokens = ctx
    claim(c, tokens, "d0")
    c.progress.report("d0", tokens["d0"], "r1", "downloaded", 1)
    clock.t += 11
    assert c.progress.stall_sweep("r1")["observing"] == ["d0"]
    clock.t += 6
    assert c.progress.stall_sweep("r1")["remedied"] == ["d0"]
    assert c.store.update_states[("d0", "r1")].stage == UpdateStage.NONE
    # 设备彻底静默（心跳也没有）；再来一个观察窗口仍被搁置
    clock.t += 11
    w = c.progress.stall_sweep("r1")
    assert w["shelved"] == ["d0"]
    assert c.store.update_states[("d0", "r1")].stage == UpdateStage.SHELVED

def test_sweep_scale_hundreds_of_devices(tmp_path):
    """数百台设备：巡检在线性可接受时间内完成，且不误伤新鲜设备。"""
    import time

    class Clock:
        def __init__(self): self.t = 100.0
        def __call__(self): return self.t

    clock = Clock()
    set_source(clock)
    try:
        n = 200
        c = AppContext(str(tmp_path), max_retries=1, stall_window=10.0,
                       remedy_grace=10.0, stage_timeout=1000.0)
        ids = [f"dev{i}" for i in range(n)]
        toks = {d: c.devices.register(d, "A1", "h1", "1.0.0")["token"]
                for d in ids}
        c.firmwares.create("fw2", "2.0.0", "A1", "h1", ["1.0.0"])
        c.rollouts.create("big", "fw2", ids, batch_size=n,
                          failure_rate_threshold=1.0)
        c.rollouts.start("big")
        for d in ids:
            c.rollouts.claim("big", d, toks[d])
            c.progress.report(d, toks[d], "big", "downloaded", 1)

        # 前 50 台卡死（同阶段新鲜上报，seq 增长但阶段不动）；
        # 其余 150 台在跳钟后立刻完整走完（保持新鲜不被误伤）
        clock.t += 11
        for d in ids[:50]:  # 同阶段新鲜上报：惰性进入 observing
            c.progress.report(d, toks[d], "big", "downloaded", 2)
        for d in ids[50:]:
            for i, stg in enumerate(["installed", "rebooted",
                                     "confirmed"], start=2):
                c.progress.report(d, toks[d], "big", stg, i)
        summ0 = c.rollouts.summary("big")["stall"]
        assert summ0["observing"]["count"] == 50  # 惰性标记已生效
        t0 = time.perf_counter()
        w1 = c.progress.stall_sweep("big")      # 已观察，无新动作
        clock.t += 11
        w2 = c.progress.stall_sweep("big")      # 补救
        clock.t += 11
        w3 = c.progress.stall_sweep("big")      # 搁置
        elapsed = time.perf_counter() - t0

        assert w1 == {"observing": [], "remedied": [], "shelved": []}
        assert sorted(w2["remedied"]) == sorted(ids[:50])
        assert sorted(w3["shelved"]) == sorted(ids[:50])
        assert elapsed < 2.0  # 三轮全量扫描 200 台应远低于 2s

        summ = c.rollouts.summary("big")["stall"]
        assert summ["shelved"]["count"] == 50
        assert summ["observing"]["count"] == 0
        assert summ["remediating"]["count"] == 0

        r = c.store.rollouts["big"]
        assert r.status == RolloutStatus.COMPLETED
    finally:
        reset()

def test_concurrent_same_stage_reports_stable(ctx):
    """大量并发同阶段上报：分类一致、游标正确、不重复触发停滞动作。"""
    import threading

    c, clock, tokens = ctx
    claim(c, tokens, "d0")
    c.progress.report("d0", tokens["d0"], "r1", "downloaded", 1)
    clock.t += 11  # 过观察窗口

    views, errors = [], []
    barrier = threading.Barrier(16)

    def worker(seq):
        try:
            barrier.wait()
            views.append(c.progress.report(
                "d0", tokens["d0"], "r1", "downloaded", seq))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(s,))
               for s in range(2, 18)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    # 乱序并发下低 seq 被分类拒绝（seq_regression），高水位生效
    assert all(getattr(e, "details", {}).get("reason") == "seq_regression"
               for e in errors)
    st = c.store.update_states[("d0", "r1")]
    assert st.stage_seq == 17 and st.stage == UpdateStage.DOWNLOADED
    # 被接受的同阶段上报都分类为“观察中”，且 observing 标记只发生一次
    assert views and all(v["report_class"] == "same_stage_observing"
                         for v in views)
    assert st.stall_state == StallState.OBSERVING
    assert sum(e.kind == "stall_observed" for e in c.audit.all()) == 1


def test_stall_state_survives_restart(tmp_path):
    """停滞簿记随 store.json 持久化，进程重建后判定继续生效。"""
    clock = FakeClock()
    set_source(clock)
    try:
        c = AppContext(str(tmp_path), stage_timeout=300.0, max_retries=1,
                       stall_window=10.0, remedy_grace=5.0)
        tok = c.devices.register("d0", "A1", "h1", "1.0.0")["token"]
        c.firmwares.create("fw2", "2.0.0", "A1", "h1", ["1.0.0"])
        c.rollouts.create("r1", "fw2", ["d0"], batch_size=1,
                          failure_rate_threshold=0.6)
        c.rollouts.start("r1")
        c.rollouts.claim("r1", "d0", tok)
        c.progress.report("d0", tok, "r1", "downloaded", 1)
        clock.t += 11
        c.progress.stall_sweep("r1")  # observing

        # 模拟进程重启：全新 AppContext 读同一目录
        c2 = AppContext(str(tmp_path), stage_timeout=300.0, max_retries=1,
                        stall_window=10.0, remedy_grace=5.0)
        st = c2.store.update_states[("d0", "r1")]
        assert st.stall_state == StallState.OBSERVING
        assert st.stall_reason == "stalled_no_progress"
        clock.t += 6
        w = c2.progress.stall_sweep("r1")
        assert w["remedied"] == ["d0"]  # 重启后继续按原计时补救
    finally:
        reset()
