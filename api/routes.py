"""HTTP 路由：设备、固件、发布、进度、回滚、审计。

错误统一由 ota.errors.OTAError 转成 {code, message, details} JSON，
HTTP 状态码与错误类型对应（401/404/409/422）。
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ota.app import AppContext
from ota.errors import OTAError

router = APIRouter()

# 默认上下文（data/ 目录）；测试用 dependency_overrides 替换
_ctx: AppContext | None = None


def get_ctx() -> AppContext:
    global _ctx
    if _ctx is None:
        _ctx = AppContext(os.environ.get("OTA_DATA_DIR", "data"))
    return _ctx


def set_ctx(ctx: AppContext | None) -> None:
    global _ctx
    _ctx = ctx


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(OTAError)
    async def _on_ota_error(_: Request, exc: OTAError):
        return JSONResponse(status_code=exc.http_status,
                            content=exc.to_dict())


# --------------------------------------------------------------- schemas
class RegisterIn(BaseModel):
    device_id: str
    model: str
    hw_version: str
    fw_version: str


class HeartbeatIn(BaseModel):
    token: str
    seq: int = Field(gt=0)


class FirmwareIn(BaseModel):
    fw_id: str
    version: str
    model: str
    hw_version: str
    compatible_from: list[str] = []
    allow_downgrade: bool = False


class RolloutIn(BaseModel):
    rollout_id: str
    fw_id: str
    device_ids: list[str]
    batch_size: int = 5
    failure_rate_threshold: float = 0.2


class ClaimIn(BaseModel):
    token: str


class ProgressIn(BaseModel):
    token: str
    seq: int = Field(gt=0)
    stage: str | None = None
    error: str = ""


class ReconcileIn(BaseModel):
    token: str
    client_seq: int


class RollbackIn(BaseModel):
    target_version: str
    idem_key: str


# --------------------------------------------------------------- health
@router.get("/health")
def health():
    return {"status": "ok"}


# -------------------------------------------------------------- devices
@router.post("/devices")
def register_device(body: RegisterIn, ctx: AppContext = Depends(get_ctx)):
    return ctx.devices.register(body.device_id, body.model,
                                body.hw_version, body.fw_version)


@router.post("/devices/{device_id}/heartbeat")
def heartbeat(device_id: str, body: HeartbeatIn,
              ctx: AppContext = Depends(get_ctx)):
    return ctx.devices.heartbeat(device_id, body.token, body.seq)


@router.get("/devices/{device_id}")
def get_device(device_id: str, ctx: AppContext = Depends(get_ctx)):
    from ota.errors import UnknownDeviceError
    with ctx.store.lock:
        d = ctx.store.devices.get(device_id)
        if d is None:
            raise UnknownDeviceError(f"未知设备: {device_id}",
                                     details={"device_id": device_id})
        return {"device_id": d.device_id, "model": d.model,
                "hw_version": d.hw_version, "fw_version": d.fw_version,
                "presence": d.presence.value,
                "heartbeat_seq": d.heartbeat_seq,
                "last_heartbeat": d.last_heartbeat,
                "registered_at": d.registered_at}


@router.post("/maintenance/offline-sweep")
def offline_sweep(ctx: AppContext = Depends(get_ctx)):
    return {"went_offline": ctx.devices.mark_offline_sweep()}


# ------------------------------------------------------------- firmwares
@router.post("/firmwares")
def create_firmware(body: FirmwareIn, ctx: AppContext = Depends(get_ctx)):
    return ctx.firmwares.create(body.fw_id, body.version, body.model,
                                body.hw_version, body.compatible_from,
                                body.allow_downgrade)


@router.get("/firmwares/{fw_id}")
def get_firmware(fw_id: str, ctx: AppContext = Depends(get_ctx)):
    fw = ctx.firmwares.get(fw_id)
    return {"fw_id": fw.fw_id, "version": fw.version, "model": fw.model,
            "hw_version": fw.hw_version,
            "compatible_from": fw.compatible_from,
            "allow_downgrade": fw.allow_downgrade}


# -------------------------------------------------------------- rollouts
@router.post("/rollouts")
def create_rollout(body: RolloutIn, ctx: AppContext = Depends(get_ctx)):
    return ctx.rollouts.create(body.rollout_id, body.fw_id,
                               body.device_ids, body.batch_size,
                               body.failure_rate_threshold)


@router.get("/rollouts/{rollout_id}")
def get_rollout(rollout_id: str, ctx: AppContext = Depends(get_ctx)):
    return ctx.rollouts.summary(rollout_id)


@router.post("/rollouts/{rollout_id}/start")
def start_rollout(rollout_id: str, ctx: AppContext = Depends(get_ctx)):
    return ctx.rollouts.start(rollout_id)


@router.post("/rollouts/{rollout_id}/pause")
def pause_rollout(rollout_id: str, ctx: AppContext = Depends(get_ctx)):
    return ctx.rollouts.pause(rollout_id)


@router.post("/rollouts/{rollout_id}/resume")
def resume_rollout(rollout_id: str, ctx: AppContext = Depends(get_ctx)):
    return ctx.rollouts.resume(rollout_id)


@router.post("/rollouts/{rollout_id}/claim/{device_id}")
def claim(rollout_id: str, device_id: str, body: ClaimIn,
          ctx: AppContext = Depends(get_ctx)):
    return ctx.rollouts.claim(rollout_id, device_id, body.token)


# -------------------------------------------------------------- progress
@router.post("/rollouts/{rollout_id}/report/{device_id}")
def report(rollout_id: str, device_id: str, body: ProgressIn,
           ctx: AppContext = Depends(get_ctx)):
    if body.stage is None:
        return ctx.progress.report_failure(device_id, body.token,
                                           rollout_id, body.seq,
                                           body.error)
    return ctx.progress.report(device_id, body.token, rollout_id,
                               body.stage, body.seq)


@router.post("/rollouts/{rollout_id}/reconcile/{device_id}")
def reconcile(rollout_id: str, device_id: str, body: ReconcileIn,
              ctx: AppContext = Depends(get_ctx)):
    return ctx.progress.reconcile(device_id, body.token, rollout_id,
                                  body.client_seq)


@router.post("/rollouts/{rollout_id}/watchdog")
def watchdog(rollout_id: str, ctx: AppContext = Depends(get_ctx)):
    return ctx.progress.watchdog(rollout_id)


@router.post("/rollouts/{rollout_id}/stall-sweep")
def stall_sweep(rollout_id: str, ctx: AppContext = Depends(get_ctx)):
    return ctx.progress.stall_sweep(rollout_id)


@router.post("/maintenance/stall-sweep")
def stall_sweep_all(ctx: AppContext = Depends(get_ctx)):
    return ctx.progress.stall_sweep(None)


# -------------------------------------------------------------- rollback
@router.post("/rollback/device/{device_id}")
def rollback_device(device_id: str, body: RollbackIn,
                    ctx: AppContext = Depends(get_ctx)):
    return ctx.rollbacks.rollback_device(device_id, body.target_version,
                                         body.idem_key)


@router.post("/rollouts/{rollout_id}/rollback-batch/{batch_index}")
def rollback_batch(rollout_id: str, batch_index: int, body: RollbackIn,
                   ctx: AppContext = Depends(get_ctx)):
    return ctx.rollbacks.rollback_batch(rollout_id, batch_index,
                                        body.target_version, body.idem_key)


@router.get("/rollback/records")
def rollback_records(ctx: AppContext = Depends(get_ctx)):
    from dataclasses import asdict
    with ctx.store.lock:
        return {"records": [asdict(r) for r in ctx.store.rollback_records]}


# ----------------------------------------------------------------- audit
@router.get("/audit")
def audit_tail(limit: int = 50, ctx: AppContext = Depends(get_ctx)):
    return {"events": [{"seq": e.seq, "ts": e.ts, "kind": e.kind,
                        "detail": e.detail} for e in ctx.audit.tail(limit)]}
