"""Types communs de la recherche Web : résultat structuré, interface d'un moteur, erreurs.

Tout ce qui vient du Web est une donnée NON FIABLE : ces types ne font que la transporter,
jamais l'interpréter ni l'exécuter.
"""

from __future__ import annotations

import html
import re
from dataclasses import asdict, dataclass
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class WebSearchError(RuntimeError):
    """Recherche ou récupération impossible (service injoignable, délai, erreur HTTP, réponse illisible...)."""


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    source: str
    position: int
    published_at: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


class WebSearchProvider(Protocol):
    """Moteur de recherche interchangeable (SearXNG aujourd'hui, un autre demain)."""

    def search(self, query: str) -> list[SearchResult]:
        """Résultats structurés, sans doublon ni résultat sans URL. Lève WebSearchError."""

    def fetch(self, url: str) -> str:
        """Contenu brut (borné en taille) d'une page. Lève WebSearchError."""

    def extract(self, content: str) -> str:
        """Texte utile d'un contenu récupéré (sans HTML, scripts ni styles)."""


_TAGS = re.compile(r"<[^>]*>")
_SPACES = re.compile(r"\s+")
_TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|mc_)")


def clean_text(value) -> str:
    """Texte brut d'un champ renvoyé par un moteur : balises et entités retirées, espaces normalisés."""
    if not isinstance(value, str):
        return ""
    return _SPACES.sub(" ", html.unescape(_TAGS.sub(" ", value))).strip()


def source_name(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")


def canonical_url(url: str) -> str:
    """Clé de dédoublonnage : hôte sans www, sans fragment, sans paramètres de suivi ni / final."""
    parts = urlsplit(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not _TRACKING.match(k)])
    path = parts.path.rstrip("/")
    return urlunsplit(("", source_name(url), path, query, ""))
