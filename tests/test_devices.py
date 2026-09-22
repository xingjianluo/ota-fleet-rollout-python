"""设备异常链路：重复注册（两种原因）、伪造身份、过期/乱序心跳、掉线判定。"""
import logging

import pytest

from ota.app import AppContext
from ota.clock import _source, reset, set_source
from ota.errors import (
    BadDeviceTokenError,
    DeviceAlreadyRegisteredError,
    StaleHeartbeatError,
    UnknownDeviceError,
)
from ota.models import DevicePresence


class FakeClock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture()
def ctx(tmp_path):
    clock = FakeClock()
    set_source(clock)
    c = AppContext(str(tmp_path), heartbeat_ttl=10.0)
    yield c, clock
    reset()


def test_register_and_duplicate_reasons(ctx):
    c, _ = ctx
    r = c.devices.register("d1", "A1", "h1", "1.0.0")
    assert len(r["token"]) == 48

    with pytest.raises(DeviceAlreadyRegisteredError) as ei:
        c.devices.register("d1", "A1", "h1", "1.0.0")
    assert ei.value.details["reason"] == "identical_reregistration"

    with pytest.raises(DeviceAlreadyRegisteredError) as ei:
        c.devices.register("d1", "A1", "h1", "2.0.0")
    assert ei.value.details["reason"] == "identity_conflict"

    # 已有状态未被覆盖
    d = c.store.devices["d1"]
    assert d.fw_version == "1.0.0" and d.presence == DevicePresence.OFFLINE


def test_unknown_and_forged_identity(ctx):
    c, _ = ctx
    r = c.devices.register("d1", "A1", "h1", "1.0.0")
    with pytest.raises(UnknownDeviceError):
        c.devices.heartbeat("ghost", r["token"], 1)
    with pytest.raises(BadDeviceTokenError) as ei:
        c.devices.heartbeat("d1", "forged-token", 1)
    assert ei.value.code == "bad_device_token"
    assert c.store.devices["d1"].heartbeat_seq == 0


def test_heartbeat_sequence_and_stale(ctx):
    c, _ = ctx
    r = c.devices.register("d1", "A1", "h1", "1.0.0")
    ok = c.devices.heartbeat("d1", r["token"], 5)
    assert ok["presence"] == "online" and ok["reconnected"] is True

    ok2 = c.devices.heartbeat("d1", r["token"], 6)
    assert ok2["reconnected"] is False

    for bad_seq in (5, 1, 0, -3):
        with pytest.raises(StaleHeartbeatError) as ei:
            c.devices.heartbeat("d1", r["token"], bad_seq)
        assert ei.value.details["reason"] == "stale_or_duplicate_seq" \
            or ei.value.details["reason"] == "bad_seq"
    # 过期心跳不得推进 seq/时间
    d = c.store.devices["d1"]
    assert d.heartbeat_seq == 6


def test_offline_sweep(ctx):
    c, clock = ctx
    r = c.devices.register("d1", "A1", "h1", "1.0.0")
    c.devices.heartbeat("d1", r["token"], 1)
    clock.t += 9
    assert c.devices.mark_offline_sweep() == []
    clock.t += 2  # 共 11s > ttl 10s
    assert c.devices.mark_offline_sweep() == ["d1"]
    assert c.store.devices["d1"].presence == DevicePresence.OFFLINE


def test_persistence_keeps_token_hash(ctx, tmp_path):
    c, _ = ctx
    r = c.devices.register("d1", "A1", "h1", "1.0.0")
    c2 = AppContext(str(tmp_path), heartbeat_ttl=10.0)
    c2.devices.heartbeat("d1", r["token"], 1)  # 重启后 token 仍可校验
    assert c2.store.devices["d1"].heartbeat_seq == 1


def test_audit_reasons_recorded(ctx):
    c, _ = ctx
    c.devices.register("d1", "A1", "h1", "1.0.0")
    try:
        c.devices.register("d1", "A2", "h1", "1.0.0")
    except DeviceAlreadyRegisteredError:
        pass
    kinds = [e.kind for e in c.audit.all()]
    assert "device_registered" in kinds
    assert kinds.count("register_rejected") == 1
