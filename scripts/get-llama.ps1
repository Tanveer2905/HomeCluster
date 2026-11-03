# Downloads a pinned llama.cpp release into <project>\tools\llama.cpp (nothing outside the project).
#   win-x64\              extracted Windows CPU build (llama-server.exe, rpc-server.exe, ...) for the PC worker
#   llama-<tag>-bin-win-cpu-x64.zip       kept so other Windows workers can fetch it from the coordinator
#   llama-<tag>-bin-android-arm64.tar.gz  served to Termux phones on Android 9+ (arm64)
#   VERSION               the tag every device must run (the RPC protocol requires identical builds)
#   win-vulkan-x64\       (-Vulkan) GPU build for the PC; uses the graphics driver already installed,
#                         changes nothing outside this folder (no driver, PATH or system settings)
# Source: https://github.com/ggml-org/llama.cpp/releases
param(
    [string]$Tag = 'b11157',
    [switch]$NoAndroid,
    [switch]$Vulkan
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$dest = Join-Path $root 'tools\llama.cpp'
New-Item -ItemType Directory -Force -Path $dest | Out-Null
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = 'SilentlyContinue'
$base = "https://github.com/ggml-org/llama.cpp/releases/download/$Tag"

function Get-Asset([string]$name) {
    $out = Join-Path $dest $name
    if (Test-Path $out) { Write-Host "have $name"; return $out }
    Write-Host "downloading $name ..."
    $tmp = "$out.part"
    Invoke-WebRequest -Uri "$base/$name" -OutFile $tmp -UseBasicParsing
    Move-Item -Force $tmp $out
    Write-Host ("  {0:N1} MB" -f ((Get-Item $out).Length / 1MB))
    return $out
}

$old = Join-Path $dest 'VERSION'
if ((Test-Path $old) -and ((Get-Content $old -Raw).Trim() -ne $Tag)) {
    Write-Host "switching llama.cpp from $((Get-Content $old -Raw).Trim()) to $Tag"
    Remove-Item -Recurse -Force (Join-Path $dest 'win-x64') -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force (Join-Path $dest 'win-vulkan-x64') -ErrorAction SilentlyContinue
}

function Expand-Build([string]$zipPath, [string]$folder) {
    $probe = { Get-ChildItem $folder -Recurse -Include 'ggml-rpc-server.exe', 'rpc-server.exe' -ErrorAction SilentlyContinue | Select-Object -First 1 }
    if ((Test-Path $folder) -and (& $probe)) { return }
    if (Test-Path $folder) { Remove-Item -Recurse -Force $folder }
    Expand-Archive -Path $zipPath -DestinationPath $folder -Force
    $nested = & $probe
    if (-not $nested) { throw "no rpc server (ggml-rpc-server.exe / rpc-server.exe) in $zipPath" }
    # some releases nest the binaries one folder down
    if ($nested.DirectoryName -ne $folder) {
        Get-ChildItem $nested.DirectoryName | Move-Item -Destination $folder -Force
    }
}

$zip = Get-Asset "llama-$Tag-bin-win-cpu-x64.zip"
$win = Join-Path $dest 'win-x64'
# the RPC server was renamed rpc-server -> ggml-rpc-server in 2026 builds; Expand-Build accepts both
Expand-Build $zip $win
if ($Vulkan) {
    $vzip = Get-Asset "llama-$Tag-bin-win-vulkan-x64.zip"
    Expand-Build $vzip (Join-Path $dest 'win-vulkan-x64')
}
if (-not $NoAndroid) { Get-Asset "llama-$Tag-bin-android-arm64.tar.gz" | Out-Null }
Set-Content -Path $old -Value $Tag -Encoding ascii
$ErrorActionPreference = 'Continue'   # llama.cpp logs to stderr
$ver = & cmd /c "`"$(Join-Path $win 'llama-server.exe')`" --version 2>&1" | Select-String 'version' | Select-Object -First 1
Write-Host "llama.cpp $Tag ready in $dest  ($ver)"
$vk = Join-Path $dest 'win-vulkan-x64\llama-server.exe'
if (Test-Path $vk) {
    Write-Host 'GPU devices seen by the Vulkan build:'
    # set only inside this one cmd process: AMD's switchable-graphics Vulkan layer makes the
    # device listing loop forever on laptops with an AMD and an NVIDIA GPU
    & cmd /c "set DISABLE_LAYER_AMD_SWITCHABLE_GRAPHICS_1=1&& `"$vk`" --list-devices 2>&1" |
        Select-String -Pattern 'Vulkan\d' | ForEach-Object { Write-Host "  $($_.Line.Trim())" }
}
