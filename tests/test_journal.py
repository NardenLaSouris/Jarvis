"""Journal des échanges (jarvis.journal) : enregistrement, recherche plein texte, conservation, secrets écartés."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from orion.journal import Journal, journal_tools  # noqa: E402

NOW = datetime(2026, 10, 15, 18, 0)


def make(tmp_path, now):
    return Journal(tmp_path / "journal.sqlite", days=90, clock=lambda: now[0])


def test_exchanges_are_found_by_their_words_accents_ignored(tmp_path):
    now = [NOW - timedelta(days=1)]
    journal = make(tmp_path, now)
    assert journal.add("message", "Le garage ferme à 19 h, pense à récupérer la voiture")
    assert journal.add("message_reply", "C'est noté.")
    now[0] = NOW
    assert journal.add("user", "Allume l'éclairage du salon")
    assert not journal.add("listening", "Je vous écoute")  # pas un échange
    found = journal.search("voiture garage")
    assert [r["channel"] for r in found] == ["message"] and found[0]["author"] == "vous"
    assert journal.search("eclairage")[0]["channel"] == "voix"  # accents ignorés
    assert journal.search("voit")  # début de mot
    assert journal.search("de la") == []  # mots trop courts : rien plutôt que tout


def test_a_secret_is_never_written(tmp_path):
    journal = make(tmp_path, [NOW])
    assert not journal.add("message", "Mon mot de passe du Wi-Fi est valeur-fictive")
    assert journal.count() == 0


def test_old_exchanges_are_purged_and_searches_can_be_bounded(tmp_path):
    now = [NOW - timedelta(days=100)]
    journal = make(tmp_path, now)
    journal.add("message", "vieux sujet de la piscine")
    now[0] = NOW - timedelta(days=10)
    journal.add("message", "sujet récent de la piscine")
    now[0] = NOW
    assert journal.purge() == 1 and journal.count() == 1
    assert journal.search("piscine", days=7) == [] and len(journal.search("piscine", days=30)) == 1


def test_search_tool_says_what_was_found(tmp_path):
    now = [NOW - timedelta(days=1)]
    journal = make(tmp_path, now)
    journal.add("message", "Le garage ferme à 19 h")
    now[0] = NOW
    tool = journal_tools(journal)[0]
    result = tool.execute({"query": "garage"})
    assert tool.say(result) == "Voici ce que je retrouve : hier à 18 h 00, vous (message) : « Le garage ferme à 19 h »."
    assert tool.say(tool.execute({"query": "bateau"})) == \
        "Je ne retrouve rien sur « bateau » dans nos derniers échanges."


def test_the_journal_survives_a_restart(tmp_path):
    journal = make(tmp_path, [NOW])
    journal.add("user", "Range la cave samedi")
    journal.close()
    assert make(tmp_path, [NOW]).search("cave")[0]["text"] == "Range la cave samedi"
