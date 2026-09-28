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

终态：`CONFIRMED / FAILED / ROLLED_BACK / SHELVED`。进入终态后，
**无论再收到哪种形式的上报（带阶段或只报失败、编号是否更大），已定局
结论都不会被覆盖**，发布汇总与批次计数也保持不变：

- `CONFIRMED / ROLLED_BACK`：相同阶段 + 相同 seq 的重放 → 幂等返回当前
  权威状态；其他任何上报（含只报失败、哪怕 seq 更新）→ 422
  `terminal_stage_locked`，状态不倒退；
- `FAILED`：相同 seq 的同一条失败重放 → 幂等返回（`duplicate=true`，
  不重复计数）；编号更大的新失败或任何其他阶段上报 → 422
  `terminal_stage_locked`；
- `SHELVED`：**任何**进度上报、失败上报与认领都明确拒绝（上报 → 422
  `device_shelved`，认领 → 409 `device_shelved`，响应带搁置原因），
  被搁置的设备不得静默回到正常进度，汇总里的搁置数量与搁置原因不消失。

被拒绝的上报不改状态、不刷新游标、不调用结果计入，因此不会重复计数、
不会占用批次容量、不会触发或回退批次推进。

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
| 已搁置设备的任何上报（含只报失败、任意 seq） | 422 | `device_shelved` | 不得静默回到正常进度，搁置原因保留 |
| confirmed/rolled_back 后再报失败（任意 seq） | 422 | `terminal_stage_locked` | 定局不被失败覆盖 |
| failed 后报更大 seq 的新失败/其他阶段 | 422 | `terminal_stage_locked` | 定局锁定 |
| failed 重发同一条失败（相同 seq） | 200 | `duplicate=true` | 幂等重放，不重复计数 |
| 其他终态的推进尝试 | 422 | `terminal_stage_locked` | 拒绝 |
| 设备未认领就上报 | 422 | `not_claimed` | 拒绝 |
| 非终态报告失败（stage 省略，seq 更新） | 200 | `classification=failed` | 进入 FAILED |
| 非终态失败上报 seq 回退/重复 | 409 | `seq_regression` | 过期，状态不变 |

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
3. **定局保护**：所有终态判定（shelved / 其他终态 / seq 回退）都在同一
   临界区内、任何状态改写之前完成；失败上报与停滞巡检并发时，设备最终
   只会落在 FAILED 或 SHELVED 中的**一个**：先到者生效并只计一次，
   后到者拿到 `device_shelved` / `terminal_stage_locked` 拒绝，不会出现
   “先搁置又被改成失败”或汇总计数来回跳。
4. **巡检**：补救/搁置的状态迁移与时间游标刷新在同一临界区，并发或
   重复巡检对同一设备恰好生效一次。
5. **回滚**：资格检查与生效在同一临界区；已回滚标记（completed 记录）
   在锁内可见，第二个并发请求只能得到 `rollback_duplicate`。
6. **批次**：预分区 + 锁内容量检查，并发认领结果为恰好 capacity 台成功；
   批次只在全部已下发设备到达终态（含搁置）后推进一次；被拒绝的上报不
   触发结果计入，因而不占用容量、不推进也不回退批次。
