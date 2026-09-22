"""语义化版本解析与升降级方向判断。"""
from __future__ import annotations

from .errors import FirmwareValidationError


def parse_version(v: str) -> tuple[int, int, int]:
    parts = str(v).strip().split(".")
    if len(parts) != 3:
        raise FirmwareValidationError(
            f"版本号必须是 x.y.z 形式: {v!r}",
            details={"version": v})
    try:
        nums = tuple(int(p) for p in parts)
    except ValueError:
        raise FirmwareValidationError(
            f"版本号必须全部为数字: {v!r}",
            details={"version": v})
    if any(n < 0 for n in nums):
        raise FirmwareValidationError(
            f"版本号不允许负数: {v!r}", details={"version": v})
    return nums  # type: ignore[return-value]


def compare(a: str, b: str) -> int:
    """返回 -1/0/1，分别表示 a<b / a==b / a>b。"""
    ta, tb = parse_version(a), parse_version(b)
    if ta < tb:
        return -1
    if ta > tb:
        return 1
    return 0


def is_upgrade(a: str, b: str) -> bool:
    """b 相对 a 是否为升级（版本严格变高）。"""
    return compare(a, b) < 0
