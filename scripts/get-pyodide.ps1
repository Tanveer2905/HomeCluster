# Downloads Pyodide (CPython compiled to WebAssembly) into <project>\tools\pyodide
# so browser workers (e.g. the Samsung J2) can run python_map jobs without a JS twin.
# Source: the official npm package (https://www.npmjs.com/package/pyodide), ~11 MB.
# Everything stays inside the project folder; the coordinator serves it at /w/pyodide/.
param(
    [string]$Version = '0.26.4'
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$tools = Join-Path $root 'tools'
$dest = Join-Path $tools 'pyodide'
$tgz = Join-Path $tools "pyodide-$Version.tgz"
$tmp = Join-Path $tools 'pyodide-tmp'
$url = "https://registry.npmjs.org/pyodide/-/pyodide-$Version.tgz"

if (Test-Path (Join-Path $dest 'pyodide.asm.wasm')) {
    Write-Host "Pyodide already present at $dest"
    Get-Content (Join-Path $dest 'pyodide-lock.json') -TotalCount 1 | Out-Null
    exit 0
}

New-Item -ItemType Directory -Force -Path $tools | Out-Null
Write-Host "Downloading $url ..."
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = 'SilentlyContinue'
Invoke-WebRequest -Uri $url -OutFile $tgz -UseBasicParsing
Write-Host ("Downloaded {0:N1} MB" -f ((Get-Item $tgz).Length / 1MB))

if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
# Windows 10 ships bsdtar as tar.exe
& tar -xzf $tgz -C $tmp
if ($LASTEXITCODE -ne 0) { throw 'tar extraction failed' }
$pkg = Join-Path $tmp 'package'
$needed = @('pyodide.js', 'pyodide.asm.js', 'pyodide.asm.wasm', 'python_stdlib.zip', 'pyodide-lock.json')
foreach ($f in $needed) {
    if (-not (Test-Path (Join-Path $pkg $f))) { throw "missing $f in the Pyodide package" }
}
New-Item -ItemType Directory -Force -Path $dest | Out-Null
foreach ($f in $needed + @('pyodide.mjs', 'LICENSE', 'README.md')) {
    $src = Join-Path $pkg $f
    if (Test-Path $src) { Copy-Item $src $dest -Force }
}
Remove-Item -Recurse -Force $tmp
Remove-Item -Force $tgz
$size = (Get-ChildItem $dest | Measure-Object -Property Length -Sum).Sum
Write-Host ("Installed Pyodide {0} to {1} ({2:N1} MB). Restart the coordinator to serve it." -f $Version, $dest, ($size / 1MB))
