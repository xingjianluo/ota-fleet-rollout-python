"""固件包定义、校验与兼容性判定。

校验规则（任一不满足即明确拒绝，设备不会被下发不确定固件）：
1. 版本号必须是合法 x.y.z；
2. 固件 model + hw_version 必须与设备完全一致；
3. 升级方向：
   - 高版本（升级）：设备当前版本必须在 compatible_from 白名单内；
     白名单为空表示允许任意低版本升级；
   - 低版本（降级）：仅当固件 allow_downgrade=True 且当前版本在
     compatible_from 白名单内才允许；
   - 同版本：拒绝（already_on_version，无意义重刷）。
"""
from __future__ import annotations

import logging

from .audit import AuditLog
from .clock import now
from .errors import (
    FirmwareNotFoundError,
    FirmwareValidationError,
    IncompatibleFirmwareError,
    UpgradePathError,
)
from .models import Firmware
from .storage import Store
from .versions import compare, parse_version

log = logging.getLogger("ota.firmwares")


class FirmwareService:
    def __init__(self, store: Store, audit: AuditLog):
        self.store = store
        self.audit = audit

    def create(self, fw_id: str, version: str, model: str, hw_version: str,
               compatible_from: list[str] | None = None,
               allow_downgrade: bool = False) -> dict:
        fw_id = str(fw_id).strip()
        if not fw_id:
            raise FirmwareValidationError("fw_id 不能为空",
                                          details={"reason": "empty_id"})
        if not model or not hw_version:
            raise FirmwareValidationError(
                "model/hw_version 不能为空",
                details={"reason": "missing_attributes"})
        parse_version(version)  # 非法版本直接拒绝
        whitelist = []
        for v in (compatible_from or []):
            parse_version(v)
            whitelist.append(v)

        with self.store.lock:
            self.store.ensure_loaded()
            if fw_id in self.store.firmwares:
                raise FirmwareValidationError(
                    f"固件 {fw_id} 已存在",
                    details={"reason": "duplicate_firmware_id",
                             "fw_id": fw_id})
            fw = Firmware(fw_id=fw_id, version=version, model=model,
                          hw_version=hw_version,
                          compatible_from=whitelist,
                          allow_downgrade=allow_downgrade,
                          created_at=now())
            self.store.firmwares[fw_id] = fw
            self.store.persist()
            self.audit.record("firmware_created", {
                "fw_id": fw_id, "version": version, "model": model,
                "hw_version": hw_version,
                "compatible_from": whitelist,
                "allow_downgrade": allow_downgrade})
            log.info("firmware create input=(id=%s ver=%s model=%s hw=%s "
                     "from=%s downgrade=%s)",
                     fw_id, version, model, hw_version, whitelist,
                     allow_downgrade)
            return {"fw_id": fw_id, "version": version, "model": model,
                    "hw_version": hw_version}

    def get(self, fw_id: str) -> Firmware:
        with self.store.lock:
            self.store.ensure_loaded()
            fw = self.store.firmwares.get(fw_id)
            if fw is None:
                raise FirmwareNotFoundError(
                    f"固件不存在: {fw_id}", details={"fw_id": fw_id})
            return fw

    def check_compatible(self, device_model: str, device_hw: str,
                         current_version: str, fw: Firmware) -> None:
        if device_model != fw.model or device_hw != fw.hw_version:
            raise IncompatibleFirmwareError(
                f"固件 {fw.fw_id}({fw.model}/{fw.hw_version}) 与设备 "
                f"({device_model}/{device_hw}) 型号/硬件不匹配",
                details={"reason": "model_hw_mismatch",
                         "device_model": device_model,
                         "device_hw": device_hw,
                         "fw_model": fw.model, "fw_hw": fw.hw_version})

        direction = compare(current_version, fw.version)
        if direction == 0:
            raise UpgradePathError(
                f"设备已在版本 {current_version}，无需刷写 {fw.version}",
                details={"reason": "already_on_version",
                         "current": current_version, "target": fw.version})
        if direction < 0:  # 当前版本更低 => 升级
            if fw.compatible_from and \
                    current_version not in fw.compatible_from:
                raise UpgradePathError(
                    f"不允许从 {current_version} 升级到 {fw.version}："
                    f"不在兼容来源白名单 {fw.compatible_from}",
                    details={"reason": "upgrade_path_not_whitelisted",
                             "current": current_version,
                             "target": fw.version,
                             "compatible_from": fw.compatible_from})
        else:  # 当前版本更高 => 降级
            if not fw.allow_downgrade:
                raise UpgradePathError(
                    f"拒绝降级 {current_version} -> {fw.version}："
                    f"固件未开放降级",
                    details={"reason": "downgrade_disallowed",
                             "current": current_version,
                             "target": fw.version})
            if current_version not in fw.compatible_from:
                raise UpgradePathError(
                    f"拒绝降级 {current_version} -> {fw.version}："
                    f"来源版本不在白名单 {fw.compatible_from}",
                    details={"reason": "downgrade_path_not_whitelisted",
                             "current": current_version,
                             "target": fw.version,
                             "compatible_from": fw.compatible_from})
