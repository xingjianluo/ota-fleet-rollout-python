"""发布链路：创建校验、分批、暂停继续、批次顺序、并发认领容量不超。"""
import threading

import pytest

from ota.app import AppContext
from ota.clock import reset, set_source
from ota.errors import (
    BatchStateError,
    FirmwareNotFoundError,
    RolloutStateError,
    RolloutValidationError,
)
from ota.models import (
    BatchStatus,
    RolloutStatus,
    UpdateStage,
)


class FakeClock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture()
def ctx(tmp_path):
    clock = FakeClock()
    set_source(clock)
    c = AppContext(str(tmp_path))
    yield c, clock
    reset()


def _provision(c, n=6, start_ver="1.0.0", model="A1", hw="h1"):
    tokens = {}
    for i in range(n):
        tokens[f"d{i}"] = c.devices.register(
            f"d{i}", model, hw, start_ver)["token"]
    c.firmwares.create("fw2", "2.0.0", model, hw, [start_ver])
    return tokens


def test_create_validation(ctx):
    c, _ = ctx
    tokens = _provision(c, n=2)
    with pytest.raises(FirmwareNotFoundError):
        c.rollouts.create("r1", "ghost", ["d0"], batch_size=1)
    with pytest.raises(RolloutValidationError) as ei:
        c.rollouts.create("r1", "fw2", [], batch_size=1)
    assert ei.value.details["reason"] == "no_devices"
    with pytest.raises(RolloutValidationError) as ei:
        c.rollouts.create("r1", "fw2", ["d0", "d0"], batch_size=1)
    assert ei.value.details["reason"] == "duplicate_devices"

    # 含未知设备 -> 整个发布被拒绝
    with pytest.raises(RolloutValidationError) as ei:
        c.rollouts.create("r1", "fw2", ["d0", "nope"], batch_size=1)
    assert ei.value.details["reason"] == "incompatible_members"
    assert "r1" not in c.store.rollouts  # 不留半成品


def test_batch_order_and_pause_resume(ctx):
    c, _ = ctx
    tokens = _provision(c, n=4)
    s = c.rollouts.create("r1", "fw2", ["d0", "d1", "d2", "d3"],
                          batch_size=2, failure_rate_threshold=1.0)
    assert [b["capacity"] for b in s["batches"]] == [2, 2]
    c.rollouts.start("r1")

    # 第二批设备提前认领被拒
    with pytest.raises(BatchStateError) as ei:
        c.rollouts.claim("r1", "d2", tokens["d2"])
    assert ei.value.details["reason"] == "not_current_batch"

    c.rollouts.claim("r1", "d0", tokens["d0"])
    c.rollouts.pause("r1")
    with pytest.raises(BatchStateError) as ei:
        c.rollouts.claim("r1", "d1", tokens["d1"])
    assert ei.value.details["reason"] == "rollout_paused"
    c.rollouts.resume("r1")
    c.rollouts.claim("r1", "d1", tokens["d1"])


def test_claim_idempotent_on_reconnect(ctx):
    c, _ = ctx
    tokens = _provision(c, n=1)
    c.rollouts.create("r1", "fw2", ["d0"], batch_size=1)
    c.rollouts.start("r1")
    first = c.rollouts.claim("r1", "d0", tokens["d0"])
    assert first["already_claimed"] is False
    second = c.rollouts.claim("r1", "d0", token=tokens["d0"])
    assert second["already_claimed"] is True
    b = c.store.rollouts["r1"].batches[0]
    assert b.claimed == ["d0"]  # 重复认领不重复计数


def test_concurrent_claim_no_oversell(ctx):
    c, _ = ctx
    n = 8
    tokens = _provision(c, n=n)
    c.rollouts.create("r1", "fw2", [f"d{i}" for i in range(n)],
                      batch_size=4, failure_rate_threshold=1.0)
    c.rollouts.start("r1")

    errors = []
    barrier = threading.Barrier(n)

    def worker(i):
        barrier.wait()
        try:
            c.rollouts.claim("r1", f"d{i}", tokens[f"d{i}"])
        except BatchStateError as exc:
            errors.append((f"d{i}", exc.details["reason"]))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    b = c.store.rollouts["r1"].batches[0]
    assert len(b.claimed) == 4 and len(set(b.claimed)) == 4
    assert len(errors) == 4
    assert all(reason == "not_current_batch" for _, reason in errors)


def test_failure_threshold_auto_halt(ctx):
    c, _ = ctx
    tokens = _provision(c, n=4)
    c.rollouts.create("r1", "fw2", ["d0", "d1", "d2", "d3"],
                      batch_size=2, failure_rate_threshold=0.5)
    c.rollouts.start("r1")
    c.rollouts.claim("r1", "d0", tokens["d0"])
    c.rollouts.claim("r1", "d1", tokens["d1"])

    def settle(d, stage):
        st = c.store.update_states[(d, "r1")]
        st.stage = stage
        c.rollouts.record_outcome("r1", d, stage)

    settle("d0", UpdateStage.FAILED)
    settle("d1", UpdateStage.CONFIRMED)

    r = c.store.rollouts["r1"]
    assert r.status == RolloutStatus.AUTO_HALTED
    assert "失败率" in r.halted_reason
    # 后续批次未下发，且暂停态拒绝新认领
    assert r.batches[1].status == BatchStatus.WAITING
    with pytest.raises(BatchStateError) as ei:
        c.rollouts.claim("r1", "d0", tokens["d0"])  # d0 已终态
    assert ei.value.details["reason"] == "already_terminal"
    # 人工 resume 后 HALTED 批次可继续认领
    c.rollouts.resume("r1")
    assert c.store.rollouts["r1"].status == RolloutStatus.IN_PROGRESS


def test_batch_advances_when_settled(ctx):
    c, _ = ctx
    tokens = _provision(c, n=3)
    c.rollouts.create("r1", "fw2", ["d0", "d1", "d2"], batch_size=2,
                      failure_rate_threshold=1.0)
    c.rollouts.start("r1")

    def settle(d, stage):
        c.store.update_states[(d, "r1")].stage = stage
        c.rollouts.record_outcome("r1", d, stage)

    c.rollouts.claim("r1", "d0", tokens["d0"])
    c.rollouts.claim("r1", "d1", tokens["d1"])
    settle("d0", UpdateStage.CONFIRMED)
    settle("d1", UpdateStage.CONFIRMED)

    r = c.store.rollouts["r1"]
    assert r.batches[0].status == BatchStatus.DONE
    assert r.batches[1].status == BatchStatus.ACTIVE

    c.rollouts.claim("r1", "d2", tokens["d2"])
    settle("d2", UpdateStage.CONFIRMED)
    assert r.status == RolloutStatus.COMPLETED


def test_lifecycle_state_conflicts(ctx):
    c, _ = ctx
    _provision(c, n=1)
    c.rollouts.create("r1", "fw2", ["d0"], batch_size=1)
    with pytest.raises(RolloutStateError):
        c.rollouts.pause("r1")  # 未开始不能暂停
    c.rollouts.start("r1")
    with pytest.raises(RolloutStateError):
        c.rollouts.start("r1")  # 重复 start
