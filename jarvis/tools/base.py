"""Types du système d'outils : outil, demande, résultat, niveaux de risque et erreurs structurées."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping


class Risk(str, Enum):
    SAFE = "safe"
    CONFIRMATION_REQUIRED = "confirmation_required"
    RESTRICTED = "restricted"


TOOL_NOT_FOUND = "tool_not_found"
INVALID_PARAMETERS = "invalid_parameters"
PERMISSION_DENIED = "permission_denied"
CONFIRMATION_REQUIRED = "confirmation_required"
CONFIRMATION_REFUSED = "confirmation_refused"
EXECUTION_FAILED = "execution_failed"
TIMEOUT = "timeout"
APPLICATION_NOT_FOUND = "application_not_found"
APPLICATION_NOT_RUNNING = "application_not_running"
INVALID_URL = "invalid_url"


class ToolError(Exception):
    """Échec prévu d'un outil : un code stable (pour le LLM et les logs) et un message court en français."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code
        self.message = message or code


@dataclass(frozen=True)
class Param:
    """Paramètre d'entrée : type attendu, obligatoire ou non, et contrôle facultatif de la valeur.

    ``check`` reçoit la valeur (déjà typée) et rend la valeur normalisée, ou lève ToolError.
    """

    kind: type
    description: str
    required: bool = True
    max_length: int = 200
    check: Callable[[Any], Any] | None = None
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: Mapping[str, Param]
    returns: Mapping[str, str]
    risk: Risk
    run: Callable[..., dict]
    question: Callable[[dict], str] | None = None

    @property
    def requires_confirmation(self) -> bool:
        return self.risk is Risk.CONFIRMATION_REQUIRED

    def execute(self, parameters: dict) -> dict:
        return self.run(**parameters)

    def confirmation_question(self, parameters: dict) -> str:
        if self.question is not None:
            return self.question(parameters)
        return f"Voulez-vous que j'exécute « {self.name} » ?"


@dataclass(frozen=True)
class ToolRequest:
    tool: str
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"type": "tool_call", "tool": self.tool, "parameters": dict(self.parameters)}


@dataclass(frozen=True)
class ToolResult:
    tool: str
    success: bool
    result: Any = None
    error: str | None = None
    message: str = ""

    def as_dict(self) -> dict:
        data = {"type": "tool_result", "tool": self.tool, "success": self.success}
        if self.success:
            data["result"] = self.result
        else:
            data["error"] = self.error
            if self.message:
                data["message"] = self.message
        return data
