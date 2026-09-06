#Requires -Version 5.1
$ErrorActionPreference = "Continue"
$Root = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
Set-Location $Root
New-Item -ItemType Directory -Force -Path (Join-Path $Root "logs") | Out-Null

function Get-Flag([string]$name, [bool]$default = $true) {
    $path = Join-Path $Root "controls\flags\$name"
    if (-not (Test-Path -LiteralPath $path)) { return $default }
    $raw = (Get-Content -LiteralPath $path -Raw -ErrorAction SilentlyContinue).Trim()
    if ($raw -in @("0", "false", "off", "no", "disabled")) { return $false }
    return $true
}

function Get-RootPythonProcesses([string]$pattern) {
    $rootNeedle = $Root.Replace('\', '\\')
    return @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
        $_.CommandLine -and
        ($_.CommandLine -like ("*" + $Root + "*")) -and
        ($_.CommandLine -match $pattern)
    } | Sort-Object ProcessId)
}

function Ensure-Single([string]$pattern, [scriptblock]$startBlock) {
    $procs = Get-RootPythonProcesses $pattern
    if ($procs.Count -gt 1) {
        foreach ($extra in $procs | Select-Object -Skip 1) {
            try { Stop-Process -Id $extra.ProcessId -Force -ErrorAction Stop } catch {}
        }
        $procs = @($procs[0])
    }
    if ($procs.Count -eq 0) { & $startBlock }
}

$python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    Add-Content -Path (Join-Path $Root "logs\autostart.log") -Value "$(Get-Date -Format o) missing venv python"
    exit 1
}

$dashPort = 8787
$lan = Get-Flag "lan.enabled" $true
$hostBind = if ($lan) { "0.0.0.0" } else { "127.0.0.1" }

Ensure-Single 'run\.py schedule' {
    Start-Process -FilePath $python -ArgumentList @("run.py","schedule","--mode","cron","--upload") -WorkingDirectory $Root -WindowStyle Hidden
}

$portListen = Get-NetTCPConnection -LocalPort $dashPort -State Listen -ErrorAction SilentlyContinue
$dashProcs = Get-RootPythonProcesses 'live_dashboard_server\.py'
if ($dashProcs.Count -gt 1) {
    foreach ($extra in $dashProcs | Select-Object -Skip 1) {
        try { Stop-Process -Id $extra.ProcessId -Force -ErrorAction Stop } catch {}
    }
    $dashProcs = @($dashProcs[0])
}
if (($dashProcs.Count -eq 0) -and (-not $portListen)) {
    $env:YT_DASHBOARD_HOST = $hostBind
    $env:YT_DASHBOARD_PORT = "$dashPort"
    Start-Process -FilePath $python -ArgumentList @("scripts\live_dashboard_server.py","--no-open") -WorkingDirectory $Root -WindowStyle Hidden
}

$env:YT_WATCHDOG_ALLOW_UPLOAD = "1"
Ensure-Single 'watchdog\.py' {
    Start-Process -FilePath $python -ArgumentList @("scripts\watchdog.py") -WorkingDirectory $Root -WindowStyle Hidden
}

# Service guard: only install task (do not stack extra guard processes)
$guard = Join-Path $Root "scripts\install_service_guard_task.ps1"
if (Test-Path -LiteralPath $guard) {
    try {
        & $guard 2>&1 | Out-Null
    } catch {}
}
Ensure-Single 'service_guard\.py' {
    # Task should start it; if still missing, start once
    Start-Process -FilePath $python -ArgumentList @("scripts\service_guard.py") -WorkingDirectory $Root -WindowStyle Hidden
}

Add-Content -Path (Join-Path $Root "logs\autostart.log") -Value "$(Get-Date -Format o) started host=$hostBind lan=$lan"