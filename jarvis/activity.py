"""Journal d'activité : les événements importants de JARVIS, conservés localement.

Stockage JSONL (une ligne JSON par événement, ajout seul) derrière une petite interface ``ActivityStore`` :
un autre stockage (SQLite...) pourra le remplacer sans toucher au reste.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol

from jarvis.events import ALL, TOOL_EXECUTED, TOOL_FAILED, Event, EventBus
from jarvis.scheduling.manager import EVENT_TYPES as SCHEDULING_EVENTS

log = logging.getLogger(__name__)

DEFAULT_TYPES = (TOOL_EXECUTED, TOOL_FAILED, *SCHEDULING_EVENTS)


class ActivityStore(Protocol):
    def append(self, record: dict) -> None: ...

    def read(self, limit: int) -> list[dict]: ...


class JsonlActivityStore:
    """Fichier JSONL ; au-delà de ``max_bytes``, l'ancien fichier devient ``<nom>.1.jsonl`` (un seul archivé)."""

    def __init__(self, path: Path, max_bytes: int = 1_000_000):
        self.path = Path(path)
        self._max_bytes = max_bytes
        self._lock = threading.Lock()

    def append(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size + len(line.encode()) > self._max_bytes:
                self.path.replace(self.path.with_suffix(".1.jsonl"))
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)

    def read(self, limit: int) -> list[dict]:
        with self._lock:
            if not self.path.exists():
                return []
            with self.path.open(encoding="utf-8", errors="replace") as fh:
                lines = deque(fh, maxlen=limit)
        records = []
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                records.append(record)
        return records


@dataclass(frozen=True)
class ActivityEntry:
    type: str
    time: datetime
    source: str
    payload: Mapping[str, Any]

    @classmethod
    def from_record(cls, record: dict) -> "ActivityEntry | None":
        try:
            return cls(str(record["type"]), datetime.fromtimestamp(float(record["timestamp"])),
                       str(record.get("source", "")), dict(record.get("payload") or {}))
        except (KeyError, TypeError, ValueError):
            return None

    @property
    def summary(self) -> str:
        subject = str(self.payload.get("tool") or self.payload.get("subject") or self.source)
        error = self.payload.get("error")
        return f"{subject} ({error})" if error else subject

    def line(self) -> str:
        return f"[{self.time:%H:%M:%S}] {self.type} — {self.summary}"


class ActivityLog:
    def __init__(self, store: ActivityStore, types: Iterable[str] = DEFAULT_TYPES):
        self._store = store
        self._types = frozenset(types)

    def attach(self, bus: EventBus) -> Callable[[], None]:
        return bus.subscribe(ALL, self.record)

    def record(self, event: Event) -> None:
        if event.type in self._types:
            self._store.append(event.as_dict())

    def recent(self, limit: int = 20) -> list[ActivityEntry]:
        entries = (ActivityEntry.from_record(r) for r in self._store.read(limit))
        return [e for e in entries if e is not None]
