param(
    [string]$TaskName = "YT Automation Service Guard"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$guard = Join-Path $root "scripts\service_guard.py"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Virtual environment Python not found: $python"
}

$taskCommand = '"{0}" "{1}"' -f $python, $guard
& schtasks.exe /Create /TN $TaskName /TR $taskCommand /SC MINUTE /MO 5 /F | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Could not create Windows recovery task."
}

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
Set-ScheduledTask -TaskName $TaskName -Settings $settings | Out-Null

& schtasks.exe /Run /TN $TaskName | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Recovery task was created but could not be started."
}

Write-Host "Installed and started: $TaskName"
