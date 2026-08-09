#Requires -Version 5.1
<#
.SYNOPSIS
  Install the Keel stack as Windows Scheduled Tasks: start at boot, run whether
  or not anyone is logged on, restart on failure, rotate their own logs.

.DESCRIPTION
  The Windows counterpart of install-multiasset-services.sh, same four verbs.
  Scheduled Tasks rather than NSSM or a service wrapper: it ships with Windows,
  needs no third-party binary on a machine that manages money, and survives a
  reboot without anyone signing in — which is the whole point, because the
  promotion gate needs the paper clock running for weeks without a gap.

  Three tasks under the \Keel\ folder, one per process:
      Keel-server       server.py         engine + EA endpoints  (port 8766)
      Keel-dashboard    dashboard_api.py  control dashboard      (port 8767)
      Keel-newsagent    news_agent.py     news monitor           (no listener)
  plus Keel-healthprobe, the watchdog that restarts whichever of the two ports
  has stopped answering.

  Registering, starting, stopping and removing tasks under \Keel\ needs an
  elevated shell. `status`, `selftest` and `probe -DryRun` do not.

.EXAMPLE
  .\keel-services.ps1 install -Python C:\venvs\keel\Scripts\python.exe
.EXAMPLE
  .\keel-services.ps1 install -DryRun        # print what would be registered
.EXAMPLE
  .\keel-services.ps1 status
.EXAMPLE
  .\keel-services.ps1 restart -Service server
#>
[CmdletBinding()]
param(
    # Default is the read-only verb on purpose: an install is the one that
    # touches the machine, so it should be typed deliberately.
    [Parameter(Position = 0)]
    [ValidateSet('install', 'status', 'uninstall', 'restart', 'probe', 'selftest')]
    [string]$Verb = 'status',

    [ValidateSet('server', 'dashboard', 'newsagent', 'all')]
    [string]$Service = 'all',

    [string]$Python,

    # System needs no password and is genuinely independent of any logon.
    # CurrentUser uses S4U — also passwordless, also runs logged off, but the
    # account needs the "Log on as a batch job" right.
    [ValidateSet('System', 'CurrentUser')]
    [string]$RunAs = 'System',

    [int]$LogMaxMB = 20,
    [int]$LogKeep = 5,
    [int]$ProbeMinutes = 2,
    [int]$ProbeGraceSeconds = 180,
    [int]$MaxRestartsPerHour = 5,

    [switch]$AllowCloudSyncedPath,
    [switch]$StopExisting,
    [switch]$NoStart,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'keel-common.ps1')

$paths  = Get-KeelPaths
$PsExe  = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'


# ------------------------------------------------------------- preflight
function Resolve-KeelPython {
    param([string]$Explicit)
    $candidates = @()
    if ($Explicit) { $candidates += $Explicit }
    if ($env:SLC_PYTHON) { $candidates += $env:SLC_PYTHON }
    $candidates += (Join-Path $paths.Root '.venv\Scripts\python.exe')
    $candidates += (Join-Path $paths.Root 'venv\Scripts\python.exe')
    $onPath = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($onPath) { $candidates += $onPath.Source }

    foreach ($c in $candidates) {
        if (-not $c) { continue }
        if (-not (Test-Path -LiteralPath $c)) { continue }
        $full = (Resolve-Path -LiteralPath $c).ProviderPath
        # The Microsoft Store alias is a 0-byte reparse stub that only resolves
        # inside an interactive user session. Under SYSTEM it launches nothing
        # and the task fails with a code nobody can read.
        if ($full -like '*\AppData\Local\Microsoft\WindowsApps\*') {
            Write-Warning ('skipping Microsoft Store python stub: ' + $full)
            continue
        }
        return $full
    }
    throw 'no usable python found. Pass -Python C:\path\to\python.exe (a venv interpreter, not the Store alias).'
}


function Invoke-KeelPreflight {
    param([string]$PythonPath)
    # Runs in the interpreter the tasks will actually use, so it catches the
    # classic Windows failure: dependencies installed into a different python
    # than the one the service starts. Program comes over stdin, which keeps
    # the check out of the argv-quoting question entirely.
    $ErrorActionPreference = 'Continue'
    $code = @'
import importlib.util, json, os, sys
req = ("yaml", "flask", "requests", "fastapi", "uvicorn", "httpx")
missing = [m for m in req if importlib.util.find_spec(m) is None]
out = {"executable": sys.executable, "version": sys.version.split()[0], "missing": missing}
if not missing:
    import yaml
    p = os.path.join(os.environ["KEEL_BOT_DIR"], "config.yaml")
    with open(p, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    out["engine_port"] = int((cfg.get("server") or {}).get("port", 8766))
    out["dashboard_port"] = int((cfg.get("dashboard") or {}).get("port", 8767))
print(json.dumps(out))
'@
    $env:KEEL_BOT_DIR = $paths.Bot
    $raw = $code | & $PythonPath -
    if ($LASTEXITCODE -ne 0) {
        throw ('preflight failed: ' + $PythonPath + ' exited ' + $LASTEXITCODE + '. Output: ' + ($raw -join ' '))
    }
    $json = ($raw | Where-Object { $_ -like '{*' } | Select-Object -Last 1)
    if (-not $json) { throw ('preflight produced no JSON. Output: ' + ($raw -join ' ')) }
    $res = $json | ConvertFrom-Json
    if ($res.missing.Count -gt 0) {
        throw ('interpreter ' + $PythonPath + ' is missing: ' + ($res.missing -join ', ') +
               ". Install them into THAT interpreter: `"" + $PythonPath + "`" -m pip install -r `"" +
               (Join-Path $paths.Bot 'requirements.txt') + "`"")
    }
    return $res
}


function Test-KeelCloudSyncedPath {
    param([string]$Path)
    foreach ($marker in @('\OneDrive', '\Dropbox', '\iCloudDrive', '\Google Drive', '\GoogleDrive')) {
        if ($Path -like ('*' + $marker + '*')) { return $true }
    }
    return $false
}


# --------------------------------------------------------- task definition
function Get-KeelTaskArguments {
    param([string]$Name)
    # The load-bearing string. Everything is absolute because Task Scheduler
    # has no PATH and no cwd worth trusting, and every path is inside double
    # quotes because this machine's profile directory contains an unmatched
    # "(" — unquoted, PowerShell's own parser stops at it.
    return ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -Service {1}' -f $paths.Wrapper, $Name)
}


function Get-KeelProbeArguments {
    return ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}"' -f $paths.ProbeScript)
}


function New-KeelSettings {
    param([int]$RestartMinutes = 1)
    # ExecutionTimeLimit 0 is the one that matters for a months-long run: the
    # default is 3 days, after which Task Scheduler would stop the engine
    # mid-position and call that normal.
    # Priority 4: the task default is 7, which puts the process in
    # BELOW_NORMAL_PRIORITY_CLASS. An engine polling on a clock should not be.
    $s = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -DontStopOnIdleEnd `
        -RestartInterval (New-TimeSpan -Minutes $RestartMinutes) -RestartCount 999 `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew
    $s.Priority = 4
    return $s
}


function New-KeelPrincipal {
    if ($RunAs -eq 'System') {
        return (New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\SYSTEM' -LogonType ServiceAccount -RunLevel Highest)
    }
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    return (New-ScheduledTaskPrincipal -UserId $me -LogonType S4U -RunLevel Highest)
}


function Register-KeelTask {
    param([string]$Name, [string]$Arguments, [object[]]$Triggers)
    $action = New-ScheduledTaskAction -Execute $PsExe -Argument $Arguments -WorkingDirectory $paths.Bot
    $task = Register-ScheduledTask -TaskPath $KeelTaskPath -TaskName $Name `
        -Action $action -Trigger $Triggers -Settings (New-KeelSettings) -Principal (New-KeelPrincipal) -Force
    return $task
}


# ------------------------------------------------------------------ verbs
function Invoke-Install {
    if (-not (Test-Path -LiteralPath (Join-Path $paths.Bot 'server.py'))) {
        throw ($paths.Bot + '\server.py not found — this script must stay in scripts\windows\ inside the repo')
    }
    if ((Test-KeelCloudSyncedPath $paths.Root) -and -not $AllowCloudSyncedPath) {
        throw ('refusing: ' + $paths.Root + ' looks cloud-synced. data\trading.db is SQLite in WAL mode and a sync ' +
               'agent writing underneath the engine corrupts the file the kill switches read (CONTRIBUTING.md). ' +
               'Move the runtime checkout to plain local disk, or pass -AllowCloudSyncedPath if you know better.')
    }
    if (-not (Test-KeelAdmin) -and -not $DryRun) {
        throw 'install needs an elevated PowerShell (registering a task under \Keel\ requires it). Re-run as Administrator, or add -DryRun to see what it would do.'
    }

    $py = Resolve-KeelPython $Python
    Write-Output ('interpreter : ' + $py)
    $pre = Invoke-KeelPreflight $py
    Write-Output ('python      : ' + $pre.version)
    Write-Output ('engine port : ' + $pre.engine_port + '   (config.yaml server.port)')
    Write-Output ('dash port   : ' + $pre.dashboard_port + '   (config.yaml dashboard.port)')

    # An existing manually-started engine holds the port; the task would start
    # and die on bind. Killing it is not implicit — it may be managing stops.
    $busy = @()
    foreach ($p in @([int]$pre.engine_port, [int]$pre.dashboard_port)) {
        $owner = Get-KeelPortOwner $p
        if ($owner) { $busy += ('port ' + $p + ' held by ' + $owner.ProcessName + ' pid ' + $owner.Id) }
    }
    if ($busy.Count -gt 0) {
        Write-Output ''
        foreach ($b in $busy) { Write-Warning $b }
        if (-not $StopExisting -and -not $NoStart -and -not $DryRun) {
            throw ('a process is already serving those ports. Stop it first (it may be managing live stops), ' +
                   'then re-run — or pass -StopExisting to have this script terminate it, or -NoStart to register ' +
                   'the tasks without starting them now.')
        }
    }

    $cfg = [pscustomobject]@{
        written_at                  = (Get-Date).ToString('s')
        written_by                  = 'scripts/windows/keel-services.ps1 install'
        repo_root                   = $paths.Root
        python                      = $py
        python_version              = $pre.version
        engine_port                 = [int]$pre.engine_port
        dashboard_port              = [int]$pre.dashboard_port
        run_as                      = $RunAs
        log_max_bytes               = ([int64]$LogMaxMB * 1MB)
        log_keep                    = $LogKeep
        probe_minutes               = $ProbeMinutes
        probe_grace_seconds         = $ProbeGraceSeconds
        probe_max_restarts_per_hour = $MaxRestartsPerHour
    }

    Write-Output ''
    Write-Output 'tasks to register:'
    foreach ($svc in $KeelServices) {
        Write-Output ('  ' + $KeelTaskPrefix + $svc.Name)
        Write-Output ('    execute : "' + $PsExe + '"')
        Write-Output ('    args    : ' + (Get-KeelTaskArguments $svc.Name))
        Write-Output ('    workdir : ' + $paths.Bot)
        Write-Output ('    log     : ' + (Get-KeelLogFile $paths.State $svc.Name))
    }
    Write-Output ('  ' + $KeelProbeTask)
    Write-Output ('    execute : "' + $PsExe + '"')
    Write-Output ('    args    : ' + (Get-KeelProbeArguments))
    Write-Output ('    every   : ' + $ProbeMinutes + ' min')
    Write-Output ''
    Write-Output ('principal   : ' + (New-KeelPrincipal).UserId + '  (' + $RunAs + ')')
    Write-Output ('config file : ' + $paths.ConfigPath)

    if ($DryRun) {
        Write-Output ''
        Write-Output '-DryRun: nothing registered, nothing written, nothing started.'
        Write-Output 'Next: .\keel-services.ps1 selftest   (proves the quoting and the rotation for real)'
        return
    }

    if (-not (Test-Path -LiteralPath $paths.State)) { New-Item -ItemType Directory -Path $paths.State -Force | Out-Null }
    Write-KeelConfig $paths.ConfigPath $cfg

    if ($StopExisting) {
        foreach ($p in @([int]$pre.engine_port, [int]$pre.dashboard_port)) {
            $owner = Get-KeelPortOwner $p
            if ($owner -and $owner.ProcessName -like 'python*') {
                Write-Output ('stopping ' + $owner.ProcessName + ' pid ' + $owner.Id + ' on port ' + $p)
                Stop-Process -Id $owner.Id -Force -ErrorAction SilentlyContinue
            }
        }
        Start-Sleep -Seconds 2
    }

    Write-Output ''
    foreach ($svc in $KeelServices) {
        $t = New-ScheduledTaskTrigger -AtStartup
        $t.Delay = 'PT30S'      # let the disk and the network stack settle first
        Register-KeelTask -Name ($KeelTaskPrefix + $svc.Name) -Arguments (Get-KeelTaskArguments $svc.Name) -Triggers @($t) | Out-Null
        Write-Output ('registered ' + $KeelTaskPrefix + $svc.Name)
    }

    $pt1 = New-ScheduledTaskTrigger -AtStartup
    $pt1.Delay = 'PT2M'
    $pt2 = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $ProbeMinutes)
    Register-KeelTask -Name $KeelProbeTask -Arguments (Get-KeelProbeArguments) -Triggers @($pt1, $pt2) | Out-Null
    Write-Output ('registered ' + $KeelProbeTask)

    if ($NoStart) {
        Write-Output ''
        Write-Output '-NoStart: registered but not started. They will come up at the next boot.'
        return
    }

    foreach ($svc in $KeelServices) {
        Start-ScheduledTask -TaskPath $KeelTaskPath -TaskName ($KeelTaskPrefix + $svc.Name)
        Write-Output ('started ' + $KeelTaskPrefix + $svc.Name)
    }
    Start-ScheduledTask -TaskPath $KeelTaskPath -TaskName $KeelProbeTask

    Write-Output ''
    Write-Output ('engine    : http://127.0.0.1:' + $cfg.engine_port + '   (EA target — point the EA at this machine on this port)')
    Write-Output ('dashboard : http://127.0.0.1:' + $cfg.dashboard_port)
    Write-Output ('logs      : ' + $paths.State + '\win_*.log   (rotated at ' + $LogMaxMB + ' MB, ' + $LogKeep + ' kept)')
    Write-Output ('watchdog  : ' + $paths.State + '\keel_watchdog.jsonl')
    Write-Output ''
    Write-Output 'Give them ~15 seconds, then: .\keel-services.ps1 status'
}


function Invoke-Status {
    Write-Output ('repo   : ' + $paths.Root)
    $cfg = Read-KeelConfig $paths.ConfigPath
    if ($null -eq $cfg) {
        Write-Output ('config : NOT INSTALLED (no ' + $paths.ConfigPath + ')')
    } else {
        Write-Output ('config : ' + $paths.ConfigPath + '  (written ' + $cfg.written_at + ', run as ' + $cfg.run_as + ')')
        Write-Output ('python : ' + $cfg.python)
        if ($cfg.repo_root -ne $paths.Root) {
            Write-Warning ('config was written for ' + $cfg.repo_root + ' — this checkout is ' + $paths.Root + '. Re-run install.')
        }
    }
    Write-Output ''

    $names = @()
    foreach ($svc in $KeelServices) { $names += ($KeelTaskPrefix + $svc.Name) }
    $names += $KeelProbeTask
    Write-Output ('{0,-20} {1,-10} {2,-20} {3,-12} {4}' -f 'TASK', 'STATE', 'LAST RUN', 'LAST RESULT', 'WRAPPER PID')
    foreach ($n in $names) {
        $t = Get-KeelTask $n
        if (-not $t) {
            Write-Output ('{0,-20} {1}' -f $n, 'not registered')
            continue
        }
        $i = $t | Get-ScheduledTaskInfo
        $last = 'never'
        if ($i.LastRunTime -and $i.LastRunTime.Year -gt 1999) { $last = $i.LastRunTime.ToString('yyyy-MM-dd HH:mm:ss') }
        $svcName = $n -replace ('^' + [regex]::Escape($KeelTaskPrefix)), ''
        $wpid = '-'
        $pf = Get-KeelPidFile $paths.State $svcName
        if (Test-Path -LiteralPath $pf) { $wpid = (Get-Content -LiteralPath $pf -Raw).Trim() }
        Write-Output ('{0,-20} {1,-10} {2,-20} {3,-12} {4}' -f $n, $t.State, $last, ('0x' + ('{0:X}' -f $i.LastTaskResult)), $wpid)
    }
    Write-Output ''

    if ($null -eq $cfg) {
        Write-Output 'ports : not checked (no install config — run install, or install -DryRun to see the ports)'
    }
    foreach ($svc in $KeelServices) {
        if (-not $svc.PortKey) { continue }
        $port = 0
        if ($cfg) { $port = [int]$cfg.($svc.PortKey) }
        if ($port -le 0) { continue }
        $r = Test-KeelHttp -Url ('http://127.0.0.1:' + $port + $svc.Probe) -TimeoutSec 5
        $owner = Get-KeelPortOwner $port
        $who = 'no listener'
        if ($owner) { $who = $owner.ProcessName + ' pid ' + $owner.Id }
        if ($r.Ok) {
            Write-Output ('port {0}: answering HTTP {1} on {2}   [{3}]' -f $port, $r.Status, $svc.Probe, $who)
        } else {
            Write-Output ('port {0}: NO ANSWER on {1} — {2}   [{3}]' -f $port, $svc.Probe, $r.Error, $who)
        }
    }
    Write-Output ''

    Write-Output 'logs:'
    $any = $false
    $stateFiles = @(Get-ChildItem -LiteralPath $paths.State -ErrorAction SilentlyContinue)
    foreach ($f in ($stateFiles | Where-Object { $_.Name -like 'win_*.log' })) {
        $any = $true
        # -Filter would use Win32 wildcard rules, where "name.log.*" also
        # matches "name.log" and every file counts itself as rotated.
        $rot = @($stateFiles | Where-Object { $_.Name -like ($f.Name + '.*') }).Count
        Write-Output ('  {0,-24} {1,8:N1} MB  (+{2} rotated)' -f $f.Name, ($f.Length / 1MB), $rot)
    }
    if (-not $any) { Write-Output '  (none yet)' }

    $wdl = Join-Path $paths.State 'keel_watchdog.jsonl'
    Write-Output ''
    Write-Output 'watchdog (last 5 events):'
    if (Test-Path -LiteralPath $wdl) {
        Get-Content -LiteralPath $wdl -Tail 5 | ForEach-Object { Write-Output ('  ' + $_) }
    } else {
        Write-Output '  (none — nothing has needed restarting)'
    }
}


function Invoke-Uninstall {
    if (-not (Test-KeelAdmin) -and -not $DryRun) {
        throw 'uninstall needs an elevated PowerShell.'
    }
    $names = @()
    foreach ($svc in $KeelServices) { $names += ($KeelTaskPrefix + $svc.Name) }
    $names += $KeelProbeTask
    foreach ($n in $names) {
        $t = Get-KeelTask $n
        if (-not $t) { Write-Output ($n + ': not registered'); continue }
        if ($DryRun) { Write-Output ('would remove ' + $n); continue }
        try { Stop-ScheduledTask -TaskPath $KeelTaskPath -TaskName $n -ErrorAction SilentlyContinue } catch { }
        Unregister-ScheduledTask -TaskPath $KeelTaskPath -TaskName $n -Confirm:$false
        Write-Output ('removed ' + $n)
    }
    if (-not $DryRun) {
        try {
            $sched = New-Object -ComObject 'Schedule.Service'
            $sched.Connect()
            $sched.GetFolder('\').DeleteFolder($KeelTaskPath.Trim('\'), 0)
        } catch { }
        # Logs, the watchdog ledger and the install config stay. They are the
        # record of what the machine did while it was unattended, and this repo
        # does not delete runtime state.
        Write-Output ''
        Write-Output ('logs and ' + (Split-Path $paths.ConfigPath -Leaf) + ' left in ' + $paths.State)
        Write-Output 'Nothing is running now — start by hand with: python server.py'
    }
}


function Invoke-Restart {
    if (-not (Test-KeelAdmin)) { throw 'restart needs an elevated PowerShell.' }
    $targets = @()
    if ($Service -eq 'all') { foreach ($svc in $KeelServices) { $targets += $svc.Name } } else { $targets += $Service }
    foreach ($n in $targets) {
        $task = $KeelTaskPrefix + $n
        if (-not (Get-KeelTask $task)) { Write-Output ($task + ': not registered'); continue }
        Stop-ScheduledTask -TaskPath $KeelTaskPath -TaskName $task -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
        Start-ScheduledTask -TaskPath $KeelTaskPath -TaskName $task
        Write-Output ('restarted ' + $task)
        Write-KeelEvent $paths.State @{ event = 'manual_restart'; service = $n; by = [Security.Principal.WindowsIdentity]::GetCurrent().Name }
    }
}


function Invoke-Probe {
    $probeArgs = @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', $paths.ProbeScript)
    if ($DryRun) { $probeArgs += '-DryRun' }
    & $PsExe @probeArgs
}


function Invoke-SelfTest {
    # Three things that are worth proving rather than assuming on this host:
    # the interpreter really has the deps, the task argument string really
    # survives a profile path with an unmatched "(", and rotation really caps
    # the file. Registers nothing and starts no service.
    $pass = 0
    $fail = 0

    Write-Output '--- interpreter preflight'
    try {
        $py = Resolve-KeelPython $Python
        $pre = Invoke-KeelPreflight $py
        Write-Output ('  ok: ' + $py + '  python ' + $pre.version + '  ports ' + $pre.engine_port + '/' + $pre.dashboard_port)
        $pass++
    } catch {
        Write-Output ('  FAIL: ' + $_.Exception.Message)
        $fail++
        $py = $null
    }

    Write-Output '--- scheduled-task argument quoting'
    # A config has to exist for the wrapper to validate against; if install has
    # not run, write a throwaway one and put the original back afterwards.
    $hadConfig = Test-Path -LiteralPath $paths.ConfigPath
    $backup = $null
    if ($hadConfig) { $backup = Get-Content -LiteralPath $paths.ConfigPath -Raw -Encoding UTF8 }
    if ($py) {
        if (-not $hadConfig) {
            if (-not (Test-Path -LiteralPath $paths.State)) { New-Item -ItemType Directory -Path $paths.State -Force | Out-Null }
            Write-KeelConfig $paths.ConfigPath ([pscustomobject]@{
                written_by = 'selftest'; repo_root = $paths.Root; python = $py
                engine_port = [int]$pre.engine_port; dashboard_port = [int]$pre.dashboard_port
                log_max_bytes = 1MB; log_keep = 2
            })
        }
        foreach ($svc in $KeelServices) {
            $argline = (Get-KeelTaskArguments $svc.Name) + ' -ValidateOnly'
            $out = Join-Path $env:TEMP ('keel_selftest_' + $svc.Name + '.out')
            $err = Join-Path $env:TEMP ('keel_selftest_' + $svc.Name + '.err')
            $p = Start-Process -FilePath $PsExe -ArgumentList $argline -Wait -PassThru -NoNewWindow `
                    -RedirectStandardOutput $out -RedirectStandardError $err
            if ($p.ExitCode -eq 0) {
                Write-Output ('  ok: ' + $svc.Name + '  ' + ((Get-Content -LiteralPath $out) -join ' | '))
                $pass++
            } else {
                Write-Output ('  FAIL: ' + $svc.Name + ' exited ' + $p.ExitCode + ' — ' + ((Get-Content -LiteralPath $err) -join ' '))
                $fail++
            }
            Remove-Item -LiteralPath $out, $err -Force -ErrorAction SilentlyContinue
        }
        if (-not $hadConfig) { Remove-Item -LiteralPath $paths.ConfigPath -Force -ErrorAction SilentlyContinue }
        elseif ($backup) { $backup | Out-File -LiteralPath $paths.ConfigPath -Encoding utf8 -Force }
    } else {
        Write-Output '  skipped (no usable interpreter)'
    }

    Write-Output '--- child stream capture and exit code'
    if ($py) {
        $childPy = Join-Path $env:TEMP 'keel_selftest_child.py'
        @'
import sys
for i in range(300):
    print("stdout line %d" % i)
    print("stderr line %d" % i, file=sys.stderr)
sys.exit(3)
'@ | Out-File -LiteralPath $childPy -Encoding ascii -Force
        $childLog = Join-Path $paths.State '_selftest_child.log'
        Remove-Item -LiteralPath $childLog -Force -ErrorAction SilentlyContinue
        $cl = New-KeelLog -Path $childLog -MaxBytes 10MB -Keep 2
        $prevEap = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'   # required by Invoke-KeelChild
        $rc = Invoke-KeelChild -Python $py -Script $childPy -Log $cl
        $ErrorActionPreference = $prevEap
        Close-KeelLog $cl
        $text = Get-Content -LiteralPath $childLog -Raw
        $sawOut = $text -match 'stdout line 299'
        $sawErr = $text -match 'stderr line 299'
        if ($rc -eq 3 -and $sawOut -and $sawErr) {
            Write-Output ('  ok: exit ' + $rc + ' propagated, stdout and stderr both captured')
            $pass++
        } else {
            Write-Output ('  FAIL: exit ' + $rc + ' (wanted 3), stdout captured=' + $sawOut + ', stderr captured=' + $sawErr)
            $fail++
        }
        Remove-Item -LiteralPath $childLog, $childPy -Force -ErrorAction SilentlyContinue
    } else {
        Write-Output '  skipped (no usable interpreter)'
    }

    Write-Output '--- watchdog restart budget'
    # The branch that decides whether the machine is allowed to restart a
    # trading process on its own. Worth checking arithmetically, because the
    # only other way to see it is a live restart loop.
    $now = 100000
    $cases = @(
        @{ name = 'no history';                 hist = @();                                          want = 'go'     }
        @{ name = 'restarted 10s ago';          hist = @(99990);                                     want = 'grace'  }
        @{ name = 'restarted 600s ago';         hist = @(99400);                                     want = 'go'     }
        @{ name = '5 in the last hour';         hist = @(97100, 97700, 98300, 98900, 99400);         want = 'giveup' }
        @{ name = '5 but 3 aged out of window'; hist = @(90000, 91000, 92000, 96000, 99400);         want = 'go'     }
        @{ name = '6 recent, newest inside grace'; hist = @(97100, 97700, 98300, 98900, 99400, 99990); want = 'grace' }
    )
    foreach ($c in $cases) {
        $d = Get-KeelRestartDecision -History ([int64[]]$c.hist) -Now $now -GraceSeconds 180 -MaxPerHour 5
        if ($d.Verdict -eq $c.want) {
            Write-Output ('  ok: ' + $c.name + ' -> ' + $d.Verdict)
            $pass++
        } else {
            Write-Output ('  FAIL: ' + $c.name + ' -> ' + $d.Verdict + ', wanted ' + $c.want)
            $fail++
        }
    }

    Write-Output '--- log rotation'
    $tmp = Join-Path $paths.State '_selftest_rotate.log'
    Get-ChildItem -LiteralPath $paths.State -Filter '_selftest_rotate.log*' -ErrorAction SilentlyContinue |
        Remove-Item -Force -ErrorAction SilentlyContinue
    $rl = New-KeelLog -Path $tmp -MaxBytes 65536 -Keep 3
    $line = 'x' * 200
    for ($i = 0; $i -lt 2000; $i++) { Write-KeelLog $rl ($line + ' ' + $i) }
    Close-KeelLog $rl
    $files = @(Get-ChildItem -LiteralPath $paths.State -Filter '_selftest_rotate.log*' -ErrorAction SilentlyContinue)
    $biggest = 0
    foreach ($f in $files) { if ($f.Length -gt $biggest) { $biggest = $f.Length } }
    # 2000 x ~204 bytes is ~400 KB against a 64 KB cap and 3 kept files: if the
    # cap held, nothing on disk exceeds it and the total is bounded.
    if ($files.Count -le 4 -and $biggest -le (65536 + 4096)) {
        Write-Output ('  ok: ' + $files.Count + ' files, largest ' + $biggest + ' bytes, cap 65536 (wrote ~' + (2000 * 204) + ')')
        $pass++
    } else {
        Write-Output ('  FAIL: ' + $files.Count + ' files, largest ' + $biggest + ' bytes against a 65536 cap')
        $fail++
    }
    Get-ChildItem -LiteralPath $paths.State -Filter '_selftest_rotate.log*' -ErrorAction SilentlyContinue |
        Remove-Item -Force -ErrorAction SilentlyContinue

    Write-Output ''
    Write-Output ($pass.ToString() + ' passed, ' + $fail.ToString() + ' failed')
    if ($fail -gt 0) { exit 1 }
}


switch ($Verb) {
    'install'   { Invoke-Install }
    'status'    { Invoke-Status }
    'uninstall' { Invoke-Uninstall }
    'restart'   { Invoke-Restart }
    'probe'     { Invoke-Probe }
    'selftest'  { Invoke-SelfTest }
}
