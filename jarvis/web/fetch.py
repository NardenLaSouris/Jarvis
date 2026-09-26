"""Récupération prudente de pages Web et extraction de leur texte utile.

- seules les adresses http(s) publiques sont visitées (pas de réseau local ni de localhost),
  y compris après chaque redirection ;
- délai et taille de réponse bornés, seuls les contenus textuels sont lus ;
- le HTML est réduit à du texte : scripts, styles et autres éléments actifs sont jetés sans
  jamais être interprétés. Aucun JavaScript ni code provenant d'une page n'est exécuté.
"""

from __future__ import annotations

import ipaddress
import socket
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import urlsplit

import httpx

from jarvis.web.base import WebSearchError

TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
SKIPPED_TAGS = {"script", "style", "noscript", "template", "svg", "math", "iframe", "object", "embed",
                "canvas", "head", "nav", "footer", "header", "aside", "form", "button", "select"}
BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
              "blockquote", "pre", "td", "th", "dd", "dt", "table", "ul", "ol"}
USER_AGENT = "Mozilla/5.0 (compatible; JARVIS-assistant/1.0)"
MIN_LINE_CHARS = 25


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skipping = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in SKIPPED_TAGS:
            self._skipping += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIPPED_TAGS:
            self._skipping = max(0, self._skipping - 1)
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skipping:
            self.parts.append(data)


def extract_text(content: str, max_chars: int = 20000) -> str:
    """Texte lisible d'une page : une ligne par bloc, lignes trop courtes (menus, boutons) écartées."""
    if "<" in content and ">" in content:
        parser = _TextExtractor()
        try:
            parser.feed(content)
            parser.close()
        except Exception:
            return ""
        content = "".join(parser.parts)
    lines = (" ".join(line.split()) for line in content.splitlines())
    text = "\n".join(line for line in lines if len(line) >= MIN_LINE_CHARS)
    return text[:max_chars]


def _resolve(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


def is_public_url(url: str, resolve: Callable[[str], list[str]] = _resolve) -> bool:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    try:
        addresses = resolve(parts.hostname)
    except OSError:
        return False
    try:
        return bool(addresses) and all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addresses)
    except ValueError:
        return False


class PageFetcher:
    def __init__(
        self,
        timeout: float = 10.0,
        max_bytes: int = 1_000_000,
        max_redirects: int = 5,
        client: httpx.Client | None = None,
        resolve: Callable[[str], list[str]] = _resolve,
    ):
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._client = client or httpx.Client(follow_redirects=False, headers={"User-Agent": USER_AGENT})
        self._resolve = resolve

    def fetch(self, url: str) -> str:
        for _ in range(self._max_redirects + 1):
            if not is_public_url(url, self._resolve):
                raise WebSearchError(f"adresse refusée (non publique ou invalide) : {url}")
            try:
                with self._client.stream("GET", url, timeout=self._timeout) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise WebSearchError(f"redirection sans destination : {url}")
                        url = str(response.url.join(location))
                        continue
                    if response.status_code >= 400:
                        raise WebSearchError(f"erreur HTTP {response.status_code} : {url}")
                    kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
                    if kind and kind not in TEXT_TYPES:
                        raise WebSearchError(f"contenu non textuel ({kind}) : {url}")
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) >= self._max_bytes:
                            del body[self._max_bytes:]
                            break
                    return body.decode(response.charset_encoding or "utf-8", errors="replace")
            except httpx.TimeoutException as exc:
                raise WebSearchError(f"délai dépassé : {url}") from exc
            except httpx.HTTPError as exc:
                raise WebSearchError(f"page injoignable : {url} ({exc})") from exc
            except LookupError as exc:
                raise WebSearchError(f"encodage inconnu : {url}") from exc
        raise WebSearchError(f"trop de redirections : {url}")
