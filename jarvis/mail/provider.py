"""Accès à la boîte mail : IMAP (lecture, tri, déplacement) et SMTP (envoi), bibliothèque standard seulement.

La lecture ne change jamais l'état de la boîte : dossier ouvert en lecture seule et contenu lu avec BODY.PEEK
(un mail lu par JARVIS reste « non lu »). Marquer lu, archiver, supprimer (vers la corbeille), envoyer et répondre
sont des opérations à part, appelées seulement après confirmation (voir jarvis/mail/tools.py).

Le mot de passe (mot de passe d'application de préférence) vient de .env (MAIL_PASSWORD) ; il n'est jamais
journalisé ni transmis au LLM.
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import logging
import re
import smtplib
import socket
import threading
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from html import unescape
from typing import Protocol

from jarvis.mail.models import MAIL_NOT_FOUND, MAIL_UNAVAILABLE, Attachment, MailError, MailMessage

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 200_000  # lecture partielle : un mail énorme n'est jamais téléchargé en entier
MAX_BODY_CHARS = 6000
FETCH = re.compile(rb"UID (\d+)")
FLAGS = re.compile(rb"FLAGS \(([^)]*)\)")
TAGS = re.compile(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", re.IGNORECASE | re.DOTALL)
KEPT_HEADERS = ("List-Unsubscribe", "Precedence", "Auto-Submitted", "X-Priority", "Importance", "Message-ID")


class MailProvider(Protocol):
    def unread_count(self) -> int: ...

    def recent(self, limit: int, unread_only: bool = False) -> list[MailMessage]: ...

    def get(self, message_id: str) -> MailMessage: ...

    def mark_read(self, message_id: str) -> None: ...

    def archive(self, message_id: str) -> None: ...

    def delete(self, message_id: str) -> None: ...

    def send(self, to: str, subject: str, body: str, reply_to: MailMessage | None = None) -> None: ...


# --- Analyse d'un message ---------------------------------------------------------------------------------

def _text(part) -> str:
    try:
        return part.get_content()
    except (LookupError, ValueError, AssertionError):
        payload = part.get_payload(decode=True) or b""
        return payload.decode("utf-8", errors="replace")


def parse_message(message_id: str, raw: bytes, flags: str = "") -> MailMessage:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    plain, html, attachments = "", "", []
    for part in msg.walk():
        if part.is_multipart():
            continue
        disposition = part.get_content_disposition()
        filename = part.get_filename()
        if disposition == "attachment" or (filename and disposition != "inline"):
            payload = part.get_payload(decode=True) or b""
            attachments.append(Attachment(str(filename or "pièce jointe")[:120], len(payload),
                                          part.get_content_type()))
            continue
        if part.get_content_type() == "text/plain" and not plain:
            plain = _text(part)
        elif part.get_content_type() == "text/html" and not html:
            html = _text(part)
    body = plain or " ".join(unescape(TAGS.sub(" ", html)).split())
    try:
        date = parsedate_to_datetime(msg["Date"]) if msg["Date"] else None
        date = date.astimezone().replace(tzinfo=None) if date and date.tzinfo else date
    except (TypeError, ValueError):
        date = None
    sender_name, sender = (getaddresses([str(msg.get("From", ""))]) or [("", "")])[0]
    return MailMessage(
        id=str(message_id), sender_name=sender_name.strip()[:80], sender=sender.strip()[:120],
        to=tuple(a for _, a in getaddresses([str(v) for v in msg.get_all("To", [])]) if a)[:10],
        cc=tuple(a for _, a in getaddresses([str(v) for v in msg.get_all("Cc", [])]) if a)[:10],
        subject=" ".join(str(msg.get("Subject", "")).split())[:200], date=date,
        unread="\\Seen" not in flags, flagged="\\Flagged" in flags, body=body.strip()[:MAX_BODY_CHARS],
        attachments=tuple(attachments[:20]),
        headers={h: str(msg[h])[:200] for h in KEPT_HEADERS if msg[h] is not None})


# --- IMAP / SMTP ---------------------------------------------------------------------------------------

class ImapSmtpProvider:
    def __init__(self, user: str, password: str, imap_host: str, smtp_host: str = "", imap_port: int = 993,
                 smtp_port: int = 465, folder: str = "INBOX", archive_folder: str = "Archive",
                 trash_folder: str = "Trash", timeout: float = 20.0, max_fetch: int = 100,
                 imap_factory=imaplib.IMAP4_SSL, smtp_factory=smtplib.SMTP_SSL):
        if not (user and password and imap_host):
            raise MailError(MAIL_UNAVAILABLE, "Boîte mail non configurée ([mail] user, imap_host, MAIL_PASSWORD).")
        self._user, self._password = user, password
        self._imap_host, self._imap_port = imap_host, imap_port
        self._smtp_host, self._smtp_port = smtp_host or imap_host.replace("imap.", "smtp.", 1), smtp_port
        self._folder, self._archive, self._trash = folder, archive_folder, trash_folder
        self._timeout, self.max_fetch = timeout, max_fetch
        self._imap_factory, self._smtp_factory = imap_factory, smtp_factory
        self._lock = threading.Lock()  # une connexion IMAP à la fois

    def _session(self, readonly: bool = True):
        try:
            imap = self._imap_factory(self._imap_host, self._imap_port, timeout=self._timeout)
            imap.login(self._user, self._password)
            status, _ = imap.select(self._quoted(self._folder), readonly=readonly)
            if status != "OK":
                raise MailError(MAIL_UNAVAILABLE, f"Dossier « {self._folder} » introuvable dans la boîte mail.")
            return imap
        except imaplib.IMAP4.error as exc:
            log.warning("Mail : connexion IMAP refusée (%s)", type(exc).__name__)
            raise MailError(MAIL_UNAVAILABLE, "La boîte mail refuse la connexion (identifiants ?).") from exc
        except (OSError, socket.timeout) as exc:
            log.warning("Mail : serveur IMAP injoignable (%s)", type(exc).__name__)
            raise MailError(MAIL_UNAVAILABLE, "La boîte mail ne répond pas.") from exc

    @staticmethod
    def _quoted(folder: str) -> str:
        return f'"{folder}"' if " " in folder else folder

    @staticmethod
    def _close(imap) -> None:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001 - fermeture au mieux, l'opération est déjà faite
            pass

    def _uids(self, imap, criteria: str) -> list[bytes]:
        status, data = imap.uid("SEARCH", None, criteria)
        if status != "OK":
            raise MailError(MAIL_UNAVAILABLE, "La recherche dans la boîte mail a échoué.")
        return (data[0] or b"").split()

    def _fetch(self, imap, uids: list[bytes]) -> list[MailMessage]:
        if not uids:
            return []
        status, data = imap.uid("FETCH", b",".join(uids).decode(), f"(UID FLAGS BODY.PEEK[]<0.{MAX_BODY_BYTES}>)")
        if status != "OK":
            raise MailError(MAIL_UNAVAILABLE, "La lecture des mails a échoué.")
        messages = []
        for item in data:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            head, raw = item[0], item[1]
            uid, flags = FETCH.search(head), FLAGS.search(head)
            if uid is None:
                continue
            try:
                messages.append(parse_message(uid.group(1).decode(), raw, flags.group(1).decode() if flags else ""))
            except Exception:  # noqa: BLE001 - un mail illisible n'empêche jamais de lire les autres
                log.warning("Mail : message %s illisible, ignoré", uid.group(1).decode())
        return sorted(messages, key=lambda m: int(m.id), reverse=True)

    def unread_count(self) -> int:
        with self._lock:
            imap = self._session()
            try:
                return len(self._uids(imap, "UNSEEN"))
            finally:
                self._close(imap)

    def recent(self, limit: int, unread_only: bool = False) -> list[MailMessage]:
        with self._lock:
            imap = self._session()
            try:
                uids = self._uids(imap, "UNSEEN" if unread_only else "ALL")
                return self._fetch(imap, uids[-max(1, min(limit, self.max_fetch)):])
            finally:
                self._close(imap)

    def get(self, message_id: str) -> MailMessage:
        if not str(message_id).isdigit():
            raise MailError(MAIL_NOT_FOUND, "Ce mail est introuvable.")
        with self._lock:
            imap = self._session()
            try:
                found = self._fetch(imap, [str(message_id).encode()])
            finally:
                self._close(imap)
        if not found:
            raise MailError(MAIL_NOT_FOUND, "Ce mail est introuvable (déplacé ou supprimé ?).")
        return found[0]

    def _change(self, message_id: str, action) -> None:
        if not str(message_id).isdigit():
            raise MailError(MAIL_NOT_FOUND, "Ce mail est introuvable.")
        with self._lock:
            imap = self._session(readonly=False)
            try:
                action(imap, str(message_id))
            finally:
                self._close(imap)

    def mark_read(self, message_id: str) -> None:
        self._change(message_id, lambda imap, uid: imap.uid("STORE", uid, "+FLAGS", "(\\Seen)"))

    def _move(self, imap, uid: str, folder: str) -> None:
        status, _ = imap.uid("MOVE", uid, self._quoted(folder))
        if status == "OK":
            return
        status, _ = imap.uid("COPY", uid, self._quoted(folder))
        if status != "OK":
            raise MailError(MAIL_UNAVAILABLE, f"Impossible de déplacer le mail vers « {folder} ».")
        imap.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
        imap.expunge()

    def archive(self, message_id: str) -> None:
        self._change(message_id, lambda imap, uid: self._move(imap, uid, self._archive))

    def delete(self, message_id: str) -> None:
        """Vers la corbeille (récupérable), jamais une suppression définitive."""
        self._change(message_id, lambda imap, uid: self._move(imap, uid, self._trash))

    def send(self, to: str, subject: str, body: str, reply_to: MailMessage | None = None) -> None:
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = self._user, to, subject
        if reply_to is not None and reply_to.headers.get("Message-ID"):
            message["In-Reply-To"] = message["References"] = reply_to.headers["Message-ID"]
        message.set_content(body)
        try:
            with self._smtp_factory(self._smtp_host, self._smtp_port, timeout=self._timeout) as smtp:
                smtp.login(self._user, self._password)
                smtp.send_message(message)
        except smtplib.SMTPException as exc:
            log.warning("Mail : envoi refusé par le serveur (%s)", type(exc).__name__)
            raise MailError(MAIL_UNAVAILABLE, "Le serveur a refusé l'envoi du mail.") from exc
        except OSError as exc:
            raise MailError(MAIL_UNAVAILABLE, "Le serveur d'envoi ne répond pas.") from exc


# --- En mémoire (tests, démonstration) -----------------------------------------------------------------------

class MemoryMailProvider:
    def __init__(self, messages: list[MailMessage] = ()):
        self.messages = list(messages)
        self.sent: list[dict] = []
        self.archived: list[str] = []
        self.trashed: list[str] = []
        self.max_fetch = 100

    def unread_count(self) -> int:
        return sum(m.unread for m in self.messages)

    def recent(self, limit: int, unread_only: bool = False) -> list[MailMessage]:
        found = [m for m in self.messages if m.unread or not unread_only]
        return sorted(found, key=lambda m: int(m.id), reverse=True)[:limit]

    def get(self, message_id: str) -> MailMessage:
        for m in self.messages:
            if m.id == str(message_id):
                return m
        raise MailError(MAIL_NOT_FOUND, "Ce mail est introuvable.")

    def _replace(self, message_id: str, **changes) -> None:
        from dataclasses import replace

        target = self.get(message_id)
        self.messages[self.messages.index(target)] = replace(target, **changes)

    def mark_read(self, message_id: str) -> None:
        self._replace(message_id, unread=False)

    def archive(self, message_id: str) -> None:
        self.messages.remove(self.get(message_id))
        self.archived.append(str(message_id))

    def delete(self, message_id: str) -> None:
        self.messages.remove(self.get(message_id))
        self.trashed.append(str(message_id))

    def send(self, to: str, subject: str, body: str, reply_to: MailMessage | None = None) -> None:
        self.sent.append({"to": to, "subject": subject, "body": body, "reply_to": reply_to.id if reply_to else None})
