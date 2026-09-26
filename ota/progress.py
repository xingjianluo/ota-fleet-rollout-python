"""升级阶段推进、乱序/重放收敛、超时看门狗与停滞识别。

阶段链：NONE -> downloaded -> installed -> rebooted -> confirmed

上报收敛规则（服务器以 stage_seq 为权威游标）：
- seq < 已接受游标        -> 过期/重放：拒绝，不改变状态（不重复计数）；
- seq == 当前游标          -> 同一阶段的重复心跳：幂等返回当前状态；
- seq 相同但阶段不同       -> 同一序号携带矛盾阶段：拒绝；
- seq 跳号                 -> 允许（设备可能合并中间上报），但阶段必须
                              不回退；回退阶段按倒退拒绝；
- confirmed/failed/...    -> 终态后只接受相同阶段的幂等重放，其余拒绝。

“活着”不等于“在推进”：seq/时间持续更新但阶段停在同一步属于重复上报，
不移动停滞锚点 stage_entered_at；停滞识别（ota.stalls）依据该锚点判定：
观察窗口 -> 有限补救 -> 机会用尽 SHELVED。

看门狗：某阶段停留超过 stage_timeout 时，retries < max_retries 将状态
重置回 NONE 让设备重领（重新下载）；超过则置 SHELVED 搁置。两者都计入
批次结果，因此单个卡死设备不会阻塞整个批次。
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
from .models import RolloutStatus, StallState, UpdateStage
from .storage import Store

log = logging.getLogger("ota.progress")

_ADVANCE = {
    UpdateStage.NONE: UpdateStage.DOWNLOADED,
    UpdateStage.DOWNLOADED: UpdateStage.INSTALLED,
    UpdateStage.INSTALLED: UpdateStage.REBOOTED,
    UpdateStage.REBOOTED: UpdateStage.CONFIRMED,
}

_TERMINAL_STAGES = (UpdateStage.CONFIRMED, UpdateStage.FAILED,
                    UpdateStage.ROLLED_BACK, UpdateStage.SHELVED)


class ProgressService:
    def __init__(self, store: Store, audit: AuditLog, *,
                 stage_timeout: float = 60.0, max_retries: int = 2,
                 rollouts=None, devices=None, stalls=None):
        self.store = store
        self.audit = audit
        self.stage_timeout = stage_timeout
        self.max_retries = max_retries
        self.rollouts = rollouts
        self.devices = devices
        self.stalls = stalls

    def _services(self):
        if self.devices is None:
            from .devices import DeviceService
            self.devices = DeviceService(self.store, self.audit)
        if self.rollouts is None:
            from .rollouts import RolloutService
            self.rollouts = RolloutService(self.store, self.audit, None,
                                           self.devices)
        if self.stalls is None:
            from .stalls import StallService
            self.stalls = StallService(self.store, self.audit,
                                       max_remedies=self.max_retries,
                                       rollouts=self.rollouts)
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
            ts = now()
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
            if old_stage in _TERMINAL_STAGES:
                if seq == state.stage_seq and wanted == old_stage:
                    # 幂等重放：搁置设备也要拿到明确的搁置反馈，不许静默回归
                    return self._view(
                        state, duplicate=True,
                        report_class=("shelved_duplicate"
                                      if old_stage == UpdateStage.SHELVED
                                      else "duplicate"))
                reason = ("shelved_locked"
                          if old_stage == UpdateStage.SHELVED
                          else "terminal_stage_locked")
                self.audit.record("progress_rejected", {
                    "device_id": device_id, "rollout_id": rollout_id,
                    "reason": reason,
                    "input_stage": wanted.value, "stage": old_stage.value})
                raise StageTransitionError(
                    f"设备已处于终态 {old_stage.value}，拒绝迁移到 "
                    f"{wanted.value}",
                    details={"reason": reason,
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
                # 活着 ≠ 在推进：同阶段重放不移动停滞锚点
                return self._view(state, duplicate=True,
                                  report_class="same_stage_duplicate")

            # 4) seq 更新：阶段不得倒退（NONE 仅在看门狗/停滞重置后配合新 seq）
            if wanted != UpdateStage.FAILED and \
                    wanted not in (UpdateStage.DOWNLOADED,
                                   UpdateStage.INSTALLED,
                                   UpdateStage.REBOOTED,
                                   UpdateStage.CONFIRMED):
                raise StageTransitionError(
                    f"设备不可直接上报到 {wanted.value}",
                    details={"reason": "illegal_stage_report",
                             "input_stage": wanted.value})

            is_failure = wanted == UpdateStage.FAILED
            advanced = (is_failure or self._rank(wanted) >= self._rank(
                old_stage))
            if not advanced:
                self.audit.record("progress_rejected", {
                    "device_id": device_id, "rollout_id": rollout_id,
                    "reason": "stage_regression",
                    "input_stage": wanted.value, "stage": old_stage.value})
                raise StageTransitionError(
                    f"阶段倒退被拒绝: {old_stage.value} -> {wanted.value}",
                    details={"reason": "stage_regression",
                             "from": old_stage.value, "to": wanted.value})

            stage_changed = wanted != old_stage
            same_stage_repeat = not is_failure and not stage_changed

            # 5) 停滞识别（只看阶段是否前移，与 seq 新鲜度无关）。
            #    NONE -> 某阶段：补救后的重新开始或首次进展。
            report_class = "progress"
            if is_failure:
                state.stage = UpdateStage.FAILED
                state.stall_state = StallState.IDLE
                state.stall_reason = ""
            elif old_stage == UpdateStage.NONE:
                state.stage = wanted
                if state.retries > 0:
                    # 看门狗/停滞补救后的重新开始：仍在补救观察期，真正
                    # 阶段前移后才回到 IDLE
                    state.stall_state = StallState.REMEDIATING
                    state.stall_reason = "remedy_resumed"
                    report_class = "remedy_resumed"
            elif same_stage_repeat:
                state.stage = wanted
                report_class = self._classify_same_stage(
                    state, ts, self.stalls.stall_window,
                    audit=self.audit)
            else:
                # 真正的阶段前移：清掉停滞簿记，重新开始观察
                state.stage = wanted
                state.stall_state = StallState.IDLE
                state.stall_reason = ""
                state.stall_since = 0.0

            state.stage_seq = seq
            state.updated_at = ts
            if stage_changed or is_failure:
                # 停滞锚点只随“阶段迁移”移动；同阶段重复不移动
                state.stage_entered_at = ts
            state.last_error = ""

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
                "seq": seq, "report_class": report_class})
            log.info("progress input=(device=%s rollout=%s seq=%d stage=%s) "
                     "%s -> %s%s [%s]", device_id, rollout_id, seq,
                     wanted.value, old_stage.value, state.stage.value,
                     " committed" if committed else "", report_class)

            self.store.persist()
            if is_failure:
                rollouts.record_outcome(rollout_id, device_id,
                                        UpdateStage.FAILED)
            else:
                rollouts.record_outcome(rollout_id, device_id, state.stage)
            return self._view(state, duplicate=False,
                              report_class=report_class)

    def report_failure(self, device_id: str, token: str, rollout_id: str,
                       seq: int, error: str = "") -> dict:
        devices, _ = self._services()
        with self.store.lock:
            devices.authenticate(device_id, token)
            state = self.store.update_states.get((device_id, rollout_id))
            if state is None:
                raise RolloutValidationError(
                    "设备尚未认领该发布", details={"reason": "not_claimed"})
            # 终态保护：搁置/已确认/已回滚的设备不得借失败上报改态，
            # 已搁置设备尤其不能静默脱离搁置（FAILED 自身允许补充错误信息）
            if state.stage in _TERMINAL_STAGES \
                    and state.stage != UpdateStage.FAILED:
                reason = ("shelved_locked"
                          if state.stage == UpdateStage.SHELVED
                          else "terminal_stage_locked")
                self.audit.record("progress_rejected", {
                    "device_id": device_id, "rollout_id": rollout_id,
                    "reason": reason, "stage": state.stage.value,
                    "input": "failure"})
                raise StageTransitionError(
                    f"设备已处于终态 {state.stage.value}，拒绝失败上报",
                    details={"reason": reason, "stage": state.stage.value})
            if seq <= state.stage_seq and state.stage != UpdateStage.FAILED:
                raise StaleReportError(
                    "失败上报 seq 已过期", details={"reason": "seq_regression"})
            state.stage = UpdateStage.FAILED
            state.stage_seq = max(seq, state.stage_seq)
            state.updated_at = now()
            state.last_error = error or "device_reported_failure"
            state.stall_state = StallState.IDLE
            state.stall_reason = ""
            self.store.persist()
            self.audit.record("device_failed", {
                "device_id": device_id, "rollout_id": rollout_id,
                "seq": state.stage_seq, "error": state.last_error})
            log.warning("progress failure device=%s seq=%d error=%s",
                        device_id, state.stage_seq, state.last_error)
            self.rollouts.record_outcome(rollout_id, device_id,
                                         UpdateStage.FAILED)
            return self._view(state, duplicate=False)

    # ------------------------------------------------------------- watchdog
    def watchdog(self, rollout_id: str | None = None) -> dict:
        """扫描进行中发布里超时停留的设备：重试或搁置。"""
        ts = now()
        retried, shelved = [], []
        with self.store.lock:
            self.store.ensure_loaded()
            _, rollouts = self._services()
            targets = [self.store.rollouts[rollout_id]] if rollout_id \
                else list(self.store.rollouts.values())
            for r in targets:
                if r.status not in (RolloutStatus.IN_PROGRESS,
                                    RolloutStatus.AUTO_HALTED,
                                    RolloutStatus.PAUSED):
                    continue
                for state in list(self.store.update_states.values()):
                    if state.rollout_id != r.rollout_id:
                        continue
                    if state.stage in _TERMINAL_STAGES:
                        continue
                    # NONE 表示设备刚认领、仍在正常下载准备窗口内，
                    # 只有看门狗重试后回到 NONE（retries>0）才继续监控
                    if state.stage == UpdateStage.NONE \
                            and state.retries == 0:
                        continue
                    if ts - state.updated_at <= self.stage_timeout:
                        continue
                    stuck_for = ts - state.updated_at
                    if state.retries < self.max_retries:
                        prev_stage = state.stage
                        # 与停滞巡检共享补救预算与簿记：重置回 NONE 只发生一次
                        self.stalls._reset_to_none(
                            state, ts, reason="watchdog_timeout_remedy",
                            audit=self.audit,
                            detail_extra={"stuck_seconds": stuck_for,
                                          "kind": "watchdog_retry",
                                          "from_stage": prev_stage.value})
                        retried.append(state.device_id)
                        log.warning("watchdog retry device=%s stage=%s "
                                    "stuck=%.0fs retries=%d",
                                    state.device_id, prev_stage.value,
                                    stuck_for, state.retries)
                    else:
                        self.stalls._shelve(
                            state, ts,
                            reason=f"shelved_after_{state.retries}_retries",
                            audit=self.audit,
                            detail_extra={"stuck_seconds": stuck_for,
                                          "kind": "watchdog_shelved"})
                        shelved.append(state.device_id)
                        log.error("watchdog shelve device=%s retries=%d "
                                  "stuck=%.0fs", state.device_id,
                                  state.retries, stuck_for)
                        rollouts.record_outcome(
                            r.rollout_id, state.device_id,
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
            view = self._view(state, duplicate=False)
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

    # ------------------------------------------------------------- sweeps
    def stall_sweep(self, rollout_id: str | None = None) -> dict:
        """停滞巡检（代理到 StallService）；重复/并发调用幂等。"""
        self._services()
        return self.stalls.sweep(rollout_id)

    # ------------------------------------------------------------ classify
    def _classify_same_stage(self, state, ts: float, window: float,
                             audit=None) -> str:
        """seq 更新但阶段相同（设备活着但没有推进）的反馈分类。

        调用方须持锁。返回：
        - ``same_stage_within_window``：仍在观察窗口内；
        - ``same_stage_observing``：已过观察窗口、尚在补救窗口内；
        - ``same_stage_remedying``：给过补救机会后仍未推进；
        IDLE 且已过窗口时惰性转入 OBSERVING（与 sweep 判定一致）。
        """
        if state.stall_state == StallState.IDLE:
            if ts - state.stage_entered_at > window:
                state.stall_state = StallState.OBSERVING
                state.stall_since = ts
                state.stall_reason = "stalled_no_progress"
                if audit is not None:
                    audit.record("stall_observed", {
                        "device_id": state.device_id,
                        "rollout_id": state.rollout_id,
                        "stage": state.stage.value,
                        "stuck_seconds": ts - state.stage_entered_at,
                        "window": window,
                        "trigger": "same_stage_report"})
                return "same_stage_observing"
            return "same_stage_within_window"
        if state.stall_state == StallState.OBSERVING:
            return "same_stage_observing"
        return "same_stage_remedying"

    # --------------------------------------------------------------- utils
    @staticmethod
    def _rank(stage: UpdateStage) -> int:
        order = [UpdateStage.NONE, UpdateStage.DOWNLOADED,
                 UpdateStage.INSTALLED, UpdateStage.REBOOTED,
                 UpdateStage.CONFIRMED]
        return order.index(stage)

    def _view(self, state, *, duplicate: bool,
              report_class: str = "progress") -> dict:
        return {"device_id": state.device_id,
                "rollout_id": state.rollout_id,
                "stage": state.stage.value, "seq": state.stage_seq,
                "retries": state.retries,
                "server_ts": state.updated_at,
                "duplicate": duplicate,
                "report_class": report_class,
                "stall_state": state.stall_state.value,
                "stall_reason": state.stall_reason}
