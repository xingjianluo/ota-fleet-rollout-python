"""升级阶段推进、乱序/重放收敛、停滞识别与超时看门狗。

阶段链：NONE -> downloaded -> installed -> rebooted -> confirmed

上报收敛规则（服务器以 stage_seq 为权威游标）：
- seq < 已接受游标        -> 过期/重放：拒绝，不改变状态（不重复计数）；
- seq == 当前游标          -> 同一阶段的重复心跳：幂等返回当前状态；
- seq 相同但阶段不同       -> 同一序号携带矛盾阶段：拒绝；
- seq 更新且阶段推进       -> 接受（classification=advanced），重置进展游标；
- seq 更新但阶段不变       -> 接受但计入停滞观察
                              （classification=same_stage_repeat）；
- seq 跳号                 -> 允许（设备可能合并中间上报），但阶段必须
                              不回退；回退阶段按倒退拒绝；
- confirmed/failed/...    -> 终态后只接受相同阶段的幂等重放，其余拒绝；
- shelved                 -> 一律以 device_shelved 拒绝，不得静默回到
                              正常进度。

“活着”与“在推进”分离：updated_at 随每次接受的上报刷新，
stage_entered_at 只在阶段真正变化时刷新。看门狗据此识别两类停滞：
- silent_timeout      ：长期没有任何上报（updated_at 超时）；
- stalled_same_stage  ：持续上报同一阶段（updated_at 新鲜）但超过
                        stall_window 未推进。

补救策略：retries < max_retries 时重置回 NONE 让设备续跑；用尽后
置 SHELVED 搁置。搁置计入批次结果，因此单个卡死/停滞设备不会阻塞
整个批次；巡检本身幂等，重复执行不重复计数。
"""
from __future__ import annotations

import logging

from .audit import AuditLog
from .clock import now
from .errors import (
    RolloutValidationError,
    StageTransitionError,
    StaleReportError,
)
from .models import RolloutStatus, UpdateStage
from .storage import Store

log = logging.getLogger("ota.progress")

_ADVANCE = {
    UpdateStage.NONE: UpdateStage.DOWNLOADED,
    UpdateStage.DOWNLOADED: UpdateStage.INSTALLED,
    UpdateStage.INSTALLED: UpdateStage.REBOOTED,
    UpdateStage.REBOOTED: UpdateStage.CONFIRMED,
}


class ProgressService:
    def __init__(self, store: Store, audit: AuditLog, *,
                 stage_timeout: float = 60.0, max_retries: int = 2,
                 stall_window: float = 120.0,
                 rollouts=None, devices=None):
        self.store = store
        self.audit = audit
        self.stage_timeout = stage_timeout
        self.max_retries = max_retries
        # 停滞观察窗口：阶段进入后超过该时长仍未推进（且期间有同阶段
        # 重复上报，即设备“活着但不推进”），判定为停滞并进入补救
        self.stall_window = stall_window
        self.rollouts = rollouts
        self.devices = devices

    def _services(self):
        if self.devices is None:
            from .devices import DeviceService
            self.devices = DeviceService(self.store, self.audit)
        if self.rollouts is None:
            from .rollouts import RolloutService
            self.rollouts = RolloutService(self.store, self.audit, None,
                                           self.devices)
        return self.devices, self.rollouts

    # --------------------------------------------------------------- report
    def report(self, device_id: str, token: str, rollout_id: str,
               stage: str, seq: int) -> dict:
        devices, rollouts = self._services()
        try:
            wanted = UpdateStage(stage)
        except ValueError:
            raise StageTransitionError(
                f"未知升级阶段: {stage!r}",
                details={"reason": "unknown_stage", "stage": stage})
        if not isinstance(seq, int) or isinstance(seq, bool) or seq <= 0:
            raise StageTransitionError(
                "上报 seq 必须为正整数",
                details={"reason": "bad_seq", "input_seq": seq})

        with self.store.lock:
            self.store.ensure_loaded()
            devices.authenticate(device_id, token)
            state = self.store.update_states.get((device_id, rollout_id))
            r = self.store.rollouts.get(rollout_id)
            if state is None or r is None:
                raise RolloutValidationError(
                    f"设备 {device_id} 尚未认领发布 {rollout_id}",
                    details={"reason": "not_claimed",
                             "rollout_id": rollout_id})

            old_stage = state.stage
            # 0) 搁置设备：任何进度上报都明确拒绝，不得静默回到正常进度
            if old_stage == UpdateStage.SHELVED:
                self.audit.record("progress_rejected", {
                    "device_id": device_id, "rollout_id": rollout_id,
                    "reason": "device_shelved",
                    "input_seq": seq, "input_stage": wanted.value,
                    "stage": old_stage.value})
                raise StageTransitionError(
                    f"设备已被搁置（{state.last_error or 'shelved'}），"
                    f"拒绝进度上报 {wanted.value}",
                    details={"reason": "device_shelved",
                             "stage": old_stage.value,
                             "shelved_reason": state.last_error,
                             "input_stage": wanted.value})

            # 1) 乱序/迟到：seq 回退
            if seq < state.stage_seq:
                self.audit.record("progress_rejected", {
                    "device_id": device_id, "rollout_id": rollout_id,
                    "reason": "seq_regression", "input_seq": seq,
                    "accepted_seq": state.stage_seq,
                    "input_stage": wanted.value, "stage": old_stage.value})
                raise StaleReportError(
                    f"过期上报被拒绝: seq {seq} < 已接受 {state.stage_seq}",
                    details={"reason": "seq_regression",
                             "input_seq": seq,
                             "accepted_seq": state.stage_seq})

            # 2) 终态保护
            if old_stage in (UpdateStage.CONFIRMED, UpdateStage.FAILED,
                             UpdateStage.ROLLED_BACK, UpdateStage.SHELVED):
                if seq == state.stage_seq and wanted == old_stage:
                    return self._view(state, duplicate=True)  # 幂等重放
                self.audit.record("progress_rejected", {
                    "device_id": device_id, "rollout_id": rollout_id,
                    "reason": "terminal_stage_locked",
                    "input_stage": wanted.value, "stage": old_stage.value})
                raise StageTransitionError(
                    f"设备已处于终态 {old_stage.value}，拒绝迁移到 "
                    f"{wanted.value}",
                    details={"reason": "terminal_stage_locked",
                             "stage": old_stage.value,
                             "input_stage": wanted.value})

            # 3) 相同 seq：只能是同一阶段的重复上报
            if seq == state.stage_seq:
                if wanted != old_stage:
                    self.audit.record("progress_rejected", {
                        "device_id": device_id, "rollout_id": rollout_id,
                        "reason": "same_seq_conflicting_stage",
                        "seq": seq, "input_stage": wanted.value,
                        "stage": old_stage.value})
                    raise StageTransitionError(
                        f"seq {seq} 携带矛盾阶段 {wanted.value} "
                        f"!= 已记录 {old_stage.value}",
                        details={"reason": "same_seq_conflicting_stage",
                                 "seq": seq})
                return self._view(state, duplicate=True)

            # 4) seq 更新：阶段不得倒退（NONE 仅在看门狗重置后配合新 seq）
            if wanted != UpdateStage.FAILED and \
                    wanted not in (UpdateStage.DOWNLOADED,
                                   UpdateStage.INSTALLED,
                                   UpdateStage.REBOOTED,
                                   UpdateStage.CONFIRMED):
                raise StageTransitionError(
                    f"设备不可直接上报到 {wanted.value}",
                    details={"reason": "illegal_stage_report",
                             "input_stage": wanted.value})
            if wanted == UpdateStage.FAILED:
                state.stage = UpdateStage.FAILED
            elif self._rank(wanted) < self._rank(old_stage) and \
                    not (old_stage == UpdateStage.NONE):
                self.audit.record("progress_rejected", {
                    "device_id": device_id, "rollout_id": rollout_id,
                    "reason": "stage_regression",
                    "input_stage": wanted.value, "stage": old_stage.value})
                raise StageTransitionError(
                    f"阶段倒退被拒绝: {old_stage.value} -> {wanted.value}",
                    details={"reason": "stage_regression",
                             "from": old_stage.value, "to": wanted.value})
            else:
                state.stage = wanted

            state.stage_seq = seq
            state.updated_at = now()
            state.last_error = ""
            if state.stage != old_stage:
                # 真正推进（或失败）：重置进展游标与同阶段计数
                state.stage_entered_at = state.updated_at
                state.same_stage_reports = 0
                classification = ("failed" if state.stage == UpdateStage.FAILED
                                  else "advanced")
            else:
                # 同一阶段的新 seq 重复上报：设备活着但未推进，计入观察
                state.same_stage_reports += 1
                classification = "same_stage_repeat"

            # confirmed：提交设备版本，批次计数
            committed = False
            if state.stage == UpdateStage.CONFIRMED:
                device = self.store.devices[device_id]
                prev = device.fw_version
                device.fw_version = r.target_version
                committed = True
                self.audit.record("device_version_committed", {
                    "device_id": device_id, "rollout_id": rollout_id,
                    "from_version": prev,
                    "to_version": r.target_version})

            self.audit.record("stage_transition", {
                "device_id": device_id, "rollout_id": rollout_id,
                "from": old_stage.value, "to": state.stage.value,
                "seq": seq, "classification": classification,
                "same_stage_reports": state.same_stage_reports})
            log.info("progress input=(device=%s rollout=%s seq=%d stage=%s) "
                     "%s -> %s [%s]%s", device_id, rollout_id, seq,
                     wanted.value, old_stage.value, state.stage.value,
                     classification, " committed" if committed else "")

            self.store.persist()
            rollouts.record_outcome(rollout_id, device_id, state.stage)
            return self._view(state, duplicate=False,
                              classification=classification)

    def report_failure(self, device_id: str, token: str, rollout_id: str,
                       seq: int, error: str = "") -> dict:
        devices, _ = self._services()
        with self.store.lock:
            devices.authenticate(device_id, token)
            state = self.store.update_states.get((device_id, rollout_id))
            if state is None:
                raise RolloutValidationError(
                    "设备尚未认领该发布", details={"reason": "not_claimed"})
            if seq <= state.stage_seq and state.stage != UpdateStage.FAILED:
                raise StaleReportError(
                    "失败上报 seq 已过期", details={"reason": "seq_regression"})
            state.stage = UpdateStage.FAILED
            state.stage_seq = max(seq, state.stage_seq)
            state.updated_at = now()
            state.last_error = error or "device_reported_failure"
            self.store.persist()
            self.audit.record("device_failed", {
                "device_id": device_id, "rollout_id": rollout_id,
                "seq": state.stage_seq, "error": state.last_error})
            log.warning("progress failure device=%s seq=%d error=%s",
                        device_id, state.stage_seq, state.last_error)
            self.rollouts.record_outcome(rollout_id, device_id,
                                         UpdateStage.FAILED)
            return self._view(state, duplicate=False,
                              classification="failed")

    # ------------------------------------------------------------- watchdog
    def watchdog(self, rollout_id: str | None = None) -> dict:
        """停滞巡检：对进行中发布里不再推进的设备给予补救或搁置。

        两类触发（互斥归类，reason 可区分）：
        - silent_timeout：updated_at 超过 stage_timeout 没有任何上报；
        - stalled_same_stage：设备持续上报（updated_at 新鲜）但停留同一
          阶段超过 stall_window（same_stage_reports > 0 证明“活着”）。

        补救 = retries+1 并重置回 NONE 让设备续跑；补救次数用尽 -> SHELVED。
        补救会同时刷新 updated_at/stage_entered_at 并清零 same_stage_reports，
        因此重复执行巡检幂等：不会重复累加 retries、不会重复搁置。
        """
        ts = now()
        retried, shelved = [], []
        with self.store.lock:
            self.store.ensure_loaded()
            _, rollouts = self._services()
            targets = [self.store.rollouts[rollout_id]] if rollout_id \
                else list(self.store.rollouts.values())
            active_ids = {
                r.rollout_id for r in targets
                if r.status in (RolloutStatus.IN_PROGRESS,
                                RolloutStatus.AUTO_HALTED,
                                RolloutStatus.PAUSED)}
            # 单趟扫描全部设备状态，避免按发布嵌套循环（O(发布数×设备数)）
            for state in list(self.store.update_states.values()):
                if state.rollout_id not in active_ids:
                    continue
                if state.stage in (UpdateStage.CONFIRMED,
                                   UpdateStage.FAILED,
                                   UpdateStage.ROLLED_BACK,
                                   UpdateStage.SHELVED):
                    continue
                # NONE 表示设备刚认领、仍在正常下载准备窗口内，
                # 只有看门狗重试后回到 NONE（retries>0）才继续监控
                if state.stage == UpdateStage.NONE \
                        and state.retries == 0:
                    continue
                silent = ts - state.updated_at > self.stage_timeout
                stalled = state.same_stage_reports > 0 \
                    and ts - state.stage_entered_at > self.stall_window
                if not (silent or stalled):
                    continue
                reason = "stalled_same_stage" if stalled else "silent_timeout"
                stuck_for = ts - state.stage_entered_at
                if state.retries < self.max_retries:
                    state.retries += 1
                    prev = state.stage
                    state.stage = UpdateStage.NONE
                    state.updated_at = ts
                    state.stage_entered_at = ts
                    state.same_stage_reports = 0
                    retried.append(state.device_id)
                    self.audit.record("watchdog_retry", {
                        "device_id": state.device_id,
                        "rollout_id": state.rollout_id,
                        "from_stage": prev.value,
                        "retries": state.retries,
                        "reason": reason,
                        "stuck_seconds": stuck_for})
                    log.warning("watchdog retry device=%s stage=%s "
                                "reason=%s stuck=%.0fs retries=%d",
                                state.device_id, prev.value, reason,
                                stuck_for, state.retries)
                else:
                    state.stage = UpdateStage.SHELVED
                    state.updated_at = ts
                    state.last_error = (
                        f"shelved_after_{state.retries}_retries({reason})")
                    shelved.append(state.device_id)
                    self.audit.record("watchdog_shelved", {
                        "device_id": state.device_id,
                        "rollout_id": state.rollout_id,
                        "retries": state.retries,
                        "reason": reason,
                        "stuck_seconds": stuck_for})
                    log.error("watchdog shelve device=%s retries=%d "
                              "reason=%s stuck=%.0fs", state.device_id,
                              state.retries, reason, stuck_for)
                    rollouts.record_outcome(
                        state.rollout_id, state.device_id,
                        UpdateStage.SHELVED)
            if retried or shelved:
                self.store.persist()
        return {"retried": retried, "shelved": shelved}

    # ------------------------------------------------------------ reconcile
    def reconcile(self, device_id: str, token: str, rollout_id: str,
                  client_seq: int) -> dict:
        """设备重连后对账：返回权威状态；迟到的客户端进度被丢弃但可感知。"""
        devices, _ = self._services()
        with self.store.lock:
            devices.authenticate(device_id, token)
            state = self.store.update_states.get((device_id, rollout_id))
            if state is None:
                raise RolloutValidationError(
                    "设备尚未认领该发布",
                    details={"reason": "not_claimed"})
            behind = client_seq < state.stage_seq
            view = self._view(state, duplicate=False,
                              classification="reconciled")
            view["client_seq"] = client_seq
            view["accepted_seq"] = state.stage_seq
            view["client_is_behind"] = behind
            view["action"] = ("resume_from_authoritative_stage"
                              if behind else "in_sync")
            self.audit.record("reconcile", {
                "device_id": device_id, "rollout_id": rollout_id,
                "client_seq": client_seq,
                "server_seq": state.stage_seq, "behind": behind,
                "authoritative_stage": state.stage.value})
            log.info("reconcile device=%s client_seq=%d server_seq=%d "
                     "stage=%s behind=%s", device_id, client_seq,
                     state.stage_seq, state.stage.value, behind)
            return view

    # --------------------------------------------------------------- utils
    @staticmethod
    def _rank(stage: UpdateStage) -> int:
        order = [UpdateStage.NONE, UpdateStage.DOWNLOADED,
                 UpdateStage.INSTALLED, UpdateStage.REBOOTED,
                 UpdateStage.CONFIRMED]
        return order.index(stage)

    def _view(self, state, *, duplicate: bool,
              classification: str | None = None) -> dict:
        return {"device_id": state.device_id,
                "rollout_id": state.rollout_id,
                "stage": state.stage.value, "seq": state.stage_seq,
                "retries": state.retries,
                "server_ts": state.updated_at,
                "duplicate": duplicate,
                "classification": classification or (
                    "duplicate" if duplicate else "advanced"),
                "same_stage_reports": state.same_stage_reports,
                "stage_entered_at": state.stage_entered_at}
