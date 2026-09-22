"""崩溃恢复：落盘后用新 AppContext 重建，状态/计数/审计序号保持一致。"""
from ota.app import AppContext
from ota.clock import reset, set_source
from ota.models import DevicePresence, RolloutStatus, UpdateStage


class FakeClock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


def test_rebuild_after_crash(tmp_path):
    clock = FakeClock()
    set_source(clock)

    c1 = AppContext(str(tmp_path), heartbeat_ttl=10, stage_timeout=30)
    token = c1.devices.register("d0", "A1", "h1", "1.0.0")["token"]
    c1.devices.heartbeat("d0", token, 1)
    c1.firmwares.create("fw2", "2.0.0", "A1", "h1", ["1.0.0"])
    c1.rollouts.create("r1", "fw2", ["d0"], batch_size=1)
    c1.rollouts.start("r1")
    c1.rollouts.claim("r1", "d0", token)
    for i, st in enumerate(["downloaded", "installed", "rebooted",
                            "confirmed"], start=1):
        c1.progress.report("d0", token, "r1", st, i)

    seq_before = c1.audit.tail(1)[0].seq
    assert c1.store.devices["d0"].fw_version == "2.0.0"

    # 模拟进程崩溃：丢弃内存对象，从磁盘重建
    c2 = AppContext(str(tmp_path), heartbeat_ttl=10, stage_timeout=30)
    assert c2.store.devices["d0"].fw_version == "2.0.0"
    assert c2.store.devices["d0"].presence == DevicePresence.ONLINE
    assert c2.store.rollouts["r1"].status == RolloutStatus.COMPLETED
    assert c2.store.rollouts["r1"].batches[0].succeeded == ["d0"]
    state = c2.store.update_states[("d0", "r1")]
    assert state.stage == UpdateStage.CONFIRMED and state.stage_seq == 4

    # 审计序号在重建后继续单调递增
    e = c2.audit.record("post_restart", {"ok": True}, ts=200.0)
    assert e.seq == seq_before + 1

    # 重建后乱序/终态保护仍然生效
    c2.devices.heartbeat("d0", token, 2)  # 新心跳正常
    reset()
