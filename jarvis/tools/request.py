"""Validation stricte d'une demande d'outil venant du LLM (donnée non fiable).

Seuls les champs ``type``, ``tool`` et ``parameters`` sont acceptés : aucun champ supplémentaire
(« confirmed », « risk », « user »...) ne peut influencer les contrôles.
"""

from __future__ import annotations

import json
import re
from typing import Any

from jarvis.tools.base import INVALID_PARAMETERS, ToolError, ToolRequest
from jarvis.tools.registry import ToolRegistry

ALLOWED_KEYS = {"type", "tool", "parameters"}
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _invalid(message: str) -> ToolError:
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
            raise _invalid("Demande d'outil illisible.") from exc
    if not isinstance(data, dict):
        raise _invalid("Demande d'outil mal formée.")
    extra = set(data) - ALLOWED_KEYS
    if extra:
        raise _invalid("Champs non autorisés dans la demande d'outil.")
    if data.get("type", "tool_call") != "tool_call":
        raise _invalid("Ce n'est pas une demande d'outil.")
    tool = registry.get(data.get("tool"))
    params = data.get("parameters")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise _invalid("Les paramètres doivent être un objet.")
    unknown = set(params) - set(tool.parameters)
    if unknown:
        raise _invalid("Paramètres inconnus pour cet outil.")
    clean: dict[str, Any] = {}
    for name, spec in tool.parameters.items():
        if name not in params:
            if spec.required:
                raise _invalid(f"Le paramètre « {name} » est obligatoire.")
            continue
        value = _typed(name, params[name], spec.kind)
        if isinstance(value, str):
            value = value.strip()
            if not value or len(value) > spec.max_length or CONTROL.search(value):
                raise _invalid(f"Valeur invalide pour « {name} ».")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if (spec.minimum is not None and value < spec.minimum) or (spec.maximum is not None and value > spec.maximum):
                raise _invalid(f"Valeur hors limites pour « {name} ».")
        if spec.choices and value not in spec.choices:
            raise _invalid(f"Valeur non autorisée pour « {name} ».")
        if spec.check is not None:
            value = spec.check(value)
        clean[name] = value
    return ToolRequest(tool.name, clean)
