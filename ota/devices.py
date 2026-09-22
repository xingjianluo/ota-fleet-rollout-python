"""设备注册、身份校验与心跳上报。

规则：
- device_id 全局唯一；重复注册一律拒绝（相同属性=identical_reregistration，
  属性冲突=identity_conflict），绝不静默覆盖已有状态。
- 注册时签发随机 token，仅 sha256 哈希落盘；上报需携带正确 token，
  伪造身份 -> BadDeviceTokenError，未知设备 -> UnknownDeviceError。
- 心跳携带单调递增 seq：重复/回退的 seq 视为过期心跳拒绝，
  不更新 last_heartbeat / presence / seq。
"""
from __future__ import annotations

import hashlib
import logging
import secrets

from .audit import AuditLog
from .clock import now
from .errors import (
    BadDeviceTokenError,
    DeviceAlreadyRegisteredError,
    StaleHeartbeatError,
    UnknownDeviceError,
)
from .models import Device, DevicePresence
from .storage import Store

log = logging.getLogger("ota.devices")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class DeviceService:
    def __init__(self, store: Store, audit: AuditLog, *,
                 heartbeat_ttl: float = 30.0):
        self.store = store
        self.audit = audit
        self.heartbeat_ttl = heartbeat_ttl

    # ------------------------------------------------------------- register
    def register(self, device_id: str, model: str, hw_version: str,
                 fw_version: str) -> dict:
        device_id = str(device_id).strip()
        if not device_id:
            raise DeviceAlreadyRegisteredError(
                "device_id 不能为空", details={"reason": "empty_id"})
        if not model or not hw_version or not fw_version:
            raise DeviceAlreadyRegisteredError(
                "model/hw_version/fw_version 均不能为空",
                details={"reason": "missing_identity_attributes"})

        with self.store.lock:
            self.store.ensure_loaded()
            existing = self.store.devices.get(device_id)
            if existing is not None:
                same = (existing.model == model
                        and existing.hw_version == hw_version
                        and existing.fw_version == fw_version)
                reason = ("identical_reregistration" if same
                          else "identity_conflict")
                self.audit.record("register_rejected", {
                    "device_id": device_id, "reason": reason,
                    "submitted": {"model": model, "hw_version": hw_version,
                                  "fw_version": fw_version},
                    "existing": {"model": existing.model,
                                 "hw_version": existing.hw_version,
                                 "fw_version": existing.fw_version}})
                raise DeviceAlreadyRegisteredError(
                    f"设备 {device_id} 已注册，拒绝覆盖",
                    details={"reason": reason, "device_id": device_id})

            token = secrets.token_hex(24)
            ts = now()
            device = self.store.devices.setdefault(device_id, Device(
                device_id=device_id, model=model, hw_version=hw_version,
                fw_version=fw_version, token_hash=_hash_token(token),
                registered_at=ts))
            if device.token_hash != _hash_token(token):
                # 极端竞态兜底：setdefault 未生效
                raise DeviceAlreadyRegisteredError(
                    f"设备 {device_id} 注册并发冲突",
                    details={"reason": "identity_conflict"})
            self.store.persist()
            self.audit.record("device_registered", {
                "device_id": device_id, "model": model,
                "hw_version": hw_version, "fw_version": fw_version})
            log.info("register input=(id=%s model=%s hw=%s fw=%s) -> issued",
                     device_id, model, hw_version, fw_version)
            return {"device_id": device_id, "token": token,
                    "registered_at": ts}

    # -------------------------------------------------------- authenticate
    def authenticate(self, device_id: str, token: str) -> None:
        with self.store.lock:
            self.store.ensure_loaded()
            device = self.store.devices.get(device_id)
            if device is None:
                self.audit.record("auth_failed", {
                    "device_id": device_id, "reason": "unknown_device"})
                raise UnknownDeviceError(
                    f"未知设备: {device_id}",
                    details={"device_id": device_id})
            if not token or device.token_hash != _hash_token(token):
                self.audit.record("auth_failed", {
                    "device_id": device_id, "reason": "bad_token"})
                raise BadDeviceTokenError(
                    f"设备 {device_id} 身份校验失败（token 不匹配）",
                    details={"device_id": device_id})

    # ------------------------------------------------------------ heartbeat
    def heartbeat(self, device_id: str, token: str, seq: int) -> dict:
        self.authenticate(device_id, token)
        with self.store.lock:
            device = self.store.devices[device_id]
            if not isinstance(seq, int) or isinstance(seq, bool) or seq <= 0:
                raise StaleHeartbeatError(
                    "心跳 seq 必须为正整数",
                    details={"device_id": device_id, "reason": "bad_seq",
                             "input_seq": seq})
            if seq <= device.heartbeat_seq:
                self.audit.record("heartbeat_rejected", {
                    "device_id": device_id, "reason": "stale_or_duplicate_seq",
                    "input_seq": seq,
                    "last_accepted_seq": device.heartbeat_seq})
                raise StaleHeartbeatError(
                    f"设备 {device_id} 心跳过期或重复: seq={seq} "
                    f"<= 已接受 {device.heartbeat_seq}",
                    details={"device_id": device_id,
                             "reason": "stale_or_duplicate_seq",
                             "input_seq": seq,
                             "last_accepted_seq": device.heartbeat_seq})
            ts = now()
            was_offline = device.presence == DevicePresence.OFFLINE
            device.heartbeat_seq = seq
            device.last_heartbeat = ts
            device.presence = DevicePresence.ONLINE
            self.store.persist()
            self.audit.record("heartbeat_ok", {
                "device_id": device_id, "seq": seq,
                "offline_to_online": was_offline})
            log.info("heartbeat input=(id=%s seq=%s) seq %s->%s presence=%s",
                     device_id, seq, seq - 1, seq,
                     "reconnected" if was_offline else "online")
            return {"device_id": device_id, "seq": seq,
                    "presence": DevicePresence.ONLINE.value,
                    "server_ts": ts, "reconnected": was_offline}

    # ------------------------------------------------- offline TTL sweeper
    def mark_offline_sweep(self) -> list[str]:
        went_offline: list[str] = []
        ts = now()
        with self.store.lock:
            for device in self.store.devices.values():
                if (device.presence == DevicePresence.ONLINE
                        and ts - device.last_heartbeat > self.heartbeat_ttl):
                    device.presence = DevicePresence.OFFLINE
                    went_offline.append(device.device_id)
                    self.audit.record("device_offline", {
                        "device_id": device.device_id,
                        "idle_seconds": ts - device.last_heartbeat,
                        "ttl": self.heartbeat_ttl})
                    log.info("offline-sweep id=%s idle=%.1fs -> offline",
                             device.device_id,
                             ts - device.last_heartbeat)
            if went_offline:
                self.store.persist()
        return went_offline
