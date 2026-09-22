"""固件校验：非法版本、型号不匹配、非法升级路径、降级管控。"""
import pytest

from ota.app import AppContext
from ota.clock import reset, set_source
from ota.errors import (
    FirmwareValidationError,
    IncompatibleFirmwareError,
    UpgradePathError,
)


@pytest.fixture()
def ctx(tmp_path):
    set_source(lambda: 1.0)
    c = AppContext(str(tmp_path))
    yield c
    reset()


def test_create_and_duplicate(ctx):
    c = ctx
    c.firmwares.create("fw1", "2.0.0", "A1", "h1", ["1.0.0"])
    with pytest.raises(FirmwareValidationError) as ei:
        c.firmwares.create("fw1", "2.0.1", "A1", "h1")
    assert ei.value.details["reason"] == "duplicate_firmware_id"
    with pytest.raises(FirmwareValidationError):
        c.firmwares.create("fwX", "2.0", "A1", "h1")


def test_model_hw_mismatch(ctx):
    c = ctx
    c.firmwares.create("fw1", "2.0.0", "A1", "h1", ["1.0.0"])
    fw = c.firmwares.get("fw1")
    with pytest.raises(IncompatibleFirmwareError) as ei:
        c.firmwares.check_compatible("A2", "h1", "1.0.0", fw)
    assert ei.value.details["reason"] == "model_hw_mismatch"
    with pytest.raises(IncompatibleFirmwareError):
        c.firmwares.check_compatible("A1", "h2", "1.0.0", fw)


def test_upgrade_path_rules(ctx):
    c = ctx
    c.firmwares.create("fw1", "2.0.0", "A1", "h1", ["1.0.0"])
    fw = c.firmwares.get("fw1")
    c.firmwares.check_compatible("A1", "h1", "1.0.0", fw)  # 白名单内
    with pytest.raises(UpgradePathError) as ei:
        c.firmwares.check_compatible("A1", "h1", "1.5.0", fw)
    assert ei.value.details["reason"] == "upgrade_path_not_whitelisted"

    c.firmwares.create("fw2", "3.0.0", "A1", "h1")
    fw_open = c.firmwares.get("fw2")
    c.firmwares.check_compatible("A1", "h1", "1.5.0", fw_open)  # 空白名单放行

    with pytest.raises(UpgradePathError) as ei:
        c.firmwares.check_compatible("A1", "h1", "2.0.0", fw)
    assert ei.value.details["reason"] == "already_on_version"


def test_downgrade_rules(ctx):
    c = ctx
    c.firmwares.create("old", "1.0.0", "A1", "h1", ["2.0.0"],
                       allow_downgrade=False)
    fw = c.firmwares.get("old")
    with pytest.raises(UpgradePathError) as ei:
        c.firmwares.check_compatible("A1", "h1", "2.0.0", fw)
    assert ei.value.details["reason"] == "downgrade_disallowed"

    c.firmwares.create("old2", "1.0.0", "A1", "h1", ["2.0.0"],
                       allow_downgrade=True)
    fw_ok = c.firmwares.get("old2")
    c.firmwares.check_compatible("A1", "h1", "2.0.0", fw_ok)
    with pytest.raises(UpgradePathError) as ei:
        c.firmwares.check_compatible("A1", "h1", "2.5.0", fw_ok)
    assert ei.value.details["reason"] == "downgrade_path_not_whitelisted"
