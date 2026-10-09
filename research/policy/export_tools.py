"""Relève le registre d'outils réel d'ORION et les décisions du PermissionManager (rôles, confirmation).

Sert à rafraîchir tests/fixtures/policy_tools.json (garder la clé « tools ») quand des outils changent. Lancé avec
l'environnement d'ORION dans une copie de travail (harness.py à côté) :
    TZ=Europe/Paris python research/policy/export_tools.py > /tmp/registre.json
"""

from __future__ import annotations

import itertools
import json
import sys

sys.path.insert(0, ".")
import harness  # noqa: E402
from jarvis.profiles import category, load_profiles  # noqa: E402
from jarvis.tools.base import Risk  # noqa: E402
from jarvis.tools.permissions import PermissionManager  # noqa: E402

RISK = {Risk.SAFE: "safe", Risk.CONFIRMATION_REQUIRED: "confirm", Risk.RESTRICTED: "restricted"}
USERS = {"monsieur": "owner", "lea": "adult", "tom": "child", "invite": "guest"}

agent = harness.build([])
harness.CONTROL["stop"]()
registry = agent._tools.registry
profiles = load_profiles({u: {"role": r} for u, r in USERS.items()}, {})
manager = PermissionManager(profiles=profiles)
tools = [{"name": t.name, "risk": RISK[t.risk], "category": category(t.name)} for t in registry.list()]
cases = []
for tool, user, confirmed in itertools.product(registry.list(), USERS, (False, True)):
    for parameters in ({}, {"topic": "tout"}) if tool.name == "forget" else ({},):
        decision = manager.decide(user, tool, parameters, confirmed=confirmed)
        cases.append({"tool": tool.name, "user": user, "confirmed": confirmed, "parameters": parameters,
                      "expected": decision.decision.value})
print(json.dumps({"users": USERS, "tools": tools, "cases": cases}, ensure_ascii=False, indent=1))
