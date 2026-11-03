# Downloads a pinned wasmtime release into <project>\tools\wasmtime (nothing outside the project).
#   wasmtime.exe   the WebAssembly runtime the PC worker uses for `wasm` tasks
#   VERSION        the pinned tag
# Phones get wasmtime from Termux (`pkg install wasmtime`, done by the worker on the first
# WebAssembly task); browsers run WebAssembly themselves.  Nothing is installed or configured on
# the system: no PATH change, no registry, no service.
# Source: https://github.com/bytecodealliance/wasmtime/releases
param(
    [string]$Tag = 'v49.0.4',
    # SHA-256 GitHub publishes for wasmtime-<tag>-x86_64-windows.zip; checked before extracting
    [string]$Sha256 = '00d14880dad5b490fe5f547785a9d6c02d40f24baa289daaf110f35fc0193885'
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$dest = Join-Path $root 'tools\wasmtime'
New-Item -ItemType Directory -Force -Path $dest | Out-Null
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = 'SilentlyContinue'

$exe = Join-Path $dest 'wasmtime.exe'
$version = Join-Path $dest 'VERSION'
if ((Test-Path $exe) -and (Test-Path $version) -and ((Get-Content $version -Raw).Trim() -eq $Tag)) {
    Write-Host "wasmtime $Tag already in $dest"
    exit 0
}

$name = "wasmtime-$Tag-x86_64-windows.zip"
$zip = Join-Path $dest $name
Write-Host "downloading $name ..."
Invoke-WebRequest -Uri "https://github.com/bytecodealliance/wasmtime/releases/download/$Tag/$name" `
    -OutFile "$zip.part" -UseBasicParsing
$hash = (Get-FileHash -Algorithm SHA256 "$zip.part").Hash.ToLowerInvariant()
if ($Sha256 -and $hash -ne $Sha256.ToLowerInvariant()) {
    Remove-Item -Force "$zip.part"
    throw "checksum mismatch for $name (got $hash, expected $Sha256)"
}
Move-Item -Force "$zip.part" $zip
Write-Host ("  {0:N1} MB, sha256 ok" -f ((Get-Item $zip).Length / 1MB))

$unpack = Join-Path $dest 'unpack'
if (Test-Path $unpack) { Remove-Item -Recurse -Force $unpack }
Expand-Archive -Path $zip -DestinationPath $unpack -Force
$found = Get-ChildItem $unpack -Recurse -Filter 'wasmtime.exe' | Select-Object -First 1
if (-not $found) { throw "no wasmtime.exe in $name" }
Copy-Item -Force $found.FullName $exe
Get-ChildItem $found.DirectoryName -Filter 'LICENSE*' | Copy-Item -Destination $dest -Force
Remove-Item -Recurse -Force $unpack
Remove-Item -Force $zip
Set-Content -Path $version -Value $Tag -Encoding ascii
$ver = & $exe --version
Write-Host "$ver ready in $dest"
