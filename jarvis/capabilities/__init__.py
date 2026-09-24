"""Point d'extension des capacités (outils) de JARVIS.

Une capacité est une action que JARVIS saura exécuter (contrôle PC, domotique,
messagerie...). Chaque capacité vivra dans son propre module et sera enregistrée
dans le registre ; le prompt système décrit au LLM ce qui est disponible.

En V1, aucune capacité n'est enregistrée : JARVIS ne fait que converser.
"""

from __future__ import annotations

from typing import Protocol


class Capability(Protocol):
    name: str
    description: str  # présentée au LLM


class CapabilityRegistry:
    def __init__(self) -> None:
        self._capabilities: dict[str, Capability] = {}

    def register(self, capability: Capability) -> None:
        if capability.name in self._capabilities:
            raise ValueError(f"Capacité déjà enregistrée : {capability.name}")
        self._capabilities[capability.name] = capability

    def __iter__(self):
        return iter(self._capabilities.values())

    def __len__(self) -> int:
        return len(self._capabilities)
