"""Point d'extension des capacités (outils) de JARVIS.

Une capacité est une action que JARVIS sait réellement exécuter (domotique,
musique, météo...). Chaque capacité vivra dans son propre module et sera
enregistrée dans le registre. Le routeur d'intentions lui propose chaque
demande ; le prompt système décrit au LLM ce qui est disponible.

En V1, aucune capacité n'est enregistrée : JARVIS ne fait que converser.
"""

from __future__ import annotations

from typing import Protocol

# Fonctionnalités annoncées mais pas encore développées : (nom court prononçable, détail pour le LLM).
PLANNED_FEATURES = (
    ("le contrôle de votre ordinateur", "lancer des programmes, gérer des fichiers, exécuter des commandes"),
    ("la gestion de vos fichiers", "créer, déplacer ou supprimer des fichiers, exécuter des commandes"),
    ("la domotique", "lumières, chauffage, appareils connectés"),
    ("les e-mails et messages", "lire ou envoyer des e-mails et des messages WhatsApp"),
    ("l'agenda", "rendez-vous et événements du calendrier"),
    ("la recherche sur Internet", "informations en temps réel : météo, actualités, cours de bourse"),
)


class Capability(Protocol):
    name: str
    description: str  # présentée au LLM et à l'utilisateur

    def handle(self, text: str) -> str | None:
        """Exécute la demande si elle relève de cette capacité et retourne la réponse, sinon None."""


class CapabilityRegistry:
    def __init__(self) -> None:
        self._capabilities: dict[str, Capability] = {}

    def register(self, capability: Capability) -> None:
        if capability.name in self._capabilities:
            raise ValueError(f"Capacité déjà enregistrée : {capability.name}")
        self._capabilities[capability.name] = capability

    def handle(self, text: str) -> tuple[str, str] | None:
        for capability in self:
            reply = capability.handle(text)
            if reply is not None:
                return capability.name, reply
        return None

    def planned(self) -> tuple[tuple[str, str], ...]:
        """Fonctionnalités prévues, moins celles qu'une capacité enregistrée rend disponibles."""
        done = set()
        for capability in self:
            replaces = getattr(capability, "replaces", None) or ()
            done |= {replaces} if isinstance(replaces, str) else set(replaces)
        if "le contrôle de votre ordinateur" not in done:
            done.add("la gestion de vos fichiers")
        return tuple(f for f in PLANNED_FEATURES if f[0] not in done)

    def __iter__(self):
        return iter(self._capabilities.values())

    def __len__(self) -> int:
        return len(self._capabilities)
