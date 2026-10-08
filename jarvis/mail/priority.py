"""Tri des mails : important, normal ou bruit — règles simples et déterministes (jamais le LLM).

Bruit : lettre d'information ou envoi automatique (en-tête List-Unsubscribe, Precedence: bulk, expéditeur
« noreply », « newsletter »...), sauf expéditeur important. Important : expéditeur listé ([mail] important_senders),
mail marqué d'une étoile, objet qui contient un mot d'alerte (urgent, facture, rendez-vous, compte fermé...), ou mail
jugé important par Gmail (hors lettres d'information). Catégories Gmail Promotions et Réseaux sociaux : bruit.
"""

from __future__ import annotations

import re

from jarvis.mail.models import MailMessage
from jarvis.personality import normalize

IMPORTANT, NORMAL, NOISE = "important", "normal", "bruit"
NOISE_SENDERS = re.compile(r"(?:^|[._+-])(?:no-?reply|ne-?pas-?repondre|newsletter|news|marketing|promo|info|"
                           r"notification|notifications|mailer-daemon|bounce)(?:[._+-]|@)", re.IGNORECASE)
ALERT_WORDS = ("urgent", "important", "facture", "paiement", "echeance", "rendez vous", "entretien", "convocation",
               "relance", "impot", "impots", "banque", "contrat", "resultat", "examen", "livraison", "colis",
               "action requise", "securite", "mot de passe",
               # Comptes : fermeture, blocage, connexion inhabituelle, vérification, double authentification.
               "compte suspendu", "compte bloque", "sera ferme", "fermeture", "suspension", "nouvelle connexion",
               "connexion inhabituelle", "code de verification", "verification en deux etapes",
               "validation en deux etapes", "alerte", "fraude", "remboursement")


class MailSorter:
    def __init__(self, important_senders: tuple[str, ...] = (), noise_senders: tuple[str, ...] = (),
                 alert_words: tuple[str, ...] = ALERT_WORDS):
        self._important = tuple(normalize(s) for s in important_senders if s)
        self._noise = tuple(normalize(s) for s in noise_senders if s)
        self._alerts = tuple(normalize(w) for w in alert_words)

    def _matches(self, message: MailMessage, patterns: tuple[str, ...]) -> bool:
        who = f" {normalize(message.sender)} {normalize(message.sender_name)} "
        return any(p and p in who for p in patterns)

    def priority(self, message: MailMessage) -> str:
        if self._matches(message, self._important) or message.flagged:
            return IMPORTANT
        labels = set(message.labels)
        if labels & {"promotions", "social"}:  # classés par Gmail : jamais annoncés
            return NOISE
        headers = {k.lower(): str(v).lower() for k, v in message.headers.items()}
        automatic = "list-unsubscribe" in headers or headers.get("precedence") in ("bulk", "list", "junk") \
            or bool(NOISE_SENDERS.search(message.sender)) or self._matches(message, self._noise)
        subject = f" {normalize(message.subject)} "
        if any(f" {w} " in subject for w in self._alerts):
            # Une facture ou une livraison envoyée par un robot reste importante ; une promotion « urgente » non.
            return NORMAL if automatic and "list-unsubscribe" in headers else IMPORTANT
        if "important" in labels and "list-unsubscribe" not in headers:  # jugé important par Gmail, hors lettres
            return IMPORTANT
        return NOISE if automatic else NORMAL
