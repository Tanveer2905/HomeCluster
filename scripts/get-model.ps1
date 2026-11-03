# Downloads a GGUF model into <project>\data\models (resumable). The coordinator lists every .gguf
# there on the Models page; distributed LLM deployments pick from it.
#   scripts\get-model.ps1                         # the small test model (Qwen2.5 0.5B Instruct, Q4_K_M)
#   scripts\get-model.ps1 -Url <https://.../file.gguf>
param(
    [string]$Url = 'https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf',
    [string]$Name = ''
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$dest = Join-Path $root 'data\models'
New-Item -ItemType Directory -Force -Path $dest | Out-Null
if (-not $Name) { $Name = [IO.Path]::GetFileName(([Uri]$Url).AbsolutePath) }
if (-not $Name.ToLower().EndsWith('.gguf')) { throw "expected a .gguf file, got $Name" }
$out = Join-Path $dest $Name
if (Test-Path $out) { Write-Host "already have $out ($([math]::Round((Get-Item $out).Length/1MB)) MB)"; exit 0 }
$part = "$out.part"
Write-Host "downloading $Name ..."
# curl.exe ships with Windows 10 and resumes (-C -) if the download was interrupted.
# Progress goes to stderr, which PowerShell would treat as an error: keep curl quiet.
$ErrorActionPreference = 'Continue'
& curl.exe -L --fail --silent --show-error --retry 3 -C - -o $part $Url
$code = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
if ($code -ne 0) { throw "download failed (curl exit $code); run again to resume" }
Move-Item -Force $part $out
Write-Host ("saved {0} ({1:N0} MB)" -f $out, ((Get-Item $out).Length / 1MB))
