"""Validation stricte d'une demande d'outil venant du LLM (donnée non fiable).

Seuls les champs ``type``, ``tool`` et ``parameters`` sont acceptés : aucun champ supplémentaire
(« confirmed », « risk », « user »...) ne peut influencer les contrôles.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from jarvis.tools.base import INVALID_PARAMETERS, ToolError, ToolRequest
from jarvis.tools.registry import ToolRegistry

log = logging.getLogger(__name__)

ALLOWED_KEYS = {"type", "tool", "parameters"}
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


# Demandes structurellement fausses (champ intrus, forme inattendue, venues du LLM) : même code d'erreur que les
# autres, mais jamais une phrase à prononcer telle quelle.
MALFORMED_MESSAGES = frozenset({
    "Demande d'outil illisible.",
    "Demande d'outil mal formée.",
    "Champs non autorisés dans la demande d'outil.",
    "Ce n'est pas une demande d'outil.",
    "Les paramètres doivent être un objet.",
    "Paramètres inconnus pour cet outil.",
})


def _invalid(message: str) -> ToolError:
    return ToolError(INVALID_PARAMETERS, message)


def _malformed(message: str) -> ToolError:
    return ToolError(INVALID_PARAMETERS, message)


def _typed(name: str, value: Any, kind: type) -> Any:
    if kind is bool:
        ok = isinstance(value, bool)
    elif kind is int:
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif kind is float:
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    else:
        ok = isinstance(value, kind)
    if not ok:
        raise _invalid(f"Le paramètre « {name} » n'a pas le bon type.")
    return value


def parse_request(data: Any, registry: ToolRegistry) -> ToolRequest:
    """Demande brute (dict ou JSON) -> ToolRequest validée. Lève ToolError sinon."""
    if isinstance(data, (str, bytes)):
        try:
            data = json.loads(data)
        except ValueError as exc:
            raise _malformed("Demande d'outil illisible.") from exc
    if not isinstance(data, dict):
        raise _malformed("Demande d'outil mal formée.")
    extra = set(data) - ALLOWED_KEYS
    if extra:
        raise _malformed("Champs non autorisés dans la demande d'outil.")
    if data.get("type", "tool_call") != "tool_call":
        raise _malformed("Ce n'est pas une demande d'outil.")
    tool = registry.get(data.get("tool"))
    params = data.get("parameters")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise _malformed("Les paramètres doivent être un objet.")
    unknown = set(params) - set(tool.parameters)
    if unknown:
        raise _malformed("Paramètres inconnus pour cet outil.")
    clean: dict[str, Any] = {}
    for name, spec in tool.parameters.items():
        # Messages dits à l'utilisateur (le nom technique du paramètre va seulement au journal).
        if name not in params:
            if spec.required:
                log.info("Outil %s : paramètre « %s » manquant", tool.name, name)
                raise _invalid("Il me manque une précision pour faire cela, monsieur.")
            continue
        value = _typed(name, params[name], spec.kind)
        if isinstance(value, str):
            value = value.strip()
            if len(value) > spec.max_length:
                log.info("Outil %s : « %s » trop long (%d caractères)", tool.name, name, len(value))
                raise _invalid("C'est un peu long pour moi, monsieur : pouvez-vous le dire plus brièvement ?")
            if not value or CONTROL.search(value):
                log.info("Outil %s : « %s » vide ou illisible", tool.name, name)
                raise _invalid("Je n'ai pas bien compris cette demande, monsieur.")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if (spec.minimum is not None and value < spec.minimum) or (spec.maximum is not None and value > spec.maximum):
                log.info("Outil %s : « %s » hors limites", tool.name, name)
                if spec.minimum is not None and spec.maximum is not None:
                    low, high = (f"{v:g}" for v in (spec.minimum, spec.maximum))
                    raise _invalid(f"Je ne peux pas, monsieur : {name} de {low} à {high} seulement.")
                raise _invalid("Cette valeur est en dehors de ce que je peux faire, monsieur.")
        if spec.choices and value not in spec.choices:
            log.info("Outil %s : « %s » non autorisé", tool.name, name)
            raise _invalid("Je ne peux pas utiliser cette valeur, monsieur.")
        if spec.check is not None:
            value = spec.check(value)
        clean[name] = value
    return ToolRequest(tool.name, clean)
