"""FastAPI 适配层：HTTP 入参校验 + 错误码映射，业务逻辑全部委托 OtaService。

错误响应统一为 {"error": {"code", "reason", "message"}}，
code 为大类、reason 为可区分的细分原因。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .clock import Clock
from .errors import OtaError
from .persistence import JsonStore
from .service import OtaService


class RegisterIn(BaseModel):
    device_id: str
    model: str
    hw_version: str
    current_fw_version: str


class HeartbeatIn(BaseModel):
    device_id: str
    token: str
    seq: int = Field(ge=0)


class FirmwareIn(BaseModel):
    fw_id: str
    version: str
    model: str
    hw_version: str
    checksum: str


class ReleaseIn(BaseModel):
    target_fw_id: str
    name: str | None = None
    batch_size: int = 5
    failure_threshold: float = 0.1
    max_stage_attempts: int = 3
    stage_timeout: float = 300.0
    heartbeat_timeout: float = 120.0
    allow_downgrade: bool = False
    allowed_from_versions: list[str] | None = None


class ClaimIn(BaseModel):
    device_id: str
    token: str


class ReportIn(BaseModel):
    device_id: str
    token: str
    job_id: str
    stage: str
    seq: int = Field(ge=0)
    failed: bool = False
    reason: str | None = None


class RollbackIn(BaseModel):
    device_id: str
    target_fw_id: str | None = None


def create_app(store: JsonStore | None = None, clock: Clock | None = None) -> FastAPI:
    app = FastAPI(title="OTA Fleet Rollout (local simulation)")
    svc = OtaService(store or JsonStore("data/ota_state.json"), clock)

    @app.exception_handler(OtaError)
    async def _ota_error_handler(request: Request, exc: OtaError) -> JSONResponse:
        # DuplicateReport 语义上是幂等成功：映射为 200
        status = 200 if exc.code == "duplicate_report" else exc.http_status
        return JSONResponse(
            status_code=status,
            content={"error": {"code": exc.code, "reason": exc.reason,
                               "message": exc.message}},
        )

    @app.get("/")
    def root() -> dict:
        return {"service": "ota-fleet-rollout", "status": "ok"}

    # ---- 设备 ----
    @app.post("/devices/register")
    def register_device(body: RegisterIn) -> dict:
        return svc.register_device(body.model_dump())

    @app.post("/devices/heartbeat")
    def heartbeat(body: HeartbeatIn) -> dict:
        return svc.heartbeat(body.model_dump())

    @app.get("/devices/{device_id}")
    def get_device(device_id: str) -> dict:
        return svc.get_device(device_id)

    # ---- 固件 ----
    @app.post("/firmwares")
    def create_firmware(body: FirmwareIn) -> dict:
        return svc.create_firmware(body.model_dump())

    # ---- 发布/批次 ----
    @app.post("/releases")
    def create_release(body: ReleaseIn) -> dict:
        return svc.create_release(body.model_dump(exclude_none=True))

    @app.get("/releases/{release_id}")
    def get_release(release_id: str) -> dict:
        return svc.get_release(release_id)

    @app.post("/releases/{release_id}/start")
    def start_release(release_id: str) -> dict:
        return svc.start_release(release_id)

    @app.post("/releases/{release_id}/pause")
    def pause_release(release_id: str) -> dict:
        return svc.pause_release(release_id)

    @app.post("/releases/{release_id}/resume")
    def resume_release(release_id: str) -> dict:
        return svc.resume_release(release_id)

    @app.post("/releases/{release_id}/abort")
    def abort_release(release_id: str) -> dict:
        return svc.abort_release(release_id)

    @app.post("/releases/{release_id}/batches/plan")
    def plan_next_batch(release_id: str) -> dict:
        return svc.plan_next_batch(release_id)

    @app.get("/batches/{batch_id}")
    def get_batch(batch_id: str) -> dict:
        return svc.get_batch(batch_id)

    # ---- 设备侧协议 ----
    @app.post("/jobs/claim")
    def claim_job(body: ClaimIn) -> dict:
        return svc.claim_job(body.model_dump())

    @app.post("/jobs/report")
    def report_stage(body: ReportIn) -> dict:
        return svc.report_stage(body.model_dump(exclude_none=True))

    # ---- 回滚 ----
    @app.post("/rollback/device")
    def rollback_device(body: RollbackIn) -> dict:
        return svc.rollback_device(body.model_dump(exclude_none=True))

    @app.post("/releases/{release_id}/batches/{batch_id}/rollback")
    def rollback_batch(release_id: str, batch_id: str,
                       body: RollbackIn | None = None) -> dict:
        target = body.target_fw_id if body is not None else None
        return svc.rollback_batch(release_id, batch_id, target)

    # ---- 看门狗与审计 ----
    @app.post("/sweep")
    def sweep() -> dict:
        return svc.sweep()

    @app.get("/audit")
    def audit(ref: str | None = None, limit: int = 100) -> dict:
        return {"events": svc.audit_log(ref, limit)}

    app.state.svc = svc
    return app
