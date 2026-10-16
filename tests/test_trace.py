"""Corrélation des demandes (jarvis.trace) et ligne de mesure des latences (jarvis.agent.measure_line)."""

from __future__ import annotations

import logging
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "scripts"))

from orion import trace  # noqa: E402
from orion.agent import Agent, measure_line  # noqa: E402
from orion.tools.base import Risk, Tool  # noqa: E402
from test_tools import SLEEP, PlannerLLM, make_core, run_agent  # noqa: E402


def traced_run(texts, llm, core, monkeypatch):
    seen = []
    original = Agent._event

    def recording(self, kind, text):
        seen.append((kind, trace.current()))
        original(self, kind, text)

    monkeypatch.setattr(Agent, "_event", recording)
    spoken, events = run_agent(texts, llm, core)
    return seen, spoken, events


def test_each_sentence_of_a_conversation_has_its_own_id_under_the_conversation_id(monkeypatch):
    core = make_core()
    in_tool = []
    core.registry.register(Tool("probe", "sonde de test", {}, {}, Risk.SAFE,
                                lambda: in_tool.append(trace.current()) or {}))
    llm = PlannerLLM({"type": "tool_call", "tool": "probe", "parameters": {}},
                     {"type": "tool_call", "tool": "probe", "parameters": {}})
    seen, _, _ = traced_run(["Lance la sonde", "Lance encore la sonde", SLEEP, "Lance la sonde"], llm, core,
                            monkeypatch)
    users = [rid for kind, rid in seen if kind == "user"]
    assert len(users) == 3 and all(users)
    base = users[0].split(".")[0]
    assert users[:2] == [f"{base}.1", f"{base}.2"]  # même conversation, phrases numérotées
    assert users[2].split(".")[0] != base and users[2].endswith(".1")  # nouvelle conversation après la veille
    assert in_tool[:2] == users[:2]  # l'identifiant suit la demande dans le fil de l'outil
    wake = [rid for kind, rid in seen if kind == "wake"]
    assert wake and wake[0] == base  # réveil et vérification : identifiant de la conversation
    measures = [rid for kind, rid in seen if kind == "mesure"]
    assert measures[:2] == users[:2]  # une ligne de mesure par phrase, avec son identifiant
    assert trace.current() == ""  # rien ne reste après la veille


def test_the_id_reaches_remote_services_in_a_header():
    from orion.stt.remote import RemoteSTT

    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            received.append(self.headers.get(trace.HEADER))
            self.rfile.read(int(self.headers["Content-Length"]))
            body = b'{"text": "ok", "confidence": -0.1, "seconds": 0.1}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        stt = RemoteSTT(f"http://127.0.0.1:{httpd.server_address[1]}", "jeton-0123456789abcdef", object(), "fr")
        with trace.request("abc123.2"):
            assert stt.transcribe(np.zeros(1600, np.int16), 16000) == "ok"
        stt.transcribe(np.zeros(1600, np.int16), 16000)  # hors demande : aucun en-tête
        assert received == ["abc123.2", None]
    finally:
        httpd.shutdown()


def test_only_well_formed_ids_from_other_machines_enter_the_logs():
    assert trace.valid("3fa2c1.2") == "3fa2c1.2"
    for bad in ("", None, "x" * 40, "3fa2c1\n[r:faux]", "<script>", "abc def"):
        assert trace.valid(bad) == ""


def test_the_log_format_carries_the_id_and_nothing_else():
    record = logging.LogRecord("x", logging.INFO, "", 0, "[user] Allume la chambre", (), None)
    flt = trace.RequestFilter()
    flt.filter(record)
    assert record.req == ""
    with trace.request("3fa2c1.1"):
        flt.filter(record)
    assert record.req == "[r:3fa2c1.1] " and record.getMessage() == "[user] Allume la chambre"


def test_measure_line_separates_deterministic_and_llm_paths_without_any_text():
    quick = measure_line({"route": "tool:quick", "stt": 0.82, "tool": 0.01, "tts_first": 0.2, "début lecture": 12.5},
                         10.0, "gpu")
    assert quick == "chemin=deterministe route=tool:quick stt=0.820 source=gpu outil=0.010 tts_premier=0.200 total=2.500"
    llm = measure_line({"route": "tool:tool.action", "stt": 0.9, "tool_plan": 2.31, "tool": 0.02}, 10.0)
    assert llm.startswith("chemin=llm route=tool:tool.action stt=0.900 choix=2.310") and "total" not in llm


def test_latency_report_computes_medians_and_p90_per_path():
    import latency_report

    lines = [f"22:00:0{i} INFO    [r:aa.{i}] [mesure] chemin=deterministe route=tool:quick stt=0.{i}00 source=gpu "
             f"total=1.{i}00" for i in range(1, 6)]
    lines += ["22:01:00 INFO    [mesure] chemin=llm route=llm stt=0.900 source=local choix=2.000 total=4.000",
              "22:01:01 INFO    [user] une phrase sans mesure"]
    rows = latency_report.parse(lines)
    assert len(rows) == 6 and rows[-1]["chemin"] == "llm"
    text = latency_report.report(rows)
    assert "chemin deterministe : 5 demandes" in text and "chemin llm : 1 demandes" in text
    assert "stt          n=   5  médiane   0.30 s  p90   0.50 s" in text
    assert "transcription local : 1 demandes" in text
    assert latency_report.report([]).startswith("Aucune ligne [mesure]")
