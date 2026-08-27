# Task Scheduler wrapper (Windows): nightly LLM review at 6 am.
$ErrorActionPreference = "Stop"

$RepoDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$KeyFile = if ($env:WEATHERBOT_KEY_FILE) { $env:WEATHERBOT_KEY_FILE } else { Join-Path $env:USERPROFILE ".weatherbot.env" }
$LogDir  = Join-Path $env:LOCALAPPDATA "weatherbot\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir "review.log"

if (Test-Path $KeyFile) {
    Get-Content $KeyFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2].Trim(), "Process")
        }
    }
}

$env:Path = "$env:USERPROFILE\.local\bin;$env:USERPROFILE\.cargo\bin;$env:Path"

Set-Location $RepoDir
Add-Content -Path $LogFile -Value "$(Get-Date -Format o) review starting"
& uv run python scripts/review.py 2>&1 | ForEach-Object { Add-Content -Path $LogFile -Value $_ }
$code = $LASTEXITCODE
Add-Content -Path $LogFile -Value "$(Get-Date -Format o) review finished rc=$code"
exit $code
