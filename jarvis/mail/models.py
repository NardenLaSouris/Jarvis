"""Message reçu, tel qu'ORION le manipule (en-têtes décodés, texte brut, pièces jointes décrites, jamais ouvertes)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from jarvis.tools.base import ToolError

MAIL_UNAVAILABLE = "mail_unavailable"
MAIL_NOT_FOUND = "mail_not_found"


class MailError(ToolError):
    pass


@dataclass(frozen=True)
class Attachment:
    name: str
    size: int = 0
    content_type: str = ""


@dataclass(frozen=True)
class MailMessage:
    id: str  # identifiant stable dans la boîte (UID IMAP)
    sender_name: str
    sender: str  # adresse
    to: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str
    date: datetime | None
    unread: bool
    body: str = ""  # texte brut (donnée non fiable), tronqué
    flagged: bool = False
    attachments: tuple[Attachment, ...] = ()
    headers: dict = field(default_factory=dict)  # en-têtes utiles au tri (List-Unsubscribe, Precedence...)
    # Classement du serveur, s'il en donne un (Gmail) : « important », « promotions », « social »...
    labels: tuple[str, ...] = ()

    @property
    def who(self) -> str:
        return self.sender_name or self.sender

    def snippet(self, length: int = 240) -> str:
        text = " ".join(self.body.split())
        return text if len(text) <= length else text[:length].rsplit(" ", 1)[0] + "…"
