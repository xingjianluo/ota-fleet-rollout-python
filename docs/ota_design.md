# OTA 固件分批发布与设备编队管理 — 设计说明

本地可复现的 OTA 模拟实现：设备注册/心跳、固件校验、分批灰度、暂停继续、
失败率熔断、看门狗、断线重连、单设备/整批回滚，全部基于本地 JSON 快照持久化
与进程内锁，不依赖任何外部服务。

## 1. 模块结构

| 文件 | 职责 |
| --- | --- |
| `ota/models.py` | 领域模型与状态枚举（纯数据） |
| `ota/errors.py` | 错误层级：`code` 为大类、`reason` 为可区分细分原因 |
| `ota/persistence.py` | JSON 快照存储：`tmp+os.replace+fsync` 原子落盘，重启恢复 |
| `ota/service.py` | **全部业务规则的唯一入口**（校验→迁移→计数→落盘同一临界区） |
| `ota/clock.py` | 可注入时钟；`FakeClock` 让超时/重连测试无需真实等待 |
| `ota/api.py` | FastAPI 路由与错误码→HTTP 状态映射 |
| `scripts/demo.py` | 本地端到端异常链路演示 |
| `tests/` | 以异常链路为主的 40 个单测（含并发） |

## 2. 设备身份与心跳

* 注册字段：`device_id / model / hw_version / current_fw_version`，缺一拒绝
  （`missing_identity_field`），版本号必须是数字点分（`bad_version`）。
* 设备令牌只在注册响应中明文出现一次，库存 PBKDF2-SHA256（salt:hash），
  使用 `hmac.compare_digest` 校验；伪造令牌 → `invalid_device_identity/bad_token`。
* **重复注册绝不覆盖**：
  * 同身份重复 → `device_already_registered/identity_conflict`（409）；
  * 同一 ID 但型号/硬件不一致（伪造）→ `identity_tampered`；
  * 两种情况既有心跳序号、状态与令牌均不改变。
* 心跳必须携带单调非负整数 `seq`：
  * `seq < 已接受序号` → `stale_heartbeat/seq_regressed`（过期，拒绝）；
  * `seq == 已接受序号` → `seq_duplicate`（重复，拒绝）；
  * 只有严格递增才会更新心跳时间与在线状态。
  时间戳只用于展示，**顺序权威是 seq**，避免时钟漂移导致状态被静默覆盖。

## 3. 固件与发布校验

固件唯一键为 `fw_id`；同 `(model, hw_version, version)` 不允许重复。
创建发布与规划批次时对每台设备做准入判定，拒绝原因逐台写入审计：

| reason | 含义 |
| --- | --- |
| `model_mismatch` / `hw_mismatch` | 固件型号/硬件版本与设备不匹配 |
| `already_on_target` | 设备已在目标版本 |
| `downgrade_not_allowed` | 目标版本更低且发布未显式 `allow_downgrade` |
| `from_version_not_allowed` | 当前版本不在发布的 `allowed_from_versions` 名单 |
| `firmware_yanked` | 目标固件已被撤下 |
| `target_fw_missing` | 发布指向不存在的固件 |

版本按数字元组比较（`1.10.0 > 1.2.0`），非法版本在入口即被拒绝。

## 4. 分批灰度、暂停继续与阈值熔断

发布状态机：

```
PENDING ──start──▶ ACTIVE ──pause──▶ PAUSED ──resume──▶ ACTIVE
                     │  │                  （仅 PAUSED 可 resume）
                     │  └────失败率达阈值──▶ THRESHOLD_HALTED（终态，需人工处置）
                     └────abort──────────▶ ABORTED
ACTIVE（全部任务终态）──────────────────▶ COMPLETED
```

* `batch_size`：每批容量；`failure_threshold`：失败率阈值 `[0,1]`。
* **规划下一批**只在 `ACTIVE` 允许；PAUSED 不能规划，但已下发批次继续收敛。
* 每出现一个终态任务（升级方向）重算失败率：
  `(failed + suspended) / 已终态计数 >= threshold` 即自动：
  1. 发布置 `THRESHOLD_HALTED`（带 `halt_reason`，如 `failure_rate 0.500 >= threshold 0.500`）；
  2. 后续批次不再规划；
  3. 已规划但设备**尚未认领**的 PENDING 任务确定性置 `SUSPENDED`
     （原因 `release_threshold_halted`），不会悬挂成“半下发”；
  4. 已在下载/安装/重启中的设备继续推进，最终全部收敛到可解释终态。
* THRESHOLD_HALTED 不能直接 resume（避免自动越过熔断决策），必须人工回滚或终止。
* 人工 `abort`：PENDING 任务置 SUSPENDED，活动批次置 STOPPED，在途设备继续收敛。
* 批次在所有任务终态时置 `DRAINED`；发布在 ACTIVE 且全部任务终态时 COMPLETED。
  PAUSED 期间即使当前批次耗尽也**不会**自动完成（剩余设备尚未纳入）。

## 5. 设备升级任务状态机

```
PENDING ─claim─▶ DOWNLOADING ─▶ DOWNLOADED ─▶ INSTALLING ─▶ INSTALLED
              ─▶ REBOOTING ─▶ CONFIRMED(终态:成功；设备版本此刻才切换)

任何活动阶段 ─failed 上报─▶ FAILED(终态)
看门狗超时且重试耗尽  ───▶ SUSPENDED(终态:搁置；设备重连心跳后可恢复)
回滚任务走同一条流水线，成功终态为 ROLLED_BACK（设备版本回到回滚目标）
```

关键不变量：

* 阶段只能相邻正向推进；跳阶段/倒退 → `invalid_stage_transition/non_adjacent_or_backward`。
* 反复上报同一阶段：超过 `stage_timeout` 且仍有重试额度时消耗一次重试
  （`same_stage_repeated_retry`），否则 `same_stage_repeated`，由看门狗搁置。
* **设备 `current_fw_version` 只在 CONFIRMED/ROLLED_BACK 成功终态切换**，
  中途掉电/失败时设备仍停留在旧版本，杜绝“半更新”。
* 终态不可变（`terminal_immutable`），迟到的高 seq 上报不能复活任务。
* 认领的并发安全：`PENDING→DOWNLOADING` 迁移本身在 store 锁内完成，即 CAS；
  并发重复认领拿到幂等结果（`duplicate=true`），批次 `claimed` 只用于可见性。

## 6. 看门狗与掉线策略

`sweep()` 由运维周期调用（或测试中手动调用）：

* 在线设备超过 `heartbeat_timeout` 无心跳 → `OFFLINE`；
* 非终态、非 PENDING 任务超过 `stage_timeout` 无进展：
  * 未达 `max_stage_attempts` → `attempts += 1` 并刷新进度（设备侧可重新下载/安装）；
  * 达到上限 → `SUSPENDED`，原因 `watchdog_stage_timeout:<stage>`，计入失败率；
* PENDING 不超时（设备还没开始，不应被惩罚）；
* 个别设备搁置**不阻塞批次**：其它设备照常成功，发布状态依然可解释。

**断线重连增量补齐**：离线设备再次发送合法心跳（seq 严格递增）时：

* 状态回到 ONLINE；
* 该设备因看门狗搁置（`watchdog_*`）的任务恢复为 DOWNLOADING：
  终态计数先冲销（`counted=False`），重试次数归零、上报序号重置，
  随后按设备真实进度顺序上报；因此“离线前搁置的一次失败”不会被永久计数，
  成功后也只计一次 confirmed，不重复、不丢失。

## 7. 乱序、迟到与重复上报的收敛规则

设备上报携带设备侧单调 `seq`：

* `seq < 任务已接受 seq` → `stale_report/seq_regressed`，丢弃；
* `seq == 已接受 seq` → `duplicate_report/seq_duplicate`（HTTP 200，幂等）；
* 阶段非法（跳阶段/倒退/同阶段）时**拒绝且不消费 seq**，
  保证后续合法上报仍可被接受、已接受最大 seq 不被污染；
* 只有全部校验通过的上报才同时推进 `last_report_seq` 与阶段。
* 重复上报绝不改变 `stage_seq / attempts / 终态计数`。

## 8. 回滚规则

### 单设备回滚（`POST /rollback/device`）

1. 设备存在未终态任务（PENDING 除外也拦截，含下载/安装/重启中）→
   `rollback_not_allowed/job_in_flight`。**升级中不可回滚，避免半更新。**
2. 必须有发布历史（`no_history`）。
3. 最近一次升级若已 CONFIRMED：只允许回到该升级任务的 `from_version`，
   且该版本固件仍在库中：
   * 原固件缺失/被撤下 → `rollback_target_missing/previous_fw_missing`；
   * 指定了其它目标 → `rollback_not_allowed/target_not_previous`。
4. 最近任务为 FAILED/SUSPENDED 且设备从未离开原版本 →
   `nothing_to_rollback`（没有可回滚内容，设备本来就是旧版）。
5. 目标型号/硬件必须匹配（`model_or_hw_mismatch`）、不能是撤下固件
   （`firmware_yanked`）、目标版本必须严格低于当前（`not_a_downgrade`）。
6. 设备级互斥：已有回滚任务在途 → `rollback_in_progress/rollback_inflight`；
   并发回滚只有一个建单成功（线程锁 + 终态任务权威状态，重启后重建标记）。

### 整批回滚（`POST /releases/{rel}/batches/{bat}/rollback`）

* 目标固件不存在 → 整个请求 `rollback_target_missing`。
* 逐设备套用同一套规则：合法设备建回滚单，不合法设备**分别归类**
  （返回 `rejected: {device_id: reason}` 并写审计），不会因为一台失败而整批脏写。
* 回滚任务把原任务 `superseded_by` 指向新任务，形成可追溯链。
* 回滚流水线与升级一致；成功终态 `ROLLED_BACK`，设备版本回到目标版本。
* 回滚任务不计入发布“升级失败率”，避免统计口径被回滚扭曲。

## 9. 并发与持久化

* 进程内一把 `RLock`：每个公开操作在**同一临界区**完成校验、状态迁移、
  统计 CAS（`job.counted`）、批次/发布收敛与落盘。
* 终态计数 CAS：`_after_terminal` 对每个任务只生效一次，重复终态上报不会
  双重计数；恢复执行的搁置任务先清 `counted`，最终成功只计一次。
* 落盘：写 `*.tmp` → flush+fsync → `os.replace` → 目录 fsync，
  崩溃后用最后一份完整快照恢复；ID 计数器一并持久化，重启不撞 ID。
* 适用范围：单进程本地模拟；多进程部署需换成带事务的数据库（本项目刻意不引入）。

## 10. HTTP 接口与错误格式

```
POST /devices/register | /devices/heartbeat
GET  /devices/{id}
POST /firmwares
POST /releases
POST /releases/{id}/start | /pause | /resume | /abort
POST /releases/{id}/batches/plan
GET  /releases/{id} | /batches/{id}
POST /jobs/claim | /jobs/report
POST /rollback/device
POST /releases/{id}/batches/{bid}/rollback
POST /sweep
GET  /audit?ref=&limit=
```

错误统一形如：

```json
{"error": {"code": "release_state_conflict",
           "reason": "cannot_plan_from_paused",
           "message": "只有 ACTIVE 发布可规划下一批，当前 paused"}}
```

`duplicate_report` 语义为幂等成功，映射 HTTP 200；其余按错误类型映射
401/404/409/422。

## 11. 本地验证方法

```bash
uv sync                                  # 安装依赖（fastapi + pytest）
.venv/bin/pytest -q                      # 40 个单测/并发测试
.venv/bin/python scripts/demo.py         # 端到端异常链路演示（状态落 data/）
.venv/bin/uvicorn main:app --reload      # 起 HTTP 服务，/docs 查看 Swagger
```

测试用 `FakeClock` 驱动时间（心跳超时、阶段超时、重连恢复均无需真实等待），
关键输入与状态迁移依据通过 `logging` 打印（前缀 `AUDIT ...` 与
`CASE/CLAIM/REPORT/ROLLBACK/SWEEP ...`），异常用例全部断言 `error.reason`，
确保拒绝原因可区分、失败不污染既有状态。

## 12. 拒绝原因码速查

| 场景 | code | reason |
| --- | --- | --- |
| 重复注册（同身份/伪造） | device_already_registered | identity_conflict / identity_tampered |
| 令牌伪造 | invalid_device_identity | bad_token |
| 心跳过期/重复 | stale_heartbeat | seq_regressed / seq_duplicate |
| 型号/硬件不匹配 | incompatible_firmware | model_mismatch / hw_mismatch / model_or_hw_mismatch |
| 降级未授权 | firmware/rollback 错误 | downgrade_not_allowed / not_a_downgrade |
| 无可纳入设备 | no_eligible_device | no_candidate |
| 发布状态不允许操作 | release_state_conflict | cannot_*_from_<state> |
| 迟到/重复上报 | stale_report / duplicate_report | seq_regressed / seq_duplicate |
| 非法阶段迁移 | invalid_stage_transition | non_adjacent_or_backward / same_stage_repeated(_retry) / terminal_immutable |
| 回滚在途/无目标/无历史 | rollback_not_allowed / rollback_target_missing | job_in_flight / target_not_previous / previous_fw_missing / nothing_to_rollback / no_history |
| 并发回滚 | rollback_in_progress | rollback_inflight |
