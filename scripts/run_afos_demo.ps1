# AFOS Integration Test Runner and Dashboard Preview (PowerShell launcher).
# Runs scripts\run_afos_demo.py using the project's existing virtual
# environment - never modifies any existing component.

$ErrorActionPreference = "Stop"
$repoRoot = Join-Path $PSScriptRoot ".."
Set-Location -Path $repoRoot

$pythonExe = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonExe)) {
    Write-Error "Could not find .venv\Scripts\python.exe - activate/create the project's virtual environment first."
    exit 1
}

& $pythonExe "scripts\run_afos_demo.py"
exit $LASTEXITCODE
