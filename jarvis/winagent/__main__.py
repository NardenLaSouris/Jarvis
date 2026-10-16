"""Compatibilité temporaire : l'ancien nom du package (« jarvis ») pour les lanceurs déjà installés
(service systemd, raccourcis Windows, passerelle d'appel). Le code est dans « orion ». À retirer une fois
la migration de production faite (docs/migration_orion.md)."""

import sys

from orion.winagent.__main__ import main

sys.exit(main())
