# Starts the coordinator (and, unless -NoWorker, the local PC worker) with the cluster's own Python.
param(
    [switch]$NoWorker,
    [int]$Port = 0
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root 'tools\python\python.exe'   # the cluster's own Python (scripts\get-python.ps1)
if (-not (Test-Path $py)) { $py = Join-Path $root '.venv\Scripts\python.exe' }
if (-not (Test-Path $py)) { Write-Host 'Python missing - run scripts\setup.ps1 first'; exit 1 }
$env:PC_ROOT = $root
$env:PC_DATA_DIR = Join-Path $root 'data'
$args = @('-m', 'coordinator')
if ($NoWorker) { $args += '--no-worker' }
if ($Port -gt 0) { $args += @('--port', "$Port") }
Set-Location $root
& $py @args
