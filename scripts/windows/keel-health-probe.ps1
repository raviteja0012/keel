#Requires -Version 5.1
<#
.SYNOPSIS
  Watchdog for the Keel Windows services. One pass: probe, restart what is
  genuinely dead, record what it did.

.DESCRIPTION
  Task Scheduler's restart-on-failure only fires when a process exits. It does
  nothing about the failure that actually matters here — the process is alive
  and the port has stopped answering, which is what a wedged Flask worker or a
  hung engine loop looks like from outside. This is the equivalent of the
  macOS watchdog-install.sh check, run on a timer.

  Every automatic restart is appended to state/keel_watchdog.jsonl. A service
  that quietly dies twice a day must not look healthy.

  Registered by keel-services.ps1 install; run by hand (with -DryRun) to see
  what it would do.

.PARAMETER DryRun
  Probe and report, restart nothing.
#>
[CmdletBinding()]
param(
    [switch]$DryRun
)

$ErrorActionPreference = 'Continue'

. (Join-Path $PSScriptRoot 'keel-common.ps1')

$paths = Get-KeelPaths
$cfg   = Read-KeelConfig $paths.ConfigPath
if ($null -eq $cfg) {
    Write-Error ('no install config at ' + $paths.ConfigPath + ' — run keel-services.ps1 install')
    exit 78
}

$graceSeconds  = 180
if ($cfg.probe_grace_seconds) { $graceSeconds = [int]$cfg.probe_grace_seconds }
$maxPerHour    = 5
if ($cfg.probe_max_restarts_per_hour) { $maxPerHour = [int]$cfg.probe_max_restarts_per_hour }
$logMax = 20MB
if ($cfg.log_max_bytes) { $logMax = [int64]$cfg.log_max_bytes }
$logKeep = 5
if ($cfg.log_keep) { $logKeep = [int]$cfg.log_keep }

$log = New-KeelLog -Path (Get-KeelLogFile $paths.State 'healthprobe') -MaxBytes $logMax -Keep $logKeep
$statePath = Join-Path $paths.State 'keel_watchdog.json'
$wd = Read-KeelConfig $statePath
if ($null -eq $wd) { $wd = [pscustomobject]@{} }

$isAdmin = Test-KeelAdmin
$now = Get-KeelEpoch


function Get-RestartHistory {
    param([string]$Name)
    $prop = $wd.PSObject.Properties[$Name]
    if (-not $prop) { return @() }
    $v = $prop.Value
    if ($null -eq $v) { return @() }
    return @($v)
}


function Set-RestartHistory {
    param([string]$Name, $Values)
    if ($wd.PSObject.Properties[$Name]) { $wd.PSObject.Properties.Remove($Name) }
    Add-Member -InputObject $wd -MemberType NoteProperty -Name $Name -Value @($Values)
}


function Write-Line {
    param([string]$Text)
    Write-KeelLog $log ((Format-KeelStamp) + ' [probe] ' + $Text)
    Write-Output $Text
}


function Write-ProbeEvent {
    # The ledger is the record of what the machine did unattended. A dry run
    # did nothing, so it leaves no marks.
    param([hashtable]$Data)
    if ($DryRun) { return }
    Write-KeelEvent $paths.State $Data
}


function Invoke-KeelRestart {
    param([string]$Name, [int]$Port, [string]$Reason)

    $taskName = $KeelTaskPrefix + $Name
    Write-Line ($Name + ': restarting (' + $Reason + ')')
    try { Stop-ScheduledTask -TaskPath $KeelTaskPath -TaskName $taskName -ErrorAction Stop } catch {
        Write-Line ($Name + ': stop failed — ' + $_.Exception.Message)
    }

    # Task Scheduler kills the wrapper, but an orphaned python still holding
    # the socket would make the restart fail on bind. Only ever reap a python
    # process: killing whatever happens to own the port is how a watchdog
    # takes out something it was never asked to manage.
    if ($Port -gt 0) {
        for ($i = 0; $i -lt 20; $i++) {
            $owner = Get-KeelPortOwner $Port
            if (-not $owner) { break }
            Start-Sleep -Seconds 1
        }
        $owner = Get-KeelPortOwner $Port
        if ($owner) {
            if ($owner.ProcessName -like 'python*') {
                Write-Line ($Name + ': port ' + $Port + ' still held by ' + $owner.ProcessName + ' pid ' + $owner.Id + ' — terminating')
                Stop-Process -Id $owner.Id -Force -ErrorAction SilentlyContinue
                Start-Sleep -Seconds 2
            } else {
                Write-Line ($Name + ': port ' + $Port + ' held by ' + $owner.ProcessName + ' pid ' + $owner.Id + ' — not ours, standing aside')
                Write-KeelEvent $paths.State @{ event = 'foreign_port_owner'; service = $Name; port = $Port; process = $owner.ProcessName; owner_pid = $owner.Id }
                return $false
            }
        }
    }

    try {
        Start-ScheduledTask -TaskPath $KeelTaskPath -TaskName $taskName -ErrorAction Stop
    } catch {
        Write-Line ($Name + ': start failed — ' + $_.Exception.Message)
        Write-KeelEvent $paths.State @{ event = 'restart_failed'; service = $Name; reason = $Reason; error = $_.Exception.Message }
        return $false
    }
    Write-KeelEvent $paths.State @{ event = 'restart'; service = $Name; reason = $Reason; port = $Port }
    return $true
}


# ---------------------------------------------------------------- one pass
$restarted = 0
foreach ($svc in $KeelServices) {

    $taskName = $KeelTaskPrefix + $svc.Name
    $task = Get-KeelTask $taskName
    $port = 0
    if ($svc.PortKey) { $port = [int]$cfg.($svc.PortKey) }

    # --- is it healthy? --------------------------------------------------
    $healthy = $true
    $detail  = ''
    if ($port -gt 0) {
        $url = 'http://127.0.0.1:' + $port + $svc.Probe
        $r = Test-KeelHttp -Url $url -TimeoutSec 5
        if (-not $r.Ok) {
            # One timeout is a hiccup; a restart is the expensive action. Two
            # failures five seconds apart is the bar.
            Start-Sleep -Seconds 5
            $r = Test-KeelHttp -Url $url -TimeoutSec 5
        }
        $healthy = $r.Ok
        if ($r.Ok) { $detail = 'HTTP ' + $r.Status + ' ' + $url } else { $detail = 'no answer on ' + $url + ' (' + $r.Error + ')' }
    } else {
        # No listener to probe. The only honest signal is whether the task is
        # running at all.
        if ($task) {
            $healthy = ($task.State -eq 'Running')
            $detail  = 'task state ' + $task.State
        } else {
            $healthy = $false
            $detail  = 'task not registered'
        }
    }

    if ($healthy) {
        Write-Line ($svc.Name + ': ok — ' + $detail)
        continue
    }
    Write-Line ($svc.Name + ': DOWN — ' + $detail)

    # --- should we touch it? ---------------------------------------------
    if (-not $task) {
        Write-Line ($svc.Name + ': no scheduled task registered, nothing to restart')
        Write-ProbeEvent @{ event = 'no_task'; service = $svc.Name; detail = $detail }
        continue
    }
    if ($task.State -eq 'Disabled') {
        # A human disabled this on purpose. Undoing that is not the watchdog's call.
        Write-Line ($svc.Name + ': task is Disabled — leaving it alone')
        Write-ProbeEvent @{ event = 'disabled'; service = $svc.Name; detail = $detail }
        continue
    }
    if ($DryRun) {
        Write-Line ($svc.Name + ': -DryRun, would restart')
        continue
    }
    if (-not $isAdmin) {
        Write-Line ($svc.Name + ': not elevated — cannot restart a task in ' + $KeelTaskPath)
        Write-ProbeEvent @{ event = 'not_admin'; service = $svc.Name; detail = $detail }
        continue
    }

    $decision = Get-KeelRestartDecision -History @(Get-RestartHistory $svc.Name) -Now $now `
                    -GraceSeconds $graceSeconds -MaxPerHour $maxPerHour
    $history = @($decision.Recent)
    if ($decision.Verdict -eq 'grace') {
        Write-Line ($svc.Name + ': restarted ' + $decision.SinceLast + 's ago, inside the ' + $graceSeconds + 's grace window')
        Set-RestartHistory $svc.Name $history
        continue
    }
    if ($decision.Verdict -eq 'giveup') {
        # A restart loop on a trading engine is worse than a stopped one: it
        # churns the DB and hides the real fault. Stop, and make the giving-up
        # loud enough that `status` shows it.
        Write-Line ($svc.Name + ': ' + $history.Count + ' restarts in the last hour — giving up until a human looks')
        Write-ProbeEvent @{ event = 'giveup'; service = $svc.Name; restarts_last_hour = $history.Count; detail = $detail }
        Set-RestartHistory $svc.Name $history
        continue
    }

    if (Invoke-KeelRestart -Name $svc.Name -Port $port -Reason $detail) {
        $restarted = $restarted + 1
        $history = @($history + @($now))
    }
    Set-RestartHistory $svc.Name $history
}

if (-not $DryRun) { Write-KeelConfig $statePath $wd }
Write-KeelLog $log ((Format-KeelStamp) + ' [probe] pass complete, ' + $restarted + ' restarted')
Close-KeelLog $log
exit 0
