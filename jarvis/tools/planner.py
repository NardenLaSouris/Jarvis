"""Le LLM propose un outil ; le Core dispose.

- ``plan`` : demande au LLM, sortie JSON contrainte par schéma, soit un appel d'outil structuré
  (``{"type": "tool_call", "tool": ..., "parameters": {...}}``), soit ``{"type": "none"}`` ;
- ``tool_request`` : message transmis au LLM après exécution, avec le résultat structuré.
La demande proposée est ensuite entièrement revalidée par le Core : rien n'est exécuté sur la foi du LLM.
"""

from __future__ import annotations

import json
import logging
import re

from jarvis.interfaces import Message
from jarvis.tools.base import ToolResult
from jarvis.tools.registry import ToolRegistry

log = logging.getLogger(__name__)

RESULT_START = "<<<RESULTAT_OUTIL>>>"
RESULT_END = "<<<FIN_RESULTAT_OUTIL>>>"
TYPES = {str: "texte", int: "entier", float: "nombre", bool: "booléen"}
EXAMPLES = (
    ("Tu peux ouvrir Discord ?", "open_application", {"application": "discord"}),
    ("Ferme Chrome", "close_application", {"application": "chrome"}),
    ("Quitte Steam", "close_application", {"application": "steam"}),
    ("Ouvre YouTube", "open_url", {"url": "https://www.youtube.com"}),
    ("Monte-moi le son à 60", "set_volume", {"volume": 60}),
    ("Mets le volume à 25 %", "set_volume", {"volume": 25}),
    ("Coupe le son", "mute_volume", {}),
    ("Remets le son", "unmute_volume", {}),
    ("Verrouille le PC", "lock_pc", {}),
    ("Quelles applications sont ouvertes ?", "list_running_applications", {}),
    ("Quelle machine utilises-tu ?", "system_info", {}),
    ("Combien d'espace disque me reste-t-il ?", "system_info", {}),
    ("Réactive le son", "unmute_volume", {}),
    ("Est-ce que Discord est ouvert ?", "list_running_applications", {}),
    ("Supprime le dossier Documents", None, None),
    ("Ouvre un terminal et tape une commande", None, None),
    ("Éteins l'ordinateur", None, None),
)


def planner_prompt(registry: ToolRegistry) -> str:
    lines = []
    for tool in registry.list():
        params = "; ".join(f"{name} ({TYPES.get(p.kind, 'texte')}) : {p.description}" for name, p in tool.parameters.items())
        lines.append(f"- {tool.name} : {tool.description} Paramètres : {params or 'aucun'}.")
    tools = "\n".join(lines)
    rules = ["- N'inventez jamais d'outil ni de paramètre."]
    if registry.exists("open_application"):
        rules.append("- open_application : recopiez le nom de l'application tel que l'utilisateur l'a dit, en "
                     "minuscules, même s'il ne figure pas dans la liste.")
    if registry.exists("close_application"):
        rules.append("- close_application : même règle pour le nom de l'application à fermer.")
    if registry.exists("set_volume"):
        rules.append("- set_volume : uniquement si un niveau précis est donné (nombre entier de 0 à 100).")
    if registry.exists("unmute_volume"):
        rules.append("- unmute_volume : remettre, réactiver ou rallumer le son (après une coupure).")
    if registry.exists("system_info"):
        rules.append("- system_info : toute question sur la machine elle-même (processeur, mémoire, espace disque, "
                     "carte graphique, système, depuis quand elle est allumée).")
    if registry.exists("list_running_applications"):
        rules.append("- list_running_applications : savoir quelles applications sont ouvertes ou si une application "
                     "tourne.")
    if registry.exists("open_url"):
        rules.append("- open_url : donnez une adresse complète commençant par https:// ; pour un site connu, "
                     "son adresse officielle.")
    rules = "\n".join(rules)
    samples = [(text, tool, params) for text, tool, params in EXAMPLES if tool is None or registry.exists(tool)]
    examples = "\n".join(
        f"« {text} » -> " + json.dumps({"type": "none"} if tool is None else
                                      {"type": "tool_call", "tool": tool, "parameters": params}, ensure_ascii=False)
        for text, tool, params in samples)
    return f"""Vous êtes le module de décision d'action de l'assistant vocal JARVIS. Vous ne répondez pas à
l'utilisateur : vous indiquez seulement si sa demande correspond à l'un de ces outils.

Outils disponibles :
{tools}

Répondez uniquement en JSON :
- {{"type": "tool_call", "tool": "<nom>", "parameters": {{...}}}} si un outil correspond exactement à la demande ;
- {{"type": "none"}} sinon : conversation, question de culture générale, ou action pour laquelle aucun outil
  n'existe (supprimer un fichier, éteindre l'ordinateur, taper une commande, domotique, messages...).

Règles :
{rules}

Exemples :
{examples}"""


JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}


def plan_schema(registry: ToolRegistry) -> dict:
    """Schéma de sortie : « none », ou un appel d'outil avec les paramètres exacts (noms et types) de cet outil."""
    options = [{"type": "object", "properties": {"type": {"const": "none"}}, "required": ["type"],
                "additionalProperties": False}]
    for tool in registry.list():
        parameters = {
            "type": "object",
            "properties": {name: {"type": JSON_TYPES.get(p.kind, "string")} for name, p in tool.parameters.items()},
            "required": [name for name, p in tool.parameters.items() if p.required],
            "additionalProperties": False,
        }
        options.append({"type": "object",
                        "properties": {"type": {"const": "tool_call"}, "tool": {"const": tool.name},
                                       "parameters": parameters},
                        "required": ["type", "tool", "parameters"], "additionalProperties": False})
    return {"anyOf": options}


def plan(llm, text: str, registry: ToolRegistry) -> dict | None:
    """Appel d'outil proposé par le LLM pour cette demande, ou None (aucun outil, ou LLM indisponible)."""
    if not len(registry) or not hasattr(llm, "chat_json"):
        return None
    messages = [Message("system", planner_prompt(registry)), Message("user", text)]
    try:
        data = llm.chat_json(messages, plan_schema(registry))
    except Exception as exc:
        log.warning("Choix d'outil impossible : %s", exc)
        return None
    if not isinstance(data, dict) or data.get("type") != "tool_call":
        return None
    if not _numbers_were_said(data, text, registry):
        log.info("Appel d'outil écarté : nombre absent de la demande (%s)", data)
        return None
    return data


def _numbers_were_said(data: dict, text: str, registry: ToolRegistry) -> bool:
    """Un paramètre numérique doit figurer dans la demande : le LLM n'invente pas de volume ni de valeur."""
    if not registry.exists(data.get("tool")) or not isinstance(data.get("parameters"), dict):
        return True
    said = set(re.findall(r"\d+(?:[.,]\d+)?", text))
    said |= {n.replace(",", ".") for n in said}
    for name, spec in registry.get(data["tool"]).parameters.items():
        value = data["parameters"].get(name)
        if spec.kind in (int, float) and isinstance(value, (int, float)) and not isinstance(value, bool):
            if f"{value:g}" not in said:
                return False
    return True


def tool_request(user_text: str, result: ToolResult) -> str:
    """Message utilisateur envoyé au LLM après exécution : le résultat structuré, puis la demande."""
    payload = json.dumps(result.as_dict(), ensure_ascii=False)
    return f"{RESULT_START}\n{payload}\n{RESULT_END}\n\n{user_text}"


class ToolsCapability:
    """Déclare les outils au registre des capacités (prompt système, « que sais-tu faire ? »).

    Le routage vers les outils passe par les intentions à outil, pas par ``handle``.
    """

    name = "outils"
    replaces = None

    def __init__(self, registry: ToolRegistry):
        known = {
            "get_time": "donner l'heure", "get_date": "donner la date", "system_info": "décrire la machine",
            "open_url": "ouvrir une page Web", "open_application": "ouvrir certaines applications",
            "close_application": "fermer certaines applications",
            "list_running_applications": "dire quelles applications sont ouvertes", "set_volume": "régler le volume",
            "mute_volume": "couper le son", "unmute_volume": "remettre le son", "lock_pc": "verrouiller l'ordinateur",
        }
        parts = [known.get(t.name, t.name) for t in registry.list()]
        actions = ", ".join(parts[:-1]) + " et " + parts[-1] if len(parts) > 1 else "".join(parts)
        self.description = f"{actions} ; certaines actions (fermer une application, verrouiller) sont confirmées avant"

    def handle(self, text: str) -> None:
        return None
