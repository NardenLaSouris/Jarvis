# ORION mobile

Application d'appel d'ORION (Expo, React Native, TypeScript), Android d'abord, iOS ensuite.
Architecture : `docs/` du dépôt et `orion/call/` (le Core seul décide d'un appel ; le téléphone jumelé est le seul
destinataire ; la passerelle `orion-call` fait sonner et transporte l'audio).

## État

Étape actuelle : interface seule, avec une passerelle simulée (bouton « Simuler un appel d'ORION »). Pas encore de
vrai jumelage, de son, de notification ni d'écran d'appel natif.

## Commandes (dans `mobile/`)

```bash
npm install                 # dépendances (node_modules, non versionné)
npm run typecheck           # vérification des types
npx expo start              # aperçu dans Expo Go (tant qu'il n'y a pas de module natif)
npx eas-cli login           # votre compte Expo (à faire vous-même)
npx eas-cli init --id <id du projet EAS>
npx eas-cli build -p android --profile preview   # APK installable, compilé dans le cloud
```

Nom de paquet : `fr.orion.mobile` (à reprendre tel quel dans Firebase, plus tard).
