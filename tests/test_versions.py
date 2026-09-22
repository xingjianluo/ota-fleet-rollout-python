"""版本号：解析、比较与非法格式归类。"""
import pytest

from ota.errors import FirmwareValidationError
from ota.versions import compare, is_upgrade, parse_version


def test_parse_and_compare():
    assert parse_version("1.2.3") == (1, 2, 3)
    assert compare("1.0.0", "1.0.1") == -1
    assert compare("2.0.0", "1.9.9") == 1
    assert compare("1.0.0", "1.0.0") == 0
    assert is_upgrade("1.0.0", "2.0.0")
    assert not is_upgrade("2.0.0", "2.0.0")


@pytest.mark.parametrize("bad", ["1.2", "v1.0.0", "1.0.x", "1.-1.0", ""])
def test_invalid_version(bad):
    with pytest.raises(FirmwareValidationError) as ei:
        parse_version(bad)
    assert ei.value.code == "firmware_validation_failed"
    assert ei.value.details["version"] == bad
