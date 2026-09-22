"""测试夹具：临时状态文件、可控时钟、带日志打印的服务与 HTTP 客户端。

约定：每个用例通过 harness.log 打印“关键输入 + 状态迁移依据”，
失败链路必须断言 error.reason，保证错误归类可区分。
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from ota.api import create_app
from ota.clock import FakeClock
from ota.persistence import JsonStore
from ota.service import OtaService

# 单测日志中打印关键输入与状态迁移依据
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


@dataclass
class Harness:
    svc: OtaService
    clock: FakeClock
    tmpdir: str
    tokens: dict[str, str] = field(default_factory=dict)
    client: TestClient | None = None

    def log(self, title: str, **kw) -> None:
        logging.getLogger("ota.test").info("CASE %s | %s", title, kw)

    # -- 便捷构造器 --
    def reg(self, device_id: str, model: str = "M1", hw: str = "h1",
            fw: str = "1.0.0") -> dict:
        r = self.svc.register_device({
            "device_id": device_id, "model": model, "hw_version": hw,
            "current_fw_version": fw,
        })
        self.tokens[device_id] = r["token"]
        self.log(f"register {device_id}", model=model, hw=hw, fw=fw)
        return r

    def make_fw(self, fw_id: str, version: str, model: str = "M1",
                hw: str = "h1", checksum: str = "sha256:x") -> dict:
        return self.svc.create_firmware({
            "fw_id": fw_id, "version": version, "model": model,
            "hw_version": hw, "checksum": checksum,
        })

    def hb(self, device_id: str, seq: int) -> dict:
        return self.svc.heartbeat({
            "device_id": device_id, "token": self.tokens[device_id], "seq": seq,
        })

    def claim(self, device_id: str) -> dict:
        return self.svc.claim_job({
            "device_id": device_id, "token": self.tokens[device_id],
        })

    def report(self, device_id: str, job_id: str, stage: str, seq: int,
               **extra) -> dict:
        payload = {"device_id": device_id, "token": self.tokens[device_id],
                   "job_id": job_id, "stage": stage, "seq": seq, **extra}
        self.log(f"report {device_id}", job=job_id, stage=stage, seq=seq, **extra)
        return self.svc.report_stage(payload)

    def drive_success(self, device_id: str, job_id: str, seq0: int = 1) -> None:
        """按状态机把一个升级/回滚任务推进到成功终态。"""
        stages = ["downloaded", "installing", "installed", "rebooting", "confirmed"]
        for i, st in enumerate(stages):
            self.hb(device_id, seq0 + i)
            self.report(device_id, job_id, st, seq0 + i)

    def job_of(self, device_id: str, predicate=lambda j: True):
        jobs = [j for j in self.svc.store.jobs.values()
                if j.device_id == device_id and predicate(j)]
        return sorted(jobs, key=lambda j: j.created_ts)[-1]


@pytest.fixture
def harness():
    tmp = tempfile.mkdtemp(prefix="ota-test-")
    store = JsonStore(os.path.join(tmp, "state.json"))
    clock = FakeClock()
    svc = OtaService(store, clock)
    yield Harness(svc=svc, clock=clock, tmpdir=tmp)


@pytest.fixture
def client(harness):
    app = create_app(store=harness.svc.store, clock=harness.clock)
    c = TestClient(app)
    harness.client = c
    return c
