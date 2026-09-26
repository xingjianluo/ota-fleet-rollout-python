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
    停滞识别: 同阶段无进展 > stall_window（见下节）
      observing（再给 remedy_grace）→ 回到 NONE 补救，retries+1
      机会用尽仍停滞/静默         → SHELVED
    回滚（confirmed 之后）  → ROLLED_BACK
```

终态：`CONFIRMED / FAILED / ROLLED_BACK / SHELVED`。进入终态后：

- 相同阶段 + 相同 seq 的重放 → 幂等返回当前权威状态；
- 其他任何推进尝试 → 422（普通终态 `terminal_stage_locked`，
  搁置设备 `shelved_locked`），状态不倒退；已搁置设备不会静默回到正常进度。

## 上报收敛规则（以 stage_seq 为权威游标）

| 情况 | 处理 | 反馈分类 / 错误原因 |
|---|---|---|
| 设备未认领就上报 | 拒绝 | `not_claimed` |
| seq 回退（迟到/重放旧包） | 拒绝，状态不变 | `seq_regression`（409） |
| seq 相同、阶段相同 | 幂等返回，不重复计数 | `same_stage_duplicate` |
| seq 相同、阶段不同 | 拒绝（矛盾包） | `same_seq_conflicting_stage` |
| seq 更新但阶段倒退 | 拒绝 | `stage_regression` |
| seq 更新、阶段前移 | 接受，停滞簿记清空 | `progress` |
| seq 更新、同阶段、窗口内 | 接受但不算进展 | `same_stage_within_window` |
| seq 更新、同阶段、过观察窗口 | 接受并标为观察中 | `same_stage_observing` |
| seq 更新、同阶段、补救后 | 接受，仍在补救观察期 | `same_stage_remedying` |
| 补救后重新进入第一阶段 | 接受，补救续跑 | `remedy_resumed` |
| 搁置后同阶段同 seq 重放 | 幂等，显式告知搁置 | `shelved_duplicate` |
| 搁置后推进/其他上报 | 拒绝 | `shelved_locked`（422） |
| 跳号但阶段向前 | 允许（设备合并了中间上报） | `progress` |
| 报告失败（stage 省略） | 进入 FAILED，带错误信息 | — |

响应体在原有字段上新增：`report_class`（上表分类）、`stall_state`
（`idle / observing / remediating`）、`stall_reason`。

## 停滞识别与自动续跑（stall sweep）

“设备还活着”和“设备在推进”是两回事：心跳/上报持续（seq 增长、时间更新）
只证明设备在线；**只有升级阶段真正前移才算进展**。判定锚点是
`stage_entered_at`（仅在阶段迁移时更新），同阶段重复上报只刷新
`updated_at`，不移动停滞时钟。

```
IDLE ──(同阶段无进展 > stall_window)──► OBSERVING
OBSERVING ──(再等 remedy_grace 仍无进展)──► REMEDIATING（重置回 NONE，retries+1）
REMEDIATING ──(重新进入阶段后再停滞 > stall_window)──► 再补救 …
                retries 达到 max_retries 后再停滞/静默 ──► SHELVED
```

- **观察窗口 `stall_window`**：同阶段停留超过它才判定停滞，此前的重复
  上报都算正常窗口内，不误伤。`OBSERVING` 只是标记，没有任何处罚。
- **有限补救**：从标记观察起再给 `remedy_grace` 宽限；仍无进展则把阶段
  重置回 NONE（设备可用原任务重新下载，认领是幂等的）。补救次数有上限
  `max_retries`，**且与阶段看门狗共享同一预算 `retries`**，两者合计最多
  补救这么多次，不会因为两条路径重复补救。
- **搁置 `SHELVED`**：机会用尽后仍停滞，或补救后彻底静默，进入搁置：
  - 明确记录原因（`shelved_stalled_after_remedies` /
    `shelved_after_N_retries`），写审计 `stall_observed` /
    `stall_remedy` / `stall_shelved`；
  - 计入所在批次的失败集合，**不再占用批次容量、不再阻塞推进**；
  - 终态锁定：推进被 `shelved_locked` 拒绝，同阶段重放返回
    `shelved_duplicate`，不会静默回到正常进度。
- **重新推进即解除**：设备在补救后真正把阶段向前移动，停滞簿记清空
  （`stall_state` 回 `idle`），重新开始观察（已用掉的补救次数保留）。
- **刚认领豁免**：停在 NONE 且没有任何停滞历史的设备仍在正常下载准备
  窗口，不监控；被补救回 NONE 后仍不续跑的设备会被继续兜底直至搁置，
  保证彻底静默也能收敛。

巡检入口：`ProgressService.stall_sweep(rollout_id?)` / HTTP
`POST /rollouts/{id}/stall-sweep` 与全局 `POST /maintenance/stall-sweep`。
巡检是纯锁内状态机：每次扫描对同一设备至多发生一次合法迁移，因此
**重复或并发触发幂等**——补救预算只增一次、搁置只发生一次、批次推进只
触发一次。发布汇总 `GET /rollouts/{id}` 新增 `stall` 块：

```json
"stall": {
  "observing":   {"count": 1, "devices": [{"device_id": "...", "stage": "downloaded",
                                          "reason": "stalled_no_progress", "retries": 0}]},
  "remediating": {"count": 0, "devices": []},
  "shelved":     {"count": 2, "devices": [{"device_id": "...", "stage": "shelved",
                                          "reason": "shelved_stalled_after_remedies", "retries": 1}]}
}
```

### 收敛语义

批次只在它名下**所有已下发设备都到达可解释终态**
（`CONFIRMED / FAILED / ROLLED_BACK / SHELVED`）后才推进，且只推进一次。
停滞设备最终一定收敛：要么补救后续跑成功，要么被搁置计入失败。单个或
少数设备无论“假新鲜”停滞、反复重报还是彻底静默，都不会让批次或整次
发布永久卡住。搁置计入失败，因此同样受失败率阈值约束——是否因搁置率
过高自动停止，沿用既有阈值规则，可人工 `resume`。

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
  重试次数）；
- 看门狗与停滞巡检**共享补救预算 `retries`**：看门狗针对“长时间无任何
  上报”（`updated_at` 锚点），停滞巡检针对“持续上报但阶段不动”
  （`stage_entered_at` 锚点），两条路径合计最多补救 `max_retries` 次。

## 并发一致性论证

所有状态变更在 `store.lock` 锁内完成：

1. **计数**：批次成功/失败集合由 claimed 顺序 + 集合去重重建，
   confirmed/failed 互换时先删后加，重复上报不产生重复计数。
2. **游标**：`stage_seq` 只在锁内比较与赋值，乱序并发后游标必为最大值，
   阶段不会倒退。
3. **回滚**：资格检查与生效在同一临界区；已回滚标记（completed 记录）
   在锁内可见，第二个并发请求只能得到 `rollback_duplicate`。
4. **批次**：预分区 + 锁内容量检查，并发认领结果为恰好 capacity 台成功；
   批次推进在锁内检查“是否全部终态”，重复/并发触发只推进一次。
5. **停滞巡检**：观察标记、补救（`retries` 自增）、搁置、批次推进都在
   同一临界区按当前状态做一次性状态迁移，重复或并发扫描不会重复累加
   补救次数、重复搁置或重复开批；巡检为 O(设备数) 线性扫描，数百台规模
   在毫秒级完成，不进入正常上报的热路径。
