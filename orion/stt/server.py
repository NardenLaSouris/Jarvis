"""Service de transcription d'ORION sur une machine à GPU (le Katana) : faster-whisper sur CUDA, appelé par le Core
(jarvis.stt.remote) qui garde son Whisper local en secours.

Fichier autonome (seulement faster-whisper et numpy) : il est copié tel quel sur la machine du GPU.
    python server.py --model large-v3-turbo --port 8766 --token-file token.txt [--models DOSSIER]

- ``POST /transcribe`` (JSON) : {"audio": PCM int16 mono 16 kHz en base64, "language", "hotwords", "beam_size",
  "temperature_fallback", "vad_filter"} -> {"text", "confidence", "seconds"} ;
- ``GET /health`` -> {"model", "device"}.
Jeton obligatoire (``Authorization: Bearer``), réseau local seulement, une transcription à la fois.
Mêmes règles que jarvis.stt.faster_whisper : segments de « non-parole » écartés, confiance = avg_logprob moyen.
"""

from __future__ import annotations

import argparse
import base64
import hmac
import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

log = logging.getLogger("orion-stt")
NO_SPEECH_THRESHOLD = 0.6
FALLBACK_TEMPERATURES = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
MAX_BODY = 4 * 1024 * 1024  # environ 60 s d'audio en base64 : bien au-delà d'une demande (15 s au plus)


def _cuda_libraries() -> None:
    """DLL CUDA des paquets pip nvidia-cublas-cu12 et nvidia-cudnn-cu12 (Windows)."""
    for name in ("cublas", "cudnn"):
        folder = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia" / name / "bin"
        if folder.exists():
            os.add_dll_directory(str(folder))
            os.environ["PATH"] = str(folder) + os.pathsep + os.environ.get("PATH", "")


class Transcriber:
    def __init__(self, model: str, models_dir: str | None, compute_type: str = "int8_float16"):
        _cuda_libraries()
        from faster_whisper import WhisperModel

        started = time.perf_counter()
        self.name = model
        self._model = WhisperModel(model, device="cuda", compute_type=compute_type, download_root=models_dir)
        self._lock = threading.Lock()
        self.transcribe({"audio": "", "language": "fr"})  # premier calcul (lent) fait au démarrage
        log.info("Whisper %s prêt sur le GPU (%.1f s)", model, time.perf_counter() - started)

    def transcribe(self, request: dict) -> dict:
        pcm = base64.b64decode(request.get("audio") or "")
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0 if pcm else np.zeros(16000, np.float32)
        started = time.perf_counter()
        with self._lock:
            segments, _ = self._model.transcribe(
                audio, language=str(request.get("language") or "fr"), beam_size=int(request.get("beam_size") or 1),
                condition_on_previous_text=False, vad_filter=bool(request.get("vad_filter")),
                hotwords=str(request.get("hotwords") or "")[:2000] or None,
                temperature=FALLBACK_TEMPERATURES if request.get("temperature_fallback", True) else 0.0)
            kept = [s for s in segments if s.no_speech_prob < NO_SPEECH_THRESHOLD]
        return {"text": " ".join(s.text.strip() for s in kept).strip(),
                "confidence": sum(s.avg_logprob for s in kept) / len(kept) if kept else None,
                "seconds": round(time.perf_counter() - started, 3)}


def handler(transcriber: Transcriber, token: str):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status: int, data: dict) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            given = self.headers.get("Authorization", "")
            return bool(token) and hmac.compare_digest(given.encode(), f"Bearer {token}".encode())

        def do_GET(self) -> None:  # noqa: N802
            if not self._authorized():
                return self._reply(401, {"error": "unauthorized"})
            if self.path != "/health":
                return self._reply(404, {"error": "not_found"})
            self._reply(200, {"model": transcriber.name, "device": "cuda"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                return self._reply(401, {"error": "unauthorized"})
            if self.path != "/transcribe":
                return self._reply(404, {"error": "not_found"})
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_BODY:
                return self._reply(413, {"error": "too_large"})
            try:
                request = json.loads(self.rfile.read(length))
                result = transcriber.transcribe(request)
                # Identifiant de la demande d'ORION (en-tête X-Orion-Request), gardé seulement s'il en a la forme ;
                # jamais le texte transcrit.
                req = (self.headers.get("X-Orion-Request") or "").strip()
                req = req if 0 < len(req) <= 16 and all(c in "0123456789abcdef." for c in req) else "-"
                log.info("[r:%s] transcription en %.2f s (%d caractères)", req, result["seconds"], len(result["text"]))
                self._reply(200, result)
            except Exception as exc:  # noqa: BLE001 - le Core se replie sur son Whisper local
                log.exception("Transcription impossible")
                self._reply(500, {"error": type(exc).__name__})

        def log_message(self, *args) -> None:  # pas de journal par requête (ni contenu ni adresse)
            pass

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Service de transcription d'ORION (GPU)")
    parser.add_argument("--model", default="large-v3-turbo")
    parser.add_argument("--compute-type", default="int8_float16")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--models", default=None)
    parser.add_argument("--log", default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        filename=args.log, encoding="utf-8")
    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    if len(token) < 16:
        raise SystemExit("Jeton absent ou trop court")
    transcriber = Transcriber(args.model, args.models, args.compute_type)
    server = ThreadingHTTPServer((args.host, args.port), handler(transcriber, token))
    log.info("Service de transcription à l'écoute sur le port %d", args.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
