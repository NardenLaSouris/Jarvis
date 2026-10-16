# Lance l'agent Windows d'ORION à chaque ouverture de session, sans fenêtre (journal : data\winagent.log).
#   powershell -ExecutionPolicy Bypass -File scripts\install_winagent_startup.ps1            # installer et démarrer
#   powershell -ExecutionPolicy Bypass -File scripts\install_winagent_startup.ps1 -Remove    # désinstaller
#   ... -NoStart : raccourci seulement (depuis SSH : l'agent doit tourner dans la session de l'utilisateur)
# Aucun droit administrateur : un raccourci dans le dossier Démarrage de l'utilisateur.
param([switch]$Remove, [switch]$NoStart)
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
$shortcut = Join-Path ([Environment]::GetFolderPath("Startup")) "ORION Agent.lnk"
# Raccourci d'avant le changement de nom : retiré, sinon deux agents se disputeraient le port.
$legacy = Join-Path ([Environment]::GetFolderPath("Startup")) "JARVIS Agent.lnk"
$arguments = "-m orion.winagent --log-file data\winagent.log"

if ($Remove) {
    foreach ($link in @($shortcut, $legacy)) { if (Test-Path $link) { Remove-Item $link } }
    Write-Output "Démarrage automatique de l'agent supprimé (l'agent en cours continue jusqu'à la fermeture de session)."
    exit 0
}
if (-not (Test-Path $pythonw)) { throw "Environnement Python introuvable : $pythonw" }
if (Test-Path $legacy) { Remove-Item $legacy; Write-Output "Ancien raccourci « JARVIS Agent » retiré." }

$link = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcut)
$link.TargetPath = $pythonw
$link.Arguments = $arguments
$link.WorkingDirectory = $root
$link.Description = "Agent Windows d'ORION"
$link.Save()
Write-Output "Démarrage automatique installé : $shortcut"
if ($NoStart) { exit 0 }

$running = Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like "*orion.winagent*" -or $_.CommandLine -like "*jarvis.winagent*" }
if ($running) {
    Write-Output "L'agent tourne déjà (PID $($running.ProcessId -join ', '))."
} else {
    Start-Process -FilePath $pythonw -ArgumentList $arguments -WorkingDirectory $root -WindowStyle Hidden
    Write-Output "Agent démarré (journal : $root\data\winagent.log)."
}
