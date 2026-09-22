"""统一错误类型：每条错误带可区分的错误码与中文说明。"""


class OTAError(Exception):
    """所有 OTA 业务错误的基类。"""

    code = "ota_error"
    http_status = 400

    def __init__(self, message: str = "", *, details: dict | None = None):
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


class DeviceAlreadyRegisteredError(OTAError):
    code = "device_already_registered"
    http_status = 409


class UnknownDeviceError(OTAError):
    code = "unknown_device"
    http_status = 404


class BadDeviceTokenError(OTAError):
    code = "bad_device_token"
    http_status = 401


class StaleHeartbeatError(OTAError):
    code = "stale_heartbeat"
    http_status = 409


class FirmwareValidationError(OTAError):
    code = "firmware_validation_failed"
    http_status = 422


class FirmwareNotFoundError(OTAError):
    code = "firmware_not_found"
    http_status = 404


class IncompatibleFirmwareError(OTAError):
    code = "incompatible_firmware"
    http_status = 422


class UpgradePathError(OTAError):
    code = "upgrade_path_rejected"
    http_status = 422


class RolloutValidationError(OTAError):
    code = "rollout_validation_failed"
    http_status = 422


class RolloutStateError(OTAError):
    code = "rollout_state_conflict"
    http_status = 409


class BatchStateError(OTAError):
    code = "batch_state_conflict"
    http_status = 409


class RollbackRejectedError(OTAError):
    code = "rollback_rejected"
    http_status = 422


class RollbackDuplicateError(OTAError):
    code = "rollback_duplicate"
    http_status = 409


class StageTransitionError(OTAError):
    code = "stage_transition_rejected"
    http_status = 422


class StaleReportError(OTAError):
    code = "stale_report_rejected"
    http_status = 409


class ConcurrencyConflictError(OTAError):
    code = "concurrency_conflict"
    http_status = 409
