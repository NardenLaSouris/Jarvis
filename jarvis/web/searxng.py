"""Moteur de recherche SearXNG (instance auto-hébergée, API JSON).

Prérequis côté SearXNG : le format "json" doit être autorisé dans settings.yml
(search: formats: [html, json]).
"""

from __future__ import annotations

import logging

import httpx

from jarvis.web.base import SearchResult, WebSearchError, canonical_url, clean_text, source_name
from jarvis.web.fetch import PageFetcher, extract_text

log = logging.getLogger(__name__)


def parse_results(raw: list, max_results: int) -> list[SearchResult]:
    """Résultats SearXNG -> SearchResult : entrées malformées, sans URL et doublons écartés."""
    results: list[SearchResult] = []
    seen: set[str] = set()
    for item in raw:
        if len(results) >= max_results:
            break
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url.strip().lower().startswith(("http://", "https://")):
            continue
        url = url.strip()
        key = canonical_url(url)
        if key in seen:
            continue
        seen.add(key)
        published = item.get("publishedDate")
        results.append(SearchResult(
            title=clean_text(item.get("title")) or source_name(url),
            url=url,
            snippet=clean_text(item.get("content")),
            source=source_name(url),
            position=len(results) + 1,
            published_at=published if isinstance(published, str) and published else None,
        ))
    return results


class SearXNGProvider:
    def __init__(
        self,
        base_url: str,
        language: str = "fr",
        max_results: int = 5,
        timeout: float = 10.0,
        client: httpx.Client | None = None,
        fetcher: PageFetcher | None = None,
        max_page_bytes: int = 1_000_000,
    ):
        self._base_url = base_url.rstrip("/")
        self._language = language
        self._max_results = max_results
        self._timeout = timeout
        self._client = client or httpx.Client()
        self._fetcher = fetcher or PageFetcher(timeout=timeout, max_bytes=max_page_bytes)

    def search(self, query: str) -> list[SearchResult]:
        if not query.strip():
            return []
        params = {"q": query, "format": "json", "language": self._language, "safesearch": 1}
        try:
            response = self._client.get(f"{self._base_url}/search", params=params, timeout=self._timeout)
        except httpx.TimeoutException as exc:
            raise WebSearchError("SearXNG ne répond pas (délai dépassé)") from exc
        except httpx.HTTPError as exc:
            raise WebSearchError(f"SearXNG injoignable ({self._base_url}) : {exc}") from exc
        if response.status_code != 200:
            raise WebSearchError(f"SearXNG : erreur HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise WebSearchError("SearXNG : réponse illisible (format json activé ?)") from exc
        raw = data.get("results") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            raise WebSearchError("SearXNG : réponse malformée")
        return parse_results(raw, self._max_results)

    def fetch(self, url: str) -> str:
        return self._fetcher.fetch(url)

    def extract(self, content: str) -> str:
        return extract_text(content)

    def available(self) -> bool:
        try:
            self._client.get(f"{self._base_url}/healthz", timeout=min(self._timeout, 2.0))
            return True
        except httpx.HTTPError:
            return False
