"""Banc de compréhension d'ORION : vrai agent (routeur, raccourcis, planificateur, contexte, LLM), outils simulés.

Lancé sur le mini-PC dans une copie de travail avec harness.py (faux agents Windows, ampoules simulées) :
    TZ=Europe/Paris python bench/comprehension/run.py dev.json [--model M] [--think false] [--out res.json]

Aucun outil n'agit : chaque exécution est remplacée par un résultat factice. Ce qui est mesuré, c'est l'appel
PROPOSÉ au Core (avant la décision de permission), tour par tour. Les permissions sont celles du PermissionManager
seul ([tools.policy] engine = "builtin", modèle de confiance) pour ne mesurer que la compréhension.

Attentes (dev.json, test.json) : alternatives séparées par « | » entouré d'espaces ;
  none / none+question ; tool:NOM [clé=valeur] [clé~a|b] ; tools:NOM1,NOM2 ; confirm:NOM [clé~...].
Catégories d'échec : compréhension (mauvaise route, action non demandée), planification (route d'outil sans
appel, actions incomplètes), choix d'outil, paramètres, ambiguïté (pas de question), exécution (demande invalide).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, ".")

import harness  # noqa: E402
from jarvis.tools.base import ToolResult  # noqa: E402
from jarvis.tools.core import CONFIRM, REJECTED  # noqa: E402

HERE = Path(__file__).resolve().parent


def norm(value) -> str:
    text = unicodedata.normalize("NFD", str(value).lower())
    return "".join(c for c in text if unicodedata.category(c) != "Mn").replace("_", " ").strip()


def parse_expect(spec: str) -> list[dict]:
    alternatives = []
    for alt in spec.split(" | "):
        words = alt.strip().split()
        head, params = words[0], words[1:]
        kind, _, name = head.partition(":")
        constraints = []
        for p in params:
            m = re.match(r"(\w+)([=~])(.+)", p)
            constraints.append((m.group(1), m.group(2), m.group(3).split("|")))
        alternatives.append({"kind": kind, "name": name, "constraints": constraints})
    return alternatives


def param_ok(parameters: dict, constraints) -> bool:
    for key, op, values in constraints:
        if key not in parameters:
            return False
        given = parameters[key]
        if op == "=":
            try:
                if float(given) != float(values[0]):
                    return False
            except (TypeError, ValueError):
                if norm(given) != norm(values[0]):
                    return False
        elif not any(norm(v) in norm(given) for v in values):
            return False
    return True


def judge(turn: dict, alternatives: list[dict]) -> tuple[bool, str]:
    calls, reply, route = turn["calls"], turn["reply"], turn["route"]
    names = [c["tool"] for c in calls]
    for alt in alternatives:
        kind = alt["kind"]
        if kind in ("none", "none+question") and not calls:
            if kind == "none" or reply.rstrip().endswith("?"):
                return True, ""
        elif kind == "tool" and any(c["tool"] == alt["name"] and param_ok(c["parameters"], alt["constraints"])
                                    for c in calls):
            return True, ""
        elif kind == "tools" and sorted(set(names)) == sorted(alt["name"].split(",")):
            return True, ""
        elif kind == "confirm" and any(c["tool"] == alt["name"] and c["status"] == CONFIRM
                                       and param_ok(c["parameters"], alt["constraints"]) for c in calls):
            return True, ""
    first = alternatives[0]
    if any(c["status"] == REJECTED and c.get("invalid") for c in calls):
        return False, "exécution"
    if first["kind"].startswith("none"):
        if calls:
            return False, "compréhension"
        return False, "ambiguïté"
    if not calls:
        return False, "planification" if route.startswith("tool") else "compréhension"
    wanted = set(first["name"].split(",")) if first["kind"] == "tools" else {first["name"]}
    if first["kind"] == "tools" and wanted & set(names):
        return False, "planification"
    if wanted & set(names):
        return False, "paramètres"
    return False, "choix d'outil"


def build(model: str | None, think: str | None):
    original = harness.load_config

    def load(path):
        cfg = original(path)
        llm = cfg.llm
        if model:
            llm = replace(llm, model=model)
        if think is not None:
            llm = replace(llm, think=think)
        llm = replace(llm, trusted=True, guard="off")
        return replace(cfg, llm=llm, tools=replace(cfg.tools, policy={"engine": "builtin"}))

    harness.load_config = load
    events: list = []
    agent = harness.build(events)
    core = agent._tools
    log: list[dict] = []
    real_submit = core.submit

    def submit(data, user=None, origin="user"):
        outcome = real_submit(data, user, origin) if user is not None else real_submit(data, origin=origin)
        tool = data.get("tool") if isinstance(data, dict) else None
        request = outcome.request
        log.append({"at": len(events), "tool": request.tool if request else tool,
                    "parameters": dict(request.parameters) if request else dict((data or {}).get("parameters") or {}),
                    "status": outcome.status, "invalid": request is None})
        return outcome

    mails = {"count": 2, "unread": 2, "mails": [
        {"position": 1, "from": "Paul", "subject": "Réunion de jeudi", "unread": True},
        {"position": 2, "from": "Julie", "subject": "Dîner samedi", "unread": True}]}
    ids = {"create_timer": "timer_id", "create_reminder": "reminder_id", "create_alarm": "alarm_id", "add_event": "id"}

    def execute(tool, request, decision, confirmation, started, user):
        result = {"ok": True, "simulated": True, **dict(request.parameters)}
        if tool.name in ids:
            result[ids[tool.name]] = 1
        if tool.name in ("check_mail", "list_mail", "search_mail"):
            result.update(mails)
        return ToolResult(tool.name, True, result=result, message="C'est fait.")

    real_answer = core.answer

    def answer(text, user=None):
        outcome = real_answer(text) if user is None else real_answer(text, user)
        if outcome is not None and outcome.request is not None:
            log.append({"at": len(events), "tool": outcome.request.tool, "parameters": dict(outcome.request.parameters),
                        "status": outcome.status, "invalid": False})
        return outcome

    core.submit = submit
    core.answer = answer
    core._execute = execute
    return agent, events, log


def run(cases_path: Path, model: str | None, think: str | None) -> dict:
    cases = json.loads(cases_path.read_text(encoding="utf-8"))["cases"]
    agent, events, log = build(model, think)
    harness.CONTROL["stop"]()
    results = []
    for case in cases:
        start_event, start_log = len(events), len(log)
        started = time.perf_counter()
        harness.converse(agent, [t[0] for t in case["turns"]], events)
        seconds = time.perf_counter() - started
        case_events = events[start_event:]
        marks = [start_event + i for i, (k, _) in enumerate(case_events) if k == "user"]
        turns = []
        for index, (text, expect) in enumerate(case["turns"]):
            lo = marks[index] if index < len(marks) else len(events)
            hi = marks[index + 1] if index + 1 < len(marks) else len(events)
            window = events[lo:hi]
            turn = {"say": text, "expect": expect,
                    "route": next((t for k, t in window if k == "routing"), ""),
                    "reply": " ".join(t for k, t in window if k == "assistant"),
                    "calls": [c for c in log[start_log:] if lo <= c["at"] <= hi and c["status"] != "cancelled"]}
            turn["ok"], turn["category"] = judge(turn, parse_expect(expect))
            turns.append(turn)
        results.append({"id": case["id"], "tag": case["tag"], "seconds": round(seconds, 1), "turns": turns,
                        "ok": all(t["ok"] for t in turns)})
    agent.close()
    turns = [t for r in results for t in r["turns"]]
    by_tag = defaultdict(lambda: [0, 0])
    for r in results:
        by_tag[r["tag"]][0] += r["ok"]
        by_tag[r["tag"]][1] += 1
    summary = {"model": model or "config", "think": think, "cases_ok": sum(r["ok"] for r in results), "cases": len(results),
               "turns_ok": sum(t["ok"] for t in turns), "turns": len(turns),
               "failures": dict(Counter(t["category"] for t in turns if not t["ok"])),
               "by_tag": {k: f"{v[0]}/{v[1]}" for k, v in sorted(by_tag.items())}}
    return {"summary": summary, "results": results}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("cases")
    parser.add_argument("--model")
    parser.add_argument("--think", choices=["true", "false"])
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    path = Path(args.cases) if Path(args.cases).exists() else HERE / args.cases
    report = run(path, args.model, args.think)
    out = args.out or Path(f"comprehension_{path.stem}.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))
    for r in report["results"]:
        for t in r["turns"]:
            if not t["ok"]:
                calls = ", ".join(f"{c['tool']}{c['parameters']}[{c['status']}]" for c in t["calls"]) or "aucun appel"
                print(f"  {r['id']} [{t['category']}] « {t['say']} » -> {t['route']} : {calls} | {t['reply'][:90]}")


if __name__ == "__main__":
    main()
