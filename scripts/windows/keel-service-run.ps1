#Requires -Version 5.1
<#
.SYNOPSIS
  Runs one Keel python process under a Windows Scheduled Task, with its stdout
  and stderr redirected into a size-capped, rotating log in trading-bot/state/.

.DESCRIPTION
  Task Scheduler has no equivalent of launchd's StandardOutPath, so something
  has to own the file handle. This wrapper does, and owning it is also what
  makes rotation work: these processes are meant to run for months without
  exiting, so a rotate-on-restart scheme would never fire.

  Never invoked by hand in normal use — keel-services.ps1 install registers it.
  Run it with -ValidateOnly to prove the paths and quoting resolve without
  starting anything.

.PARAMETER Service
  server | dashboard | newsagent

.PARAMETER ValidateOnly
  Resolve config, interpreter, script and log, report, exit 0. Starts nothing.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('server', 'dashboard', 'newsagent')]
    [string]$Service,

    [switch]$ValidateOnly
)

# NOT 'Stop'. The child's stderr is merged into the pipeline below; under
# 'Stop' the first warning python writes to stderr becomes a terminating
# NativeCommandError and takes the service down with it.
$ErrorActionPreference = 'Continue'

. (Join-Path $PSScriptRoot 'keel-common.ps1')

$paths = Get-KeelPaths
$svc   = Get-KeelService $Service

# Before a log exists there is nowhere to complain to, so early failures go to
# stderr (Task Scheduler records only the exit code) and to a fixed fallback
# file that survives even when state/ config is the thing that is broken.
function Stop-KeelStartup {
    param([string]$Message, [int]$Code)
    $line = (Format-KeelStamp) + ' [keel-run] ' + $Service + ': ' + $Message
    [Console]::Error.WriteLine($line)
    try {
        if (-not (Test-Path -LiteralPath $paths.State)) { New-Item -ItemType Directory -Path $paths.State -Force | Out-Null }
        Add-Content -LiteralPath (Join-Path $paths.State 'win_bootstrap.log') -Value $line -Encoding UTF8
    } catch { }
    exit $Code
}

$cfg = Read-KeelConfig $paths.ConfigPath
if ($null -eq $cfg) {
    Stop-KeelStartup ('no install config at ' + $paths.ConfigPath + ' — run keel-services.ps1 install') 78
}
# The config pins the interpreter and the repo it was written for. A moved or
# copied checkout must not silently run against another clone's database.
if ($cfg.repo_root -and ($cfg.repo_root -ne $paths.Root)) {
    Stop-KeelStartup ('config was written for ' + $cfg.repo_root + ' but this copy lives at ' + $paths.Root + ' — re-run install') 78
}

$python = [string]$cfg.python
$script = Join-Path $paths.Bot $svc.Script
if (-not (Test-Path -LiteralPath $python)) { Stop-KeelStartup ('interpreter not found: ' + $python) 78 }
if (-not (Test-Path -LiteralPath $script)) { Stop-KeelStartup ('script not found: ' + $script) 78 }

$logMax = 20MB
if ($cfg.log_max_bytes) { $logMax = [int64]$cfg.log_max_bytes }
$logKeep = 5
if ($cfg.log_keep) { $logKeep = [int]$cfg.log_keep }

$log = New-KeelLog -Path (Get-KeelLogFile $paths.State $Service) -MaxBytes $logMax -Keep $logKeep

# UTF-8 on both streams. Under a SYSTEM logon the ANSI code page decides text
# encoding, and a single non-ASCII news headline is otherwise a
# UnicodeEncodeError that kills the process — the Mac/launchd side never saw
# this because its locale is UTF-8 already.
$env:PYTHONUNBUFFERED = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8       = '1'
switch ($Service) {
    'server'    { $env:SLC_PORT = [string]$cfg.engine_port }
    'newsagent' { $env:SLC_SERVER_URL = 'http://127.0.0.1:' + [string]$cfg.engine_port }
    default     { }
}

Write-KeelLog $log ('=' * 70)
Write-KeelLog $log ((Format-KeelStamp) + ' [keel-run] starting ' + $Service)
Write-KeelLog $log ((Format-KeelStamp) + ' [keel-run] interpreter : ' + $python)
Write-KeelLog $log ((Format-KeelStamp) + ' [keel-run] script      : ' + $script)
Write-KeelLog $log ((Format-KeelStamp) + ' [keel-run] workdir     : ' + $paths.Bot)
Write-KeelLog $log ((Format-KeelStamp) + ' [keel-run] identity    : ' + [Security.Principal.WindowsIdentity]::GetCurrent().Name)
Write-KeelLog $log ((Format-KeelStamp) + ' [keel-run] log cap     : ' + $log.MaxBytes + ' bytes, ' + $log.Keep + ' kept')

if ($ValidateOnly) {
    Write-KeelLog $log ((Format-KeelStamp) + ' [keel-run] -ValidateOnly: everything resolved, nothing started')
    Close-KeelLog $log
    Write-Output ('keel-run ' + $Service + ': OK')
    Write-Output ('  interpreter : ' + $python)
    Write-Output ('  script      : ' + $script)
    Write-Output ('  workdir     : ' + $paths.Bot)
    Write-Output ('  log         : ' + $log.Path)
    exit 0
}

# cwd matters: news_agent.py opens state/news_agent.log by relative path.
Set-Location -LiteralPath $paths.Bot
$pidFile = Get-KeelPidFile $paths.State $Service
Set-Content -LiteralPath $pidFile -Value $PID -Encoding ASCII -Force

$code = 0
try {
    $code = Invoke-KeelChild -Python $python -Script $script -Log $log
} catch {
    Write-KeelLog $log ((Format-KeelStamp) + ' [keel-run] wrapper caught: ' + $_.Exception.Message)
    $code = 70
} finally {
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}

# A service that returns has failed, whatever it claims on the way out. Task
# Scheduler only honours RestartOnFailure for a non-zero exit, so a clean exit
# is translated rather than trusted — otherwise a python process that decides
# to sys.exit(0) would stay down until a human noticed.
if ($null -eq $code) { $code = 1 }
if ($code -eq 0) { $code = 1 }
Write-KeelLog $log ((Format-KeelStamp) + ' [keel-run] ' + $Service + ' exited; reporting ' + $code + ' so the task restarts')
Close-KeelLog $log
exit $code
