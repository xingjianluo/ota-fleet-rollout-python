"""领域模型：设备、固件、发布任务、批次、设备升级状态、审计事件。

骨架阶段只包含枚举与数据类定义，不含业务逻辑。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class DevicePresence(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"


class UpdateStage(str, Enum):
    """设备升级状态机的阶段。"""
    NONE = "none"
    DOWNLOADED = "downloaded"
    INSTALLED = "installed"
    REBOOTED = "rebooted"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    SHELVED = "shelved"


# 允许的阶段正向推进顺序（NONE 为初始态）
STAGE_ORDER = [
    UpdateStage.NONE,
    UpdateStage.DOWNLOADED,
    UpdateStage.INSTALLED,
    UpdateStage.REBOOTED,
    UpdateStage.CONFIRMED,
]

# 失败/搁置等终态，不允许再上报普通进度
TERMINAL_STAGES = {UpdateStage.CONFIRMED, UpdateStage.FAILED,
                   UpdateStage.ROLLED_BACK, UpdateStage.SHELVED}


class RolloutStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    PAUSED = "paused"
    AUTO_HALTED = "auto_halted"
    COMPLETED = "completed"
    ROLLED_BACK = "rolled_back"


class BatchStatus(str, Enum):
    WAITING = "waiting"
    ACTIVE = "active"
    PAUSED = "paused"
    HALTED = "halted"
    DONE = "done"


@dataclass
class Device:
    device_id: str
    model: str
    hw_version: str
    fw_version: str
    token_hash: str
    registered_at: float
    last_heartbeat: float = 0.0
    presence: DevicePresence = DevicePresence.OFFLINE
    heartbeat_seq: int = 0


@dataclass
class Firmware:
    fw_id: str
    version: str
    model: str
    hw_version: str
    compatible_from: list[str] = field(default_factory=list)
    allow_downgrade: bool = False
    created_at: float = 0.0


@dataclass
class RolloutBatch:
    index: int
    capacity: int
    status: BatchStatus = BatchStatus.WAITING
    claimed: list[str] = field(default_factory=list)
    succeeded: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    opened_at: float = 0.0


@dataclass
class Rollout:
    rollout_id: str
    fw_id: str
    target_version: str
    model: str
    hw_version: str
    device_ids: list[str]
    batch_size: int
    failure_rate_threshold: float
    status: RolloutStatus = RolloutStatus.PENDING
    batches: list[RolloutBatch] = field(default_factory=list)
    created_at: float = 0.0
    halted_reason: str = ""


@dataclass
class DeviceUpdateState:
    """设备在某次发布中的升级进度（按 rollout_id 维度记录）。"""
    device_id: str
    rollout_id: str
    stage: UpdateStage = UpdateStage.NONE
    stage_seq: int = 0           # 设备侧上报序号，用于乱序/重放判定
    updated_at: float = 0.0
    retries: int = 0
    last_error: str = ""
    claimed_by_batch: int | None = None


@dataclass
class AuditEvent:
    seq: int
    ts: float
    kind: str
    detail: dict


@dataclass
class RollbackRecord:
    id: str
    ts: float
    scope: str                  # "device" | "batch"
    device_id: str | None
    rollout_id: str | None
    batch_index: int | None
    from_version: str
    to_version: str
    result: str                 # "rejected" | "completed" | "duplicate"
    reason: str = ""
