"""Core des outils : validation -> permission -> confirmation -> exécution -> résultat.

C'est le seul chemin d'exécution d'un outil. Le LLM ne fait que proposer une demande structurée ;
le Core la valide, décide, demande confirmation si besoin, exécute avec un délai maximal et journalise.
"""

from __future__ import annotations

import json
import logging
import time
import threading
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from jarvis.events import TOOL_EXECUTED, TOOL_FAILED, TOOL_STARTED, Event, EventBus
from jarvis.tools.base import (
    CONFIRMATION_REFUSED, EXECUTION_FAILED, PERMISSION_DENIED, TIMEOUT, Tool, ToolError, ToolRequest, ToolResult,
)
from jarvis.tools.confirmation import NO, NONE, OTHER, YES, ConfirmationManager
from jarvis.tools.permissions import OWNER, Decision, PermissionManager
from jarvis.tools.registry import ToolRegistry
from jarvis.tools.request import parse_request

log = logging.getLogger("jarvis.tools")

DONE, CONFIRM, REJECTED, CANCELLED = "done", "confirm", "rejected", "cancelled"


@dataclass(frozen=True)
class Outcome:
    """Issue d'une demande : exécutée (``done``), en attente de confirmation, rejetée ou annulée."""

    status: str
    request: ToolRequest | None = None
    result: ToolResult | None = None
    question: str = ""


@dataclass(frozen=True)
class ToolActivity:
    """Trace d'une demande d'outil (journal et événements) : jamais le résultat brut de l'outil."""

    tool: str
    stage: str
    decision: str
    confirmation: str
    success: bool | None
    error: str | None
    message: str
    duration_ms: float
    parameters: dict | None
    user: str


def _loggable(value: Any) -> Any:
    """Valeur journalisable : chaînes tronquées, URL sans requête ni fragment (jetons éventuels)."""
    if isinstance(value, dict):
        return {k: _loggable(v) for k, v in value.items()}
    if isinstance(value, str):
        if value.lower().startswith(("http://", "https://")):
            parts = urlsplit(value)
            value = urlunsplit((parts.scheme, parts.hostname or "", parts.path, "", ""))
        return value[:120]
    return value


class ToolCore:
    def __init__(self, registry: ToolRegistry, permissions: PermissionManager | None = None,
                 confirmations: ConfirmationManager | None = None, timeout: float = 10.0, user: str = OWNER,
                 events: EventBus | None = None):
        self.registry = registry
        self.permissions = permissions or PermissionManager()
        self.confirmations = confirmations or ConfirmationManager(("oui",), ("non",))
        self._timeout = timeout
        self._user = user
        self._events = events

    @property
    def user(self) -> str:
        return self._user

    def set_user(self, user: str) -> None:
        """Utilisateur des prochaines demandes (profil du terminal qui parle) ; une confirmation en attente est
        abandonnée si l'utilisateur change."""
        if user != self._user:
            self.confirmations.clear()
        self._user = user

    def submit(self, data: Any, user: str | None = None) -> Outcome:
        """Demande venant du LLM ou du routeur : jamais exécutée sans validation ni permission. ``user`` : pour
        une demande qui n'est pas celle de l'utilisateur en cours (routine du propriétaire). L'identité suit la
        demande de bout en bout : elle n'est jamais changée temporairement dans un état partagé (plusieurs fils
        appellent le Core en même temps : conversation, routines, API)."""
        who = self._user if user is None else user
        started = time.perf_counter()
        name = data.get("tool") if isinstance(data, dict) and isinstance(data.get("tool"), str) else "?"
        try:
            request = parse_request(data, self.registry)
        except ToolError as exc:
            request, invalid = None, ToolResult(str(name)[:48], False, error=exc.code, message=exc.message)
        if request is None:
            self._log(invalid.tool, None, "validation", "invalid", "n/a", invalid, started, who)
            return Outcome(REJECTED, None, invalid)
        tool = self.registry.get(request.tool)
        decision = self.permissions.decide(who, tool, dict(request.parameters))
        if decision.decision is Decision.DENY:
            result = ToolResult(tool.name, False, error=PERMISSION_DENIED, message="Je n'ai pas l'autorisation de faire cela.")
            self._log(tool.name, request.parameters, "permission", decision.decision.value, "n/a", result, started, who)
            return Outcome(REJECTED, request, result)
        if decision.decision is Decision.REQUIRES_CONFIRMATION:
            question = self.confirmations.ask(request, tool.confirmation_question(dict(request.parameters)), who)
            self._log(tool.name, request.parameters, "confirmation", decision.decision.value, "asked", None, started,
                      who)
            return Outcome(CONFIRM, request, question=question)
        return Outcome(DONE, request, self._execute(tool, request, decision.decision.value, "n/a", started, who))

    def answer(self, text: str, user: str | None = None) -> Outcome | None:
        """Réponse de l'utilisateur à une confirmation en attente ; None s'il n'y en avait pas
        (ou si la phrase n'est ni un oui ni un non : la demande en attente est alors abandonnée). Seul celui qui a
        demandé peut confirmer ; la permission est réévaluée pour lui."""
        who = self._user if user is None else user
        verdict, pending = self.confirmations.answer(text)
        if verdict == NONE:
            return None
        started = time.perf_counter()
        request = pending.request
        if pending.user and pending.user != who:
            verdict = OTHER  # un autre utilisateur ne peut pas confirmer à la place du demandeur
        if verdict == OTHER:
            self._log(request.tool, request.parameters, "confirmation", "requires_confirmation", "abandoned", None,
                      started, who)
            return None
        tool = self.registry.get(request.tool)
        if verdict == NO:
            result = ToolResult(tool.name, False, error=CONFIRMATION_REFUSED, message="Action annulée.")
            self._log(tool.name, request.parameters, "confirmation", "requires_confirmation", "refused", result, started,
                      who)
            return Outcome(CANCELLED, request, result)
        decision = self.permissions.decide(who, tool, dict(request.parameters), confirmed=(verdict == YES))
        if decision.decision is not Decision.ALLOW:
            result = ToolResult(tool.name, False, error=PERMISSION_DENIED, message="Je n'ai pas l'autorisation de faire cela.")
            self._log(tool.name, request.parameters, "permission", decision.decision.value, "accepted", result, started,
                      who)
            return Outcome(REJECTED, request, result)
        return Outcome(DONE, request, self._execute(tool, request, decision.decision.value, "accepted", started, who))

    def cancel_pending(self) -> None:
        self.confirmations.clear()

    def _execute(self, tool: Tool, request: ToolRequest, decision: str, confirmation: str, started: float,
                 user: str) -> ToolResult:
        self._publish(TOOL_STARTED, self._activity(tool.name, request.parameters, "execution", decision, confirmation,
                                                   None, started, user))
        # Un fil par exécution : un outil bloqué (appareil muet) n'empêche jamais les autres de s'exécuter.
        future: Future = Future()

        def run() -> None:
            try:
                future.set_result(tool.execute(dict(request.parameters)))
            except BaseException as exc:  # transmis tel quel au Core
                future.set_exception(exc)

        threading.Thread(target=run, name=f"outil-{tool.name}", daemon=True).start()
        try:
            output = future.result(timeout=self._timeout)
            result = ToolResult(tool.name, True, result=output, message=tool.say(output) if tool.say else "")
        except FutureTimeout:
            result = ToolResult(tool.name, False, error=TIMEOUT, message="L'action a pris trop de temps.")
        except ToolError as exc:
            result = ToolResult(tool.name, False, error=exc.code, message=exc.message)
        except Exception:
            log.exception("Outil %s : erreur inattendue", tool.name)
            result = ToolResult(tool.name, False, error=EXECUTION_FAILED, message="L'action a échoué.")
        self._log(tool.name, request.parameters, "execution", decision, confirmation, result, started, user)
        return result

    def _activity(self, tool: str, parameters, stage: str, decision: str, confirmation: str,
                  result: ToolResult | None, started: float, user: str) -> ToolActivity:
        return ToolActivity(
            tool=tool, stage=stage, decision=decision, confirmation=confirmation,
            success=None if result is None else result.success,
            error=None if result is None else result.error,
            message="" if result is None or result.success else result.message,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            parameters=_loggable(dict(parameters)) if parameters is not None else None,
            user=user,
        )

    def _log(self, tool: str, parameters, stage: str, decision: str, confirmation: str,
             result: ToolResult | None, started: float, user: str) -> None:
        """Journalise chaque issue ; publie tool.executed / tool.failed dès qu'il y a un résultat."""
        activity = self._activity(tool, parameters, stage, decision, confirmation, result, started, user)
        record = asdict(activity)
        record.pop("message")
        log.info("outil %s", json.dumps(record, ensure_ascii=False))
        if result is not None:
            self._publish(TOOL_EXECUTED if result.success else TOOL_FAILED, activity)

    def _publish(self, event_type: str, activity: ToolActivity) -> None:
        if self._events is not None:
            self._events.publish(Event(event_type, "tools", asdict(activity)))
