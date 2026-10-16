# Mission de stabilisation V2 (16 octobre 2026)

Travail fait dans la copie de développement du mini-PC (`~/orion-dev`, branche V2), **non commité, non déployé** :
`/opt/jarvis` (production), le Katana et le PC fixe n'ont pas été modifiés.

## État initial

- Branche V2, commit `a6e1620` (plus récent que le `5de0720` annoncé), dépôt propre partout (copie de
  développement, `/opt/jarvis`, PC fixe). Services `orion`, `orion-call`, `orion-librespot` actifs.
- Tests de référence : 1845 réussis, 5 ignorés.
- Latences réelles (journal, 8 jours) :

| Étape | Avant GPU (69 réponses) | Après GPU (4 réponses) |
|---|---|---|
| Transcription | médiane 2,99 s, p90 3,30 s | médiane 0,90 s, p90 0,98 s |
| Choix d'outil | médiane 1,07 s, p90 2,59 s (42) | médiane 2,27 s (3) |
| Outil | médiane 0,01 s, p90 2,21 s | médiane 0,02 s |
| Synthèse (1re phrase) | médiane 0,21 s, p90 0,41 s | médiane 0,36 s |

  Les mesures « après » portent sur 4 réponses seulement : à confirmer à l'usage. La transcription réelle (0,9 s)
  est le double du banc (0,45 s) : réseau, encodage et premier calcul après une pause ne sont pas dans le banc.

## Problèmes constatés et corrigés

| Problème (preuve : journal) | Cause | Correctif |
|---|---|---|
| « Oui, éteins mon PC portable » abandonnait la confirmation et reposait la question (+2,2 s) | seul un « oui » exact confirmait | un oui suivi de la **redite** de la demande confirme ; chaque mot doit renvoyer à la question (« oui, ferme Chrome », « oui mais… », « oui et… » ne confirment jamais) |
| « Fous alert », « Pousse alerte », « Foussa alerte » (fausse alerte mal entendue) : envoyés au LLM, **un rappel « Fous alert » créé** | transcription non reconnue ; aucun mot de rappel exigé | réécritures de ces transcriptions ; un rappel n'est plus créé sans mot de rappel (« rappel », « rappelle-moi », « fais-moi penser »…) |
| « Faux réveil, rendors-toi », « C'était un faux réveil. Mets-toi en veille. » pris pour une demande de rappel | formules exactes seulement, « réveil » attirait l'intention des rappels | une phrase dont chaque morceau est une formule de faux réveil ou de veille est reconnue comme telle |
| « Éteins le pc », « Allume le pc », « Arrête Spotify » : 1,6 à 2,7 s de LLM chacune | pas de commande déterministe | commandes rapides (l'extinction garde sa confirmation) |
| La transcription ne disait pas où elle avait lieu | absent du journal | « STT 0,9 s [gpu] » ou « [local] » : un basculement se voit |
| Corps de mail, contenu de fichier, fait retenu, extrait de mail lu recopiés dans le journal (120 premiers caractères) | seule la longueur était bornée | ces champs sont remplacés par leur longueur, dans le journal du Core comme dans les lignes `[tool]` de l'agent |
| Deux faits contradictoires (« je préfère le bleu », puis « le vert ») donnés au LLM sans date | aucune date dans le prompt | chaque fait porte sa date, le plus récent fait foi (non observé en vrai : 1 fait en mémoire) |

Mesure à conditions identiques (163 demandes réelles du journal rejouées, rien exécuté) : passages par le LLM
**117 → 95** (−19 %), dont 16 commandes devenues rapides et 6 faux réveils reconnus.

## Vérifié sans correction nécessaire

- Routines désactivées : jamais lancées (horaire, arrivée, départ), toujours enregistrées ; test ajouté pour l'arrivée.
- Repli de la transcription : Katana éteint, jeton refusé, et désormais Katana **joignable mais muet** (délai borné,
  pas de nouvel essai pendant 30 s) : tests.
- Modèle de langage : sonde revérifiée avant de déclarer le Katana hors ligne, retour automatique (tests existants).
- Confirmations par canal, refus expliqués, outils hors liste du fond d'écran : tests existants.

## Tests

- Avant : 1845 réussis, 5 ignorés. Après : voir le compte rendu final (suite complète relancée).
- Nouveaux tests : confirmation par redite (et ses refus), faux réveils réels, rappel jamais créé sans être demandé,
  commandes rapides PC et Spotify, journal sans texte privé, mémoire datée, souvenirs absents ou supprimés,
  routine d'arrivée désactivée, transcription distante bloquée.

## À valider en vrai (non testable ici)

- Les faux réveils et la confirmation par redite, à la voix (transcriptions réelles).
- Latence du choix d'outil après déploiement (les commandes PC et Spotify ne passent plus par le LLM).

## Étapes manuelles proposées

### Transcription du Katana sans session ouverte

Ollama tourne sans session ouverte : tâche planifiée « JARVIS Ollama », déclenchée au démarrage, compte en mode
S4U (sans mot de passe enregistré), session 0, avec accès au GPU. Le service de transcription, lui, dépend du dossier
Démarrage (ouverture de session). Même mécanisme proposé, dans un PowerShell **administrateur** sur le Katana :

```powershell
$d = "$env:USERPROFILE\orion-stt"
$a = New-ScheduledTaskAction -Execute "$d\Scripts\pythonw.exe" -WorkingDirectory $d -Argument "`"$d\server.py`" --model large-v3 --port 8766 --token-file `"$d\token.txt`" --models `"$d\models`" --log `"$d\server.log`""
$t = New-ScheduledTaskTrigger -AtStartup
$p = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited
$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "ORION Transcription" -Action $a -Trigger $t -Principal $p -Settings $s
Remove-Item "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\orion-stt.cmd"
```

Au prochain démarrage du Katana, le service se lance sans session. Non testé : à vérifier après un redémarrage
(`server.log`, et « STT … [gpu] » dans le journal d'ORION).

### Déploiement

Ces correctifs ne sont ni commités ni déployés (consigne de la mission). Pour les mettre en service : commit dans
`~/orion-dev`, `git pull` de `/opt/jarvis`, redémarrage d'`orion`.

## Suite recommandée (impact, risque)

1. **Déployer ces correctifs** (impact fort, risque faible : tout est couvert par les tests).
2. **Service de transcription au démarrage du Katana** (fort, faible : même mécanisme qu'Ollama).
3. **Mesurer le choix d'outil sur une semaine** après déploiement (les 2,3 s médians récents portent sur 3 réponses).
4. **Corrélation des lignes d'une même demande** dans le journal (moyen, faible) : aujourd'hui, une routine ou une
   action du fond d'écran peut s'intercaler entre les lignes d'une conversation.
5. **« Lance la routine X » sur une routine désactivée** la lance quand même (lancement manuel) : à décider (dire
   qu'elle est désactivée et demander confirmation ?).
