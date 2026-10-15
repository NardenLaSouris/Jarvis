# Stabilisation d'ORION : rapport (15 octobre 2026)

Travail fait sur le mini-PC (copie `~/orion-dev`, déployée dans `/opt/jarvis`). Aucune intervention sur le PC fixe.

## 1. Fiabilité et sécurité (corrigé, testé, déployé)

| Problème constaté | Correctif |
|---|---|
| Une confirmation en attente était commune à la voix et aux messages : un « oui » dit dans la pièce pouvait valider une demande écrite | Une confirmation par canal (`voice`, `message`) |
| Règle Cedar `executable_link` appliquée à toute l'adresse : `https://www.youtube.com` refusé (« .com ») | Seul le chemin de l'adresse est examiné |
| « Désactive la routine » proposait de la supprimer | Outils `disable_routine` / `enable_routine` (la routine reste enregistrée) |
| « Remets-toi en veille » compris comme un réglage de luminosité | Intention `standby` : fin de conversation |
| Photo, fichier ou vocal reçu par Linphone ignoré sans réponse | Réponse claire, une fois par minute au plus |
| « Si je t'envoie alarme.mp3, tu peux le mettre… ? » a lancé `copy_file` | Une hypothèse n'exécute jamais d'action (les règles « si la porte s'ouvre… » ne sont pas visées) |
| « Je ne suis pas autorisé à… » (masculin, et faux : l'application n'est simplement pas dans la liste) | « « X » ne fait pas partie des applications que je peux ouvrir : … » |

## 2. Transcription (mesurée, configuration inchangée)

Journal réel (58 réponses, 6 jours) : STT médiane 3,0 s, p90 3,3 s, soit plus de la moitié du délai total
(médiane 5,5 s). Whisper encode une fenêtre fixe de 30 s : le coût ne dépend presque pas de la longueur de la phrase.

Banc `bench/stt/run.py`, 60 phrases dites par 3 voix Piper (corpus synthétique : sert à comparer, pas à juger) :

| Réglage | Erreurs par mot | Durée médiane |
|---|---|---|
| small, avec indice (actuel) | 7,5 % | 3,19 s |
| small, sans indice | 9,7 % | 2,83 s |
| base, avec indice | 21,1 % | 1,03 s |
| base, sans indice | 23,9 % | 0,92 s |

Conclusions :
- `base` est 3 fois plus rapide mais fait 3 fois plus d'erreurs : non retenu.
- L'indice de vocabulaire aide (−2,2 points) pour +0,36 s, mais il biaise : « Quelle heure est-il ? » devient
  « Quel temps fera-t-il ? » (phrase météo de l'indice) sur 3 voix sur 3.
- Le vrai gain serait la transcription sur le GPU du Katana (non mesuré : faster-whisper n'y est pas installé).
- Étape suivante : 20 à 30 enregistrements réels (`--audio`, format décrit dans le script) pour trancher.

## 3. Conversation et routage

- « Merde » (après un refus) recevait « cette fonction n'est pas encore disponible » : intention `frustration`,
  réponse posée.
- Le Katana passait « hors ligne » 10 s toutes les une à deux heures (26 fois en 6 jours, réseau sans perte) : un
  seul échec de connexion TCP suffisait. La sonde de fond revérifie 2 s plus tard avant de basculer.
- Refus de permission : la raison est dite quand elle est utile (lien vers un programme, appareil interdit,
  demande venue d'un contenu extérieur).

## 4. Mémoire et recherche

Journal des échanges (`jarvis/journal.py`) : SQLite FTS5 local (`data/journal.sqlite`), voix et messages,
conservation 90 jours, phrases ressemblant à un secret écartées, désactivable (`[memory] journal = false`).
Outil `search_history`, réservé au propriétaire. Le journal ne nourrit pas le prompt : il n'est lu que sur demande.

## 5. Outils et permissions

Liste blanche des applications conservée. Wallpaper Engine n'y est pas : l'ajouter demande la configuration de
l'agent du PC fixe, hors de cette mission.

## 6. Satellites

Étude seule : `docs/satellites.md`.

## Restent à faire par vous

- Pousser la branche V2 sur GitHub depuis le PC (le mini-PC n'a pas d'accès GitHub).
- Activer « Hey Orion » seulement après votre accord et un essai.
- Conservation du journal système (5 jours aujourd'hui), si vous le souhaitez :
  `sudo mkdir -p /etc/systemd/journald.conf.d && printf '[Journal]\nSystemMaxUse=500M\nMaxRetentionSec=30day\n' | sudo tee /etc/systemd/journald.conf.d/orion.conf && sudo systemctl restart systemd-journald`
- Copies de configuration non suivies dans `/opt/jarvis` (`config.local.toml.avant-*`) : à garder ou supprimer.

## Complément : transcription sur le GPU du Katana (15 octobre 2026)

Même corpus (bench/stt/export_corpus.py), même indice, mêmes paramètres ; faster-whisper 1.2.1, CUDA, int8_float16,
environnement isolé C:\Users\theju\orion-stt sur le Katana. Le modèle de langage est resté chargé à 100 % sur le GPU.

| Réglage | Erreurs par mot | Durée médiane | p90 |
|---|---|---|---|
| mini-PC, small avec indice (actuel) | 7,5 % | 3,19 s | 3,26 s |
| Katana, small avec indice | 7,2 % | 0,37 s | 0,50 s |
| Katana, medium avec indice | 2,8 % | 0,61 s | 0,72 s |
| Katana, large-v3-turbo avec indice | 3,1 % | 0,20 s | 0,70 s |

large-v3-turbo sur le Katana : 2,4 fois moins d erreurs et 16 fois plus rapide que l actuel ; l indice n y produit
plus le biais « Quelle heure » -> « Quel temps ». Non branché à ORION : il faudrait un petit service de transcription
sur le Katana, avec repli sur small du mini-PC quand le Katana est éteint.
