"""Ce qu'ORION apprend à l'usage, gardé dans ``data/learned.json``.

Vocabulaire (outils ``learn_word``, ``forget_word``, ``list_learned``) :
- « Quand je dis la lampe du fond, je veux dire la chambre » : la phrase entendue (ou écrite) est réécrite avant
  d'être comprise, sans tenir compte des accents ni des majuscules ;
- « Retiens le mot Katana » : le mot est donné à Whisper comme vocabulaire attendu (mieux transcrit).
Jamais de réécriture vers ou depuis un « oui », un « non » ou le mot de réveil : une confirmation ne peut pas être
détournée par un mot appris.

Mot de réveil :
- faux réveil signalé (« ce n'était pas pour toi ») : seuil relevé de WAKE_STEP (au plus WAKE_MAX_RAISE au-dessus
  du réglage), ce qu'avait entendu la vérification devient une phrase refusée, et un réveil franc accepté sans
  vérification passe désormais toujours par elle ;
- réveils sans suite (SILENT_LIMIT en SILENT_WINDOW) : seuil relevé de SILENT_STEP ;
- sans faux réveil, le relèvement redescend de WAKE_DECAY par jour.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Callable

from jarvis.personality import normalize
from jarvis.tools.base import Param, Risk, Tool, ToolError

log = logging.getLogger(__name__)

WAKE_STEP = 0.03
WAKE_MAX_RAISE = 0.10
WAKE_CEILING = 0.95
WAKE_DECAY = 0.01  # par jour sans faux réveil
SILENT_WINDOW = 3600.0
SILENT_LIMIT = 3
SILENT_STEP = 0.01
MAX_WORDS = 100
MAX_BLOCKED = 50
LEARNING_REFUSED = "learning_refused"
LEARNING_NOT_FOUND = "learning_not_found"

ACCENTS = {"a": "aàâä", "e": "eéèêë", "i": "iîï", "o": "oôö", "u": "uùûü", "c": "cç", "y": "yÿ"}


def _pattern(phrase: str) -> re.Pattern:
    """Phrase cherchée comme mots entiers, sans tenir compte des accents, majuscules, apostrophes ou tirets."""
    parts = []
    for char in normalize(phrase):
        if char == " ":
            parts.append(r"[\s'’-]+")
        elif char in ACCENTS:
            parts.append(f"[{ACCENTS[char]}]")
        else:
            parts.append(re.escape(char))
    return re.compile(r"(?<!\w)" + "".join(parts) + r"(?!\w)", re.IGNORECASE)


class Learning:
    def __init__(self, path: Path, protected: tuple[str, ...] = (), names: tuple[str, ...] = (),
                 clock: Callable[[], float] = time.time):
        self.path = Path(path)
        # Réponses de confirmation et noms d'ORION : jamais appris ni réécrits (une confirmation non détournable).
        self._protected = {normalize(p) for p in protected if normalize(p)}
        self._core = {"oui", "non"} | {normalize(n) for n in names if normalize(n)}
        self._clock = clock
        self._lock = threading.Lock()
        self.on_change: Callable[[], None] | None = None  # vocabulaire modifié (indices de Whisper à refaire)
        self._data = self._load()

    # --- Stockage ------------------------------------------------------------------------------------------

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError) as exc:
            log.warning("Apprentissages illisibles (%s) : on repart de zéro", type(exc).__name__)
            data = {}
        data.setdefault("vocabulary", {})
        data.setdefault("words", [])
        wake = data.setdefault("wake", {})
        wake.setdefault("raise", 0.0)
        wake.setdefault("raised_at", 0.0)
        wake.setdefault("blocked", [])
        wake.setdefault("always_verify", False)
        wake.setdefault("silent", [])
        wake.setdefault("false_wakes", 0)
        return data

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            log.warning("Apprentissages non enregistrés : %s", exc)

    # --- Vocabulaire ---------------------------------------------------------------------------------------

    def _check(self, phrase: str) -> str:
        norm = normalize(phrase)
        if len(norm) < 3 or len(norm) > 60:
            raise ToolError(LEARNING_REFUSED, "Ce mot est trop court ou trop long pour que je l'apprenne.")
        if norm in self._protected or set(norm.split()) & self._core:
            raise ToolError(LEARNING_REFUSED, "Je n'apprends pas de sens nouveau pour « oui », « non » ou mon nom.")
        return norm

    def learn(self, heard: str, meaning: str = "") -> dict:
        heard, meaning = heard.strip(" .!?«»\"'"), meaning.strip(" .!?«»\"'")
        key = self._check(heard)
        if meaning:
            self._check(meaning)
            if normalize(meaning) == key:
                meaning = ""
        with self._lock:
            if meaning:
                self._data["vocabulary"][key] = meaning
            words = self._data["words"]
            word = meaning or heard
            if normalize(word) not in {normalize(w) for w in words}:
                words.append(word)
                del words[:-MAX_WORDS]
            self._save()
        self._changed()
        log.info("Vocabulaire appris : « %s »%s", heard, f" -> « {meaning} »" if meaning else "")
        return {"heard": heard, "meaning": meaning}

    def forget(self, heard: str) -> dict:
        key = normalize(heard)
        with self._lock:
            vocabulary = self._data["vocabulary"]
            # « Oublie la lampe du fond » arrive déjà réécrit (« oublie la chambre ») : retrouvé par son sens.
            key = key if key in vocabulary else next((k for k, v in vocabulary.items() if normalize(v) == key), key)
            meaning = vocabulary.pop(key, None)
            before = len(self._data["words"])
            self._data["words"] = [w for w in self._data["words"]
                                   if normalize(w) != key and (meaning is None or w != meaning)]
            if meaning is None and len(self._data["words"]) == before:
                raise ToolError(LEARNING_NOT_FOUND, "Je n'avais rien appris à ce sujet.")
            self._save()
        self._changed()
        return {"heard": heard}

    def vocabulary(self) -> dict:
        with self._lock:
            return {"rewrites": dict(self._data["vocabulary"]), "words": list(self._data["words"])}

    def rewrite(self, text: str) -> str:
        """Phrase comprise avec le vocabulaire appris (la plus longue expression d'abord)."""
        with self._lock:
            pairs = sorted(self._data["vocabulary"].items(), key=lambda item: -len(item[0]))
        for heard, meaning in pairs:
            text = _pattern(heard).sub(meaning, text)
        return text

    def hotwords(self) -> str:
        with self._lock:
            return ", ".join(self._data["words"])

    def _changed(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                log.debug("Mise à jour du vocabulaire de Whisper impossible", exc_info=True)

    # --- Mot de réveil -------------------------------------------------------------------------------------

    def wake_threshold(self, base: float) -> float:
        with self._lock:
            wake = self._data["wake"]
            days = max(0.0, (self._clock() - wake["raised_at"]) / 86400) if wake["raised_at"] else 0.0
            extra = max(0.0, wake["raise"] - WAKE_DECAY * days)
        return round(min(WAKE_CEILING, base + extra), 3)

    def _raise(self, step: float) -> None:
        wake = self._data["wake"]
        current = max(0.0, wake["raise"] - WAKE_DECAY * max(0.0, (self._clock() - wake["raised_at"]) / 86400)
                      if wake["raised_at"] else wake["raise"])
        wake["raise"] = round(min(WAKE_MAX_RAISE, current + step), 3)
        wake["raised_at"] = self._clock()

    @property
    def always_verify(self) -> bool:
        with self._lock:
            return bool(self._data["wake"]["always_verify"])

    def blocked(self, heard: str) -> bool:
        """Ce qu'a entendu la vérification a déjà été signalé comme un faux réveil."""
        norm = normalize(heard)
        with self._lock:
            return bool(norm) and norm in self._data["wake"]["blocked"]

    def false_wake(self, meta: dict) -> dict:
        """Faux réveil signalé : seuil relevé, phrase entendue refusée désormais, vérification obligatoire."""
        heard = normalize(str(meta.get("heard") or ""))
        with self._lock:
            wake = self._data["wake"]
            self._raise(WAKE_STEP)
            wake["false_wakes"] += 1
            rule = ""
            if heard and len(heard.split()) >= 2 and heard not in wake["blocked"]:
                wake["blocked"].append(heard)
                del wake["blocked"][:-MAX_BLOCKED]
                rule = "phrase"
            if meta.get("sure") and not wake["always_verify"]:
                wake["always_verify"] = True
                rule = rule or "verify"
            self._save()
            raised = wake["raise"]
        log.info("Faux réveil signalé : seuil relevé de %.2f au total%s", raised,
                 f", nouvelle règle ({rule})" if rule else "")
        return {"raise": raised, "rule": rule}

    def silent_wake(self) -> None:
        """Réveil sans aucune demande ensuite : plusieurs en peu de temps relèvent légèrement le seuil."""
        now = self._clock()
        with self._lock:
            wake = self._data["wake"]
            wake["silent"] = [t for t in wake["silent"] if now - t < SILENT_WINDOW] + [now]
            if len(wake["silent"]) >= SILENT_LIMIT:
                self._raise(SILENT_STEP)
                wake["silent"] = []
                log.info("Réveils sans suite répétés : seuil relevé de %.2f au total", wake["raise"])
            self._save()


def learning_tools(learning: Learning) -> list[Tool]:
    def said_list(r: dict) -> str:
        parts = [f"« {a} » veut dire « {b} »" for a, b in r["rewrites"].items()]
        extra = [w for w in r["words"] if w not in r["rewrites"].values()]
        if extra:
            parts.append("je connais aussi " + ", ".join(f"« {w} »" for w in extra[:10]))
        return ("J'ai appris : " + " ; ".join(parts[:10]) + ".") if parts else "Je n'ai encore appris aucun mot."

    return [
        Tool("learn_word", "Apprend du vocabulaire : un mot à mieux reconnaître, ou ce que veut dire une expression "
             "de l'utilisateur (« quand je dis X, je veux dire Y »).",
             {"heard": Param(str, "le mot ou l'expression de l'utilisateur (X)", max_length=60,
                             evidence=lambda v, t: normalize(v) in normalize(t)),
              "meaning": Param(str, "ce qu'il veut dire (Y), si l'utilisateur le précise", required=False,
                               max_length=60, evidence=lambda v, t: normalize(v) in normalize(t))},
             {"heard": "expression", "meaning": "sens"}, Risk.SAFE,
             lambda heard, meaning="": learning.learn(heard, meaning or ""),
             say=lambda r: (f"C'est noté : quand vous dites « {r['heard']} », je comprendrai « {r['meaning']} »."
                            if r["meaning"] else f"C'est noté, je retiens le mot « {r['heard']} ».")),
        Tool("forget_word", "Oublie un mot ou une expression appris.",
             {"heard": Param(str, "le mot ou l'expression à oublier", max_length=60,
                             evidence=lambda v, t: normalize(v) in normalize(t))},
             {"heard": "expression oubliée"}, Risk.SAFE, learning.forget,
             say=lambda r: f"J'ai oublié « {r['heard']} »."),
        Tool("list_learned", "Dit le vocabulaire qu'ORION a appris.", {}, {"rewrites": "expressions", "words": "mots"},
             Risk.SAFE, learning.vocabulary, say=said_list),
    ]
