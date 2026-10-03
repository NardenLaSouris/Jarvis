"""Recherche Web pour le LLM : requête -> résultats -> sélection -> bloc de données NON FIABLES.

Le LLM ne reçoit jamais tous les résultats bruts : seulement les plus pertinents, tronqués,
nettoyés et encadrés par des balises qui les désignent comme des données et non des instructions.
Les sources complètes (titre, URL...) restent disponibles, structurées, pour une future interface.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field

from jarvis.personality import normalize
from jarvis.prompts import WEB_RULES
from jarvis.web.base import SearchResult, WebSearchError, WebSearchProvider, canonical_url

log = logging.getLogger(__name__)

DATA_START = "<<<DEBUT_DONNEES_WEB>>>"
DATA_END = "<<<FIN_DONNEES_WEB>>>"
SNIPPET_CHARS = 300
PAGE_CHARS = 1200
STOP_WORDS = {
    "le", "la", "les", "un", "une", "des", "de", "du", "d", "l", "et", "ou", "a", "au", "aux", "en", "est",
    "sont", "quel", "quelle", "quels", "quelles", "qui", "que", "qu", "quoi", "combien", "comment", "pour",
    "sur", "dans", "par", "avec", "moi", "me", "je", "tu", "vous", "il", "elle", "ce", "cette", "ces", "c",
    "s", "y", "t", "peux", "pourrais", "cherche", "recherche", "chercher", "trouve", "internet", "web", "google",
    "actuel", "actuelle", "actuellement", "moment", "dernier", "derniere", "derniers", "dernieres",
}
_REQUEST = re.compile(
    r"^\s*(?:(?:est-ce que\s+)?(?:tu peux|peux-tu|pourrais-tu|vous pouvez|pouvez-vous|tu pourrais)\s+)?"
    r"(?:me\s+)?(?:re)?cherche[rz]?(?:-moi)?\s+", re.IGNORECASE)
_WHERE = re.compile(r"\s*\b(?:sur (?:internet|le web|google)|en ligne)\b", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u200b-\u200f\u2028-\u202e\u2066-\u2069]")
_MARKERS = re.compile(r"<{2,}|>{2,}")


def without_name(text: str, assistant_name: str = "JARVIS") -> str:
    """« Jarvis, combien coûte... » -> « Combien coûte... »."""
    rest = re.sub(rf"^\s*{re.escape(assistant_name)}\b[\s,.!]*", "", text, flags=re.IGNORECASE)
    return rest[:1].upper() + rest[1:] if rest else text


def search_query(text: str, assistant_name: str = "JARVIS") -> str:
    """Requête envoyée au moteur, à partir de la demande orale (« Jarvis, cherche-moi X » -> « X »)."""
    query = _WHERE.sub("", _REQUEST.sub("", without_name(text, assistant_name)))
    return query.strip(" \t?!.,;:«»\"'") or text.strip()


def sanitize(text: str, max_chars: int) -> str:
    """Donnée Web -> texte inerte : caractères de contrôle et imitations de balises retirés, tronqué."""
    text = unicodedata.normalize("NFKC", text)
    text = _MARKERS.sub(" ", _CONTROL.sub(" ", text))
    text = " ".join(text.split())
    return text if len(text) <= max_chars else text[: max_chars - 1].rstrip() + "…"


def keywords(query: str) -> set[str]:
    return {w for w in normalize(query).split() if w not in STOP_WORDS and (len(w) > 2 or w.isdigit())}


def select_relevant(results: list[SearchResult], query: str, limit: int) -> list[SearchResult]:
    """Les ``limit`` résultats qui partagent le plus de mots-clés avec la requête (à égalité : rang du moteur)."""
    terms = keywords(query)

    def score(result: SearchResult) -> int:
        words = set(normalize(f"{result.title} {result.snippet}").split())
        return len(terms & words)

    ranked = sorted(results, key=lambda r: (-score(r), r.position))
    relevant = [r for r in ranked if score(r) > 0] or ranked
    distinct, sites = [], set()
    for result in relevant:
        if result.source not in sites:
            sites.add(result.source)
            distinct.append(result)
    return distinct[:limit]


def relevant_passages(text: str, query: str, max_chars: int = PAGE_CHARS) -> str:
    """Lignes d'une page qui contiennent des mots-clés de la requête, dans l'ordre, jusqu'à ``max_chars``."""
    terms = keywords(query)
    lines = [line for line in text.splitlines() if terms & set(normalize(line).split())] or text.splitlines()
    kept, size = [], 0
    for line in lines:
        if size + len(line) > max_chars:
            break
        kept.append(line)
        size += len(line) + 1
    return "\n".join(kept) or text[:max_chars]


@dataclass
class WebContext:
    query: str
    results: list[SearchResult] = field(default_factory=list)
    pages: dict[str, str] = field(default_factory=dict)
    error: str | None = None

    @property
    def found(self) -> bool:
        return self.error is None and bool(self.results)

    @property
    def sources(self) -> list[dict]:
        """Sources structurées (titre, URL, site, rang, date) pour une interface graphique, jamais lues à voix haute."""
        return [r.as_dict() for r in self.results]

    def for_llm(self) -> str:
        """Bloc de données transmis au LLM : sites et extraits, sans URL, encadrés comme données non fiables."""
        lines = [DATA_START, f"Recherche effectuée : {sanitize(self.query, 200)}"]
        for i, result in enumerate(self.results, 1):
            date = f", publié le {sanitize(result.published_at, 40)}" if result.published_at else ""
            lines.append(f"[{i}] Site : {sanitize(result.source, 80)}{date}")
            lines.append(f"    Titre : {sanitize(result.title, 150)}")
            if result.snippet:
                lines.append(f"    Extrait : {sanitize(result.snippet, SNIPPET_CHARS)}")
            page = self.pages.get(result.url)
            if page:
                lines.append(f"    Contenu de la page : {sanitize(page, PAGE_CHARS)}")
        lines.append(DATA_END)
        return "\n".join(lines)


def web_request(user_text: str, context: WebContext, assistant_name: str = "JARVIS") -> str:
    """Message utilisateur envoyé au LLM : les consignes, les données Web, puis la vraie demande."""
    return f"{WEB_RULES}\n\n{context.for_llm()}\n\n{without_name(user_text, assistant_name)}"


class WebResearch:
    def __init__(self, provider: WebSearchProvider, max_results: int = 5, fetch_pages: int = 1,
                 context_results: int = 3, assistant_name: str = "JARVIS"):
        self.provider = provider
        self._max_results = max_results
        self._fetch_pages = fetch_pages
        self._context_results = context_results
        self._assistant_name = assistant_name

    def run(self, user_text: str) -> WebContext:
        query = search_query(user_text, self._assistant_name)
        try:
            results = self.provider.search(query)
        except WebSearchError as exc:
            log.warning("Recherche Web impossible : %s", exc)
            return WebContext(query, error=str(exc))
        except Exception as exc:
            log.exception("Recherche Web : erreur inattendue du moteur")
            return WebContext(query, error=f"erreur inattendue : {exc}")
        results = self._clean(results)
        selected = select_relevant(results, query, self._context_results)
        context = WebContext(query, selected)
        for result in selected:
            if len(context.pages) >= self._fetch_pages:
                break
            try:
                text = self.provider.extract(self.provider.fetch(result.url))
            except WebSearchError as exc:
                log.info("Page ignorée : %s", exc)
                continue
            except Exception:
                log.exception("Page ignorée (erreur inattendue) : %s", result.url)
                continue
            if text:
                context.pages[result.url] = relevant_passages(text, query)
        return context

    def _clean(self, results: list[SearchResult]) -> list[SearchResult]:
        kept, seen = [], set()
        for result in results:
            if not isinstance(result, SearchResult) or not result.url:
                continue
            key = canonical_url(result.url)
            if key not in seen:
                seen.add(key)
                kept.append(result)
        return kept[: self._max_results]


class WebSearchCapability:
    """Déclare la recherche Web au registre des capacités (prompt système, « que sais-tu faire ? »).

    Le routage vers la recherche est fait par l'intention web.search, pas par ``handle``.
    """

    name = "recherche web"
    description = "chercher des informations actuelles sur Internet"
    replaces = "la recherche sur Internet"

    def handle(self, text: str) -> None:
        return None
