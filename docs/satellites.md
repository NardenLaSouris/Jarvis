# Satellites audio : étude préalable (octobre 2026)

Étude seulement : rien n'est installé ni activé. But : parler à ORION depuis plusieurs pièces, même PC fixe éteint.

## Situation actuelle

- Le micro et la sortie d'ORION sont ceux du **PC fixe**, par son agent (`[audio] remote`, `jarvis.winagent`).
  PC éteint, ORION n'entend plus rien à la voix (les messages Linphone continuent de fonctionner).
- Le Core (mini-PC, i3-6100T, 2 cœurs) fait tout le traitement : wake word (openWakeWord), vérification
  (Whisper base), transcription (Whisper small, ~3 s médian, voir `bench/stt`), synthèse Piper.
- Le contrat réseau existe déjà et suffit pour un satellite (agent ORION, jeton `Authorization: Bearer`) :
  - `GET /audio/input?rate=16000&frame=1280` : flux continu PCM int16 mono ;
  - `POST /audio/output?rate=22050` : lecture d'un PCM int16 mono ;
  - `POST /audio/drain`, `POST /audio/stop` : attente de fin, interruption.

## Proposition (KISS)

1. **Un satellite = un petit appareil qui sert ces 4 points d'accès**, sans aucune intelligence : Raspberry Pi
   Zero 2 W ou Pi 3/4 avec un micro USB ou une carte ReSpeaker 2-Mic, et un petit haut-parleur. Le code de l'agent
   Windows (`jarvis/winagent/server.py`, `audio.py`) devrait tourner sous Linux avec `sounddevice` (à vérifier) ;
   la partie « applications Windows » est à exclure.
2. **Premier palier : un seul satellite** à la place du PC fixe (`[audio] remote = "http://<satellite>:8765"`) :
   aucun changement du Core. Flux continu 16 kHz = 32 ko/s, négligeable en Wi-Fi.
3. **Second palier : plusieurs satellites.** Le Core n'accepte aujourd'hui qu'une source : il faudrait un
   mélangeur qui écoute toutes les sources, retient celle où le wake word a le meilleur score, puis lui réserve
   l'écoute et la réponse jusqu'à la fin de la conversation. Coût CPU sur le mini : un openWakeWord par source
   (à mesurer) ; la transcription reste unique. À ne lancer qu'après le premier palier.

## Points à trancher avant d'acheter

- **Écho** : un satellite qui joue la réponse s'entend lui-même. L'écoute est déjà coupée pendant la parole
  d'ORION ; une interruption « en parlant par-dessus » demanderait une annulation d'écho (ReSpeaker ou
  `webrtc-audio-processing`).
- **Wake word** : « Orion » seul déclenche trop sur les voix de jeu ; le modèle « Hey Orion » (entraîné, non activé)
  est à essayer d'abord avec le micro du satellite, dans la pièce prévue.
- **Sécurité** : jeton propre à chaque satellite, réseau local seulement, aucun port publié.
- **Transcription** : à placer d'après le banc `bench/stt` (CPU du mini ou GPU du Katana), indépendamment des
  satellites.

## Ce qui n'est pas proposé

ESP32 (ESPHome, Wyoming) : possible, mais protocole différent de celui du Core (refonte du transport audio) ;
à reconsidérer seulement si le Pi s'avère trop encombrant.
