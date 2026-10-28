# Entropy startup (Windows PowerShell). Creates the data layout, runs the intake chain once, then serves the desk.
#   .\entropy.ps1 [-Home C:\Users\you\Entropy] [-IntakeOnly]
param([string]$Home = "", [switch]$IntakeOnly)
$ErrorActionPreference = "Stop"
$pkg = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:PYTHONPATH = $pkg
if ($Home -ne "") { $env:ENTROPY_HOME = $Home }
if (-not $env:ENTROPY_HOME) { $env:ENTROPY_HOME = Join-Path $env:USERPROFILE "Entropy" }
$PY = if ($env:PYTHON) { $env:PYTHON } else { "python" }
& $PY -c "import fastapi, uvicorn" 2>$null
if ($LASTEXITCODE -ne 0) { Write-Host "Installing requirements (first run)..."; & $PY -m pip install -q -r (Join-Path $pkg "requirements.txt") }
& $PY -m pipeline.bootstrap
& $PY -m pipeline.intake_flow --apply
if ($IntakeOnly) { exit 0 }
$PORT = if ($env:ENTROPY_PORT) { $env:ENTROPY_PORT } else { "8646" }
Write-Host ""
Write-Host "Entropy desk: http://127.0.0.1:$PORT   (home: $env:ENTROPY_HOME)   Ctrl+C stops it."
& $PY -c "import uvicorn; from core.server import create_app; uvicorn.run(create_app(), host='127.0.0.1', port=int('$PORT'), log_level='warning')"
