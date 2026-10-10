"""Appel d'ORION quand quelque chose d'important arrive pendant votre absence.

Seulement si la maison est vide (système de présence : tous les utilisateurs suivis sont partis) et seulement pour
les événements de [call] away_events (par défaut : fuite d'eau, fumée, mouvement pendant l'absence, porte restée
ouverte). La phrase dite au décroché est celle du journal de la maison (« Une fuite d'eau a été détectée dans la
salle de bain. »). L'appel passe par le gestionnaire d'appels : appareil jumelé seul destinataire, limites
anti-harcèlement, origine « routine » (décision du Core). Un appel refusé par les limites n'est pas perdu :
l'événement reste au journal et figure dans le résumé du retour.
"""

from __future__ import annotations

import logging

from jarvis.call.manager import CallRefused
from jarvis.events import Event, EventBus

log = logging.getLogger(__name__)

DEFAULT_AWAY_EVENTS = ("leak.detected", "smoke.detected", "presence.motion_while_away", "door.left_open")


class AwayCallTrigger:
    def __init__(self, calls, presence, journal, event_types=DEFAULT_AWAY_EVENTS):
        self._calls = calls  # CallManager
        self._presence = presence  # PresenceEngine (occupancy)
        self._journal = journal  # HouseJournal (classify : priorité et phrase)
        self.event_types = tuple(event_types)

    def attach(self, bus: EventBus) -> None:
        for event_type in self.event_types:
            bus.subscribe(event_type, self.on_event)

    def on_event(self, event: Event) -> None:
        if event.type not in self.event_types or self._presence.occupancy() != "empty":
            return
        try:
            _, text = self._journal.classify(event)
        except Exception:  # noqa: BLE001 - événement mal formé : pas d'appel, le journal le note déjà
            log.exception("Appel d'absence : %s non classé", event.type)
            return
        if not text:
            return
        try:
            self._calls.request(text, "routine")
            log.info("Appel d'absence demandé (%s)", event.type)
        except CallRefused as exc:
            log.info("Appel d'absence non passé (%s) : %s ; noté au journal pour le retour", event.type, exc)
