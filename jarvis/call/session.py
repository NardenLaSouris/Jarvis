"""Session d'un appel vocal sortant d'ORION vers l'appareil mobile jumelé : états et transitions permises.

    demandé ──> sonnerie ──> accepté ──> connecté ──> terminé
                   │            │            └──────> échec (réseau)
                   ├─> refusé   └─> échec (connexion audio non établie)
                   └─> expiré (sans réponse)
Toute transition non prévue est refusée (CallError) : un message en retard ou rejoué (« accepté » après la fin) ne
change jamais un appel terminé.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field

REQUESTED, RINGING, ACCEPTED, CONNECTED = "demandé", "sonnerie", "accepté", "connecté"
ENDED, REFUSED, EXPIRED, FAILED = "terminé", "refusé", "expiré", "échec"
FINAL = frozenset({ENDED, REFUSED, EXPIRED, FAILED})
TRANSITIONS = {
    REQUESTED: {RINGING, FAILED, ENDED},
    RINGING: {ACCEPTED, REFUSED, EXPIRED, FAILED, ENDED},
    ACCEPTED: {CONNECTED, FAILED, ENDED},
    CONNECTED: {ENDED, FAILED},
}


class CallError(ValueError):
    pass


@dataclass
class CallSession:
    device: str
    reason: str  # ce qu'ORION veut dire en décrochant (« Votre colis est arrivé. »)
    origin: str  # qui l'a demandé dans le Core : « routine », « user »...
    created: float
    id: str = field(default_factory=lambda: secrets.token_urlsafe(12))
    state: str = REQUESTED
    end_reason: str = ""
    history: list[tuple[str, float]] = field(default_factory=list)

    def __post_init__(self):
        self.history.append((self.state, self.created))

    @property
    def final(self) -> bool:
        return self.state in FINAL

    def since(self, state: str) -> float | None:
        """Heure d'entrée dans ``state`` (la dernière), None si l'appel n'y est jamais passé."""
        times = [t for s, t in self.history if s == state]
        return times[-1] if times else None

    def move(self, state: str, now: float, why: str = "") -> None:
        if state not in TRANSITIONS.get(self.state, ()):
            raise CallError(f"appel {self.state} : passage à « {state} » impossible")
        self.state = state
        self.history.append((state, now))
        if state in FINAL:
            self.end_reason = why or state

    def describe(self) -> dict:
        """Résumé journalisable : jamais de jeton ni d'adresse, seulement l'identifiant et les états."""
        return {"call": self.id, "device": self.device, "origin": self.origin, "state": self.state,
                "end_reason": self.end_reason, "states": [s for s, _ in self.history]}
