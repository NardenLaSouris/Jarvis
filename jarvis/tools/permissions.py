"""Décision d'exécution d'un outil, prise par le Core et jamais par le LLM."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from jarvis.tools.base import Risk, Tool

OWNER = "owner"
# Origines d'une demande, fixées par le Core : la conversation (« user »), une routine, l'API d'administration.
# Toute autre origine (contenu d'un mail, d'une page, d'un fichier) n'est jamais exécutée.
TRUSTED_ORIGINS = frozenset({"user", "routine", "admin"})


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRES_CONFIRMATION = "requires_confirmation"


@dataclass(frozen=True)
class PermissionDecision:
    decision: Decision
    reason: str


class PermissionManager:
    """SAFE -> ALLOW ; CONFIRMATION_REQUIRED -> confirmation (ALLOW une fois confirmé) ; RESTRICTED -> DENY.

    ``confirmed`` n'est fourni que par le Core, après une réponse de l'utilisateur recueillie par le
    gestionnaire de confirmation ; il ne vient jamais de la demande du LLM.
    """

    def __init__(self, users: tuple[str, ...] = (OWNER,), profiles=None):
        self._users = set(users)
        self._profiles = profiles  # jarvis.profiles.Profiles : droits selon le rôle (propriétaire, enfant, invité...)

    def decide(self, user: str, tool: Tool, parameters: dict, confirmed: bool = False,
               origin: str = "user") -> PermissionDecision:
        if origin not in TRUSTED_ORIGINS:
            return PermissionDecision(Decision.DENY, "demande issue d'un contenu non fiable")
        if self._profiles is not None:
            allowed, reason = self._profiles.allows(user, tool, parameters)
            if not allowed:
                return PermissionDecision(Decision.DENY, reason)
        elif user not in self._users:
            return PermissionDecision(Decision.DENY, "utilisateur inconnu")
        if tool.risk is Risk.RESTRICTED:
            return PermissionDecision(Decision.DENY, "outil à risque restreint")
        if tool.risk is Risk.CONFIRMATION_REQUIRED:
            if confirmed:
                return PermissionDecision(Decision.ALLOW, "confirmé par l'utilisateur")
            return PermissionDecision(Decision.REQUIRES_CONFIRMATION, "action à confirmer")
        if tool.risk is Risk.SAFE:
            return PermissionDecision(Decision.ALLOW, "outil sans risque")
        return PermissionDecision(Decision.DENY, "niveau de risque inconnu")
