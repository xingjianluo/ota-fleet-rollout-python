"""分批灰度发布。

核心规则：
- 创建时校验每个设备存在且与固件型号/硬件/升级路径全部兼容，
  任一不兼容整个发布被拒绝（不产生半成品发布）。
- 设备按固定顺序切分为容量 batch_size 的批次（预分区 -> 天然不会超认）。
- claim 原子认领：仅当前 ACTIVE 批次中的设备可认领；重复认领幂等返回，
  已终态设备/已暂停/已自动停止的发布均按可区分原因拒绝。
- 每次终态结果计入批次成功/失败；失败率达到阈值立即 AUTO_HALTED，
  后续批次不再下发；已下发设备继续收敛，不被强制中断。
"""
from __future__ import annotations

import logging

from .audit import AuditLog
from .clock import now
from .errors import (
    BatchStateError,
    FirmwareNotFoundError,
    RolloutStateError,
    RolloutValidationError,
)
from .firmwares import FirmwareService
from .models import (
    BatchStatus,
    DeviceUpdateState,
    Rollout,
    RolloutBatch,
    RolloutStatus,
    UpdateStage,
)
from .storage import Store

log = logging.getLogger("ota.rollouts")

_TERMINAL_FOR_BATCH = {
    UpdateStage.CONFIRMED, UpdateStage.FAILED,
    UpdateStage.ROLLED_BACK, UpdateStage.SHELVED,
}


class RolloutService:
    def __init__(self, store: Store, audit: AuditLog,
                 firmwares: FirmwareService, devices=None):
        self.store = store
        self.audit = audit
        self.firmwares = firmwares
        self.devices = devices

    # -------------------------------------------------------------- helpers
    def _get(self, rollout_id: str) -> Rollout:
        """调用方须持锁；返回发布或抛 unknown_rollout。"""
        r = self.store.rollouts.get(rollout_id)
        if r is None:
            raise RolloutValidationError(
                f"发布不存在: {rollout_id}",
                details={"reason": "unknown_rollout",
                         "rollout_id": rollout_id})
        return r

    def get(self, rollout_id: str) -> Rollout:
        with self.store.lock:
            self.store.ensure_loaded()
            return self._get(rollout_id)

    def summary(self, rollout_id: str) -> dict:
        with self.store.lock:
            r = self.get(rollout_id)
            return self._summary(r)

    def _summary(self, r: Rollout) -> dict:
        batches = []
        for b in r.batches:
            batches.append({"index": b.index, "status": b.status.value,
                            "capacity": b.capacity,
                            "claimed": len(b.claimed),
                            "succeeded": len(b.succeeded),
                            "failed": len(b.failed)})
        return {"rollout_id": r.rollout_id, "fw_id": r.fw_id,
                "target_version": r.target_version,
                "status": r.status.value, "batches": batches,
                "halted_reason": r.halted_reason,
                "stall": self._stall_overview(r)}

    def _stall_overview(self, r: Rollout) -> dict:
        """停滞概览：观察中 / 补救中 / 已搁置 三档计数与各自原因。

        - observing：持续上报同一阶段（same_stage_reports>0）但尚未被
          巡检判定，仍在停滞观察窗口内；
        - remedying：已被看门狗重置回 NONE 续跑（retries>0），补救机会
          尚未用尽；
        - shelved：补救用尽被搁置，不再占用批次容量、不阻塞推进。
        调用方须持锁。
        """
        observing: dict[str, str] = {}
        remedying: dict[str, str] = {}
        shelved: dict[str, str] = {}
        for (did, rid), s in self.store.update_states.items():
            if rid != r.rollout_id:
                continue
            if s.stage == UpdateStage.SHELVED:
                shelved[did] = s.last_error or "shelved"
            elif s.stage in (UpdateStage.CONFIRMED, UpdateStage.FAILED,
                             UpdateStage.ROLLED_BACK):
                continue
            elif s.retries > 0:
                remedying[did] = (
                    f"retry_{s.retries}_in_progress"
                    f"(stage={s.stage.value})")
            elif s.same_stage_reports > 0:
                observing[did] = (
                    f"same_stage_repeat x{s.same_stage_reports}"
                    f"(stage={s.stage.value})")
        return {"observing": len(observing),
                "remedying": len(remedying),
                "shelved": len(shelved),
                "reasons": {"observing": observing,
                            "remedying": remedying,
                            "shelved": shelved}}

    # ---------------------------------------------------------------- create
    def create(self, rollout_id: str, fw_id: str, device_ids: list[str],
               batch_size: int = 5, failure_rate_threshold: float = 0.2) -> dict:
        rollout_id = str(rollout_id).strip()
        if not rollout_id:
            raise RolloutValidationError("rollout_id 不能为空",
                                         details={"reason": "empty_id"})
        if not isinstance(batch_size, int) or isinstance(batch_size, bool) \
                or batch_size < 1:
            raise RolloutValidationError(
                "batch_size 必须 >= 1",
                details={"reason": "bad_batch_size", "batch_size": batch_size})
        if not (0.0 <= float(failure_rate_threshold) <= 1.0):
            raise RolloutValidationError(
                "failure_rate_threshold 必须在 [0, 1]",
                details={"reason": "bad_threshold",
                         "threshold": failure_rate_threshold})
        if not device_ids:
            raise RolloutValidationError(
                "device_ids 不能为空", details={"reason": "no_devices"})
        if len(set(device_ids)) != len(device_ids):
            raise RolloutValidationError(
                "device_ids 存在重复",
                details={"reason": "duplicate_devices"})

        with self.store.lock:
            self.store.ensure_loaded()
            if rollout_id in self.store.rollouts:
                raise RolloutValidationError(
                    f"发布 {rollout_id} 已存在",
                    details={"reason": "duplicate_rollout_id"})
            fw = self.store.firmwares.get(fw_id)
            if fw is None:
                raise FirmwareNotFoundError(
                    f"固件不存在: {fw_id}", details={"fw_id": fw_id})

            rejected: list[dict] = []
            for did in device_ids:
                device = self.store.devices.get(did)
                if device is None:
                    rejected.append({"device_id": did,
                                     "reason": "unknown_device"})
                    continue
                try:
                    if device.model != fw.model or device.hw_version != fw.hw_version:
                        from .errors import IncompatibleFirmwareError
                        raise IncompatibleFirmwareError(
                            "model/hw mismatch",
                            details={"reason": "model_hw_mismatch"})
                    self.firmwares.check_compatible(
                        device.model, device.hw_version,
                        device.fw_version, fw)
                except Exception as exc:  # noqa: BLE001 - 归类收集
                    rejected.append({"device_id": did,
                                     "reason": getattr(exc, "code", str(exc)),
                                     "detail": getattr(exc, "details", {})})
            if rejected:
                self.audit.record("rollout_create_rejected", {
                    "rollout_id": rollout_id, "fw_id": fw_id,
                    "rejected": rejected})
                raise RolloutValidationError(
                    f"发布校验失败：{len(rejected)}/{len(device_ids)} "
                    f"台设备不兼容，拒绝创建",
                    details={"reason": "incompatible_members",
                             "rejected": rejected})

            batches: list[RolloutBatch] = []
            for i in range(0, len(device_ids), batch_size):
                chunk = device_ids[i:i + batch_size]
                batches.append(RolloutBatch(index=len(batches),
                                            capacity=len(chunk)))
            r = Rollout(
                rollout_id=rollout_id, fw_id=fw_id,
                target_version=fw.version, model=fw.model,
                hw_version=fw.hw_version, device_ids=list(device_ids),
                batch_size=batch_size,
                failure_rate_threshold=float(failure_rate_threshold),
                batches=batches, created_at=now())
            self.store.rollouts[rollout_id] = r
            self.store.persist()
            self.audit.record("rollout_created", {
                "rollout_id": rollout_id, "fw_id": fw_id,
                "target": fw.version, "devices": len(device_ids),
                "batch_size": batch_size,
                "threshold": failure_rate_threshold,
                "batch_count": len(batches)})
            log.info("rollout create input=(id=%s fw=%s devices=%d bs=%d "
                     "threshold=%.2f) batches=%d",
                     rollout_id, fw_id, len(device_ids), batch_size,
                     failure_rate_threshold, len(batches))
            return self._summary(r)

    # -------------------------------------------------- lifecycle controls
    def start(self, rollout_id: str) -> dict:
        with self.store.lock:
            r = self._get(rollout_id)
            if r.status != RolloutStatus.PENDING:
                raise RolloutStateError(
                    f"发布处于 {r.status.value}，不能 start",
                    details={"reason": "not_pending",
                             "status": r.status.value})
            r.status = RolloutStatus.IN_PROGRESS
            r.batches[0].status = BatchStatus.ACTIVE
            r.batches[0].opened_at = now()
            self.store.persist()
            self.audit.record("rollout_started", {"rollout_id": rollout_id})
            log.info("rollout %s -> in_progress, batch 0 active", rollout_id)
            return self._summary(r)

    def pause(self, rollout_id: str) -> dict:
        with self.store.lock:
            r = self._get(rollout_id)
            if r.status != RolloutStatus.IN_PROGRESS:
                raise RolloutStateError(
                    f"发布处于 {r.status.value}，不能暂停",
                    details={"reason": "not_in_progress",
                             "status": r.status.value})
            r.status = RolloutStatus.PAUSED
            for b in r.batches:
                if b.status == BatchStatus.ACTIVE:
                    b.status = BatchStatus.PAUSED
            self.store.persist()
            self.audit.record("rollout_paused", {"rollout_id": rollout_id})
            log.info("rollout %s -> paused", rollout_id)
            return self._summary(r)

    def resume(self, rollout_id: str) -> dict:
        with self.store.lock:
            r = self._get(rollout_id)
            if r.status not in (RolloutStatus.PAUSED,
                                RolloutStatus.AUTO_HALTED):
                raise RolloutStateError(
                    f"发布处于 {r.status.value}，不能继续",
                    details={"reason": "not_resumable",
                             "status": r.status.value})
            reason = r.halted_reason
            r.status = RolloutStatus.IN_PROGRESS
            r.halted_reason = ""
            for b in r.batches:
                if b.status in (BatchStatus.PAUSED, BatchStatus.HALTED):
                    b.status = BatchStatus.ACTIVE
            self.store.persist()
            self.audit.record("rollout_resumed", {
                "rollout_id": rollout_id, "was_halted_reason": reason})
            log.info("rollout %s -> in_progress (resume from %s)",
                     rollout_id, reason or "paused")
            return self._summary(r)

    # ----------------------------------------------------------------- claim
    def claim(self, rollout_id: str, device_id: str, token: str) -> dict:
        # 身份校验（store 锁可重入，全程同一把锁）
        with self.store.lock:
            self.store.ensure_loaded()
            if self.devices is None:
                from .devices import DeviceService  # 延迟导入
                self.devices = DeviceService(self.store, self.audit)
            self.devices.authenticate(device_id, token)

            r = self.store.rollouts.get(rollout_id)
            if r is None:
                raise RolloutValidationError(
                    f"发布不存在: {rollout_id}",
                    details={"reason": "unknown_rollout"})

            state = self.store.update_states.get((device_id, rollout_id))
            if state is not None:
                # 幂等：断线重连重复认领返回既有任务，不重复计数
                if state.stage == UpdateStage.SHELVED:
                    raise BatchStateError(
                        f"设备 {device_id} 已被搁置，不得重新认领",
                        details={"reason": "device_shelved",
                                 "stage": state.stage.value,
                                 "shelved_reason": state.last_error})
                if state.stage in _TERMINAL_FOR_BATCH:
                    raise BatchStateError(
                        f"设备 {device_id} 在发布中已是终态 {state.stage.value}",
                        details={"reason": "already_terminal",
                                 "stage": state.stage.value})
                return {"rollout_id": rollout_id,
                        "batch_index": state.claimed_by_batch,
                        "target_version": r.target_version,
                        "fw_id": r.fw_id,
                        "download_url": f"local://firmware/{r.fw_id}",
                        "already_claimed": True,
                        "stage": state.stage.value}

            if r.status == RolloutStatus.PAUSED:
                raise BatchStateError("发布已暂停，暂不可认领",
                                      details={"reason": "rollout_paused"})
            if r.status == RolloutStatus.AUTO_HALTED:
                raise BatchStateError(
                    f"发布因失败率阈值自动停止: {r.halted_reason}",
                    details={"reason": "rollout_auto_halted",
                             "halted_reason": r.halted_reason})
            if r.status != RolloutStatus.IN_PROGRESS:
                raise BatchStateError(
                    f"发布处于 {r.status.value}，不可认领",
                    details={"reason": f"rollout_{r.status.value}"})

            if device_id not in r.device_ids:
                raise BatchStateError(
                    f"设备 {device_id} 不属于发布 {rollout_id}",
                    details={"reason": "not_in_rollout"})

            active = next((b for b in r.batches
                           if b.status in (BatchStatus.ACTIVE,
                                           BatchStatus.HALTED)), None)
            if active is None:
                raise BatchStateError("当前没有开放批次",
                                      details={"reason": "no_active_batch"})
            if device_id not in r.device_ids[
                    active.index * r.batch_size:
                    active.index * r.batch_size + active.capacity]:
                raise BatchStateError(
                    f"设备属于后续批次，需等待批次 {active.index} 完成",
                    details={"reason": "not_current_batch",
                             "current_batch": active.index})

            # 原子认领：锁内检查容量
            if device_id in active.claimed:
                pass  # 理论不可达（state 已存在），保留兜底
            elif len(active.claimed) >= active.capacity:
                raise BatchStateError(
                    "当前批次容量已满",
                    details={"reason": "batch_full"})
            else:
                active.claimed.append(device_id)

            state = DeviceUpdateState(
                device_id=device_id, rollout_id=rollout_id,
                stage=UpdateStage.NONE, updated_at=now(),
                stage_entered_at=now(),
                claimed_by_batch=active.index)
            self.store.update_states[(device_id, rollout_id)] = state
            self.store.persist()
            self.audit.record("device_claimed", {
                "rollout_id": rollout_id, "device_id": device_id,
                "batch_index": active.index,
                "batch_claimed": len(active.claimed),
                "batch_capacity": active.capacity})
            log.info("claim input=(rollout=%s device=%s) -> batch %d slot "
                     "%d/%d", rollout_id, device_id, active.index,
                     len(active.claimed), active.capacity)
            return {"rollout_id": rollout_id,
                    "batch_index": active.index,
                    "target_version": r.target_version, "fw_id": r.fw_id,
                    "download_url": f"local://firmware/{r.fw_id}",
                    "already_claimed": False, "stage": UpdateStage.NONE.value}

    # ------------------------------------------------ outcome & advancing
    def record_outcome(self, rollout_id: str, device_id: str,
                       stage: UpdateStage) -> None:
        """设备进入终态时由进度/回滚服务在同一把锁内调用。"""
        with self.store.lock:
            r = self.store.rollouts.get(rollout_id)
            if r is None:
                return
            state = self.store.update_states.get((device_id, rollout_id))
            if state is None:
                return
            batch = r.batches[state.claimed_by_batch]

            succeeded_set = set(batch.succeeded)
            failed_set = set(batch.failed)
            if stage == UpdateStage.CONFIRMED:
                succeeded_set.add(device_id)
                failed_set.discard(device_id)
            elif stage in (UpdateStage.FAILED, UpdateStage.SHELVED,
                           UpdateStage.ROLLED_BACK):
                failed_set.add(device_id)
                succeeded_set.discard(device_id)
            else:
                return
            batch.succeeded = [d for d in batch.claimed if d in succeeded_set]
            batch.failed = [d for d in batch.claimed if d in failed_set]

            total_out = len(batch.succeeded) + len(batch.failed)
            rate = (len(batch.failed) / total_out) if total_out else 0.0

            if r.status == RolloutStatus.IN_PROGRESS and total_out > 0 \
                    and rate >= r.failure_rate_threshold \
                    and batch.failed:
                r.status = RolloutStatus.AUTO_HALTED
                r.halted_reason = (
                    f"batch {batch.index} 失败率 {rate:.0%} 达到阈值 "
                    f"{r.failure_rate_threshold:.0%}")
                batch.status = BatchStatus.HALTED
                self.audit.record("rollout_auto_halted", {
                    "rollout_id": rollout_id, "batch_index": batch.index,
                    "failed": len(batch.failed),
                    "succeeded": len(batch.succeeded),
                    "failure_rate": rate,
                    "threshold": r.failure_rate_threshold})
                log.warning("rollout %s AUTO_HALTED batch=%d rate=%.2f",
                            rollout_id, batch.index, rate)

            self._advance_locked(r)
            self.store.persist()

    def _advance_locked(self, r: Rollout) -> None:
        """当前批次全部终态后收尾并开放下一批；全部完成则发布完成。"""
        if r.status in (RolloutStatus.PAUSED, RolloutStatus.COMPLETED,
                        RolloutStatus.ROLLED_BACK, RolloutStatus.PENDING):
            return
        cur = next((b for b in r.batches
                    if b.status in (BatchStatus.ACTIVE, BatchStatus.HALTED)),
                   None)
        if cur is None:
            return
        settled = set(cur.succeeded) | set(cur.failed)
        if len(cur.claimed) < cur.capacity or not all(
                d in settled for d in cur.claimed):
            return  # 还有已下发设备未收敛，等待（不卡死，看门狗会兜底）

        # 自动停止状态下当前批次收敛完也不自动开下一批，等待人工 resume
        if r.status == RolloutStatus.AUTO_HALTED:
            if cur.status == BatchStatus.ACTIVE:
                cur.status = BatchStatus.HALTED
            return

        cur.status = BatchStatus.DONE
        nxt = next((b for b in r.batches
                    if b.status == BatchStatus.WAITING), None)
        if nxt is None:
            r.status = RolloutStatus.COMPLETED
            self.audit.record("rollout_completed", {
                "rollout_id": r.rollout_id})
            log.info("rollout %s completed", r.rollout_id)
        else:
            nxt.status = BatchStatus.ACTIVE
            nxt.opened_at = now()
            self.audit.record("batch_opened", {
                "rollout_id": r.rollout_id, "batch_index": nxt.index})
            log.info("rollout %s batch %d active", r.rollout_id, nxt.index)
