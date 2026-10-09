# Feuille de route d'amélioration continue (octobre 2026)

Chaque chantier : audit, correctifs mesurés, suite complète, tests de régression, revue du diff et des secrets,
documentation. Les points qui demandent une validation sont listés « en attente » et ne sont pas appliqués.
Distinction systématique : **mesuré** (exécuté et observé), **probable** (déduit), **non vérifié**.

## Chantier 2 — Fiabilité et reprise du Core

**Audit (mesuré, journal du mini-PC sur 7 jours)**
- Service systemd utilisateur `Restart=on-failure`, `RestartSec=10` ; 0 redémarrage sur la période.
- 2 traces d'erreur : `spotify_play` (réponse non JSON de Spotify, déjà tolérée par le code actuel) et une
  connexion coupée par le navigateur du visage (trace complète pour un événement banal).
- 1 068 « tampon du micro distant plein » : 1 055 le 3 octobre entre 17 h 17 et 17 h 20 (mise au point du micro),
  4 depuis ; pas de perte récurrente.
- Bascules du LLM principal vers le secours (Katana éteint) : 13, toutes revenues en ligne seules.
- Données (routines, mémoire, échéances, agenda, présence, mails vus) : écriture atomique (fichier temporaire puis
  `os.replace`) et fichier illisible mis de côté au démarrage (`jarvis/persist.py`) : déjà en place.

**Faiblesse trouvée** : `Agent.run` ne protégeait pas une conversation ; toute exception imprévue arrêtait le
service (10 s sans assistant, conversation perdue).

**Correctifs**
- `jarvis/agent.py` : une erreur imprévue pendant une conversation est journalisée (trace complète), la
  confirmation en attente et le contexte sont effacés, ORION repasse en veille. Plus de 3 erreurs en 60 s : le
  service s'arrête et systemd le relance (pas de boucle sur une panne persistante).
- `jarvis/face/server.py` : connexion interrompue par le navigateur journalisée en une ligne de niveau debug.

**Tests** : `tests/test_reliability.py` (conversation qui plante puis suivantes traitées ; pannes en rafale rendues
à systemd ; connexion coupée sans trace).

## Chantier 3 — Mémoire

**Audit (code et tests existants)** : mémoire explicite seulement (`remember` sur demande, jamais les
conversations) ; fait vérifié contre les mots de l'utilisateur (le LLM ne peut rien inventer à retenir) ; 200 faits
au plus par utilisateur ; écriture atomique, fichier abîmé mis de côté ; faits de l'utilisateur en cours seulement,
et seulement pour un adulte (jamais lus à un enfant ni à un invité) ; présentés au LLM comme des données ; oubli
confirmé, jamais répété à voix haute ; 12 tests existants.

**Faiblesse trouvée** : un fait pouvait contenir les balises qui délimitent les données dans les prompts
(`<<<RESULTAT_OUTIL>>>`...), de quoi brouiller la frontière entre données et consignes.

**Correctif** : balises refusées à l'enregistrement et neutralisées dans les faits déjà enregistrés au moment de
les placer dans le prompt (`jarvis/memory.py`). Test : `test_prompt_markers_never_enter_or_leave_memory`.

**Non vérifié** : effet de la mémoire sur la qualité des réponses du modèle abliterated (aucun banc dédié).

## Chantier 4 — Planification complexe

**Mesure (mesuré, `bench/comprehension/multi.json`, huihui sans réflexion, outils simulés)** : 12 demandes à
plusieurs actions (2 et 3 actions, confirmation au milieu d'une chaîne, action impossible dans la chaîne, nombres en
lettres, questions enchaînées) : **10/12**. Confirmations en chaîne, actions impossibles écartées sans bloquer les
autres, trois actions : toutes réussies.

**Faiblesse trouvée** : chaque action d'une chaîne était jugée sur la phrase entière ; « Rappelle-moi d'appeler
Paul dans dix minutes et lance un minuteur » : la règle « rien d'immédiat dans une demande de rappel » écartait aussi
le minuteur.

**Correctif** (`jarvis/tools/planner.py`) : les règles de forme (demande différée, question) s'appliquent à la partie
de la demande propre à chaque action ; une partie contenue dans une demande différée (« rappelle-moi ... de couper le
son ») reste différée ; valeurs et domaine restent vérifiés sur la phrase entière (« mets-la en bleu »). Test :
`test_each_chained_action_is_judged_on_its_own_part_of_the_request`.

**Limites (mesuré)** : les 2 échecs restants viennent du modèle (rappel avec le délai dans `time`, ambiance proposée
pour un minuteur ; écartés par les vérifications) ou d'une interprétation acceptable (`spotify_volume` pour « baisse
le son » pendant la musique).

## Chantier 5 — Performances

**Mesures de référence (mesuré, journal du service, 64 réponses)** : fin de parole -> début de la réponse 4,3 à
4,8 s, dont transcription ~3 s (Whisper small sur le processeur du mini : chantier STT séparé), attente de silence
0,95 s, outil ~0,4 s, première phrase de la voix 0,3 à 0,5 s, premier jeton du LLM ~0,45 s (médiane), choix d'outil
1,2 à 1,7 s.

**Découverte (mesuré)** : le prompt du choix d'outil fait 7 304 jetons ; le contexte d'Ollama était de 4 096 :
Ollama n'en gardait que 2 050, sans avertissement (outils, règles ou exemples perdus) et ne pouvait jamais le garder
en cache (relu en 0,75 à 0,9 s à chaque appel). C'était le cas en production depuis l'ajout des outils.

**Correctifs**
- `[llm] num_ctx = 8192` (`jarvis/llm/ollama.py`, `factory.py`) pour le modèle principal ; le secours (processeur
  du mini) garde son réglage.
- Katana : variables d'environnement **utilisateur** (pas de droits administrateur) `OLLAMA_NUM_PARALLEL=2` (un
  emplacement de cache pour la conversation, un pour le choix d'outil, qui sinon s'évinçaient : +3 s par
  alternance), `OLLAMA_FLASH_ATTENTION=1` et `OLLAMA_KV_CACHE_TYPE=q8_0` (cache moitié moins gros : sans lui, le
  modèle débordait sur le processeur, 7,6 Go pour 8 Go de carte, génération ~25 jetons/s au lieu de 46).
  Retour arrière : supprimer ces trois variables utilisateur et relancer la tâche « JARVIS Ollama ».
- `jarvis/llm/failover.py` : délai propre aux appels JSON (`json_timeout`, double du délai du premier jeton) : la
  réponse JSON n'arrive qu'une fois complète, un premier appel à froid (8 à 10 s) n'est plus pris pour un worker gelé.

**Résultats (mesuré)**
| | avant | après |
|---|---|---|
| choix d'outil (à chaud) | 1,2 à 1,7 s, prompt tronqué | 0,63 à 0,71 s, prompt complet |
| lecture du prompt de conversation | relu après chaque choix d'outil | 0,03 s (en cache) |
| mémoire vidéo (huihui 8B) | 5,38 Go | 6,06 Go, entièrement sur la carte |
| compréhension, jeu de dev | 43/45 | 44/45 |
| compréhension, jeu de test (3e passe) | 37/44 | 41/44 |
| actions enchaînées (multi.json) | 10/12 | 11/12 |

**Limites** : premier appel après un redémarrage d'Ollama lent (8 à 10 s) si une demande arrive avant la fin du
préchauffage ; le jeu de test a désormais servi trois fois (mesures, pas réglage) : un nouveau jeu vierge serait
nécessaire pour une mesure indépendante.

## Chantier 6 — Audit de sécurité

**Mesuré**
- Secrets : `.env` en 600 ; aucune des 5 valeurs du `.env` dans 14 jours de journal ; aucune dans l'historique Git
  (vérifié le 9 octobre) ; `config.local.toml` (adresse mail) passé de 644 à 600.
- Ports ouverts sur le réseau : 22 (SSH), 8765 (visage : GET seulement, état d'affichage sans transcription),
  8766 (API d'administration : jeton Bearer et liste d'adresses). Ollama (11434) et SearXNG (8080) : locaux.
  Pare-feu ufw actif (règles non lisibles sans sudo : non vérifiées).
- Dépendances de production (42 paquets) : `pip-audit` (base OSV) : aucune vulnérabilité connue.
- Autorité du Core et injections : voir `README.md` (« Autorité du Core et politiques Cedar ») et
  `tests/test_policy.py`, `tests/test_guard.py`.

**Ajouté** : `tests/test_security_hygiene.py` (`.env` et `*.local.toml` ignorés par Git ; aucun motif de secret —
clé privée, jetons GitHub, Hugging Face, AWS, clés `sk-`, secrets d'ORION affectés en clair — dans les fichiers suivis).

**En attente de validation** : durcissement du compte système (compte dédié sans sudo/docker/lxd, code en lecture
seule, service systemd durci) : procédure prête dans `docs/durcissement_systeme.md`, non appliquée (demande sudo et
touche au service actif).
