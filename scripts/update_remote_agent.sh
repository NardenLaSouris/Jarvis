#!/bin/bash
# Met à jour l'agent Windows d'un PC sans dépôt Git (le Katana) à partir du commit courant, puis le relance.
#
#   scripts/update_remote_agent.sh theju@192.168.1.73 ~/.ssh/id_ed25519_katana
#
# - Copie jarvis/, windows_agent.toml et scripts/ (git archive : seulement les fichiers versionnés, aucun secret ;
#   .env et windows_agent.local.toml de la machine sont conservés).
# - Relance l'agent par une tâche planifiée temporaire en session interactive : un processus lancé par SSH meurt
#   à la fin de la session, et l'agent doit pouvoir ouvrir des fenêtres sur le bureau.
set -euo pipefail
host="${1:?usage : $0 utilisateur@machine [clé ssh]}"
key="${2:-}"
ssh_opts=(-o ConnectTimeout=10)
[ -n "$key" ] && ssh_opts+=(-i "$key")
root="$(cd "$(dirname "$0")/.." && pwd)"
archive="$(mktemp)"
git -C "$root" archive --format=tar HEAD jarvis windows_agent.toml scripts > "$archive"
scp -q "${ssh_opts[@]}" "$archive" "$host:jarvis_agent.tar"
rm -f "$archive"
read -r -d '' script <<'PS' || true
$dir = "$env:USERPROFILE\jarvis"
Set-Location $dir
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "jarvis.winagent" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
tar -xf "$env:USERPROFILE\jarvis_agent.tar"
Remove-Item "$env:USERPROFILE\jarvis_agent.tar"
$action = New-ScheduledTaskAction -Execute "$dir\.venv\Scripts\pythonw.exe" -Argument "-m jarvis.winagent --log-file data\winagent.log" -WorkingDirectory $dir
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive
Register-ScheduledTask -TaskName "JARVIS Agent relance" -Action $action -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName "JARVIS Agent relance"
Start-Sleep -Seconds 6
Unregister-ScheduledTask -TaskName "JARVIS Agent relance" -Confirm:$false
if (Get-NetTCPConnection -State Listen -LocalPort 8765 -ErrorAction SilentlyContinue) { "agent relancé" } else { "ÉCHEC : l'agent n'écoute pas" }
PS
encoded="$(printf '%s' "$script" | iconv -f UTF-8 -t UTF-16LE | base64 -w0)"
ssh "${ssh_opts[@]}" "$host" "powershell -NoProfile -EncodedCommand $encoded" 2>&1 | grep -v "CLIXML\|<Objs" || true
