# Downloads a static ffmpeg build into <project>\tools\ffmpeg (nothing outside the project).
# Source: https://www.gyan.dev/ffmpeg/builds/ (release-essentials, ~90 MB zip)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$tools = Join-Path $root 'tools'
$dest = Join-Path $tools 'ffmpeg'
$zip = Join-Path $tools 'ffmpeg-release-essentials.zip'
$tmp = Join-Path $tools 'ffmpeg-tmp'
$url = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'

if (Test-Path (Join-Path $dest 'ffmpeg.exe')) {
    Write-Host "ffmpeg already present at $dest"
    & (Join-Path $dest 'ffmpeg.exe') -version | Select-Object -First 1
    exit 0
}

New-Item -ItemType Directory -Force -Path $tools | Out-Null
Write-Host "Downloading $url ..."
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = 'SilentlyContinue'
Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
Write-Host ("Downloaded {0:N1} MB" -f ((Get-Item $zip).Length / 1MB))

if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
Expand-Archive -Path $zip -DestinationPath $tmp -Force
$bin = Get-ChildItem -Path $tmp -Recurse -Filter 'ffmpeg.exe' | Select-Object -First 1
if (-not $bin) { throw 'ffmpeg.exe not found inside the archive' }
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Copy-Item (Join-Path $bin.DirectoryName 'ffmpeg.exe') $dest -Force
Copy-Item (Join-Path $bin.DirectoryName 'ffprobe.exe') $dest -Force -ErrorAction SilentlyContinue
$lic = Get-ChildItem -Path $tmp -Recurse -Filter 'LICENSE*' | Select-Object -First 1
if ($lic) { Copy-Item $lic.FullName (Join-Path $dest 'LICENSE.txt') -Force }
Remove-Item -Recurse -Force $tmp
Remove-Item -Force $zip
Write-Host "Installed to $dest"
& (Join-Path $dest 'ffmpeg.exe') -version | Select-Object -First 1
