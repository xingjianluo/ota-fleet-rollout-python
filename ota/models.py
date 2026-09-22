"""领域模型：设备、固件、发布、批次、任务与审计事件。

只承载状态，不包含业务规则；规则全部在 service.py 中实现，
保证状态迁移只有一个入口、便于并发控制与审计。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class DeviceStatus(str, enum.Enum):
    REGISTERED = "registered"   # 已注册，尚未收到心跳
    ONLINE = "online"
    OFFLINE = "offline"         # 心跳超时后由看门狗标记


class JobStage(str, enum.Enum):
    PENDING = "pending"                 # 已随批次纳入发布，设备尚未认领
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    INSTALLING = "installing"
    INSTALLED = "installed"
    REBOOTING = "rebooting"
    CONFIRMED = "confirmed"             # 终态：升级成功
    FAILED = "failed"                   # 终态：设备明确上报失败
    SUSPENDED = "suspended"             # 终态(搁置)：看门狗判定无进展，等人介入
    ROLLED_BACK = "rolled_back"         # 终态：已回滚到旧版本
    # 回滚任务复用同一条流水线，方向由 Job.direction 区分
    # ROLLING_BACK 单独表示“回滚任务执行中”的批次可见标记
    ROLLING_BACK = "rolling_back"


# 阶段正向顺序（下载→安装→重启→确认）
STAGE_ORDER: list[JobStage] = [
    JobStage.PENDING,
    JobStage.DOWNLOADING,
    JobStage.DOWNLOADED,
    JobStage.INSTALLING,
    JobStage.INSTALLED,
    JobStage.REBOOTING,
    JobStage.CONFIRMED,
]

TERMINAL_STAGES: frozenset[JobStage] = frozenset(
    {
        JobStage.CONFIRMED,
        JobStage.FAILED,
        JobStage.SUSPENDED,
        JobStage.ROLLED_BACK,
    }
)


class ReleaseState(str, enum.Enum):
    PENDING = "pending"       # 已创建未开始
    ACTIVE = "active"         # 发布进行中
    PAUSED = "paused"         # 人工暂停：不允许规划/认领新批次
    THRESHOLD_HALTED = "threshold_halted"  # 失败率达阈值自动停止
    COMPLETED = "completed"   # 全部设备进入终态
    ABORTED = "aborted"       # 人工终止


class BatchState(str, enum.Enum):
    PENDING = "pending"
    ACTIVE = "active"
    DRAINED = "drained"       # 本批设备全部进入终态
    STOPPED = "stopped"       # 因阈值/终止被停


class JobDirection(str, enum.Enum):
    UPGRADE = "upgrade"
    ROLLBACK = "rollback"


@dataclass
class Device:
    device_id: str
    model: str
    hw_version: str
    token_hash: str                 # 只存令牌哈希，令牌只在注册时返回一次
    current_fw_version: str
    status: DeviceStatus
    registered_ts: float
    last_heartbeat_ts: float | None = None
    last_heartbeat_seq: int = 0     # 心跳单调序号，过期心跳被拒绝

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "model": self.model,
            "hw_version": self.hw_version,
            "token_hash": self.token_hash,
            "current_fw_version": self.current_fw_version,
            "status": self.status.value,
            "registered_ts": self.registered_ts,
            "last_heartbeat_ts": self.last_heartbeat_ts,
            "last_heartbeat_seq": self.last_heartbeat_seq,
        }


@dataclass
class Firmware:
    fw_id: str
    version: str
    model: str
    hw_version: str
    checksum: str
    created_ts: float
    yanked: bool = False            # 被撤下的固件不能再作为目标

    def to_dict(self) -> dict[str, Any]:
        return {
            "fw_id": self.fw_id,
            "version": self.version,
            "model": self.model,
            "hw_version": self.hw_version,
            "checksum": self.checksum,
            "created_ts": self.created_ts,
            "yanked": self.yanked,
        }


@dataclass
class Release:
    release_id: str
    target_fw_id: str
    name: str
    batch_size: int
    failure_threshold: float        # 0~1，失败率达到即自动停止后续批次
    max_stage_attempts: int         # 单阶段最大重试次数
    stage_timeout: float            # 单阶段无进展超时秒数
    heartbeat_timeout: float        # 心跳超时秒数
    allow_downgrade: bool
    allowed_from_versions: set[str]  # 仅这些当前版本可参与；空集合表示不限制版本
    state: ReleaseState
    created_ts: float
    halt_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "release_id": self.release_id,
            "target_fw_id": self.target_fw_id,
            "name": self.name,
            "batch_size": self.batch_size,
            "failure_threshold": self.failure_threshold,
            "max_stage_attempts": self.max_stage_attempts,
            "stage_timeout": self.stage_timeout,
            "heartbeat_timeout": self.heartbeat_timeout,
            "allow_downgrade": self.allow_downgrade,
            "allowed_from_versions": sorted(self.allowed_from_versions),
            "state": self.state.value,
            "created_ts": self.created_ts,
            "halt_reason": self.halt_reason,
        }


@dataclass
class Batch:
    batch_id: str
    release_id: str
    ordinal: int
    device_ids: list[str]
    state: BatchState
    claimed: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "release_id": self.release_id,
            "ordinal": self.ordinal,
            "device_ids": list(self.device_ids),
            "state": self.state.value,
            "claimed": sorted(self.claimed),
        }


@dataclass
class Job:
    job_id: str
    release_id: str
    batch_id: str
    device_id: str
    target_fw_id: str
    from_version: str
    direction: JobDirection
    stage: JobStage
    stage_seq: int                  # 阶段单调序号，乱序/迟到上报据此收敛
    attempts: int                   # 当前阶段已尝试次数
    created_ts: float
    stage_entered_ts: float
    last_progress_ts: float
    terminal_ts: float | None = None
    terminal_reason: str | None = None
    counted: bool = False           # 终态是否已计入批次统计（CAS 去重）
    superseded_by: str | None = None  # 回滚任务创建后，原任务被指向新任务
    last_report_seq: int = -1       # 去重：设备上报自带序号

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "release_id": self.release_id,
            "batch_id": self.batch_id,
            "device_id": self.device_id,
            "target_fw_id": self.target_fw_id,
            "from_version": self.from_version,
            "direction": self.direction.value,
            "stage": self.stage.value,
            "stage_seq": self.stage_seq,
            "attempts": self.attempts,
            "created_ts": self.created_ts,
            "stage_entered_ts": self.stage_entered_ts,
            "last_progress_ts": self.last_progress_ts,
            "terminal_ts": self.terminal_ts,
            "terminal_reason": self.terminal_reason,
            "counted": self.counted,
            "superseded_by": self.superseded_by,
            "last_report_seq": self.last_report_seq,
        }


@dataclass
class AuditEvent:
    ts: float
    kind: str                       # 例如 register_rejected / rollback_rejected
    ref: str                        # 设备/发布/任务 ID，便于追溯
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "kind": self.kind,
            "ref": self.ref,
            "message": self.message,
            "details": self.details,
        }
