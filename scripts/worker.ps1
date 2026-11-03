# Runs only the PC worker (useful when the coordinator was started with -NoWorker).
# On the PC it uses the PC-only door (http://127.0.0.1:8764).  It joins with a one-time
# code from the Join page (-Join), or with the PIN since it runs on the PC itself (-Pin).
param(
    [string]$Url = 'http://127.0.0.1:8764',
    [string]$Join = '',
    [string]$Pin = '',
    [string]$Name = '',
    [int]$Slots = 0
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root 'tools\python\python.exe'   # the cluster's own Python (scripts\get-python.ps1)
if (-not (Test-Path $py)) { $py = Join-Path $root '.venv\Scripts\python.exe' }
if (-not (Test-Path $py)) { $py = 'python' }
$state = Join-Path $root 'data\worker'
New-Item -ItemType Directory -Force -Path $state | Out-Null
$env:PC_ROOT = $root
$args = @((Join-Path $root 'worker\worker.py'), '--state-dir', $state)
if ($Url) { $args += @('--url', $Url) }
if ($Join) { $args += @('--join', $Join) }
if ($Pin) { $env:PC_PIN = $Pin }   # environment, not the command line (other programs can read that)
if ($Name) { $args += @('--name', $Name) }
if ($Slots -gt 0) { $args += @('--slots', "$Slots") }
& $py @args
