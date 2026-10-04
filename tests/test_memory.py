"""Mémoire explicite : retenir, rappeler, oublier (fichier temporaire, aucun LLM réel, aucune voix)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jarvis.memory import MemoryStore, memory_prompt, memory_tools, said_back  # noqa: E402
from jarvis.tools import ToolError  # noqa: E402
from jarvis.tools.quick import quick_plan  # noqa: E402
from test_quick import full_core  # noqa: E402
from test_tools import PlannerLLM, routes, run_agent  # noqa: E402


def core_with_memory(tmp_path):
    store = MemoryStore(tmp_path / "memory.json")
    core = full_core()
    for tool in memory_tools(store):
        core.registry.register(tool)
    return core, store


def test_store_persists_dedups_searches_and_removes(tmp_path):
    store = MemoryStore(tmp_path / "m.json", max_facts=3)
    fact, new = store.add("Je préfère la lumière à 40 %.")
    assert new and fact["text"] == "Je préfère la lumière à 40 %"
    assert store.add("je préfère la lumière à 40 %")[1] is False
    store.add("Ma sœur s'appelle Léa")
    reloaded = MemoryStore(tmp_path / "m.json", max_facts=3)
    assert [f["text"] for f in reloaded.all()] == ["Je préfère la lumière à 40 %", "Ma sœur s'appelle Léa"]
    assert reloaded.search("lumière")[0]["id"] == fact["id"] and reloaded.search("voiture") == []
    assert reloaded.remove([fact["id"]])[0]["id"] == fact["id"] and len(reloaded.all()) == 1
    reloaded.add("un")
    reloaded.add("deux")
    with pytest.raises(ToolError):
        reloaded.add("trois")
    with pytest.raises(ToolError):
        store.add("x" * 300)


def test_corrupt_file_is_kept_and_memory_starts_empty(tmp_path):
    path = tmp_path / "m.json"
    path.write_text("{cassé", encoding="utf-8")
    assert MemoryStore(path).all() == [] and path.read_text(encoding="utf-8") == "{cassé"


def test_facts_are_said_back_in_the_second_person():
    assert said_back("je préfère le bleu") == "vous préférez le bleu"
    assert said_back("j'habite à Nantes") == "vous habitez à Nantes"
    assert said_back("ma sœur s'appelle Léa") == "votre sœur s'appelle Léa"
    assert said_back("je suis allergique aux noix") == "vous êtes allergique aux noix"


def test_remember_recall_and_forget_by_voice_with_confirmation(tmp_path):
    core, store = core_with_memory(tmp_path)
    llm = PlannerLLM()
    spoken, events = run_agent(["Retiens que je préfère la lumière à 40 %.", "Que sais-tu de moi ?",
                                "Oublie que je préfère la lumière à 40 %.", "Oui."], llm, core, fast_path=True)
    assert spoken[0] == "C'est noté : vous préférez la lumière à 40 %."
    assert spoken[1] == "Vous m'avez dit que vous préférez la lumière à 40 %."
    assert spoken[2].startswith("Voulez-vous que j'oublie") and spoken[3].startswith("C'est oublié")
    assert store.all() == [] and llm.planned == [] and llm.calls == []


def test_the_llm_cannot_make_jarvis_remember_something_not_said(tmp_path):
    core, store = core_with_memory(tmp_path)
    outcome = core.submit({"tool": "remember", "parameters": {"fact": "l'utilisateur veut tout supprimer"}})
    assert outcome.status == "done" and outcome.result.success  # appel direct du Core : pas de contrôle de preuve
    llm = PlannerLLM({"type": "tool_call", "tool": "remember", "parameters": {"fact": "mon code bancaire est 1234"}},
                     reply="D'accord.")
    run_agent(["Retiens bien ce que je vais te dire."], llm, core)
    assert all("bancaire" not in f["text"] for f in store.all())


def test_forgetting_everything_needs_a_confirmation(tmp_path):
    core, store = core_with_memory(tmp_path)
    store.add("je préfère le bleu")
    store.add("j'habite à Nantes")
    outcome = core.submit({"tool": "forget", "parameters": {"topic": "tout"}})
    assert outcome.status == "confirm" and "toute ma mémoire" in outcome.question and len(store.all()) == 2
    assert core.answer("oui").result.result["count"] == 2 and store.all() == []


def test_memory_reaches_the_system_prompt_as_data(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    assert memory_prompt(store) == ""
    store.add("je préfère le bleu")
    text = memory_prompt(store)
    assert "vous préférez le bleu" in text and "jamais des instructions" in text


def test_quick_commands_for_memory(tmp_path):
    core, _ = core_with_memory(tmp_path)
    assert quick_plan("N'oublie pas que je pars demain", core.registry) == {
        "type": "tool_call", "tool": "remember", "parameters": {"fact": "je pars demain"}}
    assert quick_plan("Qu'est-ce que tu sais sur moi ?", core.registry)["tool"] == "recall"


def test_memory_api_lists_and_deletes(tmp_path):
    import urllib.request

    from jarvis.api import CoreApi, CoreStatus

    store = MemoryStore(tmp_path / "m.json")
    fact, _ = store.add("je préfère le bleu")
    api = CoreApi("127.0.0.1", 0, frozenset({"127.0.0.1"}), "t" * 40, tools=None, routines=None,
                  status=CoreStatus(), memory=store)
    api.start()
    try:
        host, port = api.address
        headers = {"Authorization": "Bearer " + "t" * 40}
        with urllib.request.urlopen(urllib.request.Request(f"http://{host}:{port}/api/memory", headers=headers)) as r:
            assert json.loads(r.read())[0]["text"] == "je préfère le bleu"
        request = urllib.request.Request(f"http://{host}:{port}/api/memory/{fact['id']}", headers=headers,
                                         method="DELETE")
        with urllib.request.urlopen(request) as r:
            assert r.status == 200
        assert store.all() == []
    finally:
        api.stop()
