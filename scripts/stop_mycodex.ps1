# Stop THIS project's python/pythonw processes (any Python flavor: .venv, Anaconda, system).
# Called by restart_service.py via:
#   powershell -NoProfile -ExecutionPolicy Bypass -File stop_mycodex.ps1 -CallerPid <int>
# CallerPid is excluded so the restarting script never kills itself
# (its own ExecutablePath contains the project root and would otherwise match).
#
# IMPORTANT: matching is scoped to this project's root path ONLY. Never match
# generic patterns like 'tray.pyw' / 'app.main' — those would also kill sibling
# MyCodex-family deployments on the same machine (e.g. the mycodex project).
# Prints one "killed <pid> <name>" line per terminated process.

param([int]$CallerPid = 0)

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path.ToLower()

$procs = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match 'python' -and
    $_.ProcessId -ne $CallerPid -and
    # Never kill interactive scripts: the setup wizard triggers restarts, and
    # import_wecom_group.py must survive its own stop_local_service() call
    # (it pauses the service to own the WeCom single connection while binding).
    # Both run under .venv\Scripts\python.exe whose ExecutablePath contains the
    # project root and would otherwise match.
    -not ($_.CommandLine -and $_.CommandLine -match 'setup_wizard\.py|import_wecom_group\.py') -and
    (
        ($_.CommandLine -and $_.CommandLine.ToLower().Contains($root)) -or
        ($_.ExecutablePath -and $_.ExecutablePath.ToLower().Contains($root))
    )
}

foreach ($p in $procs) {
    try {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
        Write-Output ("killed {0} {1}" -f $p.ProcessId, $p.Name)
    } catch {
        Write-Output ("failed {0}: {1}" -f $p.ProcessId, $_.Exception.Message)
    }
}
