"""错误类型定义：所有拒绝原因必须可区分、可追溯。"""

from __future__ import annotations


class OtaError(Exception):
    """OTA 业务错误基类。

    code 用于错误归类与日志检索，HTTP 层据此映射状态码。
    """

    code: str = "ota_error"
    http_status: int = 400

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        # reason 给出机器可读的细分原因，保证调用方可以区分不同拒绝情形
        self.reason = reason or self.code


class DeviceError(OtaError):
    code = "device_error"


class DeviceAlreadyRegistered(DeviceError):
    code = "device_already_registered"
    http_status = 409


class InvalidDeviceIdentity(DeviceError):
    """身份字段缺失/格式非法/令牌伪造。"""

    code = "invalid_device_identity"
    http_status = 401


class DeviceNotFound(DeviceError):
    code = "device_not_found"
    http_status = 404


class StaleHeartbeat(DeviceError):
    """心跳时间戳早于已接受的时间戳，拒绝覆盖。"""

    code = "stale_heartbeat"
    http_status = 409


class FirmwareError(OtaError):
    code = "firmware_error"


class FirmwareNotFound(FirmwareError):
    code = "firmware_not_found"
    http_status = 404


class FirmwareAlreadyExists(FirmwareError):
    code = "firmware_already_exists"
    http_status = 409


class IncompatibleFirmware(FirmwareError):
    """型号/硬件版本不匹配。"""

    code = "incompatible_firmware"
    http_status = 422


class InvalidUpgradePath(FirmwareError):
    """升级方向或升级路径不被允许。"""

    code = "invalid_upgrade_path"
    http_status = 422


class ReleaseError(OtaError):
    code = "release_error"


class ReleaseNotFound(ReleaseError):
    code = "release_not_found"
    http_status = 404


class ReleaseStateConflict(ReleaseError):
    """发布状态机不允许该操作（如未暂停却继续、已结束再暂停）。"""

    code = "release_state_conflict"
    http_status = 409


class NoEligibleDevice(ReleaseError):
    code = "no_eligible_device"
    http_status = 422


class BatchExhausted(ReleaseError):
    """批次中没有更多可认领的设备。"""

    code = "batch_exhausted"
    http_status = 409


class RollbackError(OtaError):
    code = "rollback_error"


class RollbackTargetMissing(RollbackError):
    code = "rollback_target_missing"
    http_status = 422


class RollbackNotAllowed(RollbackError):
    """当前阶段不允许回滚 / 已被回滚 / 目标不兼容。"""

    code = "rollback_not_allowed"
    http_status = 422


class RollbackInProgress(RollbackError):
    """并发回滚保护：同一设备/批次已在回滚中。"""

    code = "rollback_in_progress"
    http_status = 409


class JobError(OtaError):
    code = "job_error"


class JobNotFound(JobError):
    code = "job_not_found"
    http_status = 404


class StaleReport(JobError):
    """乱序/迟到上报：序号落后于已收敛状态。"""

    code = "stale_report"
    http_status = 409


class InvalidStageTransition(JobError):
    """阶段推进不符合设备状态机。"""

    code = "invalid_stage_transition"
    http_status = 422


class DuplicateReport(JobError):
    """重复上报同一阶段：幂等接受但不重复计数。"""

    code = "duplicate_report"
    http_status = 200


class WatchdogSuspended(JobError):
    """设备长时间无进展，被看门狗搁置。"""

    code = "watchdog_suspended"
    http_status = 409


class ConcurrencyConflict(OtaError):
    code = "concurrency_conflict"
    http_status = 409
