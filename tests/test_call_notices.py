"""Message « Votre PC est allumé. » (jarvis/call/notices.py) : seulement après une vraie absence du PC."""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.call.notices import MIN_GAP, MIN_OFFLINE, PC_ONLINE, PcOnlineNotice  # noqa: E402


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def sent_after(notice_steps):
    sent, clock = [], Clock()
    notice = PcOnlineNotice(sent.append, clock)
    for delay, connected in notice_steps:
        clock.now += delay
        notice.status(connected)
    time.sleep(0.2)
    return sent


def test_pc_turned_on_after_being_off_sends_a_message():
    assert sent_after([(0, False), (MIN_OFFLINE + 30, True)]) == [PC_ONLINE]


def test_short_outage_or_core_start_sends_nothing():
    assert sent_after([(0, True)]) == []  # démarrage d'ORION, PC déjà allumé
    assert sent_after([(0, False), (10, True)]) == []  # agent relancé, coupure réseau


def test_at_most_one_message_per_gap():
    steps = [(0, False), (MIN_OFFLINE + 1, True), (1, False), (MIN_OFFLINE + 1, True)]
    assert sent_after(steps) == [PC_ONLINE]
    assert sent_after([*steps, (1, False), (MIN_GAP, True)]) == [PC_ONLINE, PC_ONLINE]
