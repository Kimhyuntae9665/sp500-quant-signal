[CmdletBinding()]
param(
    [string]$TaskName = "SP500 Quant Signal Local"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $task) {
    Write-Host "No autostart task is registered: $TaskName"
    return
}

$projectRoot = $PSScriptRoot
$launcherPath = Join-Path $projectRoot "run-local-server.pyw"
$serverPath = Join-Path $projectRoot "server.py"
$actionArguments = ($task.Actions | Select-Object -First 1 -ExpandProperty Arguments)
$port = if ($actionArguments -match "--port\s+(\d+)") { [int]$Matches[1] } else { 8765 }

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false

$matchingProcesses = Get-CimInstance Win32_Process | Where-Object {
    if (-not $_.CommandLine) { return $false }
    $commandLine = $_.CommandLine
    $isLauncher = (
        $_.Name -ieq "pythonw.exe" -and
        $commandLine.IndexOf($launcherPath, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -and
        $commandLine -match ("--port\s+{0}(?:\s|$)" -f $port)
    )
    $isLegacyServer = (
        $_.Name -in @("py.exe", "python.exe", "pythonw.exe") -and
        $commandLine.IndexOf($serverPath, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -and
        $commandLine -match "--host\s+127\.0\.0\.1(?:\s|$)" -and
        $commandLine -match ("--port\s+{0}(?:\s|$)" -f $port)
    )
    $isLauncher -or $isLegacyServer
}
foreach ($process in $matchingProcesses) {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
}
Write-Host "Autostart task removed: $TaskName"
