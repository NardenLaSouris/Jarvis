# Chantier STT — rapport (9 octobre 2026, branche recherche/llm-avp, non poussée)

## 1. Audit du STT actuel (vérifié dans le code et sur le mini-PC)

- **Modèle et matériel** : faster-whisper (CTranslate2) « small », int8, **sur le processeur du mini-PC**
  (`STT : matériel détecté -> cpu/int8` au démarrage). Le mini n'a pas de GPU ; l'accélération GPU citée dans la
  demande n'est pas utilisée par le service. Déplacer le STT sur le Katana (RTX 4070) est exclu par une consigne
  antérieure (« Le STT doit rester sur le mini-PC ») : non fait.
- **Chemin audio** : micro du PC (agent Windows, `jarvis/audio/devices.py`) -> blocs de 80 ms, 16 kHz, int16, mono
  -> réseau (`jarvis/audio/network.py`, tampon de 30 s) -> mot de réveil -> enregistreur à seuil d'énergie
  (`jarvis/audio/endpointing.py` : seuil adaptatif au bruit, 3 blocs forts pour commencer, 0,48 s gardés avant,
  arrêt après 0,9 s de silence, 15 s au plus ; les 0,32 s écoutées après le mot de réveil lui sont rendues) ->
  `FasterWhisperSTT.transcribe` (phrase entière, français imposé, beam 1, pas de texte précédent, indice de
  vocabulaire en `hotwords`, segments à `no_speech_prob` >= 0,6 écartés) -> correcteur des noms d'applications
  (`jarvis/stt/correction.py`, existant, non modifié).
- **Rééchantillonnage** : aucun en ce moment (le micro accepte 16 kHz). Si un micro le refuse, capture à sa
  fréquence native puis interpolation linéaire **sans filtre** : repliement des aigus dans la bande de la voix.
- **Micro réellement utilisé par l'agent du PC** : « Microphone (Voicemod Virtual Audio Device) », signalé muet
  (crête 1) dans `data/winagent.log`. Si la voix passe par Voicemod, elle est transformée avant le STT et le mot
  de réveil (non mesuré).
- **Concurrence et mémoire** : une transcription à la fois (boucle de conversation) ; vérification du mot de réveil
  par un second modèle (base) chargé à part ; aucun problème observé dans le journal.
- **Reprise sur erreur** : repli CPU si le GPU échoue au chargement ; une erreur pendant une conversation est
  désormais rattrapée par la boucle (chantier 2).

## 2. Mesures de référence

- **Service réel** (journal, 64 réponses) : transcription ~3 s par phrase ; attente de silence 0,95 s.
- **Banc synthétique** `research/stt` (318 fichiers, voix Piper, **pas un enregistrement réel**) : WER 11,2 %,
  latence médiane 3,15 s (1,08 s de calcul par seconde d'audio), 1 texte inventé sur 12 clips sans parole
  (« Sous-titres réalisés par la communauté d'Amara.org » sur un silence).
- **Segmentation** (enregistreur réel d'ORION sur ces fichiers) : 100 % de la parole gardée dans toutes les
  variantes (propre, bruit, brouhaha, pause d'hésitation de 0,7 s) : aucune coupure de début ou de fin.

## 3. Modifications

| fichier | changement | pourquoi |
|---|---|---|
| `jarvis/stt/faster_whisper.py`, `config.py`, `factory.py`, `config.toml` | `[stt] vad_filter` (Silero, fourni avec faster-whisper), activé ; `false` = comportement d'origine | plus de texte inventé sur un silence, mieux sur l'hésitation, sans coût |
| `jarvis/audio/resample.py` | moyenne glissante avant l'interpolation en réduction de fréquence | repliement des aigus (15 kHz atténué de 25 dB, voix intacte) |
| `research/stt/corpus.py`, `bench.py`, `determinisme.py` | corpus synthétique, banc (WER, latence, texte inventé, segmentation), test de reproductibilité | mesures |
| `tests/test_stt_options.py`, `tests/test_reliability.py`, `tests/test_correction.py` | options transmises à Whisper, segments sans parole écartés, rééchantillonnage | non-régression |

Non modifiés : modèle (« small »), beam, indice de vocabulaire, enregistreur, correcteur, prompts et bancs de
compréhension, permissions, politiques Cedar.

## 4. Avant / après (banc synthétique, mesuré)

| réglage | WER | latence médiane | texte inventé sans parole |
|---|---|---|---|
| actuel (318 fichiers) | 11,2 % | 3,15 s | 1/12 |
| **VAD (318 fichiers)** | **11,1 %** | 3,14 s | **0/12** |
| sans indice de vocabulaire (168) | 15,6 % | 2,81 s | 0/12 |
| beam 5 (168) | 11,7 % | 3,66 s | 1/12 |
| modèle « base » (168) | 21,5 % | 1,05 s | 7/12 |

- Indice de vocabulaire : utile (vocabulaire informatique 10 % d'erreurs contre 29 % sans), aucun texte imposé.
- VAD sur des phrases hésitantes ou bruitées (10 fichiers, 3 passes identiques) : 8,5 % -> 6,1 %.
- **Non confirmé** : gain du VAD sur un brouhaha de voix (7 % dans un lancement, 22 % dans un autre ; les
  transcriptions sont reproductibles dans un même processus mais varient entre deux chargements du modèle).
- Rejetés : « base » (deux fois plus d'erreurs, 7 textes inventés sur 12), beam 5 (+0,5 s pour un gain négligeable).

## 5. Commandes

```bash
# tests unitaires (sans modèle)
python -m pytest -q tests/test_stt_options.py tests/test_reliability.py tests/test_correction.py
# banc sur fichiers (sur le mini-PC, dans une copie du dépôt)
python research/stt/corpus.py ~/stt-bench/audio
python research/stt/bench.py ~/stt-bench/audio resultat.json [--configs actuel,vad] [--subset]
python research/stt/determinisme.py
```
Résultats bruts : `research/stt/resultats/avant.json`, `apres.json`. Retour arrière : `[stt] vad_filter = false`.

## 6. Problèmes non résolus et recommandations

- **Latence** : ~3 s de transcription sur le processeur du mini, premier poste de la latence d'ORION. Sans GPU sur
  le mini, les leviers mesurés ne suffisent pas (« base » trop imprécis). Pistes à valider : transcrire pendant que
  l'on parle (segments partiels), ou un GPU accessible au mini (exclu aujourd'hui par consigne).
- **Évaluation réelle** : tout est synthétique. Une mesure en conditions réelles demanderait des enregistrements
  autorisés de votre voix (aucun utilisé ici).
- **Micro Voicemod** : vérifier le micro choisi par l'agent du PC (`[audio] input_device` de `windows_agent.toml`).
- **Rééchantillonnage** : la correction s'applique à l'agent Windows ; elle n'agira qu'après mise à jour de l'agent
  du PC (non faite : aucun déploiement depuis cette branche).
- **Attente de silence (0,9 s)** : pourrait descendre à ~0,7 s, à valider sur une vraie voix (non testé).
