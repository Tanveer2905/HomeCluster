# CLI wrapper: scripts\pc.ps1 devices | jobs | submit ... (see docs/SPEC.md section 13)
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root 'tools\python\python.exe'   # the cluster's own Python (scripts\get-python.ps1)
if (-not (Test-Path $py)) { $py = Join-Path $root '.venv\Scripts\python.exe' }
if (-not (Test-Path $py)) { $py = 'python' }
$env:PC_ROOT = $root
$env:PYTHONPATH = (Join-Path $root 'sdk') + ';' + $env:PYTHONPATH
& $py -m phonecluster @args
