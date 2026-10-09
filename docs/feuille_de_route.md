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
