"""Médianes et p90 des latences d'ORION, à partir des lignes « [mesure] » du journal (jarvis.agent.measure_line).

Sur le mini-PC, pour une semaine :
    journalctl -u orion --since "-7 days" -o cat | python scripts/latency_report.py

Chaque demande parlée ou écrite qui a reçu une réponse donne une ligne « [mesure] chemin=… stt=… total=… ».
Les durées sont mesurées à l'horloge monotone (time.perf_counter) ; « total » va de la fin de la phrase au début de
la réponse parlée (absent pour un message écrit). Résultats séparés par chemin : « deterministe » (formule, commande
rapide, contexte) et « llm » (le modèle a choisi l'outil ou rédigé la réponse), et par source de transcription.
"""

from __future__ import annotations

import re
import statistics
import sys
from collections import defaultdict

LINE = re.compile(r"\[mesure\]\s+(.*)$")
FIELDS = ("stt", "choix", "outil", "llm_premier", "llm_total", "tts_premier", "total")


def parse(lines) -> list[dict]:
    rows = []
    for line in lines:
        match = LINE.search(line)
        if not match:
            continue
        row = {}
        for part in match.group(1).split():
            key, _, value = part.partition("=")
            try:
                row[key] = float(value)
            except ValueError:
                row[key] = value
        rows.append(row)
    return rows


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(p * (len(ordered) - 1))))]


def report(rows: list[dict]) -> str:
    if not rows:
        return "Aucune ligne [mesure] : ORION n'a pas encore répondu depuis le déploiement de cette mesure."
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[f"chemin {row.get('chemin', '?')}"].append(row)
        if "source" in row:
            groups[f"transcription {row['source']}"].append(row)
    out = [f"{len(rows)} demandes"]
    for name in sorted(groups):
        members = groups[name]
        out.append(f"\n{name} : {len(members)} demandes")
        for field in FIELDS:
            values = [r[field] for r in members if isinstance(r.get(field), float)]
            if values:
                out.append(f"  {field:12} n={len(values):4}  médiane {statistics.median(values):6.2f} s  "
                           f"p90 {percentile(values, 0.9):6.2f} s  max {max(values):6.2f} s")
    return "\n".join(out)


def main() -> None:
    print(report(parse(sys.stdin)))


if __name__ == "__main__":
    main()
