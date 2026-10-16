# Observabilité, validation conversationnelle et performances (16 octobre 2026)

Travail dans la copie de développement du mini-PC (`~/orion-dev`, branche V2), **non commité, non déployé**.
Départ : commit `25637d1`, dépôt propre (copie de développement, `/opt/orion` et PC fixe au même commit).

## Constats

- Aucune ligne du journal ne permettait de relier les étapes d'une même demande : une routine, une action du fond
  d'écran ou un message pouvaient s'intercaler entre les lignes d'une conversation.
- Le bloc « [latency] » existait, mais sur plusieurs lignes, sans le chemin (déterministe ou modèle) ni la source de
  transcription : aucun calcul de médiane ou de p90 n'était reproductible.
- L'agent Windows ne journalisait pas les actions reçues.
- **« Hier le son était à 50, c'était trop fort »** mettait le son à 50 : une description au passé passait pour une
  demande (trouvé par la nouvelle suite de garde-fous).
- **« Lance la routine soir »** lançait une routine désactivée (l'outil vocal ne regardait pas son état).

## Corrélation (`orion/trace.py`)

- Une conversation reçoit un identifiant dès le pic du mot de réveil (vérification comprise) ; chaque phrase en est
  une demande numérotée : `[r:3fa2c1.1]`, `[r:3fa2c1.2]`. Un réveil écarté garde l'identifiant de sa vérification.
- Un message Linphone, une action du fond d'écran, une exécution de routine ont chacun le leur.
- L'identifiant suit la demande dans les fils d'exécution des outils (variables de contexte, déjà copiées par le
  Core pour l'utilisateur), part vers l'agent Windows et le service de transcription (en-tête `X-Orion-Request`), qui
  l'écrivent dans leurs propres journaux. **Ollama ne le reçoit pas** : son API n'a pas de champ prévu ; ses lignes
  se rattachent par l'heure et la demande qui l'appelle.
- Seul l'identifiant est ajouté (champ `%(req)s` du format existant) ; un identifiant venu d'une autre machine n'est
  écrit que s'il en a la forme. Les lignes `[user]` et `[message]` (texte de la demande) existaient déjà et sont
  inchangées ; les nouvelles lignes n'ajoutent aucun texte.

## Mesures

Nouvelle ligne par réponse, en plus du bloc existant :

    [r:3fa2c1.2] [mesure] chemin=llm route=tool:tool.action stt=0.812 source=gpu choix=2.310 outil=0.020 tts_premier=0.300 audio_premier=0.300 total=3.540

- Horloge monotone (`time.perf_counter`) pour toutes les durées.
- `chemin=deterministe` (formule, commande rapide, contexte) ou `chemin=llm` (le modèle a choisi l'outil ou rédigé la
  réponse) ; `source=gpu|local` pour la transcription ; `total` = fin de la phrase -> début de la réponse parlée.
- Agrégation : `scripts/latency_report.py` (médiane, p90, maximum par chemin et par source de transcription).

### Recueillir une semaine de mesures

Une fois déployé, rien à faire pendant la semaine. Ensuite, sur le mini-PC :

    cd /opt/orion
    journalctl -u orion --since "-7 days" -o cat | .venv/bin/python scripts/latency_report.py

Le journal est conservé 30 jours (journald, réglage du 15 octobre 2026) : la mesure peut être faite plus tard.
Comparer seulement des périodes de même configuration (modèle, transcription GPU ou locale : voir `source=`).

## Garde-fous conversationnels (`tests/test_conversation_guards.py`)

Agent complet (transcription simulée, routage, chemin déterministe actif comme en production, outils simulés) :
1. une commande explicite ne déclenche que l'action demandée ; 2. « oui » ne confirme que l'action en attente à
laquelle il correspond ; 3. « oui » sans confirmation en attente ne déclenche rien ; 4-5. une phrase mal transcrite
ne crée pas de rappel, une heure non dite n'est jamais inventée ; 6. hypothèse et constat au passé : aucune action ;
8. un outil en échec n'est jamais présenté comme réussi ; 9. sans LLM, les commandes déterministes fonctionnent ;
10. aucune donnée privée dans les lignes de diagnostic. Le cas 7 (routine désactivée) est dans
`tests/test_scheduled_actions.py` et `tests/test_routines.py`.

## Routines désactivées

- Activer (« réactive la routine soir »), exécuter une routine active (« lance la routine soir ») et demander
  l'exécution d'une routine désactivée sont désormais distincts : la dernière est refusée avec l'explication
  (« La routine « Soir » est désactivée. Dites « réactive la routine Soir » pour la remettre en service. »), sans
  l'activer. Le bouton « Lancer » d'ORION Control (action manuelle explicite) est inchangé.

## Reste à valider en vrai

- Lignes `[r:…]` et `[mesure]` dans le journal réel après déploiement ; identifiant dans `server.log` du Katana et
  dans le journal de l'agent du PC (leur code doit être mis à jour : copie de `server.py` sur le Katana, mise à jour
  et redémarrage de l'agent du PC).
- Garde-fous à la voix (transcriptions réelles).

## Priorités suivantes (impact, risque)

1. Déployer (fort, faible) : Core, puis agent du PC et service de transcription pour la corrélation distante.
2. Une semaine de mesures, puis cibler les demandes `chemin=llm` les plus fréquentes (fort, faible).
3. Faire porter la durée de l'appel à Ollama dans la ligne de mesure séparément du choix d'outil s'il reste lent
   (moyen, faible).
