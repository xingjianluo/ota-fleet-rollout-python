"""应用装配：统一构造存储、审计与各领域服务（骨架）。"""
from __future__ import annotations

import os

from .audit import AuditLog
from .devices import DeviceService
from .firmwares import FirmwareService
from .progress import ProgressService
from .rollbacks import RollbackService
from .rollouts import RolloutService
from .stalls import StallService
from .storage import Store


class AppContext:
    def __init__(self, data_dir: str = "data", *,
                 heartbeat_ttl: float = 30.0,
                 stage_timeout: float = 60.0,
                 max_retries: int = 2,
                 stall_window: float = 60.0,
                 remedy_grace: float | None = None):
        os.makedirs(data_dir, exist_ok=True)
        self.store = Store(os.path.join(data_dir, "store.json"))
        self.audit = AuditLog(os.path.join(data_dir, "audit.log"))
        self.store.ensure_loaded()
        self.devices = DeviceService(self.store, self.audit,
                                     heartbeat_ttl=heartbeat_ttl)
        self.firmwares = FirmwareService(self.store, self.audit)
        self.rollouts = RolloutService(self.store, self.audit, self.firmwares,
                                       self.devices)
        self.stalls = StallService(self.store, self.audit,
                                   stall_window=stall_window,
                                   remedy_grace=remedy_grace,
                                   max_remedies=max_retries,
                                   rollouts=self.rollouts)
        # 汇总中展示停滞计数：回填双向引用
        self.rollouts.stalls = self.stalls
        self.progress = ProgressService(self.store, self.audit,
                                        stage_timeout=stage_timeout,
                                        max_retries=max_retries,
                                        rollouts=self.rollouts,
                                        devices=self.devices,
                                        stalls=self.stalls)
        self.rollbacks = RollbackService(self.store, self.audit,
                                         self.rollouts)
