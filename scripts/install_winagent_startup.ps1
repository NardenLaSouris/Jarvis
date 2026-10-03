# Lance l'agent Windows de JARVIS à chaque ouverture de session, sans fenêtre (journal : data\winagent.log).
#   powershell -ExecutionPolicy Bypass -File scripts\install_winagent_startup.ps1            # installer et démarrer
#   powershell -ExecutionPolicy Bypass -File scripts\install_winagent_startup.ps1 -Remove    # désinstaller
#   ... -NoStart : raccourci seulement (depuis SSH : l'agent doit tourner dans la session de l'utilisateur)
# Aucun droit administrateur : un raccourci dans le dossier Démarrage de l'utilisateur.
param([switch]$Remove, [switch]$NoStart)
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
$shortcut = Join-Path ([Environment]::GetFolderPath("Startup")) "JARVIS Agent.lnk"
$arguments = "-m jarvis.winagent --log-file data\winagent.log"

if ($Remove) {
    if (Test-Path $shortcut) { Remove-Item $shortcut }
    Write-Output "Démarrage automatique de l'agent supprimé (l'agent en cours continue jusqu'à la fermeture de session)."
    exit 0
}
if (-not (Test-Path $pythonw)) { throw "Environnement Python introuvable : $pythonw" }

$link = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcut)
$link.TargetPath = $pythonw
$link.Arguments = $arguments
$link.WorkingDirectory = $root
$link.Description = "Agent Windows de JARVIS"
$link.Save()
Write-Output "Démarrage automatique installé : $shortcut"
if ($NoStart) { exit 0 }

$running = Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like "*jarvis.winagent*" }
if ($running) {
    Write-Output "L'agent tourne déjà (PID $($running.ProcessId -join ', '))."
} else {
    Start-Process -FilePath $pythonw -ArgumentList $arguments -WorkingDirectory $root -WindowStyle Hidden
    Write-Output "Agent démarré (journal : $root\data\winagent.log)."
}
