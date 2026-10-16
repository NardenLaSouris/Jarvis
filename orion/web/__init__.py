"""Recherche Web d'ORION : moteur interchangeable, récupération prudente, données non fiables."""

from orion.web.base import SearchResult, WebSearchError, WebSearchProvider
from orion.web.research import WebContext, WebResearch, WebSearchCapability, web_request

__all__ = ["SearchResult", "WebContext", "WebResearch", "WebSearchCapability", "WebSearchError",
           "WebSearchProvider", "web_request"]
