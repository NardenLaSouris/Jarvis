"""Latence de la porte de décision en mode both (PermissionManager + Cedar), sur le registre réel d'ORION.

    python research/policy/latency.py   (avec cedarpy installé)
"""

import itertools
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_policy import USERS, call, make  # noqa: E402

started = time.perf_counter()
core, _ = make(Path(__import__("tempfile").mkdtemp()), "both")
print(f"chargement : {(time.perf_counter() - started) * 1000:.1f} ms")
gate = core.permissions
samples = []
for tool, user in itertools.islice(itertools.cycle(itertools.product(core.registry.list(), USERS)), 2000):
    t = time.perf_counter()
    gate.decide(user, tool, {})
    samples.append((time.perf_counter() - t) * 1000)
samples.sort()
print(f"décision (PermissionManager + Cedar + journal) : médiane {statistics.median(samples):.2f} ms, "
      f"p99 {samples[int(len(samples) * 0.99)]:.2f} ms, max {samples[-1]:.2f} ms ({len(samples)} décisions)")
