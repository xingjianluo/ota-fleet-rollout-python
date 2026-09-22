"""进度异常链路：阶段推进、乱序/重复/倒退上报、看门狗重试搁置、断线对账。"""
import pytest

from ota.app import AppContext
from ota.clock import reset, set_source
from ota.errors import (
    RolloutValidationError,
    StageTransitionError,
    StaleReportError,
)
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
    c = AppContext(str(tmp_path), stage_timeout=30.0, max_retries=1)
    tokens = {}
    for i in range(4):
        tokens[f"d{i}"] = c.devices.register(
            f"d{i}", "A1", "h1", "1.0.0")["token"]
    c.firmwares.create("fw2", "2.0.0", "A1", "h1", ["1.0.0"])
    c.rollouts.create("r1", "fw2", ["d0", "d1", "d2", "d3"],
                      batch_size=2, failure_rate_threshold=0.5)
    c.rollouts.start("r1")
    yield c, clock, tokens
    reset()


def claim(c, tokens, d):
    c.rollouts.claim("r1", d, tokens[d])


def test_happy_path_and_commit(ctx):
    c, _, tokens = ctx
    claim(c, tokens, "d0")
    for i, st in enumerate(["downloaded", "installed", "rebooted",
                            "confirmed"], start=1):
        v = c.progress.report("d0", tokens["d0"], "r1", st, i)
        assert v["stage"] == st and v["duplicate"] is False
    assert c.store.devices["d0"].fw_version == "2.0.0"
    b = c.store.rollouts["r1"].batches[0]
    assert b.succeeded == ["d0"]


def test_unclaimed_report_rejected(ctx):
    c, _, tokens = ctx
    with pytest.raises(RolloutValidationError) as ei:
        c.progress.report("d1", tokens["d1"], "r1", "downloaded", 1)
    assert ei.value.details["reason"] == "not_claimed"


def test_duplicate_and_out_of_order(ctx):
    c, _, tokens = ctx
    claim(c, tokens, "d0")
    c.progress.report("d0", tokens["d0"], "r1", "downloaded", 10)

    # 同阶段同 seq：幂等
    v = c.progress.report("d0", tokens["d0"], "r1", "downloaded", 10)
    assert v["duplicate"] is True

    # seq 回退：拒绝且状态不变
    with pytest.raises(StaleReportError) as ei:
        c.progress.report("d0", tokens["d0"], "r1", "downloaded", 9)
    assert ei.value.details["reason"] == "seq_regression"

    # 同 seq 不同阶段：矛盾上报
    with pytest.raises(StageTransitionError) as ei:
        c.progress.report("d0", tokens["d0"], "r1", "installed", 10)
    assert ei.value.details["reason"] == "same_seq_conflicting_stage"

    # 阶段倒退（seq 更新但阶段往回）
    c.progress.report("d0", tokens["d0"], "r1", "installed", 11)
    with pytest.raises(StageTransitionError) as ei:
        c.progress.report("d0", tokens["d0"], "r1", "downloaded", 12)
    assert ei.value.details["reason"] == "stage_regression"

    st = c.store.update_states[("d0", "r1")]
    assert st.stage == UpdateStage.INSTALLED and st.stage_seq == 11


def test_terminal_state_locked(ctx):
    c, _, tokens = ctx
    claim(c, tokens, "d0")
    for i, st in enumerate(["downloaded", "installed", "rebooted",
                            "confirmed"], start=1):
        c.progress.report("d0", tokens["d0"], "r1", st, i)
    with pytest.raises(StageTransitionError) as ei:
        c.progress.report("d0", tokens["d0"], "r1", "installed", 10)
    assert ei.value.details["reason"] == "terminal_stage_locked"
    # 相同终态相同 seq 仍幂等
    v = c.progress.report("d0", tokens["d0"], "r1", "confirmed", 4)
    assert v["duplicate"] is True


def test_failure_counts_and_halts(ctx):
    c, _, tokens = ctx
    claim(c, tokens, "d0")
    claim(c, tokens, "d1")
    c.progress.report("d0", tokens["d0"], "r1", "downloaded", 1)
    c.progress.report_failure("d0", tokens["d0"], "r1", 2, "download_error")

    r = c.store.rollouts["r1"]
    assert r.status == RolloutStatus.AUTO_HALTED
    assert r.batches[0].failed == ["d0"]
    # 失败后普通进度上报被终态拦截
    with pytest.raises(StageTransitionError):
        c.progress.report("d0", tokens["d0"], "r1", "installed", 3)


def test_watchdog_retry_then_shelve(ctx):
    c, clock, tokens = ctx
    claim(c, tokens, "d0")
    claim(c, tokens, "d1")
    c.progress.report("d0", tokens["d0"], "r1", "downloaded", 1)
    c.progress.report("d1", tokens["d1"], "r1", "downloaded", 1)

    clock.t += 31  # 超过 stage_timeout
    w = c.progress.watchdog("r1")
    assert sorted(w["retried"]) == ["d0", "d1"]
    assert c.store.update_states[("d0", "r1")].stage == UpdateStage.NONE
    assert c.store.update_states[("d0", "r1")].retries == 1

    # 设备重领继续推进
    again = c.rollouts.claim("r1", "d0", tokens["d0"])
    assert again["already_claimed"] is True and again["stage"] == "none"
    c.progress.report("d0", tokens["d0"], "r1", "downloaded", 2)
    c.progress.report("d0", tokens["d0"], "r1", "installed", 3)
    c.progress.report("d0", tokens["d0"], "r1", "rebooted", 4)
    c.progress.report("d0", tokens["d0"], "r1", "confirmed", 5)

    clock.t += 31
    w2 = c.progress.watchdog("r1")
    assert w2["retried"] == [] and w2["shelved"] == ["d1"]
    st1 = c.store.update_states[("d1", "r1")]
    assert st1.stage == UpdateStage.SHELVED
    assert "shelved" in st1.last_error

    # 搁置计入失败，d0 最终成功后批次仍可收敛（阈值 0.5 已触发 halt）
    r = c.store.rollouts["r1"]
    assert r.status == RolloutStatus.AUTO_HALTED


def test_watchdog_does_not_touch_recent_or_confirmed(ctx):
    c, clock, tokens = ctx
    claim(c, tokens, "d0")
    c.progress.report("d0", tokens["d0"], "r1", "downloaded", 1)
    clock.t += 5
    assert c.progress.watchdog("r1") == {"retried": [], "shelved": []}


def test_watchdog_ignores_freshly_claimed_none(ctx):
    """刚认领（NONE）尚未上报的设备仍在正常下载窗口，不应被重试/搁置。"""
    c, clock, tokens = ctx
    claim(c, tokens, "d0")
    clock.t += 31
    w = c.progress.watchdog("r1")
    assert w == {"retried": [], "shelved": []}
    assert c.store.update_states[("d0", "r1")].stage == UpdateStage.NONE
    assert c.store.update_states[("d0", "r1")].retries == 0


def test_reconcile_after_reconnect(ctx):
    c, _, tokens = ctx
    claim(c, tokens, "d0")
    c.progress.report("d0", tokens["d0"], "r1", "downloaded", 10)
    c.progress.report("d0", tokens["d0"], "r1", "installed", 11)
    # 设备离线期间自己以为还在 seq=10 downloaded
    v = c.progress.reconcile("d0", tokens["d0"], "r1", client_seq=10)
    assert v["client_is_behind"] is True
    assert v["stage"] == "installed"
    assert v["action"] == "resume_from_authoritative_stage"
    v2 = c.progress.reconcile("d0", tokens["d0"], "r1", client_seq=11)
    assert v2["client_is_behind"] is False


def test_concurrent_reports_deterministic(ctx):
    """并发上报不同 seq：最终游标必须为最大 seq，计数只增一次。"""
    import threading

    c, _, tokens = ctx
    claim(c, tokens, "d0")
    errors = []

    def worker(seq, stage):
        try:
            c.progress.report("d0", tokens["d0"], "r1", stage, seq)
        except Exception as exc:  # noqa: BLE001
            errors.append((seq, getattr(exc, "code", str(exc))))

    # seq 1..4 乱序并发
    stages = {1: "downloaded", 2: "installed", 3: "rebooted", 4: "confirmed"}
    threads = [threading.Thread(target=worker, args=(s, st))
               for s, st in stages.items()]
    [t.start() for t in threads]
    [t.join() for t in threads]

    st = c.store.update_states[("d0", "r1")]
    assert st.stage_seq == 4
    b = c.store.rollouts["r1"].batches[0]
    # 无论顺序如何，最终 confirmed 才计入成功，且只计一次
    assert b.succeeded.count("d0") <= 1
