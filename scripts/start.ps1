# Start the Octopus server on this machine.
#
# Double-click `start.cmd` next to this file, or from a console:
#
#   scripts\start.ps1                 start it (detached) and open the UI
#   scripts\start.ps1 -NoBrowser      ...without opening a browser
#   scripts\start.ps1 -Foreground     run it in this window, logs and all
#
# Three things this gets right that a hand-typed `python -m server.cli serve`
# does not, each of them learned the hard way on this repo:
#
#   * The working directory is the checkout. `octopus.db` is a *relative* path
#     (`config.db_path`), so a server started from anywhere else quietly opens a
#     different database -- with none of the user's sessions in it.
#   * A server already answering on the port is left alone. Starting a second one
#     fails to bind and exits 1, which looks like a crash and, twice in one
#     session, was mistaken for one.
#   * Its output goes to files under `logs\`, and a failure to come up prints the
#     tail of them instead of vanishing with the window.
#
# ASCII only, on purpose: Windows PowerShell 5.1 reads a BOM-less script as ANSI,
# so a stray non-ASCII character becomes a parse error the moment anyone edits
# this file with the wrong tool.

[CmdletBinding()]
param(
    [switch]$NoBrowser,
    [switch]$Foreground,
    # Passed by start.cmd: a double-clicked window must stay open long enough to
    # be read when something goes wrong. A console invocation wants no prompt.
    [switch]$Pause
)

$ErrorActionPreference = 'Stop'
$Repo = Split-Path -Parent $PSScriptRoot
Set-Location $Repo

$Host.UI.RawUI.WindowTitle = 'Octopus'
$Port = if ($env:OCTOPUS_PORT) { [int]$env:OCTOPUS_PORT } else { 8000 }
$Url = "http://127.0.0.1:$Port"

function Say($message) { Write-Host $message }
function Fail($message) {
    Write-Host ''
    Write-Host "Could not start: $message" -ForegroundColor Red
    Write-Host ''
    if ($Pause) { Read-Host 'Press Enter to close' | Out-Null }
    exit 1
}

# --------------------------------------------------------------- interpreter
# The checkout's own venv, which is what everything else here uses. Falling back
# to `python` on PATH is what a person would type by hand, so it is worth a try
# -- but it has to be able to import the app, or the server dies on line one.
$Python = Join-Path $Repo '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) {
    $onPath = Get-Command python -ErrorAction SilentlyContinue
    if (-not $onPath) { Fail "no interpreter: neither $Python nor python on PATH" }
    $Python = $onPath.Source
    Say "note: using $Python (the checkout has no .venv)"
}
& $Python -c 'import fastapi, uvicorn' 2>$null
if ($LASTEXITCODE -ne 0) {
    Fail "$Python cannot run the server (dependencies missing). In the checkout run:`n  $Python -m pip install -e `".[test]`""
}

# ------------------------------------------------------------- already up?
function Test-Octopus {
    try {
        $r = Invoke-WebRequest "$Url/health" -UseBasicParsing -TimeoutSec 3
        return ($r.StatusCode -eq 200)
    } catch {
        return $false
    }
}

if (Test-Octopus) {
    Say "already running: $Url"
    if (-not $NoBrowser) { Start-Process $Url }
    exit 0
}

# A port held by something that is not Octopus is a different problem, and one
# worth naming rather than letting uvicorn say "address already in use".
$busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    $owner = (Get-Process -Id $busy[0].OwningProcess -ErrorAction SilentlyContinue).ProcessName
    Fail "port $Port is held by pid $($busy[0].OwningProcess) ($owner), which is not answering /health"
}

# ----------------------------------------------------------------------- logs
$LogDir = Join-Path $Repo 'logs'
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$OutLog = Join-Path $LogDir 'server.out.log'
$ErrLog = Join-Path $LogDir 'server.err.log'

# ---------------------------------------------------------------------- start
if ($Foreground) {
    Say "running in this window; logs also go to $OutLog"
    Say "address: $Url   (Ctrl+C to stop)"
    & $Python -u -m server.cli serve
    exit $LASTEXITCODE
}

Say "starting Octopus from $Repo ..."
$proc = Start-Process -FilePath $Python `
    -ArgumentList '-u', '-m', 'server.cli', 'serve' `
    -WorkingDirectory $Repo `
    -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog

for ($i = 0; $i -lt 45; $i++) {
    Start-Sleep -Seconds 1
    if ($proc.HasExited) {
        Say ''
        Say "the server started and then exited (exit $($proc.ExitCode)). Last log lines:" -ForegroundColor Red
        Get-Content $ErrLog -Tail 15 -ErrorAction SilentlyContinue
        Get-Content $OutLog -Tail 15 -ErrorAction SilentlyContinue
        Fail "see the log files: $OutLog and $ErrLog"
    }
    if (Test-Octopus) {
        Say "up: $Url   (pid $($proc.Id))"
        Say "log: $OutLog"
        if (-not $NoBrowser) { Start-Process $Url }
        exit 0
    }
}

$tail = (Get-Content $ErrLog -Tail 15 -ErrorAction SilentlyContinue) -join "`n"
Fail "it did not answer /health within 45s. Last log lines:`n$tail"
