"""应用装配：统一构造存储、审计与各领域服务（骨架）。"""
from __future__ import annotations

import os

from .audit import AuditLog
from .devices import DeviceService
from .firmwares import FirmwareService
from .progress import ProgressService
from .rollbacks import RollbackService
from .rollouts import RolloutService
from .storage import Store


class AppContext:
    def __init__(self, data_dir: str = "data", *,
                 heartbeat_ttl: float = 30.0,
                 stage_timeout: float = 60.0,
                 max_retries: int = 2,
                 stall_window: float = 120.0):
        os.makedirs(data_dir, exist_ok=True)
        self.store = Store(os.path.join(data_dir, "store.json"))
        self.audit = AuditLog(os.path.join(data_dir, "audit.log"))
        self.store.ensure_loaded()
        self.devices = DeviceService(self.store, self.audit,
                                     heartbeat_ttl=heartbeat_ttl)
        self.firmwares = FirmwareService(self.store, self.audit)
        self.rollouts = RolloutService(self.store, self.audit, self.firmwares,
                                       self.devices)
        self.progress = ProgressService(self.store, self.audit,
                                        stage_timeout=stage_timeout,
                                        max_retries=max_retries,
                                        stall_window=stall_window,
                                        rollouts=self.rollouts,
                                        devices=self.devices)
        self.rollbacks = RollbackService(self.store, self.audit,
                                         self.rollouts)
