#Requires -Version 5.1
<#
.SYNOPSIS
  One-click portable install for the YouTube automation bot on Windows.
#>
param(
    [switch]$SkipAutostart,
    [switch]$SkipLan,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $Root "run.py"))) {
    $Root = $PSScriptRoot
}
Set-Location $Root

function Write-Step($msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg) { Write-Host "    OK: $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "    WARN: $msg" -ForegroundColor Yellow }

function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Ensure-Dir($path) {
    if (-not (Test-Path -LiteralPath $path)) {
        New-Item -ItemType Directory -Force -Path $path | Out-Null
    }
}

function Get-Flag([string]$name, [bool]$default = $true) {
    $path = Join-Path $Root "controls\flags\$name"
    if (-not (Test-Path -LiteralPath $path)) { return $default }
    $raw = (Get-Content -LiteralPath $path -Raw -ErrorAction SilentlyContinue).Trim()
    if ($raw -in @("0", "false", "off", "no", "disabled")) { return $false }
    return $true
}

function Ensure-WingetPackage {
    param([string]$Id, [string]$Name)
    Write-Step "Checking $Name"
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        Write-Warn "winget not found. Install $Name manually if missing."
        return
    }
    $list = & winget list --id $Id -e 2>$null
    if ($LASTEXITCODE -eq 0 -and ($list -join "`n") -match [regex]::Escape($Id)) {
        Write-Ok "$Name already installed"
        return
    }
    Write-Host "    Installing $Name via winget..."
    & winget install --id $Id -e --accept-package-agreements --accept-source-agreements --disable-interactivity
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "winget install for $Name returned $LASTEXITCODE (may already be present)"
    } else {
        Write-Ok "$Name installed"
    }
}

function Find-Python {
    $candidates = @(
        (Join-Path $Root ".venv\Scripts\python.exe"),
        "$env:LocalAppData\Programs\Python\Python312\python.exe",
        "$env:LocalAppData\Programs\Python\Python311\python.exe",
        "C:\Python312\python.exe",
        "C:\Python311\python.exe"
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path -LiteralPath $c)) { return $c }
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -notmatch "WindowsApps") { return $cmd.Source }
    $cmd = Get-Command py -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor White
Write-Host " YouTube Automation Bot - Portable Setup" -ForegroundColor White
Write-Host " Root: $Root" -ForegroundColor White
Write-Host "============================================================" -ForegroundColor White

if (-not (Test-IsAdmin)) {
    Write-Warn "Not running as Administrator. Firewall rules and boot-start task may be skipped."
    Write-Warn "Re-run setup\INSTALL.bat as Admin for full automation."
}

Ensure-Dir (Join-Path $Root "setup\_cache")
Ensure-Dir (Join-Path $Root "data\state")
Ensure-Dir (Join-Path $Root "secrets\tokens")
Ensure-Dir (Join-Path $Root "output")
Ensure-Dir (Join-Path $Root "logs")
Ensure-Dir (Join-Path $Root "controls\flags")
Ensure-Dir (Join-Path $Root ".runtime\bin")

# Default control flags if missing
foreach ($pair in @(
    @{ Name = "autostart.enabled"; Val = "1" },
    @{ Name = "lan.enabled"; Val = "1" },
    @{ Name = "boot_without_login.enabled"; Val = "1" }
)) {
    $fp = Join-Path $Root ("controls\flags\" + $pair.Name)
    if (-not (Test-Path -LiteralPath $fp)) {
        Set-Content -LiteralPath $fp -Value $pair.Val -Encoding ASCII
    }
}

# --- Languages / tools ---
$existingVenv = Join-Path $Root ".venv\Scripts\python.exe"
if (Test-Path -LiteralPath $existingVenv) {
    Write-Step "Existing .venv detected - skipping Python/Node winget installs"
    Write-Ok "Using $existingVenv"
} else {
    Ensure-WingetPackage -Id "Python.Python.3.12" -Name "Python 3.12"
    Ensure-WingetPackage -Id "OpenJS.NodeJS.LTS" -Name "Node.js LTS"
    Ensure-WingetPackage -Id "Git.Git" -Name "Git"
}

# Refresh PATH for this session
$env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
            [System.Environment]::GetEnvironmentVariable("Path", "User")

Write-Step "Locating Python"
$pythonLauncher = Find-Python
if (-not $pythonLauncher) {
    throw "Python was not found after install. Close this window, reopen as Admin, and run setup\INSTALL.bat again."
}
Write-Ok "Python: $pythonLauncher"

# --- venv ---
$venvPython = Join-Path $Root ".venv\Scripts\python.exe"
Write-Step "Creating / refreshing virtual environment"
if (-not (Test-Path -LiteralPath $venvPython)) {
    if ($pythonLauncher -match '\\py\.exe$') {
        & $pythonLauncher -3.12 -m venv (Join-Path $Root ".venv")
        if ($LASTEXITCODE -ne 0) { & $pythonLauncher -3 -m venv (Join-Path $Root ".venv") }
    } else {
        & $pythonLauncher -m venv (Join-Path $Root ".venv")
    }
    if (-not (Test-Path -LiteralPath $venvPython)) {
        throw "Failed to create .venv"
    }
    Write-Ok "Created .venv"
} else {
    Write-Ok ".venv already exists"
}

Write-Step "Upgrading pip / installing Python dependencies"
& $venvPython -m pip install --upgrade pip wheel setuptools
& $venvPython -m pip install -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
Write-Ok "Python packages installed"

# Ensure ffmpeg via imageio-ffmpeg
Write-Step "Ensuring FFmpeg (via imageio-ffmpeg)"
& $venvPython -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"
Write-Ok "FFmpeg ready"

# --- env template ---
Write-Step "Checking .env"
$envPath = Join-Path $Root ".env"
$example = Join-Path $Root ".env.example"
if (-not (Test-Path -LiteralPath $envPath) -and (Test-Path -LiteralPath $example)) {
    Copy-Item $example $envPath
    Write-Ok "Created .env from .env.example (fill API keys later)"
} else {
    Write-Ok ".env present or template missing"
}

# --- LAN firewall ---
$lanEnabled = (-not $SkipLan) -and (Get-Flag "lan.enabled" $true)
$dashPort = 8787
if ($lanEnabled -and (Test-IsAdmin)) {
    Write-Step "Configuring LAN dashboard firewall (TCP $dashPort)"
    $ruleName = "YT Automation Dashboard $dashPort"
    $existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
    if (-not $existing) {
        New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP -LocalPort $dashPort -Action Allow | Out-Null
        Write-Ok "Firewall rule created: $ruleName"
    } else {
        Enable-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Out-Null
        Write-Ok "Firewall rule already exists"
    }
} elseif ($lanEnabled) {
    Write-Warn "Skip firewall rule (need Admin). LAN may be blocked until you enable it from controls."
}

# --- Autostart tasks ---
$autoEnabled = (-not $SkipAutostart) -and (Get-Flag "autostart.enabled" $true)
$bootEnabled = (Get-Flag "boot_without_login.enabled" $true)
$startScript = Join-Path $Root "controls\START_BOT_SILENT.ps1"

if ($autoEnabled) {
    Write-Step "Installing Windows auto-start tasks"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument ("-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$startScript`"")
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2) -MultipleInstances IgnoreNew

    # At user logon (reliable for OAuth/tokens)
    $logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    Register-ScheduledTask -TaskName "YT Automation Bot (Logon)" -Action $action -Trigger $logonTrigger -Settings $settings -Force | Out-Null
    Write-Ok "Task installed: YT Automation Bot (Logon)"

    # At Windows startup (before/without interactive login) when Admin
    if ($bootEnabled -and (Test-IsAdmin)) {
        try {
            $startupTrigger = New-ScheduledTaskTrigger -AtStartup
            $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Highest
            Register-ScheduledTask -TaskName "YT Automation Bot (Startup)" -Action $action -Trigger $startupTrigger -Settings $settings -Principal $principal -Force | Out-Null
            Write-Ok "Task installed: YT Automation Bot (Startup) [runs at boot]"
        } catch {
            Write-Warn "Could not install boot-start task: $($_.Exception.Message)"
            Write-Warn "Logon auto-start is still active."
        }
    }

    # Keep existing recovery guard
    $guard = Join-Path $Root "scripts\install_service_guard_task.ps1"
    if (Test-Path -LiteralPath $guard) {
        & $guard
    }
} else {
    Write-Warn "Autostart disabled by controls/flags/autostart.enabled"
}

if (-not $NoStart) {
    Write-Step "Starting bot services now"
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $startScript
    Write-Ok "Start requested"
}

$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } | Select-Object -First 1 -ExpandProperty IPAddress)
Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host " SETUP COMPLETE" -ForegroundColor Green
Write-Host " Dashboard (this PC): http://127.0.0.1:$dashPort/" -ForegroundColor Green
if ($ip) { Write-Host " Dashboard (LAN):     http://${ip}:$dashPort/" -ForegroundColor Green }
Write-Host " Controls folder:     $Root\controls" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green