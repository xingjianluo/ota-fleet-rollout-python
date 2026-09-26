"""本地持久化存储：单文件 JSON 原子写 + 进程内读写锁。

store.json 保存设备/固件/发布/设备升级状态/回滚记录；
审计事件由 AuditLog 单独以 JSONL 追加，保证可追溯。
所有公开操作必须在 self.lock 保护下进行（服务层持锁调用）。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict

from .models import (
    BatchStatus,
    Device,
    DevicePresence,
    DeviceUpdateState,
    Firmware,
    RollbackRecord,
    Rollout,
    RolloutBatch,
    RolloutStatus,
    StallState,
    UpdateStage,
)


def _e(enum_cls, val):
    return enum_cls(val) if not isinstance(val, enum_cls) else val


class Store:
    def __init__(self, path: str = "data/store.json"):
        self.path = path
        self.lock = threading.RLock()
        self.devices: dict[str, Device] = {}
        self.firmwares: dict[str, Firmware] = {}
        self.rollouts: dict[str, Rollout] = {}
        self.update_states: dict[tuple[str, str], DeviceUpdateState] = {}
        self.rollback_records: list[RollbackRecord] = []
        self._loaded = False

    # ------------------------------------------------------------------ load
    def ensure_loaded(self) -> None:
        with self.lock:
            if self._loaded:
                return
            self.load()
            self._loaded = True

    def load(self) -> None:
        """从磁盘载入；文件不存在时得到空存储。"""
        with self.lock:
            if not os.path.exists(self.path):
                self._loaded = True
                return
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for did, d in raw.get("devices", {}).items():
                self.devices[did] = Device(
                    device_id=d["device_id"], model=d["model"],
                    hw_version=d["hw_version"], fw_version=d["fw_version"],
                    token_hash=d["token_hash"],
                    registered_at=d["registered_at"],
                    last_heartbeat=d.get("last_heartbeat", 0.0),
                    presence=_e(DevicePresence, d.get("presence", "offline")),
                    heartbeat_seq=d.get("heartbeat_seq", 0),
                )
            for fid, fw in raw.get("firmwares", {}).items():
                self.firmwares[fid] = Firmware(
                    fw_id=fw["fw_id"], version=fw["version"],
                    model=fw["model"], hw_version=fw["hw_version"],
                    compatible_from=list(fw.get("compatible_from", [])),
                    allow_downgrade=fw.get("allow_downgrade", False),
                    created_at=fw.get("created_at", 0.0),
                )
            for rid, r in raw.get("rollouts", {}).items():
                batches = [
                    RolloutBatch(
                        index=b["index"], capacity=b["capacity"],
                        status=_e(BatchStatus, b["status"]),
                        claimed=list(b.get("claimed", [])),
                        succeeded=list(b.get("succeeded", [])),
                        failed=list(b.get("failed", [])),
                        opened_at=b.get("opened_at", 0.0),
                    ) for b in r.get("batches", [])
                ]
                self.rollouts[rid] = Rollout(
                    rollout_id=r["rollout_id"], fw_id=r["fw_id"],
                    target_version=r["target_version"], model=r["model"],
                    hw_version=r["hw_version"],
                    device_ids=list(r.get("device_ids", [])),
                    batch_size=r["batch_size"],
                    failure_rate_threshold=r["failure_rate_threshold"],
                    status=_e(RolloutStatus, r["status"]),
                    batches=batches, created_at=r.get("created_at", 0.0),
                    halted_reason=r.get("halted_reason", ""),
                )
            for key, s in raw.get("update_states", {}).items():
                did, rid = key.split("|", 1)
                self.update_states[(did, rid)] = DeviceUpdateState(
                    device_id=s["device_id"], rollout_id=s["rollout_id"],
                    stage=_e(UpdateStage, s["stage"]),
                    stage_seq=s.get("stage_seq", 0),
                    updated_at=s.get("updated_at", 0.0),
                    retries=s.get("retries", 0),
                    last_error=s.get("last_error", ""),
                    claimed_by_batch=s.get("claimed_by_batch"),
                    stage_entered_at=s.get(
                        "stage_entered_at", s.get("updated_at", 0.0)),
                    stall_state=_e(StallState,
                                   s.get("stall_state", "idle")),
                    stall_since=s.get("stall_since", 0.0),
                    stall_reason=s.get("stall_reason", ""),
                )
            for rb in raw.get("rollback_records", []):
                self.rollback_records.append(RollbackRecord(
                    id=rb["id"], ts=rb["ts"], scope=rb["scope"],
                    device_id=rb.get("device_id"),
                    rollout_id=rb.get("rollout_id"),
                    batch_index=rb.get("batch_index"),
                    from_version=rb["from_version"],
                    to_version=rb["to_version"], result=rb["result"],
                    reason=rb.get("reason", ""),
                ))
            self._loaded = True

    # --------------------------------------------------------------- persist
    def persist(self) -> None:
        """原子写：先写临时文件再 rename。必须在持锁状态下调用。"""
        payload = {
            "devices": {d.device_id: asdict(d)
                        for d in self.devices.values()},
            "firmwares": {f.fw_id: asdict(f)
                          for f in self.firmwares.values()},
            "rollouts": {r.rollout_id: asdict(r)
                         for r in self.rollouts.values()},
            "update_states": {
                f"{did}|{rid}": asdict(s)
                for (did, rid), s in self.update_states.items()
            },
            "rollback_records": [asdict(r) for r in self.rollback_records],
        }
        d = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".store-", dir=d)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
