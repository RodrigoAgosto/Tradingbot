# Task Scheduler wrapper (Windows): sources the key env file, verifies its
# NTFS ACL is locked down, runs one cycle, and appends output to the log.
# Secrets stay in the key file, never in the scheduled task definition.
$ErrorActionPreference = "Stop"

$RepoDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$KeyFile = if ($env:WEATHERBOT_KEY_FILE) { $env:WEATHERBOT_KEY_FILE } else { Join-Path $env:USERPROFILE ".weatherbot.env" }
$LogDir  = Join-Path $env:LOCALAPPDATA "weatherbot\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir "cycle.log"

function Write-Log([string]$msg) {
    Add-Content -Path $LogFile -Value "$(Get-Date -Format o) $msg"
}

if (Test-Path $KeyFile) {
    # Refuse to run if the key file is readable by anyone but this user
    # (and the SYSTEM/Administrators accounts that own the machine anyway).
    $acl = Get-Acl $KeyFile
    $allowed = @(
        "NT AUTHORITY\SYSTEM",
        "BUILTIN\Administrators",
        "$env:USERDOMAIN\$env:USERNAME",
        "$env:COMPUTERNAME\$env:USERNAME"
    )
    $bad = $acl.Access | Where-Object {
        $allowed -notcontains $_.IdentityReference.Value
    }
    if ($bad) {
        $who = ($bad | ForEach-Object { $_.IdentityReference.Value }) -join ", "
        Write-Log "REFUSING TO RUN: $KeyFile is accessible to: $who. Fix with: icacls `"$KeyFile`" /inheritance:r /grant:r `"$($env:USERNAME):(R,W)`""
        exit 1
    }

    # Load KEY=VALUE lines into this process's environment.
    Get-Content $KeyFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2].Trim(), "Process")
        }
    }
}

# Common uv install locations, in case the task runs before PATH is set up.
$env:Path = "$env:USERPROFILE\.local\bin;$env:USERPROFILE\.cargo\bin;$env:Path"

Set-Location $RepoDir
Write-Log "cycle starting"
& uv run weatherbot cycle 2>&1 | ForEach-Object { Add-Content -Path $LogFile -Value $_ }
$code = $LASTEXITCODE
Write-Log "cycle finished rc=$code"
exit $code
