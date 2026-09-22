"""审计日志：关键状态迁移与拒绝原因以 JSONL 追加落盘，内存保留全量。"""
from __future__ import annotations

import json
import os
import threading

from .clock import now
from .models import AuditEvent


class AuditLog:
    def __init__(self, path: str | None = None):
        self._path = path
        self._lock = threading.Lock()
        self._seq = 0
        self._events: list[AuditEvent] = []
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    raw = json.loads(line)
                    self._events.append(AuditEvent(
                        seq=raw["seq"], ts=raw["ts"], kind=raw["kind"],
                        detail=raw.get("detail", {})))
                    self._seq = max(self._seq, raw["seq"])

    def record(self, kind: str, detail: dict, ts: float | None = None) -> AuditEvent:
        event = AuditEvent(seq=0, ts=ts if ts is not None else now(),
                           kind=kind, detail=detail)
        with self._lock:
            self._seq += 1
            event.seq = self._seq
            self._events.append(event)
            if self._path:
                os.makedirs(os.path.dirname(os.path.abspath(self._path)),
                            exist_ok=True)
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(
                        {"seq": event.seq, "ts": event.ts,
                         "kind": event.kind, "detail": event.detail},
                        ensure_ascii=False) + "\n")
                    f.flush()
        return event

    def tail(self, limit: int = 50) -> list[AuditEvent]:
        with self._lock:
            return list(self._events[-limit:])

    def all(self) -> list[AuditEvent]:
        with self._lock:
            return list(self._events)
