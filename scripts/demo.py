"""本地端到端演示：无需外部服务，全部状态在临时目录中模拟。

运行：uv run python scripts/demo.py
场景覆盖：
1. 注册/重复注册/伪造 token/过期心跳的可区分拒绝；
2. 固件不兼容（型号、降级、白名单）拒绝；
3. 两批灰度，第二批并发认领不超容量；
4. 乱序/重复上报收敛，看门狗重试；
5. 停滞识别：设备活着但同阶段反复上报 -> 观察 -> 补救 -> 续跑成功；
6. 失败率阈值自动停止；
7. 单设备回滚 + 回滚重复拒绝 + 幂等重放。
"""
from __future__ import annotations

import logging
import sys
import tempfile
import threading

sys.path.insert(0, ".")

from ota.app import AppContext
from ota.clock import reset, set_source
from ota.errors import OTAError

logging.basicConfig(level=logging.INFO,
                    format="%(levelname)-7s %(name)s: %(message)s")


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def tick(self, dt):
        self.t += dt


def show_reject(title, fn):
    try:
        fn()
    except OTAError as exc:
        print(f"  [拒绝] {title}: code={exc.code} "
              f"reason={exc.details.get('reason')} http={exc.http_status}")


def main():
    tmp = tempfile.mkdtemp(prefix="ota-demo-")
    print(f"数据目录: {tmp}")
    clock = Clock()
    set_source(clock)
    ctx = AppContext(tmp, heartbeat_ttl=10, stage_timeout=30, max_retries=1,
                     stall_window=20, remedy_grace=10)

    print("\n== 1. 设备注册与身份/心跳防护 ==")
    tokens = {}
    for i in range(6):
        tokens[f"d{i}"] = ctx.devices.register(
            f"d{i}", "A1", "h1", "1.0.0")["token"]
    show_reject("重复注册(相同属性)",
                lambda: ctx.devices.register("d0", "A1", "h1", "1.0.0"))
    show_reject("重复注册(属性冲突)",
                lambda: ctx.devices.register("d0", "A1", "h1", "9.9.9"))
    show_reject("伪造 token",
                lambda: ctx.devices.heartbeat("d0", "fake", 1))
    ctx.devices.heartbeat("d0", tokens["d0"], 1)
    show_reject("过期心跳 seq=1",
                lambda: ctx.devices.heartbeat("d0", tokens["d0"], 1))
    ctx.devices.heartbeat("d0", tokens["d0"], 2)

    print("\n== 2. 固件定义与不兼容拒绝 ==")
    ctx.firmwares.create("fw2", "2.0.0", "A1", "h1", ["1.0.0"])
    ctx.firmwares.create("fw1back", "1.0.0", "A1", "h1", ["2.0.0"],
                         allow_downgrade=True)
    show_reject("型号不匹配的发布",
                lambda: ctx.rollouts.create(
                    "bad", "fw2", ["d0"], ) and None
                if False else
                ctx.firmwares.check_compatible(
                    "A2", "h1", "1.0.0", ctx.firmwares.get("fw2")))

    print("\n== 3. 创建两批灰度 (batch_size=4, 阈值 0.5) ==")
    ids = [f"d{i}" for i in range(6)]
    s = ctx.rollouts.create("rel-1", "fw2", ids, batch_size=4,
                            failure_rate_threshold=0.5)
    print("  批次容量:", [b["capacity"] for b in s["batches"]])
    ctx.rollouts.start("rel-1")

    print("\n== 4. 六台设备并发认领（批次容量 4，不应超卖） ==")
    errs = []

    def claim(d):
        try:
            ctx.rollouts.claim("rel-1", d, tokens[d])
        except OTAError as exc:
            errs.append((d, exc.details["reason"]))

    ts = [threading.Thread(target=claim, args=(d,)) for d in ids]
    [t.start() for t in ts]
    [t.join() for t in ts]
    b0 = ctx.store.rollouts["rel-1"].batches[0]
    print(f"  批次0认领: {sorted(b0.claimed)} (共 {len(b0.claimed)})")
    print(f"  被拒绝: {sorted(errs)}")

    print("\n== 5. d0 正常升级，乱序/重复上报收敛 ==")
    ctx.progress.report("d0", tokens["d0"], "rel-1", "downloaded", 10)
    ctx.progress.report("d0", tokens["d0"], "rel-1", "installed", 11)
    show_reject("迟到 seq=9",
                lambda: ctx.progress.report(
                    "d0", tokens["d0"], "rel-1", "downloaded", 9))
    v = ctx.progress.report("d0", tokens["d0"], "rel-1", "installed", 11)
    print(f"  重复 seq=11 幂等: duplicate={v['duplicate']}")
    for seq, stage in [(12, "rebooted"), (13, "confirmed")]:
        ctx.progress.report("d0", tokens["d0"], "rel-1", stage, seq)
    print(f"  d0 当前固件: {ctx.store.devices['d0'].fw_version}")

    print("\n== 6. d1 卡死 -> 看门狗先重试，再搁置（d2/d3 尚未上报不受影响） ==")
    ctx.progress.report("d1", tokens["d1"], "rel-1", "downloaded", 1)
    clock.tick(31)
    print("  watchdog:", ctx.progress.watchdog("rel-1"))
    clock.tick(31)
    print("  watchdog:", ctx.progress.watchdog("rel-1"))

    print("\n== 6b. d2 “活着但停滞” -> 观察 -> 补救 -> 自动续跑成功 ==")
    ctx.progress.report("d2", tokens["d2"], "rel-1", "downloaded", 1)
    # seq 持续增长、时间持续更新，但阶段停在 downloaded
    clock.tick(5)
    v = ctx.progress.report("d2", tokens["d2"], "rel-1", "downloaded", 2)
    print(f"  窗口内同阶段新鲜上报: {v['report_class']}")
    clock.tick(16)  # 超过 stall_window=20
    v = ctx.progress.report("d2", tokens["d2"], "rel-1", "downloaded", 3)
    print(f"  过窗口同阶段新鲜上报: {v['report_class']}（活着≠在推进）")
    print("  stall-sweep:", ctx.progress.stall_sweep("rel-1"))  # 仅观察
    clock.tick(11)  # 过 remedy_grace=10
    print("  stall-sweep:", ctx.progress.stall_sweep("rel-1"))  # 补救回 NONE
    print("  重复巡检幂等:", ctx.progress.stall_sweep("rel-1"))
    # 设备获得补救后重新下载并一路推进到 confirmed
    for seq, stage in [(4, "downloaded"), (5, "installed"),
                       (6, "rebooted"), (7, "confirmed")]:
        v = ctx.progress.report("d2", tokens["d2"], "rel-1", stage, seq)
    print(f"  d2 续跑后固件: {ctx.store.devices['d2'].fw_version} "
          f"(分类 {v['report_class']} -> 推进即清停滞)")
    print("  停滞汇总:", ctx.rollouts.summary("rel-1")["stall"])

    print("\n== 7. d3 失败（叠加 d1 搁置）-> 失败率阈值自动停止 ==")
    ctx.progress.report("d3", tokens["d3"], "rel-1", "downloaded", 1)
    ctx.progress.report_failure("d3", tokens["d3"], "rel-1", 2, "power")
    r = ctx.store.rollouts["rel-1"]
    print(f"  发布状态: {r.status.value}, 原因: {r.halted_reason}")
    print(f"  批次1状态: {r.batches[1].status.value}（未下发）")
    show_reject("已搁置 d1 再回滚",
                lambda: ctx.rollbacks.rollback_device(
                    "d1", "1.0.0", "rb-d1"))

    print("\n== 8. d0 回滚、重复拒绝与幂等重放 ==")
    res = ctx.rollbacks.rollback_device("d0", "1.0.0", "rb-d0")
    print(f"  回滚结果: {res['from_version']} -> {res['to_version']} "
          f"({res['result']})")
    show_reject("换 key 再回滚同一版本",
                lambda: ctx.rollbacks.rollback_device(
                    "d0", "1.0.0", "rb-d0-again"))
    replay = ctx.rollbacks.rollback_device("d0", "1.0.0", "rb-d0")
    print(f"  相同 key 重放: duplicate={replay['duplicate']} "
          f"result={replay['result']}")

    print("\n== 9. 最终状态快照 ==")
    print(" ", ctx.rollouts.summary("rel-1"))
    completed = [x for x in ctx.store.rollback_records
                 if x.result == "completed"]
    print(f"  成功回滚记录数: {len(completed)}（无重复回滚）")
    print(f"  审计事件总数: {len(ctx.audit.all())}，"
          f"数据已落盘于 {tmp}")
    reset()


if __name__ == "__main__":
    main()
