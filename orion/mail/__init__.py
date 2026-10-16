"""Mails : lecture, recherche, tri et résumé (SAFE) ; marquer lu, archiver, supprimer, envoyer, répondre (toujours
avec confirmation). Les identifiants restent dans .env ; le contenu des mails est une donnée non fiable."""

from orion.mail.models import Attachment, MailMessage, MailError
from orion.mail.provider import ImapSmtpProvider, MemoryMailProvider
from orion.mail.priority import MailSorter
from orion.mail.tools import MailBox, mail_tools

__all__ = ["Attachment", "ImapSmtpProvider", "MailBox", "MailError", "MailMessage", "MailSorter", "MemoryMailProvider",
           "mail_tools"]
