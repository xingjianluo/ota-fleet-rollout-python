# 回滚规则

单设备与整批回滚由 `ota/rollbacks.py` 实现。所有批准与拒绝都写
`rollback_records`（`GET /rollback/records`）与 `rollback_recorded` 审计
（`GET /audit`），可追溯。

## 资格（先校验、后生效）

- 仅“已 confirmed 运行在新版本”的设备可回滚；
  downloaded/installed/rebooted 中的半更新设备 →
  `device_mid_update_not_rollbackable`；失败/搁置但版本未变的设备 →
  `already_on_target_version`。绝不留下半更新设备。
- 目标必须存在与设备型号/硬件匹配的固件
  （`target_firmware_not_found`），且：
  - 同版本 → `already_on_target_version`；
  - 目标比当前新 → `target_newer_than_current`；
  - 降级必须目标固件 `allow_downgrade=true` **且** 当前版本在其
    `compatible_from` 白名单 → `downgrade_disallowed` /
    `downgrade_path_not_whitelisted`。

## 幂等与防重

- 请求必须带 `idem_key`，否则 `missing_idem_key`；
- 同 key 重放返回原结果且 `duplicate=true`（网络重试安全）；
- 不同 key 对同一设备重复回滚到同一目标版本 → 409
  `rollback_duplicate / already_rolled_back_to_target`。

## 单设备 vs 整批

- **单设备** `POST /rollback/device/{id}`：资格通过后改写设备
  `fw_version`，升级状态置 `ROLLED_BACK` 并计入批次失败。
- **整批** `POST /rollouts/{id}/rollback-batch/{index}`：**全有或全无**。
  先对批次所有已涉及设备做全量预检，任一不通过则整批拒绝
  （`batch_precheck_blocked`，带每台设备原因），一台都不会生效；
  通过后在同一把锁内逐台生效。

回滚后批次/发布的收敛语义与其他终态一致，见
[rollout.md](rollout.md) 与 [state-machine.md](state-machine.md)。
