# HTTP API 与错误码

服务启动：`uv run uvicorn main:app --reload`，交互文档见 `/docs`。
除注册与健康检查外，设备侧接口均需在 body 中携带注册时签发的 `token`。

## 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/devices` | 注册设备，返回 `token`（仅展示一次） |
| GET | `/devices/{id}` | 设备视图（当前版本、在线状态、心跳游标） |
| POST | `/devices/{id}/heartbeat` | 心跳 `{token, seq}` |
| POST | `/maintenance/offline-sweep` | TTL 离线清扫 |
| POST | `/firmwares` | 定义固件 |
| GET | `/firmwares/{id}` | 固件视图 |
| POST | `/rollouts` | 创建发布（全量兼容校验） |
| GET | `/rollouts/{id}` | 发布与各批次计数 |
| POST | `/rollouts/{id}/start` `/pause` `/resume` | 生命周期 |
| POST | `/rollouts/{id}/claim/{device_id}` | 设备认领升级任务 |
| POST | `/rollouts/{id}/report/{device_id}` | 阶段上报；不带 `stage` 视为失败上报 |
| POST | `/rollouts/{id}/reconcile/{device_id}` | 重连对账 |
| POST | `/rollouts/{id}/watchdog` | 触发一次看门狗扫描 |
| POST | `/rollback/device/{id}` | 单设备回滚 |
| POST | `/rollouts/{id}/rollback-batch/{index}` | 整批回滚（全有或全无） |
| GET | `/rollback/records` | 回滚批准/拒绝追溯 |
| GET | `/audit?limit=N` | 审计事件 |

错误响应统一为：

```json
{"code": "stale_heartbeat",
 "message": "设备 d1 心跳过期或重复: seq=1 <= 已接受 2",
 "details": {"device_id": "d1", "reason": "stale_or_duplicate_seq",
             "input_seq": 1, "last_accepted_seq": 2}}
```

## 错误码 / HTTP / 细粒度原因

| code | HTTP | 典型 details.reason |
|---|---|---|
| `device_already_registered` | 409 | `identical_reregistration` / `identity_conflict` |
| `unknown_device` | 404 | — |
| `bad_device_token` | 401 | — |
| `stale_heartbeat` | 409 | `stale_or_duplicate_seq` / `bad_seq` |
| `firmware_validation_failed` | 422 | `duplicate_firmware_id`、版本号非法 |
| `firmware_not_found` | 404 | `target_firmware_not_found`（回滚目标） |
| `incompatible_firmware` | 422 | `model_hw_mismatch` |
| `upgrade_path_rejected` | 422 | `already_on_version` / `upgrade_path_not_whitelisted` / `downgrade_disallowed` / `downgrade_path_not_whitelisted` |
| `rollout_validation_failed` | 422 | `incompatible_members`（带每台设备原因）、`no_devices`、`bad_batch_size`、`bad_threshold`、`unknown_rollout` |
| `rollout_state_conflict` | 409 | `not_pending` / `not_in_progress` / `not_resumable` |
| `batch_state_conflict` | 409 | `not_current_batch` / `batch_full` / `rollout_paused` / `rollout_auto_halted` / `already_terminal` / `no_active_batch` |
| `stage_transition_rejected` | 422 | `terminal_stage_locked` / `shelved_locked` / `stage_regression` / `same_seq_conflicting_stage` / `unknown_stage` |
| `stale_report_rejected` | 409 | `seq_regression` |
| `rollback_rejected` | 422 | `device_mid_update_not_rollbackable` / `already_on_target_version` / `target_newer_than_current` / `batch_precheck_blocked` / `missing_idem_key` / `bad_batch_index` / `empty_batch_no_devices` |
| `rollback_duplicate` | 409 | `already_rolled_back_to_target` |

## 快速手动验证（HTTPie/curl 风格）

```bash
TOKEN=$(curl -s localhost:8000/devices -H 'content-type: application/json' \
  -d '{"device_id":"d1","model":"A1","hw_version":"h1","fw_version":"1.0.0"}' \
  | python -c 'import sys,json;print(json.load(sys.stdin)["token"])')
curl -s -XPOST localhost:8000/devices/d1/heartbeat \
  -H 'content-type: application/json' -d "{\"token\":\"$TOKEN\",\"seq\":1}"
```
