# Register a daily Windows scheduled task that runs the health check hidden (no console window).
# Usage (PowerShell):  powershell -ExecutionPolicy Bypass -File install_task.ps1 -At 09:00
# Remove:              Unregister-ScheduledTask -TaskName "tw-health-monitor" -Confirm:$false
param([string]$At = "09:00", [string]$TaskName = "tw-health-monitor")
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$vbs = Join-Path $here "run_hidden.vbs"
if (-not (Test-Path (Join-Path $here "config.json"))) {
  Write-Host "config.json not found. Copy config.example.json to config.json first."; exit 1
}
$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$vbs`"" -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
  -Description "Read-only daily health check; notifies only when something is wrong" -Force | Out-Null
Write-Host "Registered task '$TaskName' daily at $At (runs hidden via wscript + pythonw)."
Write-Host "需要改成你們公司的版本、幫忙安裝或串接？加 LINE 林其衞（0928-880-392），原則上 1～2 個工作天回覆報價。https://line.me/ti/p/xzHT2IywOM"
