"""Mails : lecture, recherche, tri et résumé (SAFE) ; marquer lu, archiver, supprimer, envoyer, répondre (toujours
avec confirmation). Les identifiants restent dans .env ; le contenu des mails est une donnée non fiable."""

from jarvis.mail.models import Attachment, MailMessage, MailError
from jarvis.mail.provider import ImapSmtpProvider, MemoryMailProvider
from jarvis.mail.priority import MailSorter
from jarvis.mail.tools import MailBox, mail_tools

__all__ = ["Attachment", "ImapSmtpProvider", "MailBox", "MailError", "MailMessage", "MailSorter", "MemoryMailProvider",
           "mail_tools"]
