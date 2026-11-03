# Stops every PhoneCluster process started with this project's Python (tools\python or .venv): the coordinator
# (python -m coordinator) and any PC worker (worker\worker.py). Tries a normal close
# first, then forces after a few seconds. Safe to run when nothing is running.
$root = Split-Path -Parent $PSScriptRoot
$ours = @((Join-Path $root 'tools\python\python.exe').ToLower(), (Join-Path $root '.venv\Scripts\python.exe').ToLower())

function Find-Ours {
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $cmd = ($_.CommandLine + '').ToLower()
        $exe = ($_.ExecutablePath + '').ToLower()
        (@($ours | Where-Object { $exe -eq $_ -or $cmd.Contains($_) }).Count -gt 0) -and
        ($cmd.Contains('-m coordinator') -or $cmd.Contains('worker\worker.py') -or $cmd.Contains('worker/worker.py'))
    }
}

# the screen windows and the cluster's own adb server (tools\scrcpy); never any other adb
$scrcpyDir = (Join-Path $root 'tools\scrcpy\').ToLower()
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { ($_.ExecutablePath + '').ToLower().StartsWith($scrcpyDir) } |
    ForEach-Object {
        Write-Host "[PhoneCluster] stopping $($_.Name) (pid $($_.ProcessId))"
        try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {}
    }

$procs = @(Find-Ours)
if (-not $procs) {
    Write-Host '[PhoneCluster] nothing running'
    exit 0
}

# coordinator first (it stops its own worker on a clean exit), then any stray workers
$ordered = @($procs | Where-Object { $_.CommandLine -like '*-m coordinator*' }) +
           @($procs | Where-Object { $_.CommandLine -notlike '*-m coordinator*' })
foreach ($p in $ordered) {
    $what = if ($p.CommandLine -like '*-m coordinator*') { 'coordinator' } else { 'worker' }
    Write-Host "[PhoneCluster] stopping $what (pid $($p.ProcessId))"
    & taskkill /PID $p.ProcessId /T 2>$null | Out-Null   # polite close of the console tree
}

$deadline = (Get-Date).AddSeconds(3)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 500
    if (-not @(Find-Ours)) { break }
}
$left = @(Find-Ours)
foreach ($p in $left) {
    Write-Host "[PhoneCluster] forcing pid $($p.ProcessId)"
    try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch {}
}
Start-Sleep -Milliseconds 300
if (@(Find-Ours)) { Write-Host '[PhoneCluster] some processes could not be stopped'; exit 1 }
Write-Host '[PhoneCluster] stopped'
