# Appels proactifs, règles d'appel, rappels et suivi des mails

État au 11 octobre 2026 (branche `feature/appels-mobiles`). Ce document décrit ce qui est en place, comment
s'en servir, comment le vérifier en conditions réelles et ses limites connues.

## Architecture

Linphone transporte les appels ; le Core décide.

| Rôle | Où | Fichier |
| --- | --- | --- |
| Compte SIP d'ORION, appels, son chiffré, messages | passerelle `orion-call` (service système) | `jarvis/call/sip.py`, `linphone.py`, `rtp.py`, `gateway.py` |
| Un appel : demande, sonnerie, décroché, fin, limites anti-harcèlement | Core | `jarvis/call/manager.py`, `limits.py`, `session.py` |
| Règles d'appel, appels déclenchés, tentatives, accusé « c'est noté » | Core | `jarvis/call/rules.py` |
| Phrase -> règle (LLM en valeurs fermées, vérifié par le Core) | Core | `jarvis/call/rule_parser.py`, `rule_tools.py` |
| « Je sors » | Core | `jarvis/presence/declaration.py`, `presence/engine.py`, `presence/tools.py` |
| Rappels (date absolue, clé, report gardé) | Core | `jarvis/scheduling/manager.py`, `tools/timers.py` |
| Échéances des mails, rappels J-1 / H-1 h 30, suivi des fils | Core | `jarvis/mail/actions.py`, `deadlines.py` |
| Dates dites ou écrites en français | Core | `jarvis/scheduling/when.py` |

Aucun service supplémentaire, aucune nouvelle dépendance, aucun port ouvert sur Internet. Les règles,
les appels déclenchés et le suivi des mails sont enregistrés à côté de la mémoire d'ORION (`data/`) :

| Fichier | Contenu |
| --- | --- |
| `data/call_rules.json` | règles, décisions (« pourquoi »), appels déclenchés et leurs états |
| `data/mail_actions.json` | dossiers de mails : état, échéance, rappels, historique |
| `data/schedule.json` (chemin de `[timers]`) | rappels, y compris ceux des mails (clé, lien, report) |
| `data/presence.json` (chemin de `[presence]`) | état de présence, départ déclaré ou annoncé |

## 1. « ORION, je sors »

- Compris sans LLM, appliqué par le Core (outil `declare_departure`) : vous passez absent tout de suite, une seule
  fois (événement `presence.departure_confirmed`, raison « départ déclaré »).
- « Je sors dans une heure », « je pars à 18 h » : absent à l'heure dite (gardé au redémarrage) ; « finalement
  je reste » l'annule.
- « Je sors deux minutes », « je sors les poubelles » : rien ne change.
- Jamais pris pour un départ : une question, une condition (« appelle-moi quand je sors »), un autre sens (« je
  sors de la douche »), le passé.
- Les délais du moteur de présence ne changent pas. Pendant 15 minutes après la déclaration, la porte, le
  mouvement et le téléphone encore sur le Wi-Fi sont ceux de votre sortie : ni retour, ni « mouvement pendant
  l'absence ». Votre retour est ensuite détecté comme d'habitude (téléphone et porte).

## 2. Règles d'appel

Exemples (à la voix ou par message Linphone) :

- « Appelle-moi quand je ne suis pas à la maison et qu'un rappel arrive à échéance. »
- « Appelle-moi si je suis absent depuis plus de trente minutes et que j'ai un rappel important. »
- « Appelle-moi demain à 18 h si je ne suis toujours pas rentré. »
- « Appelle-moi quand je reçois une réponse à ce mail. » (le mail qu'ORION vient de lire ou d'annoncer)
- « Appelle-moi si je n'ai pas répondu à ce message avant vendredi. »
- « Appelle-moi quand le serveur est en panne et que je suis absent. »
- « Appelle-moi si ce rappel est toujours en attente dans une heure. » (rappel pas encore sonné)
- « Écris-moi par message quand… » : un message Linphone au lieu d'un appel.

Fonctionnement :

1. Le LLM ne choisit que parmi des valeurs fermées : l'événement, les conditions et le canal. Il recopie les moments tels que vous les avez dits.
2. Le Core calcule les dates et refuse tout élément que vous n'avez pas dit.
3. « Ce mail » et « ce rappel » sont reliés à ce qu'ORION vient de lire ou de programmer.
4. Si un élément manque, ORION pose une question précise ; votre réponse suivante complète la demande.
5. ORION décrit la règle avec ses propres mots et demande votre « oui » avant de l'enregistrer.

Gestion :
- « Quelles sont mes règles d'appel ? » ;
- « Détaille la règle 2 » ;
- « Désactive la règle 1 » / « Réactive la règle 1 » ;
- « Supprime la règle 2 » (avec confirmation) ;
- « Modifie la règle : … » (avec confirmation) ;
- « Pourquoi m'as-tu appelé ? » / « Pourquoi ne m'as-tu pas appelé ? » (dernières décisions).

États d'un appel déclenché : planifié, tentative, décroché, acquitté, décroché sans accusé, sans réponse, échec
technique, refusé, message envoyé, annulé, résolu, abandonné.

- Un appel par règle et par fait déclencheur (même rappel, même mail) : jamais deux, même après un redémarrage.
- 2 tentatives par défaut, au plus 5, espacées de 15 minutes par défaut ; au-delà, un message Linphone de
  secours, jamais une nouvelle série d'appels.
- Au décroché, ORION dit le motif puis « Dites "c'est noté" pour que je ne vous rappelle pas ». Décroché sans
  « c'est noté » : résumé par message, plus d'appel. Appel refusé : message, plus d'appel. « C'est noté » par
  message après un résumé vaut accusé.
- Les limites du gestionnaire d'appels restent la dernière barrière (`[call] max_per_hour`,
  `min_interval_minutes`, `refusal_pause_minutes`) : un appel qu'elles refusent est reporté sans compter
  d'essai, puis abandonné après 3 heures.
- Retour à la maison : les appels encore en attente n'ont plus lieu. Les conditions sont revérifiées avant
  chaque tentative.
- Sources de confiance seulement : un rappel vient du gestionnaire de rappels, un capteur de ses pilotes, un mail
  de la surveillance. Rien de ce que dit un mail, une page Web ou le LLM ne crée une règle ni ne fait appeler.

## 3. Mails : échéances, rappels, suivi

- Tout nouveau mail personnel ou important est analysé, jamais les lettres d'information ni les publicités.
  - Le modèle donne une nature, une action, le besoin de réponse, l'urgence, et la phrase exacte qui fixe un délai.
  - L'analyse ne touche jamais au message : il reste non lu (lecture `BODY.PEEK`), sans réponse, archivage ni suppression.
- Le Core ne retient une échéance que si :
  - la phrase citée figure dans le mail ;
  - elle dit un délai (« avant », « au plus tard », « sous », « d'ici », « date limite »…).

  Une date seulement mentionnée n'en est pas une. Les délais relatifs partent de la date de réception (« sous 48 heures », « avant vendredi prochain »). Un jour sans heure vaut 18 h ce jour-là. « Au plus vite » : urgent, sans date.

  Si la citation du modèle est absente ou inexacte, le Core cherche lui-même la première phrase du mail qui dit un délai lisible. Lors de l'essai réel avec le modèle du Katana (`scripts/essai_comprehension.py`), cela a fait passer de 3 à 0 les échéances manquées sur 7 mails.
- Échéance écrite et action attendue :
  - un rappel à J-1 et un rappel à H-1 h 30, créés une seule fois ;
  - ORION l'annonce s'il est à la maison et hors heures calmes : « J'ai identifié une échéance… Je vous ai préparé deux rappels. » ;
  - un horaire déjà passé est sauté ; s'il n'en reste aucun et que l'échéance n'est pas passée, un rappel de rattrapage part tout de suite.
- Échéance estimée : ORION demande de la confirmer (« oui, c'est bien ça ») avant de programmer quoi que ce soit.
- Un nouveau message du même fil (`In-Reply-To` / `References`, jamais l'objet) réévalue le dossier et déplace
  les rappels, sauf ceux que vous avez reportés vous-même (« reporte ce rappel de 30 minutes »).
- Votre réponse est détectée par l'indicateur `\Answered`, par le dossier des envoyés ou par une réponse envoyée par ORION :
  - si elle suffit à régler l'action : dossier « en attente d'un retour », rappels annulés ;
  - sinon : « réponse envoyée », rappels gardés, et ORION vous demande si le dossier est réglé.
- Commandes :
  - « Qu'est-ce que j'ai à faire dans mes mails ? » ;
  - « Le dossier 1 est réglé » ;
  - « J'attends leur retour pour le 2 ».

## 4. Comportements ajoutés par la voix ou par message Linphone

Vos messages Linphone passent par la même compréhension que la voix. Vous pouvez y ajouter :

- **Réactions à vos retours et départs** :
  - exemples : « Quand je rentre, allume l'entrée et mets de la musique », « Quand je pars, éteins tout » ;
  - elles deviennent des routines déclenchées par le moteur de présence (retour ou départ confirmé, ou « je sors ») ;
  - la commande est comprise et validée comme une demande normale ; une action qui demande une confirmation ne peut pas être programmée ;
  - pour les gérer : « Quelles sont mes routines ? », « Supprime la routine… ».
- **Consignes durables** :
  - exemples : « Désormais, réponds-moi plus brièvement », « À partir de maintenant, ne fais plus de blagues », « Chaque fois que tu me donnes la météo, donne aussi la température ressentie » ;
  - elles sont données au modèle à chaque réponse, comme règles de style et d'habitude seulement ;
  - le Core refuse toute consigne qui touche aux confirmations, permissions, secrets, règles de sécurité, commandes ou suppressions ;
  - pour les gérer : « Quelles sont tes consignes ? », « Retire la consigne 2 ».
- Règles d'appel, vocabulaire (« quand je dis X, je veux dire Y ») et « je sors » : voir plus haut.

## 5. Identité

ORION est une femme : présentation (« votre assistante personnelle »), prompts du LLM (accords au féminin) et
phrases toutes faites (désolée, navrée, ravie…). Elle vous vouvoie et vous appelle « monsieur », sans jamais
prononcer votre prénom.

## Tests automatiques

Sur le mini-PC (jamais sur le PC de jeu) :

```bash
cd ~/v2-test && /opt/jarvis/.venv/bin/python -m pytest -q tests
```

Fichiers propres à ces fonctions :

| Fichier | Couvre |
| --- | --- |
| `tests/test_presence_declaration.py` | déclaration de départ |
| `tests/test_call_rules.py` | règles d'appel |
| `tests/test_mail_actions.py` | suivi des mails |
| `tests/test_timers.py` | rappels liés et report |
| `tests/test_identity.py` | identité |
| `tests/test_factory_wiring.py` | branchement réel |

## Essais réels à faire (iPhone, Linphone)

Les tests automatiques ne prouvent rien sur le vrai téléphone. Pour chaque essai, gardez le journal ouvert :
`journalctl -u orion -u orion-call -f`.

1. Enregistrement. Le journal de `orion-call` doit afficher « Compte SIP … enregistré ». Redémarrez la box ou coupez le réseau du mini une minute : le compte doit se réenregistrer seul.
2. Appel sortant, iPhone verrouillé, Linphone en arrière-plan, en 4G sans Wi-Fi :
   - créez « Appelle-moi demain à HH:MM » à deux minutes d'ici (ou dites « je sors » puis créez une règle sur un rappel de 3 minutes) ;
   - attendu : sonnerie par notification push, motif dit au décroché, son dans les deux sens.
3. Décroché sans « c'est noté » : raccrochez. Attendu : un message résumé, plus d'appel.
4. « C'est noté » pendant l'appel : aucun message, état « acquitté » (« pourquoi m'as-tu appelé ? »).
5. Refus (bouton rouge) : un message, plus d'appel.
6. Sans réponse : laissez sonner. Attendu : un deuxième essai 15 minutes plus tard, puis un message de secours.
7. Vous appelez ORION depuis Linphone : il décroche, répond, raccroche à la fin.
8. Messages Linphone : « je sors », une règle, « quelles sont mes règles ? », « c'est noté ».
9. ORION arrêté (`sudo systemctl stop orion`) :
   - un appel vers ORION est refusé ;
   - un message reçoit « ORION est indisponible pour le moment. » ;
   - redémarrez ORION : « ORION est en ligne et disponible. ».
10. Mail : envoyez-vous depuis une autre adresse « Merci de répondre avant <après-demain> ». Attendu : l'annonce, puis deux rappels dans « quels sont mes rappels ? ».

## Limites connues

- iOS : l'iPhone sonne verrouillé grâce aux notifications push de Linphone (serveur sip.linphone.org). Si
  Linphone est fermé de force, ou si les notifications ou le mode Concentration le bloquent, l'appel peut ne pas
  sonner. ORION le voit seulement comme un appel sans réponse et applique les essais puis le message de secours.
- Identité de l'appelant : seule votre adresse Linphone est acceptée. Cette vérification repose sur le serveur
  Linphone ; les actions sensibles demandent toujours votre « oui ».
- Messages Linphone chiffrés de bout en bout (LIME) : illisibles pour ORION ; il faut une conversation non chiffrée.
- Le LLM peut mal classer un mail. Le Core n'accepte une échéance que si la citation est dans le mail, et une
  estimation n'arme rien sans votre accord ; mais un délai écrit d'une façon non reconnue peut passer inaperçu.
  Le dossier reste alors dans la liste « à traiter ».
- Votre réponse envoyée depuis une messagerie qui ne pose pas `\Answered` et ne range pas dans un dossier
  `\Sent` n'est pas détectée. Dites alors « le dossier est réglé ».
- « Ce rappel est toujours en attente » veut dire « pas encore sonné » : un rappel qui a sonné est terminé pour
  ORION.

## Déploiement

Le code est déployé comme le reste d'ORION (`git pull` dans `/opt/jarvis`, puis `sudo -n systemctl restart orion`).
Aucune nouvelle configuration n'est nécessaire. Les appels automatiques n'existent que par les règles que vous
créez, chacune avec votre « oui ». Le déclencheur d'absence de `[call] away_events` reste vide tant que vous ne le
demandez pas.

Retour arrière (le code d'avant cette mission est le commit `a202fdb`) :

```bash
cd /opt/jarvis && git checkout a202fdb && sudo -n systemctl restart orion
```

Les fichiers `data/call_rules.json` et `data/mail_actions.json` sont alors ignorés ; les supprimer n'est pas
nécessaire.
