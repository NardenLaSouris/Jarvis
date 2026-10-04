"""Spotify (API Web) : lecture d'un morceau, d'un album, d'un artiste ou d'une playlist, pause, reprise, suivant,
précédent, volume.

Mise en place (une fois, manuelle) :
1. créer une application sur https://developer.spotify.com/dashboard (« Web API »), avec l'adresse de retour
   ``http://127.0.0.1:8888/callback`` ;
2. mettre son identifiant dans .env : ``SPOTIFY_CLIENT_ID=...`` (aucun secret : authentification PKCE) ;
3. lancer ``python -m jarvis --spotify-login`` sur le Core, ouvrir l'adresse affichée, accepter, puis coller
   l'adresse de la page de retour (même si elle affiche une erreur) : le jeton est enregistré dans
   data/spotify_token.json (hors Git, droits restreints) et renouvelé automatiquement.
Le contrôle de la lecture exige un compte Spotify Premium et une application Spotify ouverte sur un appareil.
Sans configuration, les touches multimédia du PC (jarvis.tools.media) restent disponibles.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from jarvis.memory import keywords
from jarvis.tools.base import EXECUTION_FAILED, Param, Risk, Tool, ToolError

log = logging.getLogger(__name__)

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
REDIRECT = "http://127.0.0.1:8888/callback"
SCOPES = "user-read-playback-state user-modify-playback-state playlist-read-private"
SPOTIFY_UNAVAILABLE = "spotify_unavailable"
NO_DEVICE = "spotify_no_device"
NOT_FOUND = "spotify_not_found"
KINDS = ("track", "playlist", "album", "artist")


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(client_id: str, challenge: str, state: str) -> str:
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "response_type": "code", "redirect_uri": REDIRECT, "scope": SCOPES,
        "code_challenge_method": "S256", "code_challenge": challenge, "state": state})


def _error_message(data: bytes) -> str:
    try:
        return str(json.loads(data).get("error", {}).get("message", ""))
    except (ValueError, AttributeError):
        return ""


def _default_http(method: str, url: str, headers: dict, body: bytes | None, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class SpotifyClient:
    def __init__(self, client_id: str, token_path: Path, http: Callable = _default_http, timeout: float = 6.0,
                 clock: Callable[[], float] = time.time):
        self._client_id = client_id
        self._path = Path(token_path)
        self._http, self._timeout, self._clock = http, timeout, clock

    # --- Jeton -----------------------------------------------------------------------------------------

    @property
    def configured(self) -> bool:
        return bool(self._client_id) and self._path.exists()

    def _token(self) -> dict:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ToolError(SPOTIFY_UNAVAILABLE, "Spotify n'est pas encore relié : lancez python -m jarvis "
                                                 "--spotify-login.") from exc

    def _save(self, token: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(token), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self._path)

    def _token_request(self, fields: dict) -> dict:
        body = urllib.parse.urlencode({"client_id": self._client_id, **fields}).encode()
        status, data = self._http("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"}, body,
                                  self._timeout)
        if status != 200:
            raise ToolError(SPOTIFY_UNAVAILABLE, "Spotify a refusé l'autorisation : relancez --spotify-login.")
        token = json.loads(data)
        token["expires_at"] = self._clock() + int(token.get("expires_in", 3600)) - 60
        return token

    def exchange(self, code: str, verifier: str) -> None:
        """Code reçu sur l'adresse de retour -> jeton enregistré (étape --spotify-login)."""
        self._save(self._token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                                        "code_verifier": verifier}))

    def _access(self) -> str:
        token = self._token()
        if self._clock() >= token.get("expires_at", 0):
            fresh = self._token_request({"grant_type": "refresh_token", "refresh_token": token["refresh_token"]})
            fresh.setdefault("refresh_token", token["refresh_token"])
            self._save(fresh)
            token = fresh
        return token["access_token"]

    # --- API -------------------------------------------------------------------------------------------

    def call(self, method: str, path: str, query: dict | None = None, body: dict | None = None) -> dict:
        url = API + path + ("?" + urllib.parse.urlencode(query) if query else "")
        headers = {"Authorization": f"Bearer {self._access()}", "Content-Type": "application/json"}
        try:
            status, data = self._http(method, url, headers, json.dumps(body).encode() if body is not None else None,
                                      self._timeout)
        except OSError as exc:
            raise ToolError(SPOTIFY_UNAVAILABLE, "Spotify ne répond pas.") from exc
        if status == 404 and path.startswith("/me/player"):
            raise ToolError(NO_DEVICE, "Aucun appareil Spotify actif : ouvrez Spotify sur un appareil.")
        if status == 403:
            reason = _error_message(data)
            log.warning("Spotify %s %s refusé : %s", method, path, reason)
            if "premium" in reason.lower() or path.startswith("/me/player"):
                raise ToolError(SPOTIFY_UNAVAILABLE, "Spotify refuse cette commande (compte Premium nécessaire).")
            raise ToolError(SPOTIFY_UNAVAILABLE, "Spotify refuse cette demande.")
        if status >= 400:
            log.warning("Spotify %s %s : %s", method, path, status)
            raise ToolError(EXECUTION_FAILED, "Spotify n'a pas pu exécuter la commande.")
        return json.loads(data) if data else {}

    def _device(self) -> str | None:
        """Appareil actif ; à défaut, le premier disponible (la lecture y est transférée)."""
        devices = self.call("GET", "/me/player/devices").get("devices", [])
        active = next((d for d in devices if d.get("is_active")), None)
        if active:
            return None
        if not devices:
            raise ToolError(NO_DEVICE, "Aucun appareil Spotify n'est disponible : ouvrez Spotify sur un appareil.")
        # Rien ne joue : l'ordinateur d'abord (plutôt que le téléphone dans une poche).
        preferred = sorted(devices, key=lambda d: 0 if d.get("type") == "Computer" else 1)
        return preferred[0]["id"]

    def find(self, query: str, kind: str) -> tuple[str, str]:
        """(uri, nom) : d'abord vos playlists pour une playlist, sinon la recherche Spotify."""
        if kind == "playlist":
            wanted = keywords(query)
            mine = self.call("GET", "/me/playlists", {"limit": 50}).get("items", [])
            for playlist in mine:
                if wanted and wanted <= keywords(playlist.get("name", "")):
                    return playlist["uri"], playlist["name"]
        found = self.call("GET", "/search", {"q": query, "type": kind, "limit": 1})
        items = found.get(f"{kind}s", {}).get("items", [])
        if not items:
            raise ToolError(NOT_FOUND, f"Je ne trouve rien sur Spotify pour « {query[:40]} ».")
        item = items[0]
        artist = f" de {item['artists'][0]['name']}" if kind in ("track", "album") and item.get("artists") else ""
        return item["uri"], item["name"] + artist

    def play(self, query: str | None, kind: str) -> str:
        device = self._device()
        params = {"device_id": device} if device else None
        if not query:
            self.call("PUT", "/me/player/play", params)
            return ""
        uri, name = self.find(query, kind)
        body = {"uris": [uri]} if kind == "track" else {"context_uri": uri}
        self.call("PUT", "/me/player/play", params, body)
        return name


def spotify_tools(client: SpotifyClient) -> list[Tool]:
    def play(query: str | None = None, kind: str | None = None) -> dict:
        name = client.play(query, kind or "track")
        return {"playing": name or "la lecture", "kind": kind or "track"}

    def simple(method: str, path: str, message: str, query: dict | None = None):
        def run() -> dict:
            client.call(method, path, query)
            return {"message": message}
        return run

    def volume(volume: int) -> dict:
        client.call("PUT", "/me/player/volume", {"volume_percent": volume})
        return {"volume": volume}

    return [
        Tool("spotify_play", "Lance sur Spotify un morceau, un album, un artiste ou une playlist (ou reprend la lecture).",
             {"query": Param(str, "ce qu'il faut jouer, tel que dit (titre, artiste, nom de playlist)", required=False,
                             max_length=100),
              "kind": Param(str, "track, playlist, album ou artist", required=False, choices=KINDS)},
             {"playing": "ce qui est joué"}, Risk.SAFE, play,
             say=lambda r: "Je reprends la lecture." if r["playing"] == "la lecture" else f"Je lance {r['playing']}."),
        Tool("spotify_pause", "Met Spotify en pause.", {}, {}, Risk.SAFE,
             simple("PUT", "/me/player/pause", "pause"), say=lambda r: "Spotify est en pause."),
        Tool("spotify_next", "Passe au morceau suivant sur Spotify.", {}, {}, Risk.SAFE,
             simple("POST", "/me/player/next", "suivant"), say=lambda r: "Morceau suivant."),
        Tool("spotify_previous", "Revient au morceau précédent sur Spotify.", {}, {}, Risk.SAFE,
             simple("POST", "/me/player/previous", "précédent"), say=lambda r: "Morceau précédent."),
        Tool("spotify_volume", "Règle le volume de Spotify (0 à 100 %).",
             {"volume": Param(int, "volume en pourcentage, seulement s'il est dit", minimum=0, maximum=100)},
             {"volume": "%"}, Risk.SAFE, volume, say=lambda r: f"Volume de Spotify à {r['volume']} %."),
    ]


def login(client_id: str, token_path: Path, ask: Callable[[str], str] = input, show: Callable[[str], None] = print) -> int:
    """``python -m jarvis --spotify-login`` : autorisation PKCE en collant l'adresse de retour (machine sans écran)."""
    if not client_id:
        show("SPOTIFY_CLIENT_ID manquant dans .env (créer une application sur developer.spotify.com).")
        return 2
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    show("1. Ouvrez cette adresse dans un navigateur et acceptez :\n\n" + authorize_url(client_id, challenge, state))
    show("\n2. La page de retour (http://127.0.0.1:8888/callback?...) peut afficher une erreur : c'est normal.")
    pasted = ask("3. Collez ici son adresse complète : ").strip()
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(pasted).query)
    if query.get("state", [""])[0] != state or "code" not in query:
        show("Adresse invalide (code ou état absent) : recommencez.")
        return 1
    try:
        SpotifyClient(client_id, token_path).exchange(query["code"][0], verifier)
    except ToolError as exc:
        show(exc.message)
        return 1
    show(f"Spotify est relié. Jeton enregistré dans {token_path}.")
    return 0
