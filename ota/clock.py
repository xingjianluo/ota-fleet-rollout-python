"""时间源：可注入的时钟，测试中可冻结/快进，保证超时行为可复现。"""

from __future__ import annotations

import time
from collections.abc import Callable


class Clock:
    """默认走系统墙钟；测试可传入受控时间源（如手动推进的 FakeClock）。"""

    def __init__(self, source: Callable[[], float] | None = None) -> None:
        self._source = source or time.time

    def now(self) -> float:
        return self._source()


class FakeClock:
    """测试用时钟：手动 advance，避免真实 sleep。"""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self._t = start

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> float:
        self._t += seconds
        return self._t
