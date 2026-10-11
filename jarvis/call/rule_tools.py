"""Outils des règles d'appel : créer, lister, consulter, modifier, activer, désactiver, supprimer, expliquer.

Créer et modifier passent par le Core : la phrase est traduite (jarvis/call/rule_parser.py) pendant la validation
de la demande, puis la règle décrite par le Core est soumise à votre confirmation avant d'être enregistrée. Une
précision manquante devient une question (code NEEDS_DETAILS) : votre réponse suivante la complète.
"""

from __future__ import annotations

import json
import re
from typing import Callable

from jarvis.call.rule_parser import RuleParser, RuleQuestion
from jarvis.call.rules import CallRules, RuleError, describe_rule, validate_rule
from jarvis.personality import normalize
from jarvis.tools.base import INVALID_PARAMETERS, Param, Risk, Tool, ToolError

NEEDS_DETAILS = "needs_details"
RULE_REFUSED = "call_rule_refused"


def _enabled(text: str) -> str | None:
    norm = f" {normalize(text)} "
    if re.search(r"\b(?:desactive|desactiver|suspends|suspendre|mets en pause|coupe|arrete|stoppe)\b", norm):
        return "non"
    if re.search(r"\b(?:active|activer|reactive|reactiver|remets|reprends|relance)\b", norm):
        return "oui"
    return None


def call_rule_tools(rules: CallRules, parser: RuleParser, context: Callable[[], dict]) -> list[Tool]:
    def converted(text: str, previous: dict | None = None) -> str:
        """Phrase -> règle validée (JSON), pendant la validation de la demande : la confirmation la décrit."""
        if text.lstrip().startswith("{"):  # déjà traduite (confirmation) : seulement revalidée
            try:
                data = json.loads(text)
                return json.dumps({"rule": validate_rule(data["rule"], None), "label": data.get("label"),
                                   "text": str(data.get("text", ""))[:300]}, ensure_ascii=False)
            except (ValueError, KeyError, TypeError, RuleError) as exc:
                raise ToolError(INVALID_PARAMETERS, "Règle illisible.") from exc
        try:
            rule, label = parser.parse(text, context(), previous)
        except RuleQuestion as exc:
            raise ToolError(NEEDS_DETAILS, str(exc)) from exc
        except RuleError as exc:
            raise ToolError(RULE_REFUSED, str(exc)) from exc
        return json.dumps({"rule": rule, "label": label, "text": text[:300]}, ensure_ascii=False)

    def described(value: str) -> str:
        data = json.loads(value)
        labels = dict(rules.labels)
        if data.get("label"):
            labels[data["label"][0]] = data["label"][1]
        return describe_rule(data["rule"], labels)

    def create(request: str) -> dict:
        data = json.loads(request)
        try:
            entry = rules.add(data["rule"], data["text"], tuple(data["label"]) if data.get("label") else None)
        except RuleError as exc:
            raise ToolError(RULE_REFUSED, str(exc)) from exc
        return {"rule": rules.describe(entry), "position": rules.rules.index(entry) + 1}

    def modify(request: str, position: int | None = None) -> dict:
        data = json.loads(request)
        try:
            entry = rules.replace(position or 1, data["rule"], data["text"])
        except RuleError as exc:
            raise ToolError(RULE_REFUSED, str(exc)) from exc
        return {"rule": rules.describe(entry), "position": position or 1}

    def listing() -> dict:
        return {"rules": [{"position": i + 1, "rule": rules.describe(r), "enabled": r["enabled"] and not r["done"]}
                          for i, r in enumerate(rules.rules)]}

    def listed(r: dict) -> str:
        if not r["rules"]:
            return "Vous n'avez aucune règle d'appel, monsieur."
        parts = [f"{x['position']}, {x['rule']}{'' if x['enabled'] else ' (désactivée)'}" for x in r["rules"][:8]]
        return f"Vous avez {len(r['rules'])} règle{'s' if len(r['rules']) > 1 else ''} d'appel : " + " ; ".join(parts) + "."

    def pick(position: int | None) -> int:
        if position is None:
            if len(rules.rules) != 1:
                raise ToolError(INVALID_PARAMETERS, "Quelle règle, monsieur ? Donnez-moi son numéro."
                                if rules.rules else "Vous n'avez aucune règle d'appel.")
            return 1
        return position

    def guarded(action):
        try:
            return action()
        except RuleError as exc:
            raise ToolError(INVALID_PARAMETERS, str(exc)) from exc

    def consult(position: int | None = None) -> dict:
        entry = guarded(lambda: rules.at(pick(position)))
        last = entry["log"][-1][1] if entry["log"] else "elle ne s'est encore jamais déclenchée"
        return {"rule": rules.describe(entry), "enabled": entry["enabled"] and not entry["done"], "last": last,
                "said": entry["text"]}

    def toggle(enabled: str, position: int | None = None) -> dict:
        entry = guarded(lambda: rules.set_enabled(pick(position), enabled == "oui"))
        return {"rule": rules.describe(entry), "enabled": entry["enabled"]}

    def delete(position: int | None = None) -> dict:
        entry = guarded(lambda: rules.remove(pick(position)))
        return {"rule": describe_rule(entry["rule"], rules.labels)}

    def explain(position: int | None = None) -> dict:
        return {"decisions": guarded(lambda: rules.explain(position))}

    def explained(r: dict) -> str:
        if not r["decisions"]:
            return "Aucune de vos règles ne s'est encore déclenchée, monsieur."
        return "Voici mes dernières décisions : " + " ; ".join(r["decisions"]) + "."

    position = Param(int, "numéro de la règle, s'il a été dit", required=False, minimum=1, maximum=30)
    request = Param(str, "demande", max_length=4000, hidden=True, resolve=lambda text: text, check=converted)

    def modify_check(value: str) -> str:
        return converted(value, rules.rules[0]["rule"] if len(rules.rules) == 1 else None)

    return [
        Tool("create_call_rule", "Crée une règle d'appel dite par l'utilisateur (« appelle-moi quand… », « appelle-moi "
             "si… »), après confirmation.", {"request": request}, {"rule": "règle créée"},
             Risk.CONFIRMATION_REQUIRED, create,
             question=lambda p: f"Voulez-vous que je crée cette règle : {described(p['request'])} ?",
             say=lambda r: f"C'est noté, monsieur. Règle numéro {r['position']} : {r['rule']}."),
        Tool("list_call_rules", "Liste les règles d'appel.", {}, {"rules": "règles"}, Risk.SAFE, listing, say=listed),
        Tool("describe_call_rule", "Détaille une règle d'appel et sa dernière décision.", {"position": position},
             {"rule": "règle", "last": "dernière décision"}, Risk.SAFE, consult,
             say=lambda r: f"Cette règle prévoit de {r['rule']}. {'Elle est active' if r['enabled'] else 'Elle est désactivée'} ; "
                           f"dernière décision : {r['last']}."),
        Tool("set_call_rule_enabled", "Active ou désactive une règle d'appel.",
             {"position": position, "enabled": Param(str, "activer ou désactiver", choices=("oui", "non"),
                                                     hidden=True, resolve=_enabled)},
             {"rule": "règle", "enabled": "active ?"}, Risk.SAFE, toggle,
             say=lambda r: f"La règle est {'activée' if r['enabled'] else 'désactivée'} : {r['rule']}."),
        Tool("delete_call_rule", "Supprime une règle d'appel.", {"position": position}, {"rule": "règle supprimée"},
             Risk.CONFIRMATION_REQUIRED, delete,
             question=lambda p: "Voulez-vous vraiment supprimer cette règle : "
                                f"{rules.describe(rules.at(pick(p.get('position'))))} ?"
             if rules.rules else "Voulez-vous supprimer cette règle ?",
             say=lambda r: f"Règle supprimée : {r['rule']}."),
        Tool("modify_call_rule", "Modifie une règle d'appel existante (nouvelle formulation), après confirmation.",
             {"position": position, "request": Param(str, "demande", max_length=4000, hidden=True,
                                                     resolve=lambda text: text, check=modify_check)},
             {"rule": "règle modifiée"}, Risk.CONFIRMATION_REQUIRED, modify,
             question=lambda p: f"Voulez-vous que la règle devienne : {described(p['request'])} ?",
             say=lambda r: f"C'est modifié : {r['rule']}."),
        Tool("explain_calls", "Explique pourquoi ORION a appelé, ou n'a pas appelé (dernières décisions des règles).",
             {"position": position}, {"decisions": "décisions"}, Risk.SAFE, explain, say=explained),
    ]
