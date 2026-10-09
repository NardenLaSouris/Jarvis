"""Filtre de cohérence des transcriptions : commandes mal entendues corrigées, phrases ordinaires intactes."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.config import load_config  # noqa: E402
from jarvis.factory import build_corrector, vocabulary_hint  # noqa: E402
from jarvis.personality import load_personality  # noqa: E402
from jarvis.stt.correction import sound_key  # noqa: E402
from jarvis.stt.faster_whisper import FasterWhisperSTT  # noqa: E402

CFG = load_config(ROOT / "config.toml", local=False)
CORRECTOR = build_corrector(CFG, load_personality(ROOT / "personality.toml"))

MISHEARD = {
    "ou vos teams": "ouvre steam",
    "Tite Steam.": "quitte steam",
    "Jarvis, ouvre-ste-iem.": "ouvre steam",
    "Ferme Discorde.": "ferme discord",
    "Ouvre crom.": "ouvre chrome",
    "Lance stime.": "lance steam",
    "fer me steam": "fermer steam",
    "Ouvre dis corde.": "ouvre discord",
    "Ferme le blog note.": "ferme notepad",
    "Où vrai Discord ?": "ouvrir discord",
    "ouvres Steam": "ouvre steam",
    "Quitte discorde s'il te plaît": "quitte discord",
    "Jervis, ou vos teams.": "ouvre steam",
    "Charvis, ouvre crom": "ouvre chrome",
}

ORDINARY = [
    "Ouvre Steam.", "Ferme Discord.", "ouvre le bloc note", "Où vas-tu ce soir ?", "Oui.", "Non merci.",
    "Ferme la porte.", "Ouvre la fenêtre.", "Quelle heure est-il ?", "Comment vas-tu ?", "Il fait beau.",
    "Merci beaucoup.", "Tu es là ?", "C'est bon.", "Bonne nuit.", "Au revoir.", "Lance une recherche.",
    "Ferme les volets.", "Qui es-tu ?", "Ouvre les volets.", "Où est Steve ?", "Ouvre Teams.", "Ouvre Photoshop.",
    "Ferme tout.", "Ouvre YouTube.", "Où vont les tims ?", "Oui vas-y", "Et combien d'habitants ?",
    "Raconte-moi une blague.", "Ouvre le site de la SNCF", "Lance la musique", "Où vous êtes ?", "Ferme ta bouche",
    "Tu dis quoi ?", "Des cordes", "Quelle est la capitale de l'Australie ?",
    "Peux-tu ouvrir Firefox et envoyer un mail à Paul ?",
]


@pytest.mark.parametrize("heard, expected", MISHEARD.items())
def test_misheard_commands_are_corrected(heard, expected):
    assert CORRECTOR.correct(heard) == expected


@pytest.mark.parametrize("text", ORDINARY)
def test_ordinary_sentences_are_left_untouched(text):
    assert CORRECTOR.correct(text) == text


def test_sound_key_brings_spellings_together():
    assert sound_key(["steam"]) == sound_key(["stime"]) == "stim"
    assert sound_key(["discorde"]) == sound_key(["discord"])
    assert sound_key(["chrome"]) == sound_key(["crom"])


def test_no_corrector_without_application_tools():
    from dataclasses import replace

    assert build_corrector(replace(CFG, tools=replace(CFG.tools, enabled=False)),
                           load_personality(ROOT / "personality.toml")) is None


def test_whisper_gets_the_vocabulary_hint(tmp_path):
    from dataclasses import replace as _replace

    global CFG
    real, CFG = CFG, _replace(CFG, spotify=_replace(CFG.spotify, catalog_path=tmp_path / "absent.json"))
    try:
        _vocabulary_checks()
    finally:
        CFG = real


def test_your_spotify_artists_join_the_vocabulary_hint(tmp_path):
    import json
    from dataclasses import replace

    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"updated": 0, "tracks": [
        {"name": "Sonne", "uri": "u1", "artists": [{"name": "Rammstein"}]},
        {"name": "Du hast", "uri": "u2", "artists": [{"name": "Rammstein"}]},
        {"name": "Dragostea din tei", "uri": "u3", "artists": [{"name": "O-Zone"}]}]}), encoding="utf-8")
    cfg = replace(CFG, spotify=replace(CFG.spotify, catalog_path=catalog))
    assert vocabulary_hint(cfg).endswith("Mets Rammstein, O-Zone.")


def _vocabulary_checks():
    assert vocabulary_hint(CFG) == ("Orion, ouvre Discord, ferme Steam, lance Google Chrome, quitte Spotify, "
                                   "ouvre Visual Studio Code, ferme le Bloc-notes. Mets un minuteur. Rappelle-moi. "
                                   f"Recherche-moi. Raconte-moi. Arrête. Tais-toi. Quel temps fera-t-il à {CFG.weather.default_location} ?")
    from dataclasses import replace

    quiet = replace(CFG, web=replace(CFG.web, enabled=False), timers=replace(CFG.timers, enabled=False),
                    weather=replace(CFG.weather, enabled=False))
    assert vocabulary_hint(quiet).endswith("ferme le Bloc-notes. Raconte-moi. Arrête. Tais-toi.")

    class Model:
        def transcribe(self, samples, **options):
            self.options = options
            return iter([]), None

    stt = FasterWhisperSTT.__new__(FasterWhisperSTT)
    stt._model, stt._language, stt._beam_size, stt.hotwords = Model(), "fr", 1, "Jarvis, ouvre Steam."
    stt.vad_filter = False
    stt.transcribe(np.zeros(1600, np.int16), 16000)
    assert stt._model.options["hotwords"] == "Jarvis, ouvre Steam."
    stt.hotwords = ""
    stt.transcribe(np.zeros(1600, np.int16), 16000)
    assert stt._model.options["hotwords"] is None



@pytest.mark.parametrize("heard", ["Quel heure est-il ?", "Quelle heure est-il ?"])
def test_common_grammar_slips_still_route(heard):
    from jarvis.capabilities import CapabilityRegistry
    from jarvis.router import IntentRouter

    router = IntentRouter(load_personality(ROOT / "personality.toml"), CapabilityRegistry(), tools=("get_time",))
    assert router.route(heard).label == "tool:time"


def test_other_command_verbs_are_never_turned_into_open():
    # Session QA : « Joue Spotify » corrigé en « ouvre spotify » ouvrait l'application au lieu de la musique.
    from jarvis.stt.correction import CommandCorrector

    corrector = CommandCorrector({"ouvre": ("ouvre", "lance"), "ferme": ("ferme",)},
                                 {"spotify": "spotify", "steam": "steam", "discord": "discord"})
    for text in ("Joue Spotify", "Mets Spotify", "Relance Spotify", "Coupe Discord"):
        assert corrector.correct(text) == text, text
    assert corrector.correct("ou vos teams") == "ouvre steam"
