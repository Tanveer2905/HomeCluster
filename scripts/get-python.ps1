# The cluster's own Python, inside the project: <project>\tools\python
#
# Python's official "embeddable" package is a plain zip: no installer runs, so nothing is
# written to the registry, PATH, the Start menu, file associations or the py launcher, and
# the system Python is untouched.  The python312._pth file written here makes it ignore
# PYTHON* environment variables and the user's site-packages (isolated mode), and puts the
# project folder and sdk\ on its path.  The cluster's libraries go into tools\python\Lib\
# site-packages; the pip cache stays in <project>\.pip-cache.
#
# Why: a Windows firewall rule can then name <project>\tools\python\python.exe alone, so it
# opens nothing for any other program (a venv's python.exe only launches the system Python).
#
# 3.12.10 is the newest 3.12 with a Windows package (later 3.12 releases are source-only).
# python.org publishes an MD5 for it; python.exe must also carry the Python Software
# Foundation's Authenticode signature.  Source: https://www.python.org/downloads/release/python-31210/
param(
    [string]$Version = '3.12.10',
    [string]$Md5 = 'fe8ef205f2e9c3ba44d0cf9954e1abd3'
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$tools = Join-Path $root 'tools'
$dest = Join-Path $tools 'python'
$exe = Join-Path $dest 'python.exe'
$site = Join-Path $dest 'Lib\site-packages'
$env:PIP_CACHE_DIR = Join-Path $root '.pip-cache'
# ignore pip settings files outside the project (C:\ProgramData\pip, %APPDATA%\pip, ...)
$env:PIP_CONFIG_FILE = 'NUL'
Get-ChildItem Env: | Where-Object { $_.Name -like 'PIP_*' -and $_.Name -notin @('PIP_CACHE_DIR', 'PIP_CONFIG_FILE') } |
    ForEach-Object { Remove-Item "Env:$($_.Name)" }
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = 'SilentlyContinue'
New-Item -ItemType Directory -Force -Path $tools | Out-Null

if (-not (Test-Path $exe)) {
    $name = "python-$Version-embed-amd64.zip"
    $zip = Join-Path $tools $name
    Write-Host "downloading $name ..."
    Invoke-WebRequest -Uri "https://www.python.org/ftp/python/$Version/$name" -OutFile "$zip.part" -UseBasicParsing
    $hash = (Get-FileHash -Algorithm MD5 "$zip.part").Hash.ToLowerInvariant()
    if ($hash -ne $Md5.ToLowerInvariant()) {
        Remove-Item -Force "$zip.part"
        throw "checksum mismatch for $name (got $hash, expected $Md5)"
    }
    Move-Item -Force "$zip.part" $zip
    $unpack = Join-Path $tools 'python.unpack'
    if (Test-Path $unpack) { Remove-Item -Recurse -Force $unpack }
    Expand-Archive -Path $zip -DestinationPath $unpack -Force
    $sig = Get-AuthenticodeSignature (Join-Path $unpack 'python.exe')
    if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
        Remove-Item -Recurse -Force $unpack
        throw "python.exe is not validly signed by the Python Software Foundation ($($sig.Status))"
    }
    Write-Host ("  {0:N1} MB, checksum ok, signed by the Python Software Foundation" -f ((Get-Item $zip).Length / 1MB))
    if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
    Move-Item $unpack $dest
    Remove-Item -Force $zip
}

# Search path: the standard library, its own site-packages, the project and the SDK -
# nothing else.  Any ._pth file switches the interpreter to isolated mode (PYTHON*
# variables ignored); leaving out 'import site' also keeps the user's own package folder
# (%APPDATA%\Python) out for good.  None of the cluster's libraries needs .pth hooks.
$pth = Get-ChildItem $dest -Filter 'python3*._pth' | Select-Object -First 1
if (-not $pth) { throw "no ._pth file in $dest" }
$zipName = (Get-ChildItem $dest -Filter 'python3*.zip' | Select-Object -First 1).Name
@($zipName, '.', 'Lib\site-packages', '..\..', '..\..\sdk') |
    Set-Content -Path $pth.FullName -Encoding ascii
New-Item -ItemType Directory -Force -Path $site | Out-Null

# from here on native programs report through exit codes (they may print to stderr)
$ErrorActionPreference = 'Continue'

# pip: copied in once by any Python that has it (the project's .venv, else the system one)
& $exe -c "import pip" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    $boot = Join-Path $root '.venv\Scripts\python.exe'
    if (-not (Test-Path $boot)) {
        $launcher = Get-Command py -ErrorAction SilentlyContinue
        $boot = if ($launcher) { 'py' } else { 'python' }
    }
    Write-Host "putting pip into tools\python (using $boot) ..."
    if ($boot -eq 'py') { & py -3.12 -m pip install --quiet --target $site pip }
    else { & $boot -m pip install --quiet --target $site pip }
    if ($LASTEXITCODE -ne 0) { throw 'could not put pip into tools\python' }
}

Write-Host 'installing the cluster''s libraries into tools\python ...'
& $exe -m pip install --quiet --no-warn-script-location -r (Join-Path $root 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
$ver = & $exe -c "import sys; print(sys.version.split()[0], 'isolated' if sys.flags.isolated and 'site' not in sys.modules else 'NOT isolated')"
Write-Host "Python $ver ready in $dest"
Write-Host ''
Write-Host 'Windows firewall (optional, once, in an administrator PowerShell) - lets phones reach this program only:'
Write-Host "  netsh advfirewall firewall add rule name=""PhoneCluster"" dir=in action=allow program=""$exe"" protocol=TCP localport=8765 profile=private remoteip=localsubnet"
