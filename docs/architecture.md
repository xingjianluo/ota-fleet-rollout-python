# 架构与存储设计

## 分层

```
HTTP 层 (api/routes.py)
    │  请求校验(pydantic) → 服务调用 → OTAError 统一转 JSON
    ▼
服务层 ota/{devices,firmwares,rollouts,progress,stalls,rollbacks}.py
    │  所有变更操作在 store.lock（threading.RLock）同一把锁内完成
    ▼
模型/持久化 ota/{models,storage,audit}.py + ota/clock.py
```

- **时间注入**：`ota/clock.py` 默认 `time.time()`，测试和演示用
  `set_source()` 替换为虚拟时钟，确定性地推进 TTL 与超时。
- **无外部依赖**：认证 token 用 `secrets.token_hex` 生成，仅以
  sha256 哈希落盘；不连接任何第三方。

## 持久化

| 文件 | 格式 | 用途 |
|---|---|---|
| `data/store.json` | JSON（原子 `os.replace`） | 设备、固件、发布、批次、设备升级状态、回滚记录 |
| `data/audit.log` | JSONL（每行一条，append/fsync） | 注册/拒绝/认领/阶段迁移/自动停止/回滚等全部关键事件 |

- `store.json` 写操作走"临时文件 + rename"，进程崩溃不会产生半截 JSON；
  读操作在启动时一次性载入内存，所有运行期状态在内存中、按策略落盘。
- 审计只追加不修改，`AuditLog` 序号在进程内严格单调；重建上下文时
  从 JSONL 恢复序号，回滚幂等键（`unique_key`）也以审计记录为准。

## 并发模型

- 全系统只有一把 **可重入锁** `store.lock`：认领、上报、回滚、批次推进
  都是"锁内检查 + 锁内变更 + 锁内持久化"的原子操作，因此
  - 批次认领天然不会超卖（预分区 + 容量检查在锁内）；
  - 批次成功/失败集合按设备号去重后重建，重复终态上报不重复计数；
  - 同一设备并发回滚到同一版本，恰好一次 `completed`，其余得到
    `rollback_duplicate`。
- 锁只保护内存结构与持久化，持有时间短，不包含 sleep/IO 等待。

## 错误归类

所有业务错误继承 `OTAError`，携带：

- `code`：机器可读错误码（如 `stale_heartbeat`、`incompatible_firmware`）；
- `http_status`：401/404/409/422；
- `details.reason`：**更细粒度、可区分的拒绝原因**（见
  [api.md](api.md) 的原因表）。

任何拒绝都不静默修改已有状态，并同时写入审计日志。
