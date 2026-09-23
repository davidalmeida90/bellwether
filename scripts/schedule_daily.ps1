# Register the daily refresh with Windows Task Scheduler.
#
#   powershell -ExecutionPolicy Bypass -File scripts\schedule_daily.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\schedule_daily.ps1 -At 08:30
#   powershell -ExecutionPolicy Bypass -File scripts\schedule_daily.ps1 -Remove
#
# Growth, runs on the board and alerts all depend on the boards being captured
# every day, so this is what turns a one-off run into a series.

param(
    [string]$At = "20:30",
    [string]$TaskName = "Bellwether daily",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "removed the task '$TaskName'"
    exit 0
}

$python = (Get-Command py -ErrorAction SilentlyContinue)
if (-not $python) { throw "the py launcher was not found on PATH" }

$action = New-ScheduledTaskAction -Execute "py.exe" `
    -Argument "-3 refresh.py --trending" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "Capture the GitHub trending boards and run alert rules" `
    -Force | Out-Null

Write-Host "'$TaskName' runs every day at $At in $root"
Write-Host "run it now:  Start-ScheduledTask -TaskName '$TaskName'"
