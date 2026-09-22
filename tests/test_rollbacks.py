"""回滚异常链路：目标不存在、半更新拒绝、重复回滚、幂等重放、整批预检失败。"""
import threading

import pytest

from ota.app import AppContext
from ota.clock import reset, set_source
from ota.errors import (
    FirmwareNotFoundError,
    RollbackDuplicateError,
    RollbackRejectedError,
)
from ota.models import UpdateStage


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
    tokens = {}
    for i in range(4):
        tokens[f"d{i}"] = c.devices.register(
            f"d{i}", "A1", "h1", "1.0.0")["token"]
    c.firmwares.create("fw2", "2.0.0", "A1", "h1", ["1.0.0"])
    # 回滚目标：允许从 2.0.0 降级回 1.0.0
    c.firmwares.create("fw1-back", "1.0.0", "A1", "h1", ["2.0.0"],
                       allow_downgrade=True)
    c.rollouts.create("r1", "fw2", ["d0", "d1", "d2", "d3"],
                      batch_size=2, failure_rate_threshold=1.0)
    c.rollouts.start("r1")
    yield c, clock, tokens
    reset()


def confirm(c, tokens, d, base=1):
    """设备认领并一路确认到 2.0.0。"""
    c.rollouts.claim("r1", d, tokens[d])
    for i, st in enumerate(["downloaded", "installed", "rebooted",
                            "confirmed"], start=base):
        c.progress.report(d, tokens[d], "r1", st, i)


def test_rollback_missing_idem_and_unknown_target(ctx):
    c, _, tokens = ctx
    confirm(c, tokens, "d0")
    with pytest.raises(RollbackRejectedError) as ei:
        c.rollbacks.rollback_device("d0", "1.0.0", idem_key="")
    assert ei.value.details["reason"] == "missing_idem_key"

    with pytest.raises(FirmwareNotFoundError):
        c.rollbacks.rollback_device("d0", "0.9.0", idem_key="k1")

    # 目标固件只存在于别的型号上 -> 对 A1 设备等于不存在
    c.firmwares.create("b1", "1.1.0", "A2", "h1", ["2.0.0"],
                       allow_downgrade=True)
    with pytest.raises(FirmwareNotFoundError) as ei:
        c.rollbacks.rollback_device("d0", "1.1.0", idem_key="k1")
    assert ei.value.details["reason"] == "target_firmware_not_found"


def test_rollback_mid_update_rejected(ctx):
    c, _, tokens = ctx
    c.rollouts.claim("r1", "d0", tokens["d0"])
    c.progress.report("d0", tokens["d0"], "r1", "downloaded", 1)
    c.progress.report("d0", tokens["d0"], "r1", "installed", 2)
    with pytest.raises(RollbackRejectedError) as ei:
        c.rollbacks.rollback_device("d0", "1.0.0", idem_key="k1")
    assert ei.value.details["reason"] == "device_mid_update_not_rollbackable"
    # 半更新状态保留，没有改版本
    assert c.store.devices["d0"].fw_version == "1.0.0"
    assert c.store.update_states[("d0", "r1")].stage == UpdateStage.INSTALLED
    # 拒绝记录可追溯
    recs = [r for r in c.store.rollback_records
            if r.device_id == "d0" and r.result == "rejected"]
    assert recs and recs[0].reason == "device_mid_update_not_rollbackable"


def test_rollback_success_and_duplicate(ctx):
    c, _, tokens = ctx
    confirm(c, tokens, "d0")
    assert c.store.devices["d0"].fw_version == "2.0.0"

    res = c.rollbacks.rollback_device("d0", "1.0.0", idem_key="k1")
    assert res["result"] == "completed"
    assert c.store.devices["d0"].fw_version == "1.0.0"
    assert c.store.update_states[("d0", "r1")].stage == \
        UpdateStage.ROLLED_BACK

    # 不同 idem_key 再次回滚到同一版本 -> 重复回滚被拒
    with pytest.raises(RollbackDuplicateError) as ei:
        c.rollbacks.rollback_device("d0", "1.0.0", idem_key="k2")
    assert ei.value.details["reason"] == "already_rolled_back_to_target"

    # 相同 idem_key 重放 -> 幂等返回，不新增 completed
    replay = c.rollbacks.rollback_device("d0", "1.0.0", idem_key="k1")
    assert replay["duplicate"] is True and replay["result"] == "completed"
    done = [r for r in c.store.rollback_records
            if r.device_id == "d0" and r.result == "completed"]
    assert len(done) == 1


def test_rollback_target_rules(ctx):
    c, _, tokens = ctx
    confirm(c, tokens, "d0")
    # 目标比当前还新 / 相同
    with pytest.raises(RollbackRejectedError) as ei:
        c.rollbacks.rollback_device("d0", "2.0.0", idem_key="kx")
    assert ei.value.details["reason"] == "already_on_target_version"

    # 未开放降级的目标固件
    c.firmwares.create("fw1-nodeg", "1.5.0", "A1", "h1", ["2.0.0"],
                       allow_downgrade=False)
    with pytest.raises(RollbackRejectedError) as ei:
        c.rollbacks.rollback_device("d0", "1.5.0", idem_key="ky")
    assert ei.value.details["reason"] == "downgrade_disallowed"


def test_batch_rollback_all_or_nothing(ctx):
    c, _, tokens = ctx
    confirm(c, tokens, "d0")
    c.rollouts.claim("r1", "d1", tokens["d1"])
    c.progress.report("d1", tokens["d1"], "r1", "downloaded", 1)
    # d1 半更新 -> 整批预检失败，d0 也不能被回滚
    with pytest.raises(RollbackRejectedError) as ei:
        c.rollbacks.rollback_batch("r1", 0, "1.0.0", idem_key="b1")
    assert ei.value.details["reason"] == "batch_precheck_blocked"
    blocked = dict(ei.value.details["blocked"])
    assert blocked.get("d1") == "device_mid_update_not_rollbackable"
    assert c.store.devices["d0"].fw_version == "2.0.0"  # 未生效

    # d1 失败定态后整批可回滚（失败设备当前仍是 1.0.0，不满足回滚条件）
    c.progress.report_failure("d1", tokens["d1"], "r1", 2, "boom")
    with pytest.raises(RollbackRejectedError) as ei:
        c.rollbacks.rollback_batch("r1", 0, "1.0.0", idem_key="b2")
    reasons = dict(ei.value.details["blocked"])
    # d1 已经在目标版本 1.0.0 -> already_on_target_version
    assert reasons["d1"] == "already_on_target_version"
    assert c.store.devices["d0"].fw_version == "2.0.0"  # 仍然未生效


def test_batch_rollback_success(ctx):
    c, _, tokens = ctx
    confirm(c, tokens, "d0")
    confirm(c, tokens, "d1", base=10)
    res = c.rollbacks.rollback_batch("r1", 0, "1.0.0", idem_key="b1")
    assert res["result"] == "completed"
    assert sorted(res["rolled_back"]) == ["d0", "d1"]
    assert all(c.store.devices[d].fw_version == "1.0.0"
               for d in ("d0", "d1"))
    b = c.store.rollouts["r1"].batches[0]
    assert set(b.failed) == {"d0", "d1"}


def test_concurrent_rollback_only_one(ctx):
    """两台请求并发回滚同一设备到同一版本：只允许一次成功。"""
    c, _, tokens = ctx
    confirm(c, tokens, "d0")
    outcomes = []

    def worker(key):
        try:
            outcomes.append(c.rollbacks.rollback_device(
                "d0", "1.0.0", idem_key=key))
        except Exception as exc:  # noqa: BLE001
            outcomes.append(exc)

    t1 = threading.Thread(target=worker, args=("k1",))
    t2 = threading.Thread(target=worker, args=("k2",))
    t1.start(); t2.start(); t1.join(); t2.join()

    def is_completed(o):
        return isinstance(o, dict) and o.get("result") == "completed" \
            and not o.get("duplicate")

    completed = [o for o in outcomes if is_completed(o)]
    duplicates = [o for o in outcomes
                  if isinstance(o, RollbackDuplicateError)]
    assert len(completed) == 1
    assert len(duplicates) == 1
    done = [r for r in c.store.rollback_records
            if r.device_id == "d0" and r.result == "completed"]
    assert len(done) == 1
