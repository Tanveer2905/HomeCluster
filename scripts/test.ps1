# Runs the test suite with the cluster's own Python. Extra args are passed to pytest (e.g. scripts\test.ps1 -k api).
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root 'tools\python\python.exe'   # the cluster's own Python (scripts\get-python.ps1)
if (-not (Test-Path $py)) { $py = Join-Path $root '.venv\Scripts\python.exe' }
$env:PC_ROOT = $root
$env:PYTHONPATH = (Join-Path $root 'sdk') + ';' + $root
Set-Location $root
& $py -m pytest -q @args
