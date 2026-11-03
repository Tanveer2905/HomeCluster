# Downloads a pinned scrcpy release into <project>\tools\scrcpy (nothing outside the project).
#   scrcpy.exe     shows an Android phone's screen in a window on this PC and lets you control it
#   adb.exe        the copy of adb that ships with scrcpy (the cluster uses this one, not any other)
#   VERSION        the pinned tag
# Nothing is installed on the phone or the PC: no PATH change, no registry, no service.  The phone
# needs USB debugging switched on (docs/PHONE-SETUP.md).  The adb key the cluster uses lives in
# <project>\data\adb, not in the user's home folder.
# Source: https://github.com/Genymobile/scrcpy/releases
param(
    [string]$Tag = 'v5.0.1',
    # SHA-256 from the release's SHA256SUMS.txt for scrcpy-win64-<tag>.zip; checked before extracting
    [string]$Sha256 = 'b12a2c4ee8be317422451fc7dcf8ee20a71b5ea7ef9ad73ddd825a25316227a5'
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$dest = Join-Path $root 'tools\scrcpy'
New-Item -ItemType Directory -Force -Path $dest | Out-Null
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = 'SilentlyContinue'

$exe = Join-Path $dest 'scrcpy.exe'
$version = Join-Path $dest 'VERSION'
if ((Test-Path $exe) -and (Test-Path $version) -and ((Get-Content $version -Raw).Trim() -eq $Tag)) {
    Write-Host "scrcpy $Tag already in $dest"
    exit 0
}

$name = "scrcpy-win64-$Tag.zip"
$zip = Join-Path $dest $name
Write-Host "downloading $name ..."
Invoke-WebRequest -Uri "https://github.com/Genymobile/scrcpy/releases/download/$Tag/$name" `
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
$found = Get-ChildItem $unpack -Recurse -Filter 'scrcpy.exe' | Select-Object -First 1
if (-not $found) { throw "no scrcpy.exe in $name" }
# the release is one folder: scrcpy.exe, adb.exe and their DLLs, scrcpy-server, licence
Get-ChildItem $dest -Exclude 'unpack', $name | Remove-Item -Recurse -Force
Get-ChildItem $found.DirectoryName | Copy-Item -Destination $dest -Recurse -Force
Remove-Item -Recurse -Force $unpack
Remove-Item -Force $zip
Set-Content -Path $version -Value $Tag -Encoding ascii
$ver = (& $exe --version 2>&1 | Select-Object -First 1)
Write-Host "$ver ready in $dest"
exit 0
