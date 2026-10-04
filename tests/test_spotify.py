"""Spotify : autorisation PKCE, renouvellement du jeton, commandes de lecture (API simulée, aucun accès réseau)."""

from __future__ import annotations

import json
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.spotify import TOKEN_URL, SpotifyClient, login, pkce_pair, spotify_tools  # noqa: E402
import pytest  # noqa: E402

from jarvis.tools import PermissionManager, ToolCore, ToolError, ToolRegistry  # noqa: E402


class FakeSpotify:
    def __init__(self, devices=({"id": "pc", "is_active": True},), premium=True):
        self.calls, self.devices, self.premium, self.tokens = [], list(devices), premium, 0
        self.urls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url.split("?")[0], body))
        self.urls.append(url)
        if url == TOKEN_URL:
            self.tokens += 1
            fields = urllib.parse.parse_qs(body.decode())
            assert fields["client_id"] == ["client"]
            return 200, json.dumps({"access_token": f"jeton{self.tokens}", "refresh_token": "r", "expires_in": 3600}).encode()
        assert headers["Authorization"].startswith("Bearer jeton")
        path = url.split("/v1", 1)[1].split("?")[0]
        if path == "/me/player/devices":
            return 200, json.dumps({"devices": self.devices}).encode()
        if path == "/me/playlists":
            return 200, json.dumps({"items": [{"name": "Chill du soir", "uri": "spotify:playlist:chill"}]}).encode()
        if path == "/search":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            kind = query["type"][0]
            if query["q"][0] == "introuvable":
                return 200, json.dumps({f"{kind}s": {"items": []}}).encode()
            return 200, json.dumps({f"{kind}s": {"items": [{"name": "Back in Black", "uri": "spotify:track:bib",
                                                            "artists": [{"name": "AC/DC"}]}]}}).encode()
        if path.startswith("/me/player"):
            return (200 if self.premium else 403), (b"Q5P97CQHqwLZ3Hnzas9tnmMFEJA" if self.premium else b"")
        return 404, b""


def client(tmp_path, fake, expired=False):
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"access_token": "jeton0", "refresh_token": "r",
                                 "expires_at": 0 if expired else 10 ** 12}), encoding="utf-8")
    return SpotifyClient("client", token, http=fake, clock=lambda: 1000.0)


def core_for(spotify):
    registry = ToolRegistry()
    for tool in spotify_tools(spotify):
        registry.register(tool)
    return ToolCore(registry, PermissionManager())


def run(core, tool, **parameters):
    return core.submit({"tool": tool, "parameters": parameters}).result


def test_play_a_track_a_playlist_and_controls(tmp_path):
    fake = FakeSpotify()
    core = core_for(client(tmp_path, fake))
    assert run(core, "spotify_play", query="Back in Black", kind="track").message == "Je lance Back in Black de AC/DC."
    assert json.loads(fake.calls[-1][2]) == {"uris": ["spotify:track:bib"]}
    assert run(core, "spotify_play", query="chill", kind="playlist").message == "Je lance Chill du soir."
    assert json.loads(fake.calls[-1][2]) == {"context_uri": "spotify:playlist:chill"}
    assert run(core, "spotify_pause").message == "Spotify est en pause."
    assert run(core, "spotify_next").success and run(core, "spotify_volume", volume=30).message.endswith("30 %.")
    assert run(core, "spotify_play").message == "Je reprends la lecture."


def test_errors_are_explained(tmp_path):
    assert "ouvrez Spotify" in run(core_for(client(tmp_path, FakeSpotify(devices=()))), "spotify_play").message
    assert "Premium" in run(core_for(client(tmp_path, FakeSpotify(premium=False))), "spotify_pause").message
    assert "Je ne trouve pas" in run(core_for(client(tmp_path, FakeSpotify())), "spotify_play",
                                      query="introuvable", kind="album").message


def test_expired_token_is_refreshed_and_saved(tmp_path):
    fake = FakeSpotify()
    spotify = client(tmp_path, fake, expired=True)
    run(core_for(spotify), "spotify_pause")
    saved = json.loads((tmp_path / "token.json").read_text(encoding="utf-8"))
    assert fake.tokens == 1 and saved["access_token"] == "jeton1" and saved["refresh_token"] == "r"


def test_login_checks_the_state_and_stores_the_token(tmp_path, monkeypatch):
    import jarvis.spotify as module

    fake = FakeSpotify()
    shown = []
    monkeypatch.setattr(module, "_default_http", fake)
    monkeypatch.setattr(module.SpotifyClient.__init__, "__defaults__", (fake, 6.0, module.time.time, ""))
    state = {}

    def ask(prompt):
        url = shown[0].split("\n\n")[1]
        state.update(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query))
        return f"http://127.0.0.1:8888/callback?code=abc&state={state['state'][0]}"

    assert login("client", tmp_path / "t.json", ask=ask, show=shown.append) == 0
    assert state["code_challenge_method"] == ["S256"] and json.loads((tmp_path / "t.json").read_text())["access_token"]
    assert login("client", tmp_path / "t2.json", ask=lambda p: "http://127.0.0.1:8888/callback?code=x&state=faux",
                 show=lambda m: None) == 1
    assert login("", tmp_path / "t3.json", show=lambda m: None) == 2


def test_pkce_pair_matches_the_specification():
    import base64
    import hashlib

    verifier, challenge = pkce_pair()
    assert 43 <= len(verifier) <= 128
    assert challenge == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def test_computer_is_preferred_when_nothing_plays(tmp_path):
    fake = FakeSpotify(devices=[{"id": "tel", "type": "Smartphone", "is_active": False},
                                {"id": "pc", "type": "Computer", "is_active": False}])
    run(core_for(client(tmp_path, fake)), "spotify_play", query="Back in Black", kind="track")
    play = [u for u in fake.urls if "/me/player/play" in u][-1]
    assert play.endswith("device_id=pc")


def test_search_never_asks_for_extra_scopes(tmp_path):
    fake = FakeSpotify()
    spotify = client(tmp_path, fake)
    spotify.find("Back in Black", "track")
    assert any("/search" in u for u in fake.urls) and all("market" not in u for u in fake.urls)


# --- Filtre de cohérence ---------------------------------------------------------------------------

def test_coherence_filter_picks_the_requested_track_or_nothing():
    from jarvis.spotify import best_match, split_title_artist

    items = [{"name": "Remember The Name (feat. Eminem & 50 Cent)", "artists": [{"name": "Ed Sheeran"}], "uri": "a"},
             {"name": "Without Me", "artists": [{"name": "Eminem"}], "uri": "b"},
             {"name": "Without Me", "artists": [{"name": "Halsey"}], "uri": "c"}]
    assert split_title_artist("without me de Eminem") == ("without me", "Eminem")
    assert split_title_artist("Back in Black d'AC/DC") == ("Back in Black", "AC/DC")
    assert best_match(items, "without me", "Eminem", "track")["uri"] == "b"
    assert best_match(items[:1], "without me", "Eminem", "track") is None  # autre morceau : refusé


def test_words_invented_by_the_llm_are_removed_from_the_search():
    from jarvis.spotify import _said_words
    from jarvis.tools.planner import plan

    assert _said_words("Butterfly Without Me Eminem", "Mais without me de Eminem sur Spotify") == "Without Me Eminem"
    assert _said_words("Butterfly", "Mets without me") is None
    import tempfile

    registry = ToolRegistry()
    for tool in spotify_tools(client(Path(tempfile.mkdtemp()), FakeSpotify())):
        registry.register(tool)

    class LLM:
        def chat_json(self, messages, schema):
            return {"type": "tool_call", "tool": "spotify_play",
                    "parameters": {"query": "Butterfly Without Me Eminem", "kind": "track"}}

    assert plan(LLM(), "Mets without me de Eminem sur Spotify", registry)["parameters"]["query"] == "Without Me Eminem"


def test_nothing_coherent_means_nothing_is_played(tmp_path):
    fake = FakeSpotify()
    result = run(core_for(client(tmp_path, fake)), "spotify_play", query="without me de Eminem", kind="track")
    assert not result.success and result.message == "Je ne trouve pas « without me » de Eminem sur Spotify."
    assert not any("/me/player/play" in u for u in fake.urls)


def test_misheard_mets_plays_the_right_track_and_questions_still_get_answers(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_tools import PlannerLLM, routes, run_agent

    class Eminem(FakeSpotify):
        def __call__(self, method, url, headers, body, timeout):
            if "/search" in url and "without" in urllib.parse.unquote_plus(url).lower():
                self.calls.append((method, url.split("?")[0], body))
                self.urls.append(url)
                return 200, json.dumps({"tracks": {"items": [
                    {"name": "Remember The Name", "uri": "spotify:track:faux", "artists": [{"name": "Ed Sheeran"}]},
                    {"name": "Without Me", "uri": "spotify:track:wm", "artists": [{"name": "Eminem"}]}]}}).encode()
            return super().__call__(method, url, headers, body, timeout)

    fake = Eminem()
    core = core_for(client(tmp_path, fake))
    llm = PlannerLLM(reply="Le ciel est bleu à cause de la diffusion de la lumière.")
    spoken, events = run_agent(["Mais without me de Eminem.", "Mais pourquoi le ciel est bleu de jour ?"], llm, core)
    assert spoken[0] == "Je lance Without Me de Eminem."
    assert json.loads([c for c in fake.calls if c[1].endswith("/me/player/play")][-1][2]) == {"uris": ["spotify:track:wm"]}
    assert spoken[1].startswith("Le ciel est bleu") and "tool:quick (promue)" in routes(events)


# --- Titres étrangers (transcription phonétique) et catalogue --------------------------------------

def test_foreign_titles_heard_in_french_still_match():
    from jarvis.spotify import best_match

    items = [{"name": "Astroboy", "artists": [{"name": "Indochine"}], "uri": "x"},
             {"name": "Du hast", "artists": [{"name": "Rammstein"}], "uri": "dh"},
             {"name": "Dragostea din tei", "artists": [{"name": "O-Zone"}], "uri": "dt"},
             {"name": "Ich will", "artists": [{"name": "Rammstein"}], "uri": "iw"},
             {"name": "Mein Herz brennt", "artists": [{"name": "Rammstein"}], "uri": "mh"}]
    assert best_match(items, "dou ast", "Ramstein", "track")["uri"] == "dh"
    assert best_match(items, "dragosta dine tei", "", "track")["uri"] == "dt"
    assert best_match(items, "ich vil", "", "track")["uri"] == "iw"
    assert best_match(items, "mein herz brent", "rammstein", "track")["uri"] == "mh"
    assert best_match(items, "without me", "Eminem", "track") is None


def test_title_containing_de_is_split_both_ways():
    from jarvis.spotify import title_artist_splits

    assert title_artist_splits("nu ma las de limba noastra de O-Zone") == [
        ("nu ma las de limba noastra", "O-Zone"), ("nu ma las", "limba noastra de O-Zone"),
        ("nu ma las de limba noastra de O-Zone", "")]


def test_catalog_is_built_from_your_playlists_and_searched_first(tmp_path):
    from jarvis.spotify import SpotifyCatalog

    class WithPlaylist(FakeSpotify):
        def __call__(self, method, url, headers, body, timeout):
            if "/playlists/p1/items" in url:
                self.urls.append(url)
                return 200, json.dumps({"items": [{"item": {"name": "Sonne", "uri": "spotify:track:sonne",
                                                            "artists": [{"name": "Rammstein"}]}}], "next": None}).encode()
            if url.split("?")[0].endswith("/me/playlists"):
                return 200, json.dumps({"items": [{"id": "p1", "name": "Metal", "uri": "spotify:playlist:p1"}]}).encode()
            return super().__call__(method, url, headers, body, timeout)

    fake = WithPlaylist()
    spotify = client(tmp_path, fake)
    spotify.catalog = SpotifyCatalog(tmp_path / "catalog.json")
    assert spotify.catalog.stale and spotify.catalog.refresh(spotify) == 1
    assert SpotifyCatalog(tmp_path / "catalog.json").artists() == ["Rammstein"]
    assert spotify.find("sone de ramstein", "track") == ("spotify:track:sonne", "Sonne de Rammstein")
    assert not any("/search" in u for u in fake.urls)  # trouvé dans vos titres, sans recherche


def test_quick_music_commands_for_foreign_titles(tmp_path):
    from jarvis.tools.quick import quick_plan

    registry = ToolRegistry()
    for tool in spotify_tools(client(tmp_path, FakeSpotify())):
        registry.register(tool)
    play = lambda text: quick_plan(text, registry)["parameters"]  # noqa: E731
    assert play("Mets Du hast de Rammstein") == {"query": "Du hast de Rammstein", "kind": "track"}
    assert play("Mets du Rammstein") == {"query": "Rammstein", "kind": "artist"}
    assert play("Joue Sonne de Ramstein sur Spotify") == {"query": "Sonne de Ramstein", "kind": "track"}
    assert play("Mets Nu ma las de limba noastra d'O-Zone")["query"] == "Nu ma las de limba noastra d'O-Zone"


def test_close_sounds_but_different_words_are_rejected():
    from jarvis.phonetic import coverage

    assert coverage("mein herz brent", "Rein raus") == 0.0
    assert coverage("dou ast", "Mon cœur bat vite") < 0.75
    assert coverage("dragosta dine tei", "The Dirt") < 0.75  # sous le seuil sans artiste


def test_spotify_volume_and_play_spotify_are_not_confused_with_the_pc(tmp_path):
    from jarvis.tools.quick import quick_plan
    from test_tools import make_core

    core = make_core()
    for tool in spotify_tools(client(tmp_path, FakeSpotify())):
        core.registry.register(tool)
    assert quick_plan("Mets le volume de Spotify à 30 %.", core.registry)["tool"] == "spotify_volume"
    assert quick_plan("Mets le volume à 30 %.", core.registry)["tool"] == "set_volume"
    assert quick_plan("Joue Spotify.", core.registry) == {"type": "tool_call", "tool": "spotify_play", "parameters": {}}
    assert quick_plan("Ouvre Spotify", core.registry)["tool"] == "open_application"


# --- Erreurs de l'API (session red team) -----------------------------------------------------------

def scripted(tmp_path, statuses):
    """Client dont les appels à l'API répondent successivement ``statuses`` (le jeton, lui, se renouvelle)."""
    sequence = iter(statuses)
    calls = []

    def http(method, url, headers, body, timeout):
        if url == TOKEN_URL:
            calls.append("jeton")
            return 200, json.dumps({"access_token": "jeton9", "refresh_token": "r", "expires_in": 3600}).encode()
        calls.append(url.split("/v1")[-1].split("?")[0])
        return next(sequence), b""

    spotify = client(tmp_path, http)
    spotify._sleep = lambda seconds: calls.append(f"pause {seconds}")
    return spotify, calls


def test_rate_limit_server_errors_and_revoked_token_are_retried_once(tmp_path):
    spotify, calls = scripted(tmp_path, [429, 204])
    assert spotify.call("PUT", "/me/player/pause") == {} and "pause 1.0" in calls
    spotify, calls = scripted(tmp_path, [503, 200])
    spotify.call("PUT", "/me/player/pause")
    spotify, calls = scripted(tmp_path, [401, 204])
    spotify.call("PUT", "/me/player/pause")
    assert calls.count("jeton") == 1  # jeton renouvelé après le refus


def test_persistent_api_errors_give_a_clear_message(tmp_path):
    for statuses, words in (([429, 429], "limite"), ([401, 401], "spotify-login"), ([500, 500], "pas pu")):
        spotify, _ = scripted(tmp_path, statuses)
        with pytest.raises(ToolError) as error:
            spotify.call("PUT", "/me/player/pause")
        assert words in error.value.message


def test_slow_or_unreachable_spotify_is_reported(tmp_path):
    def http(method, url, headers, body, timeout):
        raise TimeoutError("trop lent")

    with pytest.raises(ToolError) as error:
        client(tmp_path, http).call("GET", "/me/player")
    assert error.value.message == "Spotify ne répond pas."


def test_track_with_a_volume_sets_the_spotify_volume_not_the_pc(tmp_path):
    from jarvis.tools.quick import quick_plan
    from test_tools import make_core

    core = make_core()
    for tool in spotify_tools(client(tmp_path, FakeSpotify())):
        core.registry.register(tool)
    data = quick_plan("Mets Back in Black de AC-DC en volume 30.", core.registry)
    assert [(c["tool"], c["parameters"]) for c in data["calls"]] == [
        ("spotify_play", {"query": "Back in Black de AC-DC", "kind": "track"}), ("spotify_volume", {"volume": 30})]
    assert quick_plan("Mets le volume à 30 %.", core.registry)["tool"] == "set_volume"  # sans musique : le PC


def test_volume_right_after_music_targets_spotify(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_tools import PlannerLLM, make_core, run_agent

    fake = FakeSpotify()
    core = make_core()
    for tool in spotify_tools(client(tmp_path, fake)):
        core.registry.register(tool)
    spoken, _ = run_agent(["Mets Back in Black de AC-DC.", "Mets le volume à 30 %."], PlannerLLM(), core, fast_path=True)
    assert spoken[1] == "Volume de Spotify à 30 %." and any("volume_percent=30" in u for u in fake.urls)
