"""存储与审计：落盘后重建、原子持久化、事件追加与序号单调。"""
from ota.app import AppContext
from ota.clock import reset
from ota.models import Device, DevicePresence


def test_persistence_roundtrip(tmp_path):
    ctx = AppContext(str(tmp_path))
    d = Device(device_id="d1", model="A1", hw_version="h1",
               fw_version="1.0.0", token_hash="x", registered_at=1.0)
    ctx.store.devices["d1"] = d
    with ctx.store.lock:
        ctx.store.persist()

    ctx2 = AppContext(str(tmp_path))
    assert ctx2.store.devices["d1"].model == "A1"
    assert ctx2.store.devices["d1"].presence == DevicePresence.OFFLINE


def test_audit_seq_and_reload(tmp_path):
    ctx = AppContext(str(tmp_path))
    e1 = ctx.audit.record("hello", {"a": 1}, ts=1.0)
    e2 = ctx.audit.record("world", {"b": 2}, ts=2.0)
    assert e1.seq == 1 and e2.seq == 2

    ctx2 = AppContext(str(tmp_path))
    assert [e.seq for e in ctx2.audit.all()] == [1, 2]
    assert ctx2.audit.record("next", {}, ts=3.0).seq == 3
    reset()
