"""Corrélation des demandes : un identifiant court par demande, repris par chaque ligne du journal qui la concerne.

- Une conversation ouverte par le mot de réveil reçoit un identifiant (« 3fa2c1 ») dès le pic du wake word, avant
  sa vérification ; chaque phrase de la conversation en est une demande numérotée (« 3fa2c1.2 »).
- Un message Linphone, une action du fond d'écran, une routine reçoivent le leur.
- L'identifiant suit la demande dans les fils d'exécution des outils (variables de contexte, copiées par le Core) et
  part vers l'agent Windows et le service de transcription (en-tête ``X-Orion-Request``). Ollama ne le reçoit pas :
  son protocole ne permet pas d'en transmettre un ; ses lignes se rattachent par l'heure et la demande qui l'appelle.

Seul l'identifiant est ajouté au journal (champ ``%(req)s`` du format) : aucun texte de la demande en plus.
"""

from __future__ import annotations

import contextvars
import logging
import secrets
from contextlib import contextmanager
from typing import Iterator

HEADER = "X-Orion-Request"
REQUEST_ID: contextvars.ContextVar[str] = contextvars.ContextVar("orion_request_id", default="")


def current() -> str:
    return REQUEST_ID.get()


def new_id() -> str:
    return secrets.token_hex(3)


def begin(request_id: str | None = None) -> str:
    """Démarre une demande (ou une conversation) dans le contexte courant ; rend son identifiant."""
    value = request_id or new_id()
    REQUEST_ID.set(value)
    return value


def end() -> None:
    REQUEST_ID.set("")


@contextmanager
def request(request_id: str | None = None) -> Iterator[str]:
    """Le temps d'un bloc (message, action, routine) ; l'identifiant précédent est rétabli ensuite."""
    token = REQUEST_ID.set(request_id or new_id())
    try:
        yield REQUEST_ID.get()
    finally:
        REQUEST_ID.reset(token)


def valid(value: str | None) -> str:
    """Identifiant reçu d'une autre machine : gardé seulement s'il en a la forme (rien d'autre n'entre au journal)."""
    value = (value or "").strip()
    return value if 0 < len(value) <= 16 and all(c in "0123456789abcdef." for c in value) else ""


class RequestFilter(logging.Filter):
    """Ajoute ``record.req`` (« [r:3fa2c1.2] » ou vide) pour le format du journal."""

    def filter(self, record: logging.LogRecord) -> bool:
        value = REQUEST_ID.get()
        record.req = f"[r:{value}] " if value else ""
        return True


def install(handler_filter: logging.Filter | None = None) -> None:
    """Sur chaque sortie du journal racine (format « %(req)s »)."""
    flt = handler_filter or RequestFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(flt)
