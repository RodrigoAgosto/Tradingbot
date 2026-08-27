# weatherbot installer for Windows (Task Scheduler).
# Run from a normal PowerShell prompt in the repo (no admin needed for the
# scheduled tasks; the power-settings step at the end wants an elevated one):
#   powershell -ExecutionPolicy Bypass -File ops\install.ps1
$ErrorActionPreference = "Stop"

$RepoDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$KeyFile = Join-Path $env:USERPROFILE ".weatherbot.env"
$LogDir  = Join-Path $env:LOCALAPPDATA "weatherbot\logs"

Write-Host "==> repo: $RepoDir"

Write-Host "==> creating log dir $LogDir"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

Write-Host "==> key file"
if (-not (Test-Path $KeyFile)) {
    Copy-Item (Join-Path $RepoDir ".env.example") $KeyFile
    Write-Host "    created $KeyFile from .env.example - edit it with your values"
}
# Owner-only ACL: the NTFS equivalent of chmod 600. run_cycle.ps1 refuses
# to start if anyone else is later granted access.
icacls $KeyFile /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
Write-Host "    ACL on $KeyFile restricted to $env:USERNAME"

Write-Host "==> syncing python environment (paper mode: no order client installed)"
Push-Location $RepoDir
uv sync
uv run weatherbot init-db
Pop-Location

Write-Host "==> registering scheduled tasks"
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

# Cycle: at logon, then every 20 minutes, indefinitely (StartInterval 1200 +
# RunAtLoad equivalent).
$cycleAction = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$RepoDir\ops\run_cycle.ps1`""
$cycleTriggers = @(
    (New-ScheduledTaskTrigger -AtLogOn),
    (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes 20) `
        -RepetitionDuration (New-TimeSpan -Days 3650))
)
Register-ScheduledTask -TaskName "weatherbot-cycle" -Action $cycleAction `
    -Trigger $cycleTriggers -Settings $settings -Force | Out-Null
Write-Host "    registered weatherbot-cycle (every 20 min + at logon)"

# Nightly review at 6:00.
$reviewAction = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$RepoDir\ops\run_review.ps1`""
$reviewTrigger = New-ScheduledTaskTrigger -Daily -At 6:00AM
$reviewSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 45)
Register-ScheduledTask -TaskName "weatherbot-review" -Action $reviewAction `
    -Trigger $reviewTrigger -Settings $reviewSettings -Force | Out-Null
Write-Host "    registered weatherbot-review (daily 06:00)"

@"

==> DONE. Manual steps you must do by hand:

1. Edit the key file (secrets, alerts config):
     notepad $KeyFile
   and fill in config.yaml (heartbeat URL, telegram chat_id, email addrs).

2. Keep the machine awake and self-recovering (ELEVATED PowerShell):
     powercfg /change standby-timeout-ac 0
     powercfg /change hibernate-timeout-ac 0
     powercfg /change monitor-timeout-ac 10
   Auto-restart after power failure is a BIOS/UEFI setting on PCs:
   enable "Restore on AC Power Loss" (or similar) in firmware setup.

3. The tasks run in your user session. For unattended reboots either:
   - enable auto-login (Settings > Accounts, or 'netplwiz'), OR
   - re-register both tasks to run whether the user is logged on or not:
     open Task Scheduler > weatherbot-cycle > Properties >
     "Run whether user is logged on or not" (you'll be asked for your
     password; repeat for weatherbot-review).

4. Create a healthchecks.io check: schedule = every 20 minutes,
   grace = 30 minutes. Paste its ping URL into config.yaml under
   heartbeat.url.

5. Verify the job fires:
     Get-ScheduledTask weatherbot-cycle | Get-ScheduledTaskInfo
     Get-Content "$LogDir\cycle.log" -Tail 30 -Wait
"@ | Write-Host
