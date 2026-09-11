[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8765,
    [ValidateSet("none", "quick", "full")]
    [string]$RefreshOnStart = "quick",
    [ValidateRange(2, 300)]
    [int]$RestartDelaySeconds = 5
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$serverPath = Join-Path $projectRoot "server.py"
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$cacheRoot = Join-Path $projectRoot ".cache"
$logPath = Join-Path $cacheRoot "local-autostart.log"
$previousLogPath = Join-Path $cacheRoot "local-autostart.previous.log"
$stdoutPath = Join-Path $cacheRoot "local-server.stdout.log"
$stderrPath = Join-Path $cacheRoot "local-server.stderr.log"

if (-not (Test-Path -LiteralPath $serverPath -PathType Leaf)) {
    throw "server.py was not found: $serverPath"
}
if (-not (Test-Path -LiteralPath $cacheRoot -PathType Container)) {
    New-Item -ItemType Directory -Path $cacheRoot | Out-Null
}

# Keep the watchdog log bounded across long-running daily use.
if ((Test-Path -LiteralPath $logPath -PathType Leaf) -and
    (Get-Item -LiteralPath $logPath).Length -gt 5MB) {
    Move-Item -LiteralPath $logPath -Destination $previousLogPath -Force
}

function Write-WatchdogLog {
    param([Parameter(Mandatory = $true)][string]$Message)

    try {
        Add-Content -LiteralPath $logPath -Encoding UTF8 -Value $Message -ErrorAction Stop
    } catch {
        # A logging failure must never take down the local server watchdog.
    }
}

Set-Location -LiteralPath $projectRoot
Write-WatchdogLog -Message (
    "[{0}] localhost watchdog started (127.0.0.1:{1})" -f (Get-Date -Format o), $Port
)

$pythonPrefixArguments = @()
if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
    $pythonExecutable = $venvPython
} else {
    $pyLauncher = Get-Command "py" -ErrorAction SilentlyContinue
    if ($null -ne $pyLauncher) {
        $pythonExecutable = $pyLauncher.Source
        $pythonPrefixArguments = @("-3")
    } else {
        $pythonCommand = Get-Command "python" -ErrorAction SilentlyContinue
        if ($null -eq $pythonCommand) {
            throw "Python 3 was not found."
        }
        $pythonExecutable = $pythonCommand.Source
    }
}

# The localhost instance intentionally remains unauthenticated and unreachable
# from other devices. Mobile/LAN access uses the separate run-mobile.ps1 path.
$env:QUANT_ACCESS_TOKEN = ""

while ($true) {
    $exitCode = $null
    try {
        $serverArguments = @(
            $pythonPrefixArguments
            $serverPath
            "--host", "127.0.0.1"
            "--port", $Port.ToString()
            "--refresh-on-start", $RefreshOnStart
        )
        $process = Start-Process `
            -FilePath $pythonExecutable `
            -ArgumentList $serverArguments `
            -WorkingDirectory $projectRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath `
            -PassThru `
            -Wait
        $exitCode = $process.ExitCode
    } catch {
        Write-WatchdogLog -Message (
            "[{0}] process launcher error: {1}" -f (Get-Date -Format o), $_.Exception.Message
        )
    }

    Write-WatchdogLog -Message (
        "[{0}] server stopped (exit={1}); restarting in {2}s" -f `
            (Get-Date -Format o), $exitCode, $RestartDelaySeconds
    )
    Start-Sleep -Seconds $RestartDelaySeconds
}
