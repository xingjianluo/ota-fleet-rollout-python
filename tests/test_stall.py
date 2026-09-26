"""停滞识别与自动续跑：活着但不推进的设备被补救、搁置，批次正常收敛。"""
import threading

import pytest

from ota.app import AppContext
from ota.clock import reset, set_source
from ota.errors import BatchStateError, StageTransitionError
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
    # 阈值 1.0：单台搁置（失败率 0.5）不会触发自动停止
    c.rollouts.create("r1", "fw2", ["d0", "d1", "d2", "d3"],
                      batch_size=2, failure_rate_threshold=1.0)
    c.rollouts.start("r1")
    yield c, clock, tokens
    reset()


def claim(c, tokens, d):
    c.rollouts.claim("r1", d, tokens[d])


def stall_report(c, tokens, clock, d, stage, seq, repeats, step=10.0):
    """让设备在 stall_window 内持续上报同一阶段（每次 seq 递增）。"""
    view = None
    for k in range(repeats):
        clock.t += step
        view = c.progress.report(d, tokens[d], "r1", stage, seq + k)
    return view


def test_same_stage_repeat_classification(ctx):
    """同阶段重复上报被接受但明确分类，区别于推进与幂等重放。"""
    c, clock, tokens = ctx
    claim(c, tokens, "d0")
    v = c.progress.report("d0", tokens["d0"], "r1", "downloaded", 1)
    assert v["classification"] == "advanced"
    assert v["same_stage_reports"] == 0

    # 设备活着、seq 持续增长，但阶段不动
    v = c.progress.report("d0", tokens["d0"], "r1", "downloaded", 2)
    assert v["classification"] == "same_stage_repeat"
    assert v["duplicate"] is False
    assert v["same_stage_reports"] == 1

    # 汇总里能看到该设备处于停滞观察
    stall = c.rollouts.summary("r1")["stall"]
    assert stall["observing"] == 1
    assert "d0" in stall["reasons"]["observing"]

    # 真正推进后计数清零
    v = c.progress.report("d0", tokens["d0"], "r1", "installed", 3)
    assert v["classification"] == "advanced"
    assert v["same_stage_reports"] == 0
    assert c.rollouts.summary("r1")["stall"]["observing"] == 0


def test_stall_retry_then_shelve_and_batch_converges(ctx):
    """完整链路：反复上报同一阶段 -> 补救 -> 再停滞 -> 搁置 -> 批次收敛。"""
    c, clock, tokens = ctx
    claim(c, tokens, "d0")
    claim(c, tokens, "d1")

    # d0 正常推进到 confirmed
    for i, st in enumerate(["downloaded", "installed", "rebooted",
                            "confirmed"], start=1):
        c.progress.report("d0", tokens["d0"], "r1", st, i)

    # d1 停滞在 downloaded：持续上报同一阶段，updated_at 一直新鲜
    c.progress.report("d1", tokens["d1"], "r1", "downloaded", 1)
    stall_report(c, tokens, clock, "d1", "downloaded", 2, repeats=7)  # +70s

    # 超过 stall_window：第一次巡检 -> 补救（重置 NONE 续跑）
    w = c.progress.watchdog("r1")
    assert w["retried"] == ["d1"] and w["shelved"] == []
    st1 = c.store.update_states[("d1", "r1")]
    assert st1.stage == UpdateStage.NONE and st1.retries == 1
    assert st1.same_stage_reports == 0
    stall = c.rollouts.summary("r1")["stall"]
    assert stall["remedying"] == 1 and "d1" in stall["reasons"]["remedying"]

    # d1 续跑后再次停滞在同一阶段
    c.progress.report("d1", tokens["d1"], "r1", "downloaded", 20)
    stall_report(c, tokens, clock, "d1", "downloaded", 21, repeats=7)

    # 补救机会用尽 -> 搁置
    w = c.progress.watchdog("r1")
    assert w["retried"] == [] and w["shelved"] == ["d1"]
    st1 = c.store.update_states[("d1", "r1")]
    assert st1.stage == UpdateStage.SHELVED
    assert "stalled_same_stage" in st1.last_error

    # 批次 0 已收敛（d0 confirmed + d1 shelved），批次 1 自动开放
    r = c.store.rollouts["r1"]
    assert r.batches[0].status == BatchStatus.DONE
    assert r.batches[0].succeeded == ["d0"]
    assert r.batches[0].failed == ["d1"]
    assert r.batches[1].status == BatchStatus.ACTIVE

    # 搁置设备不得静默回到正常进度：上报与认领都被明确拒绝
    with pytest.raises(StageTransitionError) as ei:
        c.progress.report("d1", tokens["d1"], "r1", "installed", 30)
    assert ei.value.details["reason"] == "device_shelved"
    with pytest.raises(BatchStateError) as ei:
        c.rollouts.claim("r1", "d1", tokens["d1"])
    assert ei.value.details["reason"] == "device_shelved"

    # 汇总可一眼看出搁置及原因
    stall = c.rollouts.summary("r1")["stall"]
    assert stall["shelved"] == 1
    assert "stalled_same_stage" in stall["reasons"]["shelved"]["d1"]

    # 后续批次正常推进，整个发布最终完成
    claim(c, tokens, "d2")
    claim(c, tokens, "d3")
    for d in ("d2", "d3"):
        for i, st in enumerate(["downloaded", "installed", "rebooted",
                                "confirmed"], start=1):
            c.progress.report(d, tokens[d], "r1", st, i)
    assert c.store.rollouts["r1"].status == RolloutStatus.COMPLETED


def test_watchdog_repeat_run_is_idempotent(ctx):
    """同一窗口内重复巡检：不重复累加补救次数、不重复搁置。"""
    c, clock, tokens = ctx
    claim(c, tokens, "d0")
    c.progress.report("d0", tokens["d0"], "r1", "downloaded", 1)
    stall_report(c, tokens, clock, "d0", "downloaded", 2, repeats=7)

    w1 = c.progress.watchdog("r1")
    assert w1["retried"] == ["d0"]
    # 不推进时钟立即再巡两次：状态已被刷新，不应再次触发
    assert c.progress.watchdog("r1") == {"retried": [], "shelved": []}
    assert c.progress.watchdog("r1") == {"retried": [], "shelved": []}
    st = c.store.update_states[("d0", "r1")]
    assert st.retries == 1 and st.stage == UpdateStage.NONE
    retries_events = [e for e in c.audit.all()
                      if e.kind == "watchdog_retry"]
    assert len(retries_events) == 1


def test_concurrent_watchdog_counts_exactly_once(ctx):
    """并发巡检同一批停滞设备：补救与搁置都恰好一次。"""
    c, clock, tokens = ctx
    claim(c, tokens, "d0")
    claim(c, tokens, "d1")
    # d0 正常推进；d1 停滞
    for i, st in enumerate(["downloaded", "installed", "rebooted",
                            "confirmed"], start=1):
        c.progress.report("d0", tokens["d0"], "r1", st, i)
    c.progress.report("d1", tokens["d1"], "r1", "downloaded", 1)
    stall_report(c, tokens, clock, "d1", "downloaded", 2, repeats=7)

    def run_watchdog(results):
        results.append(c.progress.watchdog("r1"))

    # 第一轮并发巡检：d1 恰好补救一次
    results = []
    threads = [threading.Thread(target=run_watchdog, args=(results,))
               for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    total_retried = sum(len(r["retried"]) for r in results)
    assert total_retried == 1  # 恰好一次，不因并发重复累加
    assert c.store.update_states[("d1", "r1")].retries == 1

    # d1 再次停滞（max_retries=1，下一轮应搁置）
    c.progress.report("d1", tokens["d1"], "r1", "downloaded", 20)
    stall_report(c, tokens, clock, "d1", "downloaded", 21, repeats=7)

    results = []
    threads = [threading.Thread(target=run_watchdog, args=(results,))
               for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    total_shelved = sum(len(r["shelved"]) for r in results)
    assert total_shelved == 1  # 恰好搁置一次
    shelved_events = [e for e in c.audit.all()
                      if e.kind == "watchdog_shelved"]
    assert len(shelved_events) == 1
    b = c.store.rollouts["r1"].batches[0]
    assert b.failed == ["d1"] and b.succeeded == ["d0"]
    assert b.status == BatchStatus.DONE  # 全部终态，批次只推进一次
    assert c.store.rollouts["r1"].batches[1].status == BatchStatus.ACTIVE


def test_concurrent_same_stage_reports_no_double_count(ctx):
    """并发同阶段重复上报：每个新 seq 恰好计一次，重复 seq 幂等不计。"""
    c, _, tokens = ctx
    claim(c, tokens, "d0")
    c.progress.report("d0", tokens["d0"], "r1", "downloaded", 1)

    errors = []

    def worker(seq):
        try:
            c.progress.report("d0", tokens["d0"], "r1", "downloaded", seq)
        except Exception as exc:  # noqa: BLE001
            errors.append((seq, getattr(exc, "code", str(exc))))

    # 50 个并发上报：seq 2..26 各出现两次（一次新 seq、一次重复）
    seqs = [s for s in range(2, 27) for _ in (0, 1)]
    threads = [threading.Thread(target=worker, args=(s,)) for s in seqs]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert errors == []
    st = c.store.update_states[("d0", "r1")]
    assert st.stage_seq == 26
    # 25 个新 seq 的同阶段重复各计一次；重复 seq 走幂等分支不计
    assert st.same_stage_reports == 25
    assert c.rollouts.summary("r1")["stall"]["observing"] == 1


def test_shelved_device_never_returns_to_normal(ctx):
    """搁置后：普通上报、失败上报、认领都被拒，状态不倒退。"""
    c, clock, tokens = ctx
    claim(c, tokens, "d0")
    claim(c, tokens, "d1")
    c.progress.report("d0", tokens["d0"], "r1", "downloaded", 1)
    stall_report(c, tokens, clock, "d0", "downloaded", 2, repeats=7)
    c.progress.watchdog("r1")  # 补救
    c.progress.report("d0", tokens["d0"], "r1", "downloaded", 20)
    stall_report(c, tokens, clock, "d0", "downloaded", 21, repeats=7)
    c.progress.watchdog("r1")  # 搁置
    st = c.store.update_states[("d0", "r1")]
    assert st.stage == UpdateStage.SHELVED

    with pytest.raises(StageTransitionError) as ei:
        c.progress.report("d0", tokens["d0"], "r1", "downloaded", 30)
    assert ei.value.details["reason"] == "device_shelved"
    with pytest.raises(StageTransitionError) as ei:
        c.progress.report("d0", tokens["d0"], "r1", "confirmed", 31)
    assert ei.value.details["reason"] == "device_shelved"
    assert c.store.update_states[("d0", "r1")].stage == UpdateStage.SHELVED


def test_stall_scan_scales_to_hundreds(tmp_path):
    """数百台设备停滞：巡检单趟完成且耗时可控，不拖慢正常上报。"""
    import time

    clock = FakeClock()
    set_source(clock)
    n = 300
    c = AppContext(str(tmp_path), stage_timeout=30.0, max_retries=1,
                   stall_window=60.0)
    tokens = {}
    for i in range(n):
        did = f"dev{i}"
        tokens[did] = c.devices.register(did, "A1", "h1", "1.0.0")["token"]
    c.firmwares.create("fw2", "2.0.0", "A1", "h1", ["1.0.0"])
    ids = list(tokens)
    c.rollouts.create("big", "fw2", ids, batch_size=n,
                      failure_rate_threshold=1.0)
    c.rollouts.start("big")
    for did in ids:
        c.rollouts.claim("big", did, tokens[did])
        c.progress.report(did, tokens[did], "big", "downloaded", 1)
        c.progress.report(did, tokens[did], "big", "downloaded", 2)

    clock.t += 61  # 全部进入停滞
    t0 = time.monotonic()
    w = c.progress.watchdog("big")
    elapsed = time.monotonic() - t0
    assert sorted(w["retried"]) == sorted(ids)
    assert elapsed < 5.0

    # 一半设备恢复推进并确认，另一半继续停滞后被搁置：整批仍收敛
    half = n // 2
    for did in ids[:half]:
        for i, st in enumerate(["downloaded", "installed", "rebooted",
                                "confirmed"], start=3):
            c.progress.report(did, tokens[did], "big", st, i)
    for did in ids[half:]:
        c.progress.report(did, tokens[did], "big", "downloaded", 3)
    clock.t += 61
    t0 = time.monotonic()
    w = c.progress.watchdog("big")
    elapsed = time.monotonic() - t0
    assert sorted(w["shelved"]) == sorted(ids[half:])
    assert elapsed < 5.0
    r = c.store.rollouts["big"]
    assert r.status == RolloutStatus.COMPLETED
    assert len(r.batches[0].succeeded) == half
    assert len(r.batches[0].failed) == n - half
    reset()
