#Requires -Version 5.1
# Shared helpers for the Keel Windows deployment. Dot-sourced by
# keel-services.ps1 (installer), keel-service-run.ps1 (process wrapper) and
# keel-health-probe.ps1 (watchdog) so path resolution, log rotation and the
# liveness check have one implementation between the three, not three that
# drift apart.
#
# Deliberately Windows PowerShell 5.1 and stdlib only. A host that has to come
# back unattended after a reboot must not depend on a module install, and 5.1
# is what Windows 11 ships. No `&&`, no ternary, no `??`.

$KeelTaskPath   = '\Keel\'
$KeelTaskPrefix = 'Keel-'
$KeelProbeTask  = 'Keel-healthprobe'

# One row per managed process. PortKey names the field in the install config
# that holds the port; $null means the process has no listener and can only be
# judged by its task state.
$KeelServices = @(
    [pscustomobject]@{ Name = 'server';    Script = 'server.py';         PortKey = 'engine_port';    Probe = '/api/pairs'  }
    [pscustomobject]@{ Name = 'dashboard'; Script = 'dashboard_api.py';  PortKey = 'dashboard_port'; Probe = '/api/health' }
    [pscustomobject]@{ Name = 'newsagent'; Script = 'news_agent.py';     PortKey = $null;            Probe = $null         }
)

# Captured at dot-source time. Functions defined here resolve $PSScriptRoot to
# this file's directory, but the caller's own $PSScriptRoot shadows it in some
# nesting cases, so pin it once.
$KeelScriptDir = $PSScriptRoot


function Get-KeelPaths {
    # scripts/windows/ -> scripts/ -> repo root. Derived from the script's own
    # location so a clone anywhere works and nothing has to be configured.
    $root = Split-Path (Split-Path $KeelScriptDir -Parent) -Parent
    $bot  = Join-Path $root 'trading-bot'
    return [pscustomobject]@{
        ScriptDir  = $KeelScriptDir
        Root       = $root
        Bot        = $bot
        State      = (Join-Path $bot 'state')
        ConfigPath = (Join-Path (Join-Path $bot 'state') 'windows-services.json')
        Wrapper    = (Join-Path $KeelScriptDir 'keel-service-run.ps1')
        ProbeScript= (Join-Path $KeelScriptDir 'keel-health-probe.ps1')
    }
}


function Get-KeelService {
    param([string]$Name)
    foreach ($s in $KeelServices) { if ($s.Name -eq $Name) { return $s } }
    return $null
}


function Format-KeelStamp {
    # Local time, matching what python's logging writes into the same files.
    return (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
}


function Get-KeelEpoch {
    return [int64]([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())
}


# ------------------------------------------------------------------ config
function Read-KeelConfig {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try {
        $raw = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
        return ($raw | ConvertFrom-Json)
    } catch {
        return $null
    }
}


function Write-KeelConfig {
    param([string]$Path, $Config)
    $dir = Split-Path $Path -Parent
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    ($Config | ConvertTo-Json -Depth 6) | Out-File -LiteralPath $Path -Encoding utf8 -Force
}


# ------------------------------------------------------- rotating log files
# Task Scheduler cannot redirect stdout, so the wrapper owns the file handle.
# Owning it is also what makes rotation possible mid-run: a months-long process
# never exits, so a rotate-on-restart scheme would never fire and the log would
# grow until the disk went with it.
function Open-KeelLogWriter {
    param([string]$Path)
    $enc = New-Object System.Text.UTF8Encoding($false)
    $w = New-Object System.IO.StreamWriter($Path, $true, $enc)
    $w.AutoFlush = $true      # a crash must not take the last lines with it
    return $w
}


function Invoke-KeelLogRoll {
    param($Log)
    if ($Log.Writer) {
        try { $Log.Writer.Flush(); $Log.Writer.Dispose() } catch { }
        $Log.Writer = $null
    }
    $oldest = ('{0}.{1}' -f $Log.Path, $Log.Keep)
    if (Test-Path -LiteralPath $oldest) { Remove-Item -LiteralPath $oldest -Force -ErrorAction SilentlyContinue }
    for ($i = $Log.Keep - 1; $i -ge 1; $i--) {
        $src = ('{0}.{1}' -f $Log.Path, $i)
        $dst = ('{0}.{1}' -f $Log.Path, ($i + 1))
        if (Test-Path -LiteralPath $src) { Move-Item -LiteralPath $src -Destination $dst -Force -ErrorAction SilentlyContinue }
    }
    if (Test-Path -LiteralPath $Log.Path) {
        Move-Item -LiteralPath $Log.Path -Destination ('{0}.1' -f $Log.Path) -Force -ErrorAction SilentlyContinue
    }
    $Log.Bytes = 0
}


function New-KeelLog {
    param([string]$Path, [int64]$MaxBytes = 20MB, [int]$Keep = 5)
    if ($MaxBytes -lt 65536) { $MaxBytes = 65536 }   # a cap this small only churns files
    if ($Keep -lt 1) { $Keep = 1 }
    $dir = Split-Path $Path -Parent
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $log = [pscustomobject]@{ Path = $Path; MaxBytes = [int64]$MaxBytes; Keep = [int]$Keep; Bytes = [int64]0; Writer = $null }
    if (Test-Path -LiteralPath $Path) {
        $len = (Get-Item -LiteralPath $Path).Length
        if ($len -ge $log.MaxBytes) { Invoke-KeelLogRoll $log } else { $log.Bytes = [int64]$len }
    }
    $log.Writer = Open-KeelLogWriter $Path
    return $log
}


function Write-KeelLog {
    param($Log, [string]$Line)
    if ($null -eq $Line) { $Line = '' }
    $Log.Writer.WriteLine($Line)
    $Log.Bytes = $Log.Bytes + [System.Text.Encoding]::UTF8.GetByteCount($Line) + 2
    if ($Log.Bytes -ge $Log.MaxBytes) {
        Invoke-KeelLogRoll $Log
        $Log.Writer = Open-KeelLogWriter $Log.Path
        $Log.Writer.WriteLine((Format-KeelStamp) + ' [keel-run] rotated; previous file is ' + (Split-Path $Log.Path -Leaf) + '.1')
    }
}


function Invoke-KeelChild {
    # Runs one python process to completion, both streams into $Log, and hands
    # back its exit code. 2>&1 merges stderr into the pipeline so the two land
    # in one file in order — the equivalent of launchd's StandardOutPath and
    # StandardErrorPath pointing at the same file.
    #
    # The caller MUST be running with $ErrorActionPreference = 'Continue':
    # under 'Stop' the first line python writes to stderr becomes a
    # terminating NativeCommandError and kills the service.
    param([string]$Python, [string]$Script, $Log)
    & $Python -u $Script 2>&1 | ForEach-Object { Write-KeelLog $Log ([string]$_) } | Out-Null
    $code = $LASTEXITCODE
    if ($null -eq $code) { $code = 1 }
    return $code
}


function Close-KeelLog {
    param($Log)
    if ($Log -and $Log.Writer) {
        try { $Log.Writer.Flush(); $Log.Writer.Dispose() } catch { }
        $Log.Writer = $null
    }
}


# ------------------------------------------------------------- health probe
function Test-KeelHttp {
    param([string]$Url, [int]$TimeoutSec = 5)
    # Raw HttpWebRequest rather than Invoke-WebRequest: a system proxy must
    # never sit between the watchdog and 127.0.0.1, and 5.1's Invoke-WebRequest
    # honours one.
    $req = $null
    try {
        $req = [System.Net.HttpWebRequest]::Create($Url)
        $req.Method = 'GET'
        $req.Proxy = $null
        $req.Timeout = $TimeoutSec * 1000
        $req.ReadWriteTimeout = $TimeoutSec * 1000
        $resp = $req.GetResponse()
        $code = [int]$resp.StatusCode
        $resp.Close()
        return [pscustomobject]@{ Ok = $true; Status = $code; Error = '' }
    } catch [System.Net.WebException] {
        # Any HTTP status means something is listening and serving. This is a
        # liveness probe, not a correctness probe — restarting a live engine
        # because a route was renamed would be worse than the symptom.
        if ($_.Exception.Response) {
            $code = 0
            try { $code = [int]$_.Exception.Response.StatusCode } catch { }
            try { $_.Exception.Response.Close() } catch { }
            if ($code -gt 0) { return [pscustomobject]@{ Ok = $true; Status = $code; Error = '' } }
        }
        return [pscustomobject]@{ Ok = $false; Status = 0; Error = $_.Exception.Message }
    } catch {
        return [pscustomobject]@{ Ok = $false; Status = 0; Error = $_.Exception.Message }
    }
}


function Get-KeelPortOwner {
    param([int]$Port)
    $c = $null
    try {
        $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop | Select-Object -First 1
    } catch {
        return $null
    }
    if (-not $c) { return $null }
    return (Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue)
}


function Get-KeelRestartDecision {
    # Lifted out of the probe so the arithmetic can be tested without
    # registering a task. Given the timestamps of previous automatic restarts:
    #   grace  — one just happened; the service has not had time to bind yet
    #   giveup — too many in the last hour; a human needs to look
    #   go      — restart it
    param([int64[]]$History, [int64]$Now, [int]$GraceSeconds, [int]$MaxPerHour)
    $recent = @()
    foreach ($h in $History) { if (($Now - [int64]$h) -lt 3600) { $recent += [int64]$h } }
    $sinceLast = -1
    $verdict = 'go'
    if ($recent.Count -gt 0) {
        $last = ($recent | Measure-Object -Maximum).Maximum
        $sinceLast = $Now - [int64]$last
        if ($sinceLast -lt $GraceSeconds) { $verdict = 'grace' }
    }
    if ($verdict -eq 'go' -and $recent.Count -ge $MaxPerHour) { $verdict = 'giveup' }
    return [pscustomobject]@{ Verdict = $verdict; Recent = @($recent); SinceLast = $sinceLast }
}


function Write-KeelEvent {
    # Append-only watchdog ledger. Every restart the machine performs on its own
    # has to leave a trace, or a service that dies twice a day looks healthy.
    param([string]$StateDir, [hashtable]$Data)
    $Data['t']  = Get-KeelEpoch
    $Data['ts'] = (Get-Date).ToString('s')
    $path = Join-Path $StateDir 'keel_watchdog.jsonl'
    try {
        if (-not (Test-Path -LiteralPath $StateDir)) { New-Item -ItemType Directory -Path $StateDir -Force | Out-Null }
        Add-Content -LiteralPath $path -Value ($Data | ConvertTo-Json -Compress -Depth 4) -Encoding UTF8
    } catch { }
}


# ------------------------------------------------------------------- tasks
function Get-KeelTask {
    param([string]$Name)
    return (Get-ScheduledTask -TaskPath $KeelTaskPath -TaskName $Name -ErrorAction SilentlyContinue)
}


function Test-KeelAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}


function Get-KeelPidFile {
    param([string]$StateDir, [string]$Service)
    return (Join-Path $StateDir ('win_{0}.pid' -f $Service))
}


function Get-KeelLogFile {
    param([string]$StateDir, [string]$Service)
    return (Join-Path $StateDir ('win_{0}.log' -f $Service))
}
