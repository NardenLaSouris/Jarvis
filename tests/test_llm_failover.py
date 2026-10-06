"""LLM principal distant, LLM local en secours (LLM factices, aucune connexion réelle)."""

from __future__ import annotations

import logging
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.config import load_config  # noqa: E402
from jarvis.factory import build_llm  # noqa: E402
from jarvis.interfaces import Message  # noqa: E402
from jarvis.llm.failover import DEGRADED, OFFLINE, ONLINE, FailoverLLM, LLMUnavailable, tcp_reachable  # noqa: E402
from jarvis.llm.ollama import LLMError, LLMTimeout, LLMUnreachable, OllamaLLM  # noqa: E402

HELLO = [Message("user", "Bonjour")]


class FakeLLM:
    def __init__(self, name, error=None, fail_after=None):
        self.name, self.error, self.fail_after = name, error, fail_after
        self.calls, self.primed, self.warmed = [], [], 0
        self.last_stats = {"modèle": name}

    def chat(self, messages):
        self.calls.append("chat")
        if self.error:
            raise self.error
        return f"réponse de {self.name}"

    def chat_json(self, messages, schema):
        self.calls.append("json")
        if self.error:
            raise self.error
        return {"type": "none", "par": self.name}

    def stream(self, messages):
        self.calls.append("stream")
        for i, piece in enumerate((f"{self.name} ", "parle.")):
            if self.error and (self.fail_after is None or i >= self.fail_after):
                raise self.error
            yield piece

    def warm_up(self):
        self.warmed += 1

    def prime(self, prompts):
        self.primed.append(len(prompts))


def failover(primary, fallback, up=True, clock=None, **options):
    state = {"up": up, "t": 0.0, "checks": 0}

    def reachable(url):
        state["checks"] += 1
        return state["up"]

    llm = FailoverLLM(primary, fallback, "http://katana:11434", retry_after=30, reachable=reachable,
                      clock=clock or (lambda: state["t"]), probe_interval=0, **options)
    return llm, state


def test_primary_answers_when_reachable():
    primary, local = FakeLLM("katana"), FakeLLM("local")
    llm, _ = failover(primary, local)
    assert llm.chat(HELLO) == "réponse de katana" and llm.chat_json(HELLO, {})["par"] == "katana"
    assert "".join(llm.stream(HELLO)) == "katana parle." and local.calls == []
    assert llm.last_stats == {"modèle": "katana"}


def test_unreachable_primary_switches_to_local_and_is_retried_later(caplog):
    primary, local = FakeLLM("katana"), FakeLLM("local")
    llm, state = failover(primary, local, up=False)
    with caplog.at_level(logging.INFO):
        assert llm.chat(HELLO) == "réponse de local"
        assert "".join(llm.stream(HELLO)) == "local parle."
        assert state["checks"] == 1 and primary.calls == [] and llm.last_stats == {"modèle": "local"}
        state["up"], state["t"] = True, 31.0
        assert llm.chat(HELLO) == "réponse de katana"
    messages = [r.message for r in caplog.records]
    assert sum("indisponible" in m for m in messages) == 1 and any("de retour" in m for m in messages)


def test_primary_error_falls_back_for_the_same_request():
    primary, local = FakeLLM("katana", error=LLMError("modèle introuvable")), FakeLLM("local")
    llm, _ = failover(primary, local, json_fallback=True)
    assert llm.chat_json(HELLO, {})["par"] == "local"
    assert llm.chat(HELLO) == "réponse de local" and primary.calls == ["json"]


def test_tool_choice_is_never_sent_to_the_slow_local_llm_by_default():
    primary, local = FakeLLM("katana"), FakeLLM("local")
    llm, state = failover(primary, local, up=False)
    with pytest.raises(LLMUnavailable):
        llm.chat_json(HELLO, {})
    assert local.calls == [] and llm.degraded


def test_worker_states_errors_and_latency_metrics():
    primary, local = FakeLLM("katana"), FakeLLM("local")
    clock = {"t": 0.0}
    llm, state = failover(primary, local, clock=lambda: clock["t"], slow_after=8.0)
    assert llm.state == ONLINE and not llm.degraded
    llm.chat(HELLO)
    assert llm.status()["ok"] == 1 and llm.status()["last_latency_s"] == 0.0
    primary.error = LLMTimeout("trop long")
    assert llm.chat(HELLO) == "réponse de local"
    assert llm.state == DEGRADED and llm.degraded and llm.status()["errors"]["timeout"] == 1
    state["up"] = False
    clock["t"] = 40.0
    llm.chat(HELLO)
    assert llm.state == OFFLINE and llm.status()["errors"]["unreachable"] == 1
    assert llm.status()["last_error_kind"] == "unreachable" and llm.status()["fallback"]["ok"] == 2


def test_slow_answers_mark_the_worker_degraded():
    clock = {"t": 0.0}

    class Slow(FakeLLM):
        def chat(self, messages):
            clock["t"] += 12.0
            return super().chat(messages)

    llm, _ = failover(Slow("katana"), FakeLLM("local"), clock=lambda: clock["t"], slow_after=8.0)
    llm.chat(HELLO)
    assert llm.state == DEGRADED and not llm.degraded  # lent mais utilisable : toujours sollicité


def test_probe_reconnects_as_soon_as_the_worker_is_back(caplog):
    primary, local = FakeLLM("katana"), FakeLLM("local")
    llm, state = failover(primary, local, up=False)
    assert llm.probe() == OFFLINE and llm.degraded
    state["up"] = True
    with caplog.at_level(logging.INFO):
        assert llm.probe() == ONLINE
    assert not llm.degraded and llm.chat(HELLO) == "réponse de katana"
    assert any("OFFLINE -> ONLINE" in r.message for r in caplog.records)


def test_degraded_conversation_uses_the_short_prompt():
    seen = []

    class Recorder(FakeLLM):
        def stream(self, messages):
            seen.append(messages)
            yield from super().stream(messages)

    llm, _ = failover(FakeLLM("katana"), Recorder("local"), up=False, compact_system=lambda: "PROMPT COURT")
    long_prompt = [Message("system", "x" * 8000), Message("user", "Bonjour")]
    assert "".join(llm.stream(long_prompt)) == "local parle."
    assert seen[0][0].content == "PROMPT COURT" and seen[0][1].content == "Bonjour"


def test_ollama_errors_are_classified():
    import socket
    import urllib.error

    from jarvis.llm.ollama import _network_error

    assert isinstance(_network_error("u", urllib.error.URLError(ConnectionRefusedError())), LLMUnreachable)
    assert isinstance(_network_error("u", urllib.error.URLError(socket.timeout())), LLMTimeout)
    assert isinstance(_network_error("u", TimeoutError()), LLMTimeout)


def test_unreachable_ollama_fails_fast():
    import socket
    import time

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    started = time.perf_counter()
    with pytest.raises(LLMUnreachable):
        OllamaLLM(f"http://127.0.0.1:{port}", "m", timeout=5).chat(HELLO)
    assert time.perf_counter() - started < 3


def test_stream_falls_back_only_before_the_first_words():
    llm, _ = failover(FakeLLM("katana", error=LLMError("coupé"), fail_after=0), FakeLLM("local"))
    assert "".join(llm.stream(HELLO)) == "local parle."
    llm, _ = failover(FakeLLM("katana", error=LLMError("coupé"), fail_after=1), FakeLLM("local"))
    with pytest.raises(LLMError):
        "".join(llm.stream(HELLO))


def test_warm_up_and_prime_prepare_both_but_skip_an_unreachable_primary():
    primary, local = FakeLLM("katana"), FakeLLM("local")
    llm, state = failover(primary, local)
    llm.warm_up()
    llm.prime([HELLO, HELLO])
    assert (primary.warmed, local.warmed, primary.primed, local.primed) == (1, 1, [2], [2])
    state["up"] = False
    llm.prime([HELLO])
    assert primary.primed == [2] and local.primed == [2, 1]


def test_local_llm_only_primes_the_short_prompt():
    primary, local = FakeLLM("katana"), FakeLLM("local")
    llm, _ = failover(primary, local, compact_system=lambda: "court")
    llm.prime([HELLO, HELLO, HELLO])
    assert primary.primed == [3] and local.primed == [1]


def test_configuration_builds_the_failover_only_when_asked():
    cfg = load_config(ROOT / "config.toml", local=False)
    assert isinstance(build_llm(cfg), OllamaLLM)
    remote = replace(cfg, llm=replace(cfg.llm, host="http://192.168.1.73:11434", model="qwen2.5:7b",
                                      fallback_host="http://127.0.0.1:11434", fallback_model="qwen2.5:3b"))
    llm = build_llm(remote)
    assert isinstance(llm, FailoverLLM)
    assert (llm._primary._model, llm._fallback._model, llm._fallback._url) == ("qwen2.5:7b", "qwen2.5:3b",
                                                                               "http://127.0.0.1:11434")


def test_tcp_reachable_on_a_closed_port():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert not tcp_reachable(f"http://127.0.0.1:{port}", timeout=0.5)


def test_worker_back_online_is_warmed_up_again_in_background():
    import time as clock_time

    primary, local = FakeLLM("katana"), FakeLLM("local")
    llm, state = failover(primary, local)
    llm.prime([HELLO, HELLO])
    state["up"] = False
    llm.probe()
    state["up"] = True
    llm.probe()
    for _ in range(50):
        if primary.warmed and len(primary.primed) == 2:
            break
        clock_time.sleep(0.02)
    assert primary.warmed == 1 and primary.primed == [2, 2]  # modèle et prompts rechargés au retour


class FrozenLLM(FakeLLM):
    """Worker qui accepte la demande mais ne répond jamais (Ollama gelé)."""

    def __init__(self, name):
        super().__init__(name)
        self.release = threading.Event()

    def stream(self, messages):
        self.calls.append("stream")
        self.release.wait(5)
        yield "trop tard"

    def chat_json(self, messages, schema):
        self.calls.append("json")
        self.release.wait(5)
        return {"type": "none"}


def test_a_frozen_worker_falls_back_after_the_first_token_timeout():
    # Session QA : worker gelé (connexion acceptée, aucune réponse) : 23 s de silence avant le secours.
    frozen = FrozenLLM("katana")
    llm, _ = failover(frozen, FakeLLM("local"), first_token_timeout=0.3, json_fallback=True)
    started = time.monotonic()
    assert "".join(llm.stream(HELLO)) == "local parle."
    assert time.monotonic() - started < 2 and llm.state == "DEGRADED" and llm.stats.errors["timeout"] == 1
    frozen.release.set()


def test_a_frozen_worker_does_not_block_the_tool_choice():
    frozen = FrozenLLM("katana")
    llm, _ = failover(frozen, FakeLLM("local"), first_token_timeout=0.3)
    started = time.monotonic()
    with pytest.raises(Exception):
        llm.chat_json(HELLO, {})
    assert time.monotonic() - started < 2 and llm.stats.errors["timeout"] == 1
    frozen.release.set()


def test_bounded_stream_keeps_all_pieces_and_errors():
    llm, _ = failover(FakeLLM("katana"), FakeLLM("local"), first_token_timeout=1)
    assert "".join(llm.stream(HELLO)) == "katana parle."
    llm, _ = failover(FakeLLM("katana", error=LLMError("coupé"), fail_after=1), FakeLLM("local"), first_token_timeout=1)
    with pytest.raises(LLMError):
        "".join(llm.stream(HELLO))


def test_a_frozen_worker_is_announced_before_the_slower_fallback():
    # Session QA : 22 s de silence (10 s d'attente, puis le secours sur CPU) ; une phrase d'attente aussitôt.
    frozen = FrozenLLM("katana")
    llm, _ = failover(frozen, FakeLLM("local"), first_token_timeout=0.3, waiting_notice="Un instant, monsieur.")
    pieces = list(llm.stream(HELLO))
    assert pieces[0] == "Un instant, monsieur. " and "".join(pieces[1:]) == "local parle."
    frozen.release.set()
    unreachable, _ = failover(FakeLLM("katana"), FakeLLM("local"), up=False, waiting_notice="Un instant, monsieur.")
    assert "".join(unreachable.stream(HELLO)) == "local parle."  # worker éteint : secours immédiat, rien à annoncer
