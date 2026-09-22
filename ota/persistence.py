"""本地持久化：JSON 文件快照 + fsync 落盘，外加进程内 RLock 串行化。

所有写操作在 store 的锁内完成并原子替换文件（tmp + rename + fsync），
因此并发请求不会出现计数偏差，进程重启后状态可恢复。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from .models import (
    Batch,
    BatchState,
    Device,
    DeviceStatus,
    Firmware,
    Job,
    JobDirection,
    JobStage,
    Release,
    ReleaseState,
)


class JsonStore:
    """带锁的 JSON 快照存储。

    业务规则全部位于 service 层；本类只负责：
    1. 进程内一把 RLock（service 的每个操作整体持锁，保证复合状态迁移原子）；
    2. 快照落盘（tmp 文件 + os.replace 原子替换，目录 fsync 防丢）；
    3. 重启后把 JSON 还原成领域对象。
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.lock = threading.RLock()
        self.devices: dict[str, Device] = {}
        self.firmwares: dict[str, Firmware] = {}
        self.releases: dict[str, Release] = {}
        self.batches: dict[str, Batch] = {}
        self.jobs: dict[str, Job] = {}
        self.audit: list[dict[str, Any]] = []
        self._counters: dict[str, int] = {}

    # ------------------------------------------------------------------ ID
    def next_id(self, kind: str) -> str:
        """单调 ID（如 dev-3）。持锁调用，重启后从快照恢复计数器。"""
        n = self._counters.get(kind, 0) + 1
        self._counters[kind] = n
        prefix = {
            "device": "dev",
            "firmware": "fw",
            "release": "rel",
            "batch": "bat",
            "job": "job",
        }[kind]
        return f"{prefix}-{n}"

    # ------------------------------------------------------------ 序列化
    def to_snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "devices": {k: v.to_dict() for k, v in self.devices.items()},
                "firmwares": {k: v.to_dict() for k, v in self.firmwares.items()},
                "releases": {k: v.to_dict() for k, v in self.releases.items()},
                "batches": {k: v.to_dict() for k, v in self.batches.items()},
                "jobs": {k: v.to_dict() for k, v in self.jobs.items()},
                "audit": self.audit,
                "counters": self._counters,
            }

    def save(self) -> None:
        """原子落盘；调用方必须已持锁（与复合状态迁移在同一临界区）。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = json.dumps(self.to_snapshot(), ensure_ascii=False, sort_keys=True)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        # fsync 目录，保证 rename 在掉电场景下也可持久
        try:
            dfd = os.open(str(self.path.parent), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass

    def append_audit(self, event: dict[str, Any]) -> None:
        with self.lock:
            self.audit.append(event)
            self.save()

    def load(self) -> None:
        """从快照恢复；文件不存在则视为空库。"""
        with self.lock:
            if not self.path.exists():
                return
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.devices = {
                k: Device(
                    device_id=v["device_id"],
                    model=v["model"],
                    hw_version=v["hw_version"],
                    token_hash=v["token_hash"],
                    current_fw_version=v["current_fw_version"],
                    status=DeviceStatus(v["status"]),
                    registered_ts=v["registered_ts"],
                    last_heartbeat_ts=v.get("last_heartbeat_ts"),
                    last_heartbeat_seq=v.get("last_heartbeat_seq", 0),
                )
                for k, v in raw.get("devices", {}).items()
            }
            self.firmwares = {
                k: Firmware(
                    fw_id=v["fw_id"],
                    version=v["version"],
                    model=v["model"],
                    hw_version=v["hw_version"],
                    checksum=v["checksum"],
                    created_ts=v["created_ts"],
                    yanked=v.get("yanked", False),
                )
                for k, v in raw.get("firmwares", {}).items()
            }
            self.releases = {
                k: Release(
                    release_id=v["release_id"],
                    target_fw_id=v["target_fw_id"],
                    name=v["name"],
                    batch_size=v["batch_size"],
                    failure_threshold=v["failure_threshold"],
                    max_stage_attempts=v["max_stage_attempts"],
                    stage_timeout=v["stage_timeout"],
                    heartbeat_timeout=v["heartbeat_timeout"],
                    allow_downgrade=v["allow_downgrade"],
                    allowed_from_versions=set(v.get("allowed_from_versions", [])),
                    state=ReleaseState(v["state"]),
                    created_ts=v["created_ts"],
                    halt_reason=v.get("halt_reason"),
                )
                for k, v in raw.get("releases", {}).items()
            }
            self.batches = {
                k: Batch(
                    batch_id=v["batch_id"],
                    release_id=v["release_id"],
                    ordinal=v["ordinal"],
                    device_ids=list(v["device_ids"]),
                    state=BatchState(v["state"]),
                    claimed=set(v.get("claimed", [])),
                )
                for k, v in raw.get("batches", {}).items()
            }
            self.jobs = {
                k: Job(
                    job_id=v["job_id"],
                    release_id=v["release_id"],
                    batch_id=v["batch_id"],
                    device_id=v["device_id"],
                    target_fw_id=v["target_fw_id"],
                    from_version=v["from_version"],
                    direction=JobDirection(v["direction"]),
                    stage=JobStage(v["stage"]),
                    stage_seq=v["stage_seq"],
                    attempts=v["attempts"],
                    created_ts=v["created_ts"],
                    stage_entered_ts=v["stage_entered_ts"],
                    last_progress_ts=v["last_progress_ts"],
                    terminal_ts=v.get("terminal_ts"),
                    terminal_reason=v.get("terminal_reason"),
                    counted=v.get("counted", False),
                    superseded_by=v.get("superseded_by"),
                    last_report_seq=v.get("last_report_seq", -1),
                )
                for k, v in raw.get("jobs", {}).items()
            }
            self.audit = list(raw.get("audit", []))
            self._counters = dict(raw.get("counters", {}))
