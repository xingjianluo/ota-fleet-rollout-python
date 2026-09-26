"""停滞识别与自动续跑。

核心目标：把“设备还活着”和“设备在推进”分开看。心跳正常、上报的 seq
与时间持续更新，但升级阶段一直停在同一步，仍然算停滞。判定锚点是
``DeviceUpdateState.stage_entered_at``（仅在阶段真正前移时更新），重复
上报只刷新 ``updated_at``，不会把停滞时钟往后推。

生命周期（stall_state）::

    IDLE ──(同阶段无进展超过 stall_window)──► OBSERVING
    OBSERVING ──(再给 remedy_grace 仍无进展)──► REMEDIATING（阶段重置回 NONE）
    REMEDIATING ──(再次停滞，按补救预算)──► REMEDIATING … ──► SHELVED

- 补救机会有上限（max_remedies），补救预算 ``state.retries`` 与阶段看门狗
  共享：两者合计最多补救 max_remedies 次，机会用尽进入 SHELVED。
- SHELVED 是终态：计入批次失败、不占容量、不阻塞推进，且不得静默回到正常。
- sweep 是纯锁内状态机：每次重复扫描对同一设备至多发生一次合法迁移，
  因此重复/并发触发幂等，不会重复计数或重复搁置。
"""
from __future__ import annotations

import logging

from .audit import AuditLog
from .clock import now
from .models import RolloutStatus, StallState, UpdateStage
from .storage import Store

log = logging.getLogger("ota.stalls")

# 停滞巡检不处理的终态（NONE 单独判断：刚认领豁免、补救后兜底）
_SKIP_STAGES = (
    UpdateStage.CONFIRMED,
    UpdateStage.FAILED,
    UpdateStage.ROLLED_BACK,
    UpdateStage.SHELVED,
)

_WATCHED_ROLLOUT_STATUS = (
    RolloutStatus.IN_PROGRESS,
    RolloutStatus.AUTO_HALTED,
    RolloutStatus.PAUSED,
)


class StallService:
    def __init__(self, store: Store, audit: AuditLog, *,
                 stall_window: float = 60.0, remedy_grace: float | None = None,
                 max_remedies: int = 2, rollouts=None):
        self.store = store
        self.audit = audit
        self.stall_window = stall_window
        self.remedy_grace = stall_window if remedy_grace is None \
            else remedy_grace
        self.max_remedies = max_remedies
        self.rollouts = rollouts

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _reset_to_none(state, ts: float, *, reason: str,
                       audit: "AuditLog | None" = None,
                       detail_extra: dict | None = None) -> None:
        """给一次补救机会：阶段重置回 NONE，记录预算与停滞簿记。

        调用方必须持 store.lock；retries 的自增只在这里与看门狗各自
        恰好发生一次。
        """
        prev = state.stage
        state.retries += 1
        state.stage = UpdateStage.NONE
        state.updated_at = ts
        state.stage_entered_at = ts
        state.stall_state = StallState.REMEDIATING
        state.stall_since = ts
        state.stall_reason = reason
        if audit is not None:
            detail = {"device_id": state.device_id,
                      "rollout_id": state.rollout_id,
                      "from_stage": prev.value, "retries": state.retries,
                      "reason": reason}
            if detail_extra:
                detail.update(detail_extra)
            audit.record("stall_remedy", detail)

    @staticmethod
    def _shelve(state, ts: float, *, reason: str, audit,
                detail_extra: dict | None = None) -> None:
        state.stage = UpdateStage.SHELVED
        state.updated_at = ts
        state.last_error = reason
        state.stall_reason = reason
        detail = {"device_id": state.device_id,
                  "rollout_id": state.rollout_id,
                  "retries": state.retries, "reason": reason}
        if detail_extra:
            detail.update(detail_extra)
        audit.record("stall_shelved", detail)

    def _services(self):
        if self.rollouts is None:
            from .rollouts import RolloutService  # 延迟导入避免环
            self.rollouts = RolloutService(self.store, self.audit, None)
        return self.rollouts

    # -------------------------------------------------------------- sweep
    def sweep(self, rollout_id: str | None = None) -> dict:
        """巡检一次：观察标记 → 补救重置 → 搁置。

        返回 ``{"observing": [...], "remedied": [...], "shelved": [...]}。
        纯锁内状态机，重复/并发调用幂等：预算只增一次、搁置只发生一次、
        批次推进只触发一次。
        """
        ts = now()
        observing: list[str] = []
        remedied: list[str] = []
        shelved: list[str] = []
        with self.store.lock:
            self.store.ensure_loaded()
            rollouts = self._services()
            if rollout_id is not None:
                r = self.store.rollouts.get(rollout_id)
                targets = [r] if r is not None else []
            else:
                targets = list(self.store.rollouts.values())
            changed = False
            for r in targets:
                if r.status not in _WATCHED_ROLLOUT_STATUS:
                    continue
                for state in list(self.store.update_states.values()):
                    if state.rollout_id != r.rollout_id \
                            or state.stage in _SKIP_STAGES:
                        continue
                    # 刚认领还停在 NONE（没有任何停滞历史）仍在正常下载
                    # 准备窗口，不监控；被补救回 NONE 后仍不续跑的设备
                    # （REMEDIATING）要继续兜底，不能漏网。
                    if state.stage == UpdateStage.NONE \
                            and state.stall_state == StallState.IDLE:
                        continue
                    stuck_for = ts - state.stage_entered_at
                    if state.stall_state == StallState.IDLE:
                        # 第一层：超过观察窗口 -> 标记观察（尚无处罚）
                        if stuck_for > self.stall_window:
                            state.stall_state = StallState.OBSERVING
                            state.stall_since = ts
                            state.stall_reason = "stalled_no_progress"
                            observing.append(state.device_id)
                            changed = True
                            self.audit.record("stall_observed", {
                                "device_id": state.device_id,
                                "rollout_id": r.rollout_id,
                                "stage": state.stage.value,
                                "stuck_seconds": stuck_for,
                                "window": self.stall_window})
                            log.warning("stall observe device=%s stage=%s "
                                        "stuck=%.0fs", state.device_id,
                                        state.stage.value, stuck_for)
                        continue

                    # 已在观察/补救中：
                    # - observing 从标记时刻起再等 remedy_grace 才给第一次补救；
                    # - remediating 重新进入某阶段后，再过 stall_window 仍
                    #   无进展，则给下一次补救或搁置。
                    if state.stall_state == StallState.OBSERVING:
                        deadline = state.stall_since + self.remedy_grace
                    else:
                        deadline = state.stage_entered_at + self.stall_window
                    if ts < deadline:
                        continue
                    if state.retries < self.max_remedies:
                        self._reset_to_none(
                            state, ts, reason="stall_no_progress_remedy",
                            audit=self.audit)
                        remedied.append(state.device_id)
                        changed = True
                        log.warning("stall remedy device=%s retries=%d "
                                    "stuck=%.0fs", state.device_id,
                                    state.retries, stuck_for)
                    else:
                        self._shelve(
                            state, ts,
                            reason="shelved_stalled_after_remedies",
                            audit=self.audit,
                            detail_extra={"stuck_seconds": stuck_for})
                        shelved.append(state.device_id)
                        changed = True
                        log.error("stall shelve device=%s retries=%d "
                                  "stuck=%.0fs", state.device_id,
                                  state.retries, stuck_for)
                        rollouts.record_outcome(
                            r.rollout_id, state.device_id,
                            UpdateStage.SHELVED)
            if changed:
                self.store.persist()
        return {"observing": observing, "remedied": remedied,
                "shelved": shelved}

    # ------------------------------------------------------------ summary
    def stall_summary(self, rollout_id: str) -> dict:
        """发布维度的停滞计数与原因明细（调用方通常已持锁或通过服务调用）。"""
        with self.store.lock:
            self.store.ensure_loaded()
            return self._stall_summary_locked(rollout_id)

    def _stall_summary_locked(self, rollout_id: str) -> dict:
        observing, remediating, shelved = [], [], []
        for state in self.store.update_states.values():
            if state.rollout_id != rollout_id:
                continue
            item = {"device_id": state.device_id,
                    "stage": state.stage.value,
                    "reason": state.stall_reason,
                    "retries": state.retries}
            if state.stage == UpdateStage.SHELVED:
                item["reason"] = state.last_error or state.stall_reason
                shelved.append(item)
            elif state.stall_state == StallState.OBSERVING:
                observing.append(item)
            elif state.stall_state == StallState.REMEDIATING:
                remediating.append(item)
        return {"observing": {"count": len(observing), "devices": observing},
                "remediating": {"count": len(remediating),
                                "devices": remediating},
                "shelved": {"count": len(shelved), "devices": shelved}}
