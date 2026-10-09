"""Corpus SYNTHÉTIQUE du banc STT : voix Piper (aucun enregistrement réel ni privé), lu silencieusement en fichiers.

    python research/stt/corpus.py SORTIE      (sur le mini-PC, voix dans /opt/jarvis/models/piper)

Chaque phrase de référence est dite par plusieurs voix et à plusieurs débits ; des variantes ajoutent un bruit
synthétique (bruit rose, brouhaha de phrases synthétiques) et une hésitation (pause au milieu). Des clips sans parole
(silence, souffle, bruit) vérifient que rien n'est inventé. Ce corpus ne remplace pas une évaluation en conditions
réelles : il compare des réglages entre eux, à conditions égales.
"""

from __future__ import annotations

import json
import random
import sys
import wave
from pathlib import Path

import numpy as np

SENTENCES = {
    "naturel": ["Bonjour, comment ça va aujourd'hui ?", "Je rentre à la maison dans une heure environ.",
                "Raconte-moi une histoire courte pour m'endormir.", "J'ai oublié mon téléphone dans la voiture.",
                "Qu'est-ce que tu penses de cette idée ?", "Il fait vraiment froid dehors ce matin."],
    "question": ["Quel temps fera-t-il demain à Bordeaux ?", "Est-ce que j'ai reçu des mails importants ?",
                 "Combien de temps reste-t-il sur le minuteur ?", "Qu'est-ce que j'ai de prévu cette semaine ?"],
    "nombres": ["Mets le volume à trente-cinq pour cent.", "Lance un minuteur de douze minutes.",
                "La température est de vingt et un degrés.", "Règle la luminosité à soixante-quinze pour cent."],
    "dates_heures": ["Réveille-moi demain à sept heures et demie.", "Ajoute un rendez-vous vendredi à quatorze heures.",
                     "Rappelle-moi le neuf octobre à dix-huit heures.", "Il est vingt-deux heures quarante-cinq."],
    "noms_propres": ["Envoie un mail à Julie Martin.", "Quel temps fait-il à Saint-Étienne ?",
                     "Ouvre Discord et appelle Thomas.", "Lance la playlist de Daft Punk."],
    "informatique": ["Ouvre Visual Studio Code.", "Vérifie la connexion Wi-Fi du serveur.",
                     "Ferme Google Chrome et lance Steam.", "Le processeur est utilisé à quarante-deux pour cent."],
    "anglicismes": ["Mets le podcast en streaming.", "Fais un reset du router.",
                    "Lance le playback en mode shuffle.", "Le meeting est reporté à demain."],
}
VOICES = [("fr_FR-siwis-medium", ""), ("fr_FR-tom-medium", ""), ("fr_FR-upmc-medium", "jessica"),
          ("fr_FR-upmc-medium", "pierre")]
SPEEDS = {"normal": 1.0, "rapide": 0.75}
SR = 16000


def resample(audio: np.ndarray, rate: int) -> np.ndarray:
    if rate == SR:
        return audio.astype(np.float32)
    x = np.arange(0, len(audio), rate / SR)
    return np.interp(x, np.arange(len(audio)), audio.astype(np.float32)).astype(np.float32)


def pink(n: int, rng: np.random.Generator) -> np.ndarray:
    white = rng.standard_normal(n)
    spectrum = np.fft.rfft(white) / np.sqrt(np.maximum(np.arange(n // 2 + 1), 1))
    noise = np.fft.irfft(spectrum, n)
    return noise / (np.sqrt(np.mean(noise ** 2)) + 1e-9)


def mix(speech: np.ndarray, noise: np.ndarray, snr_db: float) -> np.ndarray:
    level = np.sqrt(np.mean(speech ** 2)) + 1e-9
    noise = noise[: len(speech)] / (np.sqrt(np.mean(noise[: len(speech)] ** 2)) + 1e-9)
    return speech + noise * level / 10 ** (snr_db / 20)


def save(path: Path, audio: np.ndarray) -> None:
    pcm = np.clip(audio, -32768, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(SR), w.writeframes(pcm.tobytes())


def main(out: Path) -> None:
    sys.path.insert(0, "/opt/jarvis")
    from jarvis.tools.base import Param  # noqa: F401 - vérifie que le dépôt d'ORION est importable
    from jarvis.tts.piper import PiperTTS

    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(7)
    rows, voices = [], {}
    babble_src = []
    for voice, speaker in VOICES:
        for speed_name, speed in SPEEDS.items():
            key = (voice, speaker, speed)
            voices[key] = PiperTTS(Path(f"/opt/jarvis/models/piper/{voice}.onnx"), length_scale=speed, speaker=speaker)
    for category, sentences in SENTENCES.items():
        for i, text in enumerate(sentences):
            for (voice, speaker, speed), tts in voices.items():
                if speed != 1.0 and (i + len(voice)) % 2:  # débit rapide : une phrase sur deux par voix
                    continue
                audio, rate = tts.synthesize(text)
                speech = resample(audio, rate)
                if speed == 1.0 and len(babble_src) < 12:
                    babble_src.append(speech)
                pad = np.zeros(int(0.4 * SR), np.float32)
                clean = np.concatenate([pad, speech, pad])
                name = f"{category}_{i}_{voice.split('-')[1]}{'-' + speaker if speaker else ''}_{'rapide' if speed != 1.0 else 'normal'}"
                variants = {"propre": clean}
                if speed == 1.0 and i % 2 == 0:
                    variants["bruit_rose_10dB"] = mix(clean, pink(len(clean), rng), 10)
                if speed == 1.0 and i % 2 == 1:
                    variants["hesitation"] = np.concatenate(
                        [pad, speech[: len(speech) // 2], np.zeros(int(0.7 * SR), np.float32), speech[len(speech) // 2:], pad])
                for variant, data in variants.items():
                    path = out / f"{name}_{variant}.wav"
                    save(path, data)
                    rows.append({"file": path.name, "text": text, "category": category, "voice": f"{voice}:{speaker}",
                                 "speed": "rapide" if speed != 1.0 else "normal", "variant": variant})
    babble = np.concatenate(babble_src)
    for i, text in enumerate(sum(SENTENCES.values(), [])[::5]):
        tts = voices[("fr_FR-siwis-medium", "", 1.0)]
        audio, rate = tts.synthesize(text)
        speech = np.concatenate([np.zeros(int(0.4 * SR), np.float32), resample(audio, rate), np.zeros(int(0.4 * SR), np.float32)])
        start = int(rng.integers(0, max(1, len(babble) - len(speech))))
        path = out / f"brouhaha_{i}.wav"
        save(path, mix(speech, babble[start:start + len(speech)], 8))
        rows.append({"file": path.name, "text": text, "category": "brouhaha_8dB", "voice": "siwis", "speed": "normal",
                     "variant": "brouhaha_8dB"})
    for kind in ("silence", "souffle", "bruit_rose"):
        for i in range(4):
            n = int(rng.uniform(1.5, 4.0) * SR)
            data = {"silence": np.zeros(n), "souffle": rng.normal(0, 30, n), "bruit_rose": pink(n, rng) * 600}[kind]
            path = out / f"sans_parole_{kind}_{i}.wav"
            save(path, data)
            rows.append({"file": path.name, "text": "", "category": "sans_parole", "voice": "", "speed": "", "variant": kind})
    (out / "references.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(rows)} fichiers dans {out}")


if __name__ == "__main__":
    random.seed(7)
    main(Path(sys.argv[1]))
