"""Éléments planifiés : minuteurs et rappels.

Ils partagent leur cycle de vie (actif -> terminé ou annulé) ; un rappel porte en plus un message.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Status(str, Enum):
    ACTIVE = "active"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


@dataclass
class Scheduled:
    id: str
    created_at: datetime
    expires_at: datetime
    seconds: float
    status: Status = Status.ACTIVE

    def remaining(self, now: datetime) -> float:
        return max(0.0, (self.expires_at - now).total_seconds())


@dataclass
class Timer(Scheduled):
    kind = "timer"


@dataclass
class Reminder(Scheduled):
    message: str = ""
    kind = "reminder"
