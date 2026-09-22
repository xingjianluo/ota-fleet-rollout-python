"""单设备与整批回滚。

硬性规则：
- 回滚目标版本必须存在对应固件，且型号/硬件匹配、降级方向被允许；
- 仅允许对"已经确认运行在新版本上"的设备回滚；下载/安装/重启中、
  失败、已搁置的设备属于半更新或未定态，一律拒绝，不留下半更新；
- 同一 (设备, 目标版本) 只允许一次有效回滚，重复回滚被
  rollback_duplicate 拒绝；idem_key 用于请求幂等（网络重试返回同一结果）；
- 所有批准与拒绝都写 rollback_records + 审计，保证可追溯。

整批回滚先做全量预检（目标固件、每台设备资格），任一不通过整体拒绝，
不做半截回滚；预检通过后在同一把锁内逐台生效。
"""
from __future__ import annotations

import logging
import uuid

from .audit import AuditLog
from .clock import now
from .errors import (
    FirmwareNotFoundError,
    RollbackDuplicateError,
    RollbackRejectedError,
    RolloutValidationError,
)
from .models import RollbackRecord, UpdateStage
from .storage import Store
from .versions import compare

log = logging.getLogger("ota.rollbacks")


class RollbackService:
    def __init__(self, store: Store, audit: AuditLog, rollouts=None):
        self.store = store
        self.audit = audit
        self.rollouts = rollouts

    def _rollouts(self):
        if self.rollouts is None:
            from .rollouts import RolloutService
            self.rollouts = RolloutService(self.store, self.audit, None)
        return self.rollouts

    # -------------------------------------------------------- target lookup
    def _find_target_firmware(self, model: str, hw_version: str,
                              target_version: str):
        candidates = [fw for fw in self.store.firmwares.values()
                      if fw.model == model and fw.hw_version == hw_version
                      and fw.version == target_version]
        if not candidates:
            raise FirmwareNotFoundError(
                f"不存在适用于 {model}/{hw_version} 的目标固件 "
                f"{target_version}",
                details={"reason": "target_firmware_not_found",
                         "model": model, "hw_version": hw_version,
                         "target_version": target_version})
        return candidates[0]

    # ----------------------------------------------------------- recording
    def _record(self, *, scope: str, device_id: str | None,
                rollout_id: str | None, batch_index: int | None,
                from_version: str, to_version: str, result: str,
                reason: str, idem_key: str, unique_key: str | None = None) -> dict:
        rec = RollbackRecord(
            id=uuid.uuid4().hex, ts=now(), scope=scope, device_id=device_id,
            rollout_id=rollout_id, batch_index=batch_index,
            from_version=from_version, to_version=to_version,
            result=result, reason=reason)
        self.store.rollback_records.append(rec)
        self.audit.record("rollback_recorded", {
            "id": rec.id, "scope": scope, "device_id": device_id,
            "rollout_id": rollout_id, "batch_index": batch_index,
            "from_version": from_version, "to_version": to_version,
            "result": result, "reason": reason,
            "idem_key": idem_key,
            "unique_key": unique_key or idem_key})
        log.warning("rollback scope=%s device=%s %s -> %s result=%s reason=%s",
                    scope, device_id, from_version, to_version, result, reason)
        return {"id": rec.id, "result": result, "reason": reason,
                "from_version": from_version, "to_version": to_version,
                "device_id": device_id}

    def _find_idem(self, idem_key: str) -> dict | None:
        """仅匹配请求级 unique_key（整批的设备子记录使用派生键，不命中）。"""
        if not idem_key:
            return None
        for e in self.audit.tail(10000):
            if e.kind == "rollback_recorded" \
                    and e.detail.get("unique_key") == idem_key:
                return e.detail
        return None

    @staticmethod
    def _device_already_rolled_back(device_id: str, target: str,
                                    records: list[RollbackRecord]) -> bool:
        return any(r.device_id == device_id and r.to_version == target
                   and r.result == "completed" for r in records)

    # ---------------------------------------------------- eligibility check
    def _check_device(self, device, target_fw, target_version: str,
                      idem_key: str) -> tuple[str | None, str | None]:
        """返回 (rollout_id, reject_reason)；reject_reason 非空即拒绝。"""
        # 找到该设备当前进行/完成中的发布状态
        state = None
        rid = None
        for (did, r_id), st in self.store.update_states.items():
            if did == device.device_id:
                state, rid = st, r_id
                break

        if self._device_already_rolled_back(device.device_id,
                                            target_version,
                                            self.store.rollback_records):
            return rid, "already_rolled_back_to_target"

        # 半更新（下载/安装/重启中）优先拒绝：不得对不确定状态做回滚
        if state is not None and state.stage not in (
                UpdateStage.CONFIRMED, UpdateStage.FAILED,
                UpdateStage.ROLLED_BACK, UpdateStage.SHELVED):
            return rid, "device_mid_update_not_rollbackable"

        cmp_dir = compare(device.fw_version, target_version)
        if cmp_dir == 0:
            return rid, "already_on_target_version"
        if cmp_dir < 0:
            return rid, "target_newer_than_current"

        # 降级必须目标固件显式允许且当前版本在白名单
        if not target_fw.allow_downgrade:
            return rid, "downgrade_disallowed"
        if device.fw_version not in target_fw.compatible_from:
            return rid, "downgrade_path_not_whitelisted"

        return rid, None

    # ----------------------------------------------------- single rollback
    def rollback_device(self, device_id: str, target_version: str,
                        idem_key: str) -> dict:
        if not idem_key:
            raise RollbackRejectedError(
                "回滚必须携带 idem_key 以保证幂等",
                details={"reason": "missing_idem_key"})
        with self.store.lock:
            self.store.ensure_loaded()
            prior = self._find_idem(idem_key)
            if prior is not None:
                return {"id": prior["id"], "result": prior["result"],
                        "reason": "idempotent_replay",
                        "from_version": prior["from_version"],
                        "to_version": prior["to_version"],
                        "device_id": prior["device_id"],
                        "duplicate": True}

            device = self.store.devices.get(device_id)
            if device is None:
                raise RollbackRejectedError(
                    f"未知设备: {device_id}",
                    details={"reason": "unknown_device"})
            target_fw = self._find_target_firmware(
                device.model, device.hw_version, target_version)
            rid, reason = self._check_device(
                device, target_fw, target_version, idem_key)

            if reason is not None:
                self.store.persist()
                self._record(scope="device", device_id=device_id,
                             rollout_id=rid, batch_index=None,
                             from_version=device.fw_version,
                             to_version=target_version, result="rejected",
                             reason=reason, idem_key=idem_key)
                if reason == "already_rolled_back_to_target":
                    raise RollbackDuplicateError(
                        f"设备 {device_id} 已回滚到 {target_version}",
                        details={"reason": reason})
                raise RollbackRejectedError(
                    f"设备 {device_id} 回滚到 {target_version} 被拒绝: "
                    f"{reason}", details={"reason": reason})

            from_ver = device.fw_version
            device.fw_version = target_version
            state = self.store.update_states.get((device_id, rid)) \
                if rid else None
            if state is not None:
                state.stage = UpdateStage.ROLLED_BACK
                state.updated_at = now()
                state.last_error = ""
                self._rollouts().record_outcome(
                    rid, device_id, UpdateStage.ROLLED_BACK)
            result = self._record(scope="device", device_id=device_id,
                                  rollout_id=rid, batch_index=None,
                                  from_version=from_ver,
                                  to_version=target_version,
                                  result="completed", reason="",
                                  idem_key=idem_key)
            self.store.persist()
            result["duplicate"] = False
            return result

    # ------------------------------------------------------ batch rollback
    def rollback_batch(self, rollout_id: str, batch_index: int,
                       target_version: str, idem_key: str) -> dict:
        if not idem_key:
            raise RollbackRejectedError(
                "回滚必须携带 idem_key 以保证幂等",
                details={"reason": "missing_idem_key"})
        with self.store.lock:
            self.store.ensure_loaded()
            prior = self._find_idem(idem_key)
            if prior is not None:
                return {"result": prior["result"],
                        "reason": "idempotent_replay",
                        "rolled_back": [], "duplicate": True}

            r = self.store.rollouts.get(rollout_id)
            if r is None:
                raise RolloutValidationError(
                    f"发布不存在: {rollout_id}",
                    details={"reason": "unknown_rollout"})
            if not isinstance(batch_index, int) or batch_index < 0 \
                    or batch_index >= len(r.batches):
                raise RollbackRejectedError(
                    f"批次序号越界: {batch_index}",
                    details={"reason": "bad_batch_index"})
            batch = r.batches[batch_index]
            target_fw = self._find_target_firmware(
                r.model, r.hw_version, target_version)

            members = list(dict.fromkeys(batch.claimed
                                         + batch.succeeded
                                         + batch.failed))
            if not members:
                self._record(scope="batch", device_id=None,
                             rollout_id=rollout_id, batch_index=batch_index,
                             from_version=r.target_version,
                             to_version=target_version, result="rejected",
                             reason="empty_batch_no_devices",
                             idem_key=idem_key)
                self.store.persist()
                raise RollbackRejectedError(
                    "批次尚无设备认领，无可回滚对象",
                    details={"reason": "empty_batch_no_devices"})

            # ---- 全量预检：任一设备资格不通过 -> 整体拒绝，不生效 ----
            precheck = []
            for did in members:
                device = self.store.devices[did]
                rid, reason = self._check_device(
                    device, target_fw, target_version, idem_key)
                precheck.append((device, reason))
            blocked = [(d.device_id, why) for d, why in precheck
                       if why is not None]
            if blocked:
                self._record(scope="batch", device_id=None,
                             rollout_id=rollout_id, batch_index=batch_index,
                             from_version=r.target_version,
                             to_version=target_version, result="rejected",
                             reason=f"precheck_blocked:{len(blocked)}",
                             idem_key=idem_key)
                self.audit.record("rollback_batch_rejected", {
                    "rollout_id": rollout_id, "batch_index": batch_index,
                    "blocked": blocked})
                self.store.persist()
                raise RollbackRejectedError(
                    f"整批回滚预检失败：{len(blocked)}/{len(members)} "
                    f"台设备不满足条件，整批拒绝",
                    details={"reason": "batch_precheck_blocked",
                             "blocked": blocked})

            rolled_back = []
            for device, _ in precheck:
                from_ver = device.fw_version
                device.fw_version = target_version
                state = self.store.update_states.get(
                    (device.device_id, rollout_id))
                if state is not None:
                    state.stage = UpdateStage.ROLLED_BACK
                    state.updated_at = now()
                self._record(scope="device", device_id=device.device_id,
                             rollout_id=rollout_id, batch_index=batch_index,
                             from_version=from_ver,
                             to_version=target_version, result="completed",
                             reason="batch_rollback", idem_key=idem_key,
                             unique_key=f"{idem_key}:dev:{device.device_id}")
                rolled_back.append(device.device_id)

            self._record(scope="batch", device_id=None,
                         rollout_id=rollout_id, batch_index=batch_index,
                         from_version=r.target_version,
                         to_version=target_version, result="completed",
                         reason=f"{len(rolled_back)} devices",
                         idem_key=idem_key)
            for did in rolled_back:
                self._rollouts().record_outcome(
                    rollout_id, did, UpdateStage.ROLLED_BACK)
            self.store.persist()
            return {"result": "completed", "rolled_back": rolled_back,
                    "reason": "", "duplicate": False}
