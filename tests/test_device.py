"""设备注册与心跳异常链路。

重点：重复注册、伪造身份、过期/重复心跳都不得静默覆盖已有状态，
且原因码必须可区分。
"""

from __future__ import annotations

import pytest

from ota.service import OtaService

from ota.errors import (
    DeviceAlreadyRegistered,
    InvalidDeviceIdentity,
    StaleHeartbeat,
)


def test_register_then_duplicate_same_identity_rejected(harness):
    r = harness.reg("sensor-1")
    assert r["token"]
    with pytest.raises(DeviceAlreadyRegistered) as ei:
        harness.reg("sensor-1")
    assert ei.value.reason == "identity_conflict"
    harness.log("重复注册(同身份)被拒绝", reason=ei.value.reason)
    # 既有状态未被覆盖：心跳计数与令牌仍然有效
    harness.hb("sensor-1", 1)
    dev = harness.svc.get_device("sensor-1")["device"]
    assert dev["last_heartbeat_seq"] == 1


def test_register_tampered_identity_distinct_reason(harness):
    harness.reg("sensor-2", model="M1", hw="h1")
    # 伪造不同型号/硬件信息再次注册同一 ID
    with pytest.raises(DeviceAlreadyRegistered) as ei:
        harness.reg("sensor-2", model="M-FORGE", hw="h1")
    assert ei.value.reason == "identity_tampered"
    harness.log("重复注册(身份不一致)被拒绝", reason=ei.value.reason)
    dev = harness.svc.get_device("sensor-2")["device"]
    assert dev["model"] == "M1"  # 既有型号未被污染


def test_register_missing_fields_and_bad_version(harness):
    with pytest.raises(InvalidDeviceIdentity) as ei:
        harness.svc.register_device({"device_id": "", "model": "M1",
                                     "hw_version": "h1",
                                     "current_fw_version": "1.0.0"})
    assert ei.value.reason == "missing_identity_field"
    with pytest.raises(InvalidDeviceIdentity) as ei:
        harness.svc.register_device({"device_id": "x", "model": "M1",
                                     "hw_version": "h1",
                                     "current_fw_version": "vX"})
    assert ei.value.reason == "bad_version"


def test_forged_token_rejected_and_state_untouched(harness):
    harness.reg("sensor-3")
    harness.hb("sensor-3", 1)
    with pytest.raises(InvalidDeviceIdentity) as ei:
        harness.svc.heartbeat({"device_id": "sensor-3",
                               "token": "tok-forged", "seq": 2})
    assert ei.value.reason == "bad_token"
    # 伪造心跳不得推进心跳序号/时间
    dev = harness.svc.get_device("sensor-3")["device"]
    assert dev["last_heartbeat_seq"] == 1
    harness.log("伪造令牌被拒绝，既有 seq 保持", seq=dev["last_heartbeat_seq"])


def test_stale_and_duplicate_heartbeat_distinct_reasons(harness):
    harness.reg("sensor-4")
    harness.hb("sensor-4", 10)
    harness.hb("sensor-4", 11)
    # 过期：seq 回退
    with pytest.raises(StaleHeartbeat) as ei:
        harness.hb("sensor-4", 9)
    assert ei.value.reason == "seq_regressed"
    # 重复：seq 相同
    with pytest.raises(StaleHeartbeat) as ei:
        harness.hb("sensor-4", 11)
    assert ei.value.reason == "seq_duplicate"
    dev = harness.svc.get_device("sensor-4")["device"]
    assert dev["last_heartbeat_seq"] == 11
    harness.log("过期/重复心跳被区分拒绝", accepted_seq=dev["last_heartbeat_seq"])


def test_unknown_device_heartbeat(harness):
    from ota.errors import DeviceNotFound
    with pytest.raises(DeviceNotFound):
        harness.svc.heartbeat({"device_id": "ghost", "token": "x", "seq": 0})


def test_persistence_survives_restart(harness):
    """掉电/重启恢复：重新加载后设备与心跳状态一致。"""
    harness.reg("sensor-5")
    harness.hb("sensor-5", 3)
    svc2 = OtaService(harness.svc.store, harness.clock)
    dev = svc2.get_device("sensor-5")["device"]
    assert dev["last_heartbeat_seq"] == 3
    # 令牌哈希仍然可校验原令牌
    svc2.heartbeat({"device_id": "sensor-5",
                    "token": harness.tokens["sensor-5"], "seq": 4})
