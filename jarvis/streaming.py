"""Réponse vocale en flux : le LLM, la synthèse et la lecture travaillent en parallèle.

    fragments du LLM -> SentenceBuffer -> file de phrases -> thread TTS -> file audio -> AudioSink

JARVIS commence à parler dès que la première phrase complète est disponible, pendant
que le LLM génère la suite et que le TTS synthétise les phrases suivantes. Les files
garantissent l'ordre : chaque phrase est synthétisée puis jouée une seule fois, l'une
après l'autre, sans superposition.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator

from jarvis.interfaces import AudioSink, TextToSpeech

log = logging.getLogger(__name__)

TERMINATORS = ".!?;…"
ABBREVIATIONS = {"m", "mm", "mme", "mmes", "mlle", "dr", "pr", "st", "ste", "etc", "cf", "ex", "p", "av", "apr", "j", "c"}
MIN_SENTENCE_CHARS = 12
_BOUNDARY = re.compile(rf"[{re.escape(TERMINATORS)}]+[\"'»)\]]*(?=\s)")


class SentenceBuffer:
    """Accumule les fragments du LLM et rend des phrases complètes, jamais des bouts de mots."""

    def __init__(self, min_chars: int = MIN_SENTENCE_CHARS):
        self._text = ""
        self._min_chars = min_chars

    def feed(self, fragment: str) -> list[str]:
        self._text += fragment
        sentences, start = [], 0
        for match in _BOUNDARY.finditer(self._text):
            candidate = self._text[start : match.end()].strip()
            if len(candidate) < self._min_chars or self._is_abbreviation_or_number(match.start()):
                continue
            sentences.append(candidate)
            start = match.end()
        self._text = self._text[start:]
        return sentences

    def flush(self) -> str:
        rest, self._text = self._text.strip(), ""
        return rest

    def _is_abbreviation_or_number(self, position: int) -> bool:
        if self._text[position] != ".":
            return False
        word = re.search(r"(\w+)$", self._text[:position])
        if not word or self._text[:word.start()].endswith("°"):
            return False
        return word.group(1).lower() in ABBREVIATIONS or word.group(1).isdigit()


def split_sentences(text: str) -> list[str]:
    buffer = SentenceBuffer()
    sentences = buffer.feed(text)
    rest = buffer.flush()
    return sentences + ([rest] if rest else [])


def sentences_from_llm(
    fragments: Iterator[str],
    reply_filter,
    marks: dict[str, float],
    done_reason: Callable[[], str] = lambda: "",
) -> Iterator[str]:
    """Transforme le flux de fragments du LLM en phrases filtrées, prêtes à être prononcées.

    ``marks`` reçoit les instants (s depuis l'appel) du premier fragment et de la première phrase.
    """
    started = time.perf_counter()
    buffer = SentenceBuffer()
    try:
        for fragment in fragments:
            marks.setdefault("llm_first_token", time.perf_counter() - started)
            for sentence in buffer.feed(fragment):
                out = reply_filter.accept(sentence)
                if out:
                    marks.setdefault("llm_first_sentence", time.perf_counter() - started)
                    yield out
                if reply_filter.stopped:
                    break
            if reply_filter.stopped:
                break
    finally:
        close = getattr(fragments, "close", None)
        if close:
            close()
    marks["llm_total"] = time.perf_counter() - started
    tail = buffer.flush()
    truncated = done_reason() == "length" and reply_filter.emitted
    if tail and not reply_filter.stopped and not truncated:
        out = reply_filter.accept(tail)
        if out:
            marks.setdefault("llm_first_sentence", time.perf_counter() - started)
            yield out
    for out in reply_filter.finish():
        marks.setdefault("llm_first_sentence", time.perf_counter() - started)
        yield out


@dataclass
class SpeechStats:
    started: float
    first_sentence: float | None = None
    tts_first: float | None = None
    first_audio: float | None = None
    tts_total: float = 0.0
    generation_done: float | None = None
    finished: float | None = None
    audio_seconds: float = 0.0
    sentences: list[str] = field(default_factory=list)

    def since_start(self, instant: float | None) -> float | None:
        return None if instant is None else instant - self.started


class SpeechPipeline:
    """Fait parler JARVIS phrase par phrase, avec synthèse et lecture en parallèle de la génération."""

    def __init__(self, tts: TextToSpeech, sink: AudioSink, stream_audio: bool = False, merge_under: int = 0):
        self._tts = tts
        self._merge_under = merge_under
        self._sink = sink
        self._stream_audio = stream_audio and hasattr(tts, "stream")
        self._cancel = threading.Event()

    def cancel(self) -> None:
        """Interrompt la réponse en cours : génération arrêtée, files vidées, plus rien n'est joué."""
        self._cancel.set()

    def speak(self, sentences: Iterable[str]) -> SpeechStats:
        self._cancel.clear()
        stats = SpeechStats(started=time.perf_counter())
        sentence_queue: queue.Queue = queue.Queue()
        audio_queue: queue.Queue = queue.Queue()
        errors: list[BaseException] = []

        def produce() -> None:
            iterator = iter(sentences)
            try:
                for sentence in iterator:
                    if self._cancel.is_set():
                        break
                    if stats.first_sentence is None:
                        stats.first_sentence = time.perf_counter()
                    sentence_queue.put(sentence)
            except BaseException as exc:
                errors.append(exc)
            finally:
                close = getattr(iterator, "close", None)
                if close:
                    close()
                stats.generation_done = time.perf_counter()
                sentence_queue.put(None)

        def next_text() -> str | None:
            text = sentence_queue.get()
            while text is not None and len(text) < self._merge_under:
                following = sentence_queue.get()
                if following is None:
                    sentence_queue.put(None)
                    break
                text = f"{text} {following}"
            return text

        def synthesize() -> None:
            try:
                while (sentence := next_text()) is not None:
                    if self._cancel.is_set():
                        continue
                    started = time.perf_counter()
                    first = True
                    for audio, rate in self._audio_for(sentence):
                        if stats.tts_first is None:
                            stats.tts_first = time.perf_counter() - started
                        if audio.size and not self._cancel.is_set():
                            audio_queue.put((sentence if first else None, audio, rate))
                            first = False
                    stats.tts_total += time.perf_counter() - started
            except BaseException as exc:
                errors.append(exc)
                self._cancel.set()
                while sentence_queue.get() is not None:
                    pass
            finally:
                audio_queue.put(None)

        workers = [threading.Thread(target=produce, name="llm-phrases", daemon=True),
                   threading.Thread(target=synthesize, name="tts", daemon=True)]
        for worker in workers:
            worker.start()
        while (item := audio_queue.get()) is not None:
            if self._cancel.is_set():
                continue
            sentence, audio, rate = item
            if stats.first_audio is None:
                stats.first_audio = time.perf_counter()
            self._sink.play(audio, rate)
            stats.audio_seconds += len(audio) / rate
            if sentence is not None:
                stats.sentences.append(sentence)
        drain = getattr(self._sink, "drain", None)
        if drain:
            drain()
        for worker in workers:
            worker.join()
        stats.finished = time.perf_counter()
        if errors:
            raise errors[0]
        return stats

    def _audio_for(self, sentence: str) -> Iterator[tuple]:
        if self._stream_audio:
            for chunk in self._tts.stream(sentence):
                yield chunk, self._tts.sample_rate
        else:
            yield self._tts.synthesize(sentence)
