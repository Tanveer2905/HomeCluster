# One-time setup: the cluster's own Python + its libraries + data folders, all inside the
# project (scripts\get-python.ps1: Python's embeddable package in tools\python; no installer,
# registry, PATH or user-folder changes).  Nothing is installed outside the project folder.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
& (Join-Path $PSScriptRoot 'get-python.ps1')
if (-not (Test-Path (Join-Path $root 'tools\python\python.exe'))) { throw 'setting up tools\python failed' }

foreach ($d in 'data', 'data\files', 'data\storage\cache', 'data\logs\tasks', 'data\worker', 'data\plugins', 'tools') {
    New-Item -ItemType Directory -Force -Path (Join-Path $root $d) | Out-Null
}
Write-Host ''
Write-Host 'Setup complete. Next:'
Write-Host '  scripts\get-ffmpeg.ps1   (optional, enables media jobs on the PC worker)'
Write-Host '  scripts\start.ps1        (starts the coordinator + local PC worker)'
