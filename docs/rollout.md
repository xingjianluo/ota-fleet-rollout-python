# 发布流程与失败率阈值

## 1. 前置校验（创建发布时一次性完成）

`RolloutService.create(rollout_id, fw_id, device_ids, batch_size, threshold)`
对每台设备执行：

1. 设备必须存在（`unknown_device`）；
2. 固件必须存在（`firmware_not_found`）；
3. 设备 `model + hw_version` 与固件完全一致（`incompatible_firmware`）；
4. 升级路径合法（见下）。

**任一设备不通过，整个发布被拒绝，不产生半成品发布**（错误中带
`rejected` 明细，说明每台设备的原因）。

### 升级路径

| 当前 → 目标 | 条件 | 拒绝原因 |
|---|---|---|
| 同版本 | 永不允许 | `already_on_version` |
| 升级 | 白名单为空放行；否则当前版本须在 `compatible_from` | `upgrade_path_not_whitelisted` |
| 降级 | 固件须 `allow_downgrade=true` **且** 当前版本在白名单 | `downgrade_disallowed` / `downgrade_path_not_whitelisted` |

## 2. 分批与生命周期

- 设备按提交顺序切分为容量 `batch_size` 的批次（最后一批可以更小），
  **预分区**意味着并发认领绝不可能把后续批次设备提前计入。
- 状态：`pending → in_progress → (paused | auto_halted) → completed`。
- `start` 仅开放第 0 批；`pause/resume` 只在合法状态间切换，非法切换
  返回 `rollout_state_conflict` / `batch_state_conflict`。
- **批次推进**：当前批次容量全部被认领 **且** 全部设备收敛到终态
  （confirmed/failed/shelved/rolled_back）后，批次置 `done`，开放下一批；
  最后一批收敛完发布 `completed`。已下发但未收敛的设备不会阻塞推进判定
  之外的批次，看门狗负责让它们收敛（重试或搁置）。
- **收敛保证**：批次只在它名下所有已下发设备都到达可解释终态
  （**含搁置**）后推进，且只推进一次；单个或少数停滞/掉线设备不会让
  整批或整次发布永久卡住（见
  [state-machine.md](state-machine.md) 的“看门狗 / 停滞巡检”一节）。

## 3. 认领（claim）

设备通过带 token 的认领请求获取升级任务。规则：

- 未 `in_progress` 的发布按状态拒绝（`rollout_paused`、
  `rollout_auto_halted`、`no_active_batch`）；
- 不属于发布 → `not_in_rollout`；属于后续批次 → `not_current_batch`；
- 容量满 → `batch_full`；
- **重复认领幂等**：设备已认领但未终态时再次认领（典型为断线重连），
  返回同一任务且 `already_claimed=true`，批次 `claimed` 不重复计数；
- 已终态设备再认领 → `already_terminal`；**已搁置设备**再认领 →
  `device_shelved`（带搁置原因），不得静默回到正常进度。

## 3.1 停滞概览（发布汇总）

`GET /rollouts/{id}` 的响应中带 `stall` 字段，一眼看出停滞处置情况：

```json
"stall": {"observing": 1, "remedying": 1, "shelved": 2,
          "reasons": {"observing": {"d3": "same_stage_repeat x5(stage=downloaded)"},
                      "remedying": {"d4": "retry_1_in_progress(stage=none)"},
                      "shelved": {"d5": "shelved_after_2_retries(stalled_same_stage)",
                                  "d6": "shelved_after_2_retries(silent_timeout)"}}}
```

- `observing`：持续上报同一阶段、仍在停滞观察窗口内的设备数；
- `remedying`：已被巡检重置续跑、补救机会尚未用尽的设备数；
- `shelved`：补救用尽被搁置的设备数（不再占用批次容量、不阻塞推进）；
- `reasons`：每档设备 → 具体原因，便于判断是否需要人工介入。

## 4. 失败率阈值与自动停止

- 每次设备进入终态，结果计入所在批次（成功/失败集合按设备 ID 去重）。
- `失败率 = failed / (succeeded + failed)`，一旦 `>= threshold`
  （阈值可配，且至少有 1 个失败），发布立即 `auto_halted`：
  - 当前批次置 `halted`，后续批次保持 `waiting`，**不再下发**；
  - 已经下发的设备不被强制中断，继续按状态机收敛到可解释终态；
  - 审计写 `rollout_auto_halted`（含失败率与阈值依据）；
  - 人工评估后可 `resume`：发布回到 `in_progress`，halted/paused
    批次重新开放认领。

## 回滚规则

本节与 [state-machine.md](state-machine.md) 共同描述回滚语义。

- **资格**：仅"已 confirmed 运行在新版本"的设备可回滚；
  downloaded/installed/rebooted 中的半更新设备 →
  `device_mid_update_not_rollbackable`；失败/搁置但版本未变的设备 →
  `already_on_target_version`。绝不留下半更新设备。
- **目标**：必须存在与设备型号/硬件匹配的目标固件，且其
  `allow_downgrade` 与白名单允许当前版本降级；否则
  `target_firmware_not_found` / `downgrade_disallowed` /
  `downgrade_path_not_whitelisted`。
- **幂等与防重**：请求必须带 `idem_key`（`missing_idem_key`）；
  同 key 重放返回原结果 `duplicate=true`；不同 key 对同一设备重复回滚到
  同一目标版本 → 409 `rollback_duplicate / already_rolled_back_to_target`。
- **整批回滚是全有或全无**：先对批次所有已涉及设备做预检，任一不通过则
  `batch_precheck_blocked`（带每台设备原因），**一台都不会生效**；
  通过后在同一把锁内逐台生效。
- 所有批准与拒绝都写 `rollback_records` 与 `rollback_recorded` 审计，
  可通过 `GET /rollback/records` 与 `GET /audit` 追溯。
