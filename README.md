# OTA 固件分批发布与设备编队管理（本地可复现）

纯本地模拟的 OTA 灰度发布系统：设备注册与心跳、固件兼容性校验、分批灰度
（容量/失败率阈值可配、暂停/继续、自动停止）、阶段看门狗、**停滞识别与
自动续跑**（持续上报但阶段不动的“假新鲜”设备也能被观察、有限补救、搁置
而不拖死整批）、断线重连对账、单设备/整批回滚，以及并发下的计数一致性。
**不依赖任何外部服务、真实凭据或付费资源**，数据落在本地 `data/` 目录
（JSON + JSONL）。

## 快速开始

```bash
uv sync
uv run pytest -q            # 全部用例（异常链路 + 停滞专项 + 并发）
uv run python scripts/demo.py   # 端到端场景演示，打印状态迁移依据
uv run uvicorn main:app --reload  # 启动 HTTP 服务（可选，交互见 /docs）
```

HTTP 服务默认读写 `./data/`，可用环境变量 `OTA_DATA_DIR` 指定其他目录。

## 文档

- [架构与存储](docs/architecture.md)
- [发布流程与失败率阈值](docs/rollout.md)
- [回滚规则](docs/rollback.md)
- [设备与升级状态机（含停滞识别规则）](docs/state-machine.md)
- [HTTP API 与错误码](docs/api.md)
- [验证方法与场景清单](docs/verification.md)

## 代码结构

```
ota/
  models.py     枚举与领域数据类（设备/固件/发布/批次/升级状态/回滚记录）
  errors.py     统一错误类型（code + http_status + details.reason）
  versions.py   语义化版本解析、升降级方向
  clock.py      可替换时间源（测试用虚拟时钟）
  storage.py    JSON 原子写持久化 + RLock
  audit.py      JSONL 追加审计（所有拒绝与状态迁移可追溯）
  devices.py    注册/鉴权/心跳/离线清扫
  firmwares.py  固件定义与型号/硬件/升级路径校验
  rollouts.py   发布生命周期、批次推进、原子认领、阈值自动停止
  progress.py   阶段上报、乱序/重放收敛、看门狗、停滞分类、重连对账
  stalls.py     停滞巡检：观察窗口 -> 有限补救 -> 搁置（幂等状态机）
  rollbacks.py  单设备/整批回滚、幂等、全量预检
  app.py        依赖装配
api/routes.py   FastAPI 路由与错误 JSON 化
scripts/demo.py 本地端到端演示
tests/          单元/并发/HTTP 测试
```
