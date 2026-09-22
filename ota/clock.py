"""时间源：默认取系统时钟，测试可替换为虚拟时钟。"""
import time

_source = time.time


def now() -> float:
    return _source()


def set_source(fn) -> None:
    global _source
    _source = fn


def reset() -> None:
    global _source
    _source = time.time
