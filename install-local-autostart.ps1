[CmdletBinding()]
param(
    [string]$TaskName = "SP500 Quant Signal Local",
    [ValidateRange(1, 65535)]
    [int]$Port = 8765
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$watchdogPath = Join-Path $projectRoot "run-local-forever.ps1"
$launcherPath = Join-Path $projectRoot "run-local-server.pyw"
$serverPath = Join-Path $projectRoot "server.py"
$venvPythonw = Join-Path $projectRoot ".venv\Scripts\pythonw.exe"

if (-not (Test-Path -LiteralPath $launcherPath -PathType Leaf)) {
    throw "Console-free server launcher was not found: $launcherPath"
}
if (Test-Path -LiteralPath $venvPythonw -PathType Leaf) {
    $pythonwExe = $venvPythonw
} else {
    $pythonwCommand = Get-Command "pythonw.exe" -ErrorAction SilentlyContinue
    if ($null -eq $pythonwCommand) {
        $pythonCommand = Get-Command "python.exe" -ErrorAction SilentlyContinue
        if ($null -ne $pythonCommand) {
            $siblingPythonw = Join-Path (Split-Path -Parent $pythonCommand.Source) "pythonw.exe"
            if (Test-Path -LiteralPath $siblingPythonw -PathType Leaf) {
                $pythonwCommand = Get-Item -LiteralPath $siblingPythonw
            }
        }
    }
    if ($null -eq $pythonwCommand) {
        throw "Python's console-free pythonw.exe was not found."
    }
    if ($pythonwCommand.PSObject.Properties.Name -contains "Source") {
        $pythonwExe = [string]$pythonwCommand.Source
    } else {
        $pythonwExe = [string]$pythonwCommand.FullName
    }
}

# Replacing an existing task can otherwise leave its Python child alive for a
# moment. Stop only processes whose command lines identify this exact project,
# localhost binding, and port; never terminate an unrelated port owner.
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $existing) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        $state = (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue).State
        if ($state -ne "Running") {
            break
        }
        Start-Sleep -Milliseconds 250
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$matchingProcesses = Get-CimInstance Win32_Process | Where-Object {
    if (-not $_.CommandLine) {
        return $false
    }

    $commandLine = $_.CommandLine
    $isWatchdog = (
        $_.Name -ieq "powershell.exe" -and
        $commandLine.IndexOf($watchdogPath, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
    )
    $isConsoleFreeServer = (
        $_.Name -ieq "pythonw.exe" -and
        $commandLine.IndexOf($launcherPath, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -and
        $commandLine -match "--host\s+127\.0\.0\.1(?:\s|$)" -and
        $commandLine -match ("--port\s+{0}(?:\s|$)" -f $Port)
    )
    $isLocalServer = (
        $_.Name -in @("py.exe", "python.exe", "pythonw.exe") -and
        $commandLine.IndexOf($serverPath, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -and
        $commandLine -match "--host\s+127\.0\.0\.1(?:\s|$)" -and
        $commandLine -match ("--port\s+{0}(?:\s|$)" -f $Port)
    )
    $isWatchdog -or $isConsoleFreeServer -or $isLocalServer
}
foreach ($process in $matchingProcesses) {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
}

for ($attempt = 0; $attempt -lt 20; $attempt++) {
    $listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    if ($null -eq $listener) {
        break
    }
    Start-Sleep -Milliseconds 250
}
$listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
if ($null -ne $listener) {
    $ownerIds = ($listener | Select-Object -ExpandProperty OwningProcess -Unique) -join ", "
    throw "Port $Port is already used by another process (PID: $ownerIds). Stop it or select another -Port."
}

$arguments = '"{0}" --host 127.0.0.1 --port {1} --refresh-on-start quick' -f $launcherPath, $Port
$action = New-ScheduledTaskAction `
    -Execute $pythonwExe `
    -Argument $arguments `
    -WorkingDirectory $projectRoot
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
# Task Scheduler does not consistently apply RestartInterval when pythonw is
# externally terminated.  This lightweight repeating trigger is a second
# recovery path: IgnoreNew prevents duplicates while the healthy server runs,
# and a stopped server is relaunched at the next one-minute tick.
$recoveryTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 1)
$recoveryTrigger.Repetition.StopAtDurationEnd = $false
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -Hidden `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger @($logonTrigger, $recoveryTrigger) `
    -Principal $principal `
    -Settings $settings `
    -Description "Runs and relaunches the console-free local S&P 500 Quant Signal server on 127.0.0.1." `
    -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName

$healthUrl = "http://127.0.0.1:$Port/api/health"
$healthy = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Milliseconds 500
    try {
        $response = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
        if ($response.ok) {
            $healthy = $true
            break
        }
    } catch {
        # Python may still be importing the engine; retry within the bounded loop.
    }
}

$task = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host "Autostart installed: $TaskName" -ForegroundColor Green
Write-Host "Task state: $($task.State) | Last result: $($info.LastTaskResult)"
if ($healthy) {
    Write-Host "Local URL: http://127.0.0.1:$Port" -ForegroundColor Cyan
} else {
    throw "The task was registered, but the health check failed. See .cache\local-server.stderr.log."
}
