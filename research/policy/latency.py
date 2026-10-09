"""Latence d'une décision (appel d'outil complet : contexte de confiance, Cedar, journal) sur le mini-PC."""
import json
import statistics
import tempfile
import time
from pathlib import Path

from engine import PolicyEngine, ToolInfo, build_context

data = json.loads((Path(__file__).parent / "tools.json").read_text(encoding="utf-8"))
tools = {t["name"]: ToolInfo(t["name"], t["risk"], t["category"]) for t in data["tools"]}
started = time.perf_counter()
engine = PolicyEngine(tools, data["users"], audit=Path(tempfile.mkdtemp()) / "audit.jsonl")
print(f"chargement des politiques : {(time.perf_counter() - started) * 1000:.1f} ms")
samples = []
for i in range(2000):
    case = data["cases"][i % len(data["cases"])]
    ctx = build_context({"tool": case["tool"], "parameters": case["parameters"]}, case["user"],
                        data["users"][case["user"]], tools, case["confirmed"], "user")
    t = time.perf_counter()
    engine.decide(ctx)
    samples.append((time.perf_counter() - t) * 1000)
samples.sort()
print(f"décision : médiane {statistics.median(samples):.2f} ms, p99 {samples[int(len(samples) * 0.99)]:.2f} ms, "
      f"max {samples[-1]:.2f} ms ({len(samples)} décisions)")
