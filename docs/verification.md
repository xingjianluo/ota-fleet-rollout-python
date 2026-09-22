# 验证方法与场景清单

## 如何运行

```bash
uv sync                       # 安装依赖（含 pytest；无外部服务）
uv run pytest -q              # 全部用例
uv run pytest --collect-only -q | tail -n +1  # 查看用例清单
# 单模块
uv run pytest tests/test_devices.py tests/test_progress.py -q
# 演示（带关键输入与状态迁移日志）
uv run python scripts/demo.py
# 可选 HTTP 联调
uv run uvicorn main:app --reload
```

测试使用虚拟时钟（`ota/clock.py`），无需真实等待；并发用线程 + Barrier
模拟，持久化用 `tmp_path` 目录，每条关键操作都有 `logging` 输出
（认领槽位、seq 迁移、失败率、看门狗动作、回滚结果）。

## 覆盖的异常链路（对照需求）

### 1. 注册与心跳身份安全 — `tests/test_devices.py`
- 重复注册按相同/冲突属性区分原因，且不覆盖任何状态；
- 未知设备、伪造 token、重复/回退心跳被分类拒绝；
- TTL 离线清扫；token 哈希在进程重建后仍可校验。

### 2. 固件与发布校验 — `tests/test_firmwares.py`, `tests/test_rollouts.py`
- 非法版本号、重复固件 ID；
- 型号/硬件不匹配、白名单外升级、同版本重刷、未开放降级；
- 发布成员含未知/不兼容设备时整体拒绝（无半成品发布）。

### 3. 分批灰度与阈值 — `test_concurrent_claim_no_oversell`,
`test_failure_threshold_auto_halt`, `test_batch_advances_when_settled`
- 8 设备 barrier 并发认领，批次0恰好 4 台，其余明确原因；
- 失败率达阈值立即 `auto_halted`，第二批保持 waiting，可人工 resume；
- 批次全部终态后自动开下一批，全部完成变 completed。

### 4. 回滚失败与幂等 — `tests/test_rollbacks.py`
- 半更新设备回滚被拒且状态/版本不变（无半更新）；
- 目标不存在、型号不符、未开放降级、同版本/向新版回滚分类拒绝；
- 成功回滚后再回滚同版本 → 409 重复；同 key 重放幂等；
- 整批预检失败时一台都不生效；并发回滚恰好一次 completed。

### 5. 阶段推进、看门狗、乱序/重放、断线对账 — `tests/test_progress.py`
- 同 seq 重复幂等、seq 回退拒绝、同 seq 矛盾阶段拒绝、阶段倒退拒绝；
- 终态锁定；失败上报触发自动停止；
- 看门狗：正常下载窗口（NONE 初始态）不监控；超时先回 NONE 重试，
  再超 SHELVED 并计入失败；confirmed 设备不受影响；
- reconcile 用权威游标判定落后，返回权威阶段；
- 4 个阶段请求乱序并发，最终游标为最大 seq，成功最多计一次。

### 6. HTTP 归类与完整链路 — `tests/test_api.py`
- 401/404/409/422 与 code/reason 对应；
- 注册→固件→发布→并发（演示脚本）→成功/失败→自动停止→回滚→审计
  全链路；
- 审计事件种类齐全（`rollout_auto_halted`、`stage_transition`、
  `rollback_recorded` 等），关键输入与迁移依据在日志中可见。

## 审计示例（data/audit.log 每行一个事件）

```json
{"seq":42,"ts":1234.0,"kind":"rollout_auto_halted",
 "detail":{"batch_index":0,"failed":2,"succeeded":1,
           "failure_rate":0.66,"threshold":0.5}}
{"seq":43,"ts":1235.0,"kind":"rollback_recorded",
 "detail":{"scope":"device","device_id":"d0",
           "from_version":"2.0.0","to_version":"1.0.0",
           "result":"completed","unique_key":"rb-1"}}
```

## 已知边界（模拟实现的取舍）

- 单进程并发（线程锁）。多进程/多实例部署需把锁与状态外置，不在本模拟
  范围内；崩溃恢复依赖原子写的 store.json 与只追加的 audit.log，
  尚未刷盘的最后一次操作会丢失（崩溃在 persist 之前时语义等同未发生）。
- 固件二进制不存在，`download_url` 为 `local://firmware/{fw_id}` 占位；
  安装/重启由设备侧上报驱动，服务端只做权威状态收敛。
- 看门狗/离线清扫是被调用时执行的同步扫描，测试与演示直接调用；
  生产可由定时器触发，逻辑不变。
