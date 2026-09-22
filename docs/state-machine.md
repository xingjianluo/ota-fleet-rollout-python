# 设备状态机与收敛规则

## 在线状态（presence）

```
OFFLINE --(接受心跳, seq 严格增大)--> ONLINE
ONLINE  --(TTL 内无有效心跳, 离线清扫)--> OFFLINE
```

- 注册后初始 OFFLINE；注册时签发一次 token，只存哈希。
- 心跳带单调整数 `seq`：`seq <= 已接受最大 seq` 的请求（重复或回退）
  一律拒绝 `stale_heartbeat`，**不更新时间戳、在线状态或游标**。
- 设备伪造 token → `bad_device_token`；未知设备 → `unknown_device`。
  这些请求不会改变任何状态。
- TTL 由构造参数或环境配置决定（演示 10s，60s 可选），调用
  `mark_offline_sweep`（或 `POST /maintenance/offline-sweep`）判定。

## 升级阶段状态机

```
        claim           downloaded        installed         rebooted
NONE ─────────► NONE ──────────► DOWNLOADED ───────► INSTALLED ───────► REBOOTED
                  │                                                          │
                  │ confirmed（确认提交，设备 fw_version 改写为目标版本）      │
                  └──────────────────────────────────────────────────────► CONFIRMED
                               各阶段报告 failure ───────────────► FAILED
    看门狗: 停留 > stage_timeout
      retries < max_retries  → 回到 NONE（重新下载），retries+1
      retries >= max_retries → SHELVED（搁置，计入批次失败）
    回滚（confirmed 之后）  → ROLLED_BACK
```

终态：`CONFIRMED / FAILED / ROLLED_BACK / SHELVED`。进入终态后：

- 相同阶段 + 相同 seq 的重放 → 幂等返回当前权威状态；
- 其他任何推进尝试 → 422 `terminal_stage_locked`，状态不倒退。

## 上报收敛规则（以 stage_seq 为权威游标）

| 情况 | 处理 | 错误原因 |
|---|---|---|
| 设备未认领就上报 | 拒绝 | `not_claimed` |
| seq 回退（迟到/重放旧包） | 拒绝，状态不变 | `seq_regression`（409） |
| seq 相同、阶段相同 | 幂等返回，不重复计数 | `duplicate=true` |
| seq 相同、阶段不同 | 拒绝（矛盾包） | `same_seq_conflicting_stage` |
| seq 更新但阶段倒退 | 拒绝 | `stage_regression` |
| 跳号但阶段向前 | 允许（设备合并了中间上报） | — |
| 报告失败（stage 省略） | 进入 FAILED，带错误信息 | — |

## 断线重连增量补齐（reconcile）

设备重连携带自己的 `client_seq`：

- `client_seq >= 服务器 stage_seq`：`in_sync`；
- `client_seq < stage_seq`：`client_is_behind=true`，响应带权威阶段与
  已接受游标，设备从权威阶段继续，**离线期间已收敛的进度不重复计数、
  不丢失**；
- 心跳重连（`reconnected=true`）只改变在线标记，与升级进度相互独立。

## 看门狗（watchdog）

- 监控 `in_progress / paused / auto_halted` 发布下所有非终态设备；
- 刚认领还在 NONE 且从未重试（仍在正常下载窗口）的设备**不监控**，避免
  误伤；只有已进入某阶段后停留超过 `stage_timeout` 才动作：
  - 重试次数未到上限：阶段重置回 NONE，设备可用原任务重新下载
    （重复认领是幂等的）；
  - 已达上限：SHELVED 并计入批次失败——保证单个卡死设备不会让批次
    永远等待；
- 动作都写审计（`watchdog_retry` / `watchdog_shelved`，含停留时长与
  重试次数）。

## 并发一致性论证

所有状态变更在 `store.lock` 锁内完成：

1. **计数**：批次成功/失败集合由 claimed 顺序 + 集合去重重建，
   confirmed/failed 互换时先删后加，重复上报不产生重复计数。
2. **游标**：`stage_seq` 只在锁内比较与赋值，乱序并发后游标必为最大值，
   阶段不会倒退。
3. **回滚**：资格检查与生效在同一临界区；已回滚标记（completed 记录）
   在锁内可见，第二个并发请求只能得到 `rollback_duplicate`。
4. **批次**：预分区 + 锁内容量检查，并发认领结果为恰好 capacity 台成功。
