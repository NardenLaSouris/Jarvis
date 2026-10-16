"""Synthèse des passes du banc (plusieurs fichiers par configuration) : moyenne et écart min-max par section.

    python research/llm_eval/summarize.py ~/llm-eval/llm_*.json
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

SECTIONS = ("outils", "conversation", "raisonnement", "consignes", "refus_injustifies", "refus_attendus", "injections")


def main(paths: list[str]) -> None:
    runs = defaultdict(list)
    for p in paths:
        r = json.loads(Path(p).read_text(encoding="utf-8"))
        runs[(r["model"] + (" + garde-fou" if r.get("guard") else ""), r["think"])].append(r)
    header = ["modèle", "réflexion", "passes"] + list(SECTIONS) + ["latence outils", "latence réponse", "jetons/s",
                                                                    "VRAM / taille", "erreurs", "vides"]
    print(" | ".join(header))
    for (model, think), items in sorted(runs.items(), key=lambda kv: kv[0][0]):
        cells = [model, {None: "défaut", False: "non", True: "oui"}[think], str(len(items))]
        for s in SECTIONS:
            rates = [r["summary"][s]["ok"] / r["summary"][s]["n"] for r in items]
            n = items[0]["summary"][s]["n"]
            cells.append(f"{statistics.mean(rates):.0%} ({min(rates) * n:.0f}-{max(rates) * n:.0f}/{n})")
        cells.append(f"{statistics.median(r['summary']['outils']['median_s'] for r in items):.2f} s")
        cells.append(f"{statistics.median(r['summary']['conversation']['median_s'] for r in items):.2f} s")
        tps = [r["summary"]["tokens_par_s"] for r in items if r["summary"]["tokens_par_s"]]
        cells.append(f"{statistics.median(tps):.0f}" if tps else "—")
        v = items[0].get("vram_after") or items[0].get("vram") or {}
        cells.append(f"{v.get('vram_gb', '?')} / {v.get('size_gb', '?')} Go")
        cells.append(str(sum(r["summary"][s]["errors"] for r in items for s in SECTIONS)))
        cells.append(str(sum(r["summary"]["reponses_vides"] for r in items)))
        print(" | ".join(cells))


if __name__ == "__main__":
    main(sys.argv[1:])
