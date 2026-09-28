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
    看门狗: 无上报停留 > stage_timeout（silent_timeout）
            或同阶段反复上报超过 stall_window（stalled_same_stage）
      retries < max_retries  → 回到 NONE（续跑，重新下载），retries+1
      retries >= max_retries → SHELVED（搁置，计入批次失败）
    回滚（confirmed 之后）  → ROLLED_BACK
```

终态：`CONFIRMED / FAILED / ROLLED_BACK / SHELVED`。**一旦定局，之后
收到任何形式的上报（带阶段的普通上报，或不带 `stage` 的纯失败上报）
都不会再改变设备状态、版本与批次/停滞汇总计数**，具体分两档：

- `CONFIRMED / FAILED / ROLLED_BACK`：相同阶段 + 相同 seq 的重放 →
  幂等返回当前权威状态（FAILED 设备重发同一条失败上报同样幂等，
  `duplicate=true`，不重复计数、不重复写审计）；其他任何上报（含
  seq 更大的新失败上报、向任何阶段的迁移）→ 422
  `terminal_stage_locked`，状态、已提交版本、失败原因都不倒退；
- `SHELVED`：**任何**进度上报与认领都明确拒绝（普通上报 → 422
  `device_shelved`，纯失败上报 → 422 `device_shelved`，认领 → 409
  `device_shelved`）。已记录的搁置结论（`last_error` 中的补救次数与
  停滞原因、汇总 `stall.shelved` 计数与原因）不会被后来的上报覆盖。
- 被拒绝的上报**不占用批次容量、不改变成功/失败计数、不触发阈值、
  不推进也不阻塞后续批次**；每次拒绝都写 `progress_rejected` 审计，
  失败上报路径带 `via=failure_report` 以便追溯。

## 进展与停滞的判定（“活着”≠“在推进”）

每台设备维护两个时间游标与一个计数器：

| 字段 | 刷新时机 | 含义 |
|---|---|---|
| `updated_at` | 每次被接受的上报 | 设备“还活着” |
| `stage_entered_at` | 仅阶段真正变化时 | 设备“在推进”的进展游标 |
| `same_stage_reports` | 同阶段新 seq 上报 +1；阶段变化清零 | 停滞观察计数 |

- **进展**：上报使阶段沿状态机前移（或首次进入某阶段），
  `classification=advanced`，进展游标重置；
- **停滞观察**：设备持续上报**同一阶段**（seq 在增长、时间在更新，
  但阶段不动），被接受但分类为 `classification=same_stage_repeat`，
  计数器累加——这类上报**不算进展**，不会刷新进展游标；
- **停滞判定**：`same_stage_reports > 0`（证明设备活着）且
  `now - stage_entered_at > stall_window`（可配置的停滞观察窗口，
  默认 120s）时，巡检判定为 `stalled_same_stage`，进入补救；
- **无上报停滞**：`now - updated_at > stage_timeout`（默认 60s）判定为
  `silent_timeout`，与停滞共用补救次数预算。

## 上报反馈分类

| 情况 | HTTP | 反馈 | 说明 |
|---|---|---|---|
| 阶段推进（含跳号向前） | 200 | `classification=advanced` | 正常推进 |
| 同阶段、新 seq | 200 | `classification=same_stage_repeat` | 活着但未推进，计入停滞观察 |
| 同阶段、同 seq | 200 | `duplicate=true` | 幂等重放，不重复计数 |
| seq 回退（迟到/重放旧包） | 409 | `seq_regression` | 过期上报，状态不变 |
| 同 seq、阶段不同 | 422 | `same_seq_conflicting_stage` | 矛盾包 |
| seq 更新但阶段倒退 | 422 | `stage_regression` | 拒绝 |
| 已搁置设备的任何上报（含纯失败上报） | 422 | `device_shelved` | 不得静默回到正常进度，搁置结论不被覆盖 |
| 其他终态后再迁移/再报失败 | 422 | `terminal_stage_locked` | 拒绝；FAILED 同 seq 重发仍幂等 |
| 设备未认领就上报 | 422 | `not_claimed` | 拒绝 |
| 报告失败（stage 省略） | 200 | `classification=failed` | 进入 FAILED（仅非终态设备可接受） |

## 断线重连增量补齐（reconcile）

设备重连携带自己的 `client_seq`：

- `client_seq >= 服务器 stage_seq`：`in_sync`；
- `client_seq < stage_seq`：`client_is_behind=true`，响应带权威阶段与
  已接受游标，设备从权威阶段继续，**离线期间已收敛的进度不重复计数、
  不丢失**；
- 心跳重连（`reconnected=true`）只改变在线标记，与升级进度相互独立。

## 看门狗 / 停滞巡检（watchdog）

- 监控 `in_progress / paused / auto_halted` 发布下所有非终态设备，
  **单趟扫描**全部设备状态（不随发布数倍增），数百台规模下一次巡检
  在毫秒级完成；
- 刚认领还在 NONE 且从未重试（仍在正常下载窗口）的设备**不监控**，避免
  误伤；触发条件为 `silent_timeout` 或 `stalled_same_stage`（审计与
  `last_error` 中 reason 可区分）：
  - 重试次数未到上限：阶段重置回 NONE，设备可用原任务续跑
    （重复认领是幂等的），同时刷新 `updated_at/stage_entered_at` 并
    清零 `same_stage_reports`；
  - 已达上限：SHELVED 并计入批次失败——保证单个卡死/停滞设备不会让
    批次永远等待；
- **巡检幂等**：补救/搁置都会把设备移出触发条件（时间游标已刷新或已
  终态），同一窗口内重复或并发执行巡检不会重复累加 `retries`、不会
  重复搁置；
- 动作都写审计（`watchdog_retry` / `watchdog_shelved`，含触发原因、
  停留时长与重试次数）。

## 并发一致性论证

所有状态变更在 `store.lock` 锁内完成：

1. **计数**：批次成功/失败集合由 claimed 顺序 + 集合去重重建，
   confirmed/failed 互换时先删后加，重复上报不产生重复计数。
2. **游标**：`stage_seq` 只在锁内比较与赋值，乱序并发后游标必为最大值，
   阶段不会倒退；`same_stage_reports` 只在锁内对“新 seq 同阶段”上报
   +1，并发重复上报不会多计或漏计。
3. **巡检与上报竞态**：补救/搁置的状态迁移与时间游标刷新在同一临界区；
   失败上报在写入前会用与巡检**完全相同**的停滞判定检查设备，若补救
   机会已用尽且停滞条件已成立，则先让搁置落定、再以 `device_shelved`
   拒绝失败上报。因此失败上报与巡检并发（无论谁先拿到锁），设备最终
   都是 SHELVED 且搁置事件、批次计数恰好一次，不会出现“先搁置又被
   改成失败”或汇总数字来回跳。
4. **回滚**：资格检查与生效在同一临界区；已回滚标记（completed 记录）
   在锁内可见，第二个并发请求只能得到 `rollback_duplicate`。
5. **批次**：预分区 + 锁内容量检查，并发认领结果为恰好 capacity 台成功；
   批次只在全部已下发设备到达终态（含搁置）后推进一次；被拒绝的上报
   不调用结果计数，因此不占容量、不影响推进。
