"""本地端到端演示（无需任何外部服务）：

    .venv/bin/python scripts/demo.py

流程：注册 -> 固件 -> 灰度两批 -> 一台失败触发阈值熔断 ->
看门狗搁置离线设备 -> 重连恢复 -> 单设备回滚，并演示各类拒绝原因。
状态快照写入 data/demo_state.json，删除后可重新演示。
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ota.clock import FakeClock
from ota.errors import OtaError
from ota.persistence import JsonStore
from ota.service import OtaService

PATH = "data/demo_state.json"


def show(title: str, obj) -> None:
    print(f"\n===== {title} =====")
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def expect_error(fn, label: str) -> None:
    try:
        fn()
    except OtaError as e:
        print(f"[拒绝] {label}: code={e.code} reason={e.reason} msg={e.message}")


def main() -> None:
    os.makedirs("data", exist_ok=True)
    if os.path.exists(PATH):
        os.remove(PATH)
    clk = FakeClock()
    svc = OtaService(JsonStore(PATH), clk)

    # 1) 注册 4 台设备
    tokens = {}
    for i, (model, hw, fw) in enumerate([
        ("sensor-x", "hw2", "1.0.0"),
        ("sensor-x", "hw2", "1.0.0"),
        ("sensor-x", "hw2", "1.0.0"),
        ("sensor-y", "hw2", "1.0.0"),  # 型号不匹配
    ], start=1):
        r = svc.register_device({"device_id": f"dev{i}", "model": model,
                                 "hw_version": hw, "current_fw_version": fw})
        tokens[f"dev{i}"] = r["token"]

    expect_error(lambda: svc.register_device(
        {"device_id": "dev1", "model": "sensor-x", "hw_version": "hw2",
         "current_fw_version": "1.0.0"}), "重复注册")
    expect_error(lambda: svc.heartbeat(
        {"device_id": "dev1", "token": "forged", "seq": 1}), "伪造令牌")

    # 2) 固件（旧版用于回滚，目标版用于发布）
    svc.create_firmware({"fw_id": "fw-1.0", "version": "1.0.0",
                         "model": "sensor-x", "hw_version": "hw2",
                         "checksum": "sha256:old"})
    svc.create_firmware({"fw_id": "fw-2.0", "version": "2.0.0",
                         "model": "sensor-x", "hw_version": "hw2",
                         "checksum": "sha256:new"})

    # 3) 创建发布：每批 2 台，失败率阈值 0.5
    rel = svc.create_release({
        "target_fw_id": "fw-2.0", "name": "2.0 灰度",
        "batch_size": 2, "failure_threshold": 0.5,
        "stage_timeout": 300, "heartbeat_timeout": 120,
        "max_stage_attempts": 2,
    })["release"]["release_id"]
    svc.start_release(rel)

    b1 = svc.plan_next_batch(rel)
    show("第一批（dev4 因型号不匹配被筛除）", b1["batch"]["device_ids"])

    # 第二批先规划（dev3），随后第一批的失败才触发熔断：
    # 已在第二批但未认领的 dev3 会被确定性搁置，而不是悬挂
    b2 = svc.plan_next_batch(rel)
    show("第二批（熔断前已规划）", b2["batch"]["device_ids"])

    # dev1 成功；dev2 在下载阶段明确失败 -> 触发阈值熔断
    j1 = svc.claim_job({"device_id": "dev1", "token": tokens["dev1"]})["job"]["job_id"]
    j2 = svc.claim_job({"device_id": "dev2", "token": tokens["dev2"]})["job"]["job_id"]
    for i, st in enumerate(["downloaded", "installing", "installed",
                            "rebooting", "confirmed"], start=1):
        svc.heartbeat({"device_id": "dev1", "token": tokens["dev1"], "seq": i})
        svc.report_stage({"device_id": "dev1", "token": tokens["dev1"],
                          "job_id": j1, "stage": st, "seq": i})
    svc.report_stage({"device_id": "dev2", "token": tokens["dev2"],
                      "job_id": j2, "stage": "downloaded", "seq": 1,
                      "failed": True, "reason": "flash_checksum_mismatch"})
    show("熔断后发布状态", svc.get_release(rel)["release"])

    # 熔断后不能继续/规划
    expect_error(lambda: svc.plan_next_batch(rel), "熔断后规划新批次")
    expect_error(lambda: svc.resume_release(rel), "熔断后直接继续")

    # 4) 看门狗：dev3 在熔断时已被搁置（未认领）；这里再扫一次确认幂等
    clk.advance(400)
    show("看门狗清扫（dev3 已为 suspended，无新增搁置）", svc.sweep())

    # 5) dev1 成功后回滚到升级前版本
    rb = svc.rollback_device({"device_id": "dev1"})
    rjid = rb["job"]["job_id"]
    expect_error(lambda: svc.rollback_device({"device_id": "dev1"}), "重复回滚")
    svc.claim_job({"device_id": "dev1", "token": tokens["dev1"]})
    for i, st in enumerate(["downloaded", "installing", "installed",
                            "rebooting", "confirmed"], start=10):
        svc.report_stage({"device_id": "dev1", "token": tokens["dev1"],
                          "job_id": rjid, "stage": st, "seq": i})
    show("dev1 回滚后设备视图", svc.get_device("dev1"))
    show("发布最终统计", svc.get_release(rel)["stats"])
    print("\n演示完成，审计事件条数:", len(svc.audit_log(limit=10_000)))


if __name__ == "__main__":
    main()
