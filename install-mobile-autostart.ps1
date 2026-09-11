[CmdletBinding()]
param(
    [string]$TaskName = "SP500 Quant Signal Mobile",
    [ValidateRange(1, 65535)]
    [int]$Port = 8766,
    [switch]$RotateToken
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$launcherPath = Join-Path $projectRoot "run-mobile-server.pyw"
$cacheRoot = Join-Path $projectRoot ".cache"
$tokenPath = Join-Path $cacheRoot "mobile-access-token.txt"
$privateDist = Join-Path $projectRoot "dist\private"
$urlPath = Join-Path $privateDist "Galaxy-S23-server-url.txt"
$venvPythonw = Join-Path $projectRoot ".venv\Scripts\pythonw.exe"

if (-not (Test-Path -LiteralPath $launcherPath -PathType Leaf)) {
    throw "모바일 서버 실행 파일을 찾을 수 없습니다: $launcherPath"
}

$tailscale = Get-Command "tailscale.exe" -ErrorAction SilentlyContinue
if ($null -eq $tailscale) {
    $fallback = "C:\Program Files\Tailscale\tailscale.exe"
    if (Test-Path -LiteralPath $fallback -PathType Leaf) {
        $tailscale = Get-Item -LiteralPath $fallback
    }
}
if ($null -eq $tailscale) {
    throw "Tailscale을 찾을 수 없습니다."
}
$tailscalePath = if ($tailscale.PSObject.Properties.Name -contains "Path") {
    [string]$tailscale.Path
} elseif ($tailscale.PSObject.Properties.Name -contains "Source") {
    [string]$tailscale.Source
} else {
    [string]$tailscale.FullName
}
$tailscaleStatus = (& $tailscalePath status --json | ConvertFrom-Json)
if ($tailscaleStatus.BackendState -ne "Running" -or -not $tailscaleStatus.Self.Online) {
    throw "PC Tailscale이 온라인 상태가 아닙니다."
}
$bindHost = @($tailscaleStatus.TailscaleIPs | Where-Object { $_ -match '^100\.' }) | Select-Object -First 1
if (-not $bindHost) {
    throw "PC의 Tailscale IPv4 주소를 확인할 수 없습니다."
}

if (-not (Test-Path -LiteralPath $cacheRoot -PathType Container)) {
    New-Item -ItemType Directory -Path $cacheRoot | Out-Null
}
if ((Test-Path -LiteralPath $tokenPath -PathType Leaf) -and -not $RotateToken) {
    $accessToken = (Get-Content -LiteralPath $tokenPath -Raw).Trim()
} else {
    $bytes = [byte[]]::new(24)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $accessToken = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    [System.IO.File]::WriteAllText($tokenPath, $accessToken, [System.Text.Encoding]::ASCII)
}
if ($accessToken -notmatch '^[A-Za-z0-9_-]{16,}$') {
    throw "모바일 접근 토큰 파일이 손상되었습니다: $tokenPath"
}

if (Test-Path -LiteralPath $venvPythonw -PathType Leaf) {
    $pythonwExe = $venvPythonw
} else {
    $pythonwCommand = Get-Command "pythonw.exe" -ErrorAction SilentlyContinue
    if ($null -eq $pythonwCommand) {
        throw "pythonw.exe를 찾을 수 없습니다."
    }
    $pythonwExe = [string]$pythonwCommand.Source
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $existing) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        if ((Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue).State -ne "Running") {
            break
        }
        Start-Sleep -Milliseconds 250
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$matchingProcesses = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -ieq "pythonw.exe" -and
    $_.CommandLine -and
    $_.CommandLine.IndexOf($launcherPath, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
}
foreach ($process in $matchingProcesses) {
    Stop-Process -Id $process.ProcessId -ErrorAction SilentlyContinue
}
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    if (-not (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)) {
        break
    }
    Start-Sleep -Milliseconds 250
}
$listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
if ($listener) {
    $ownerIds = ($listener | Select-Object -ExpandProperty OwningProcess -Unique) -join ", "
    throw "포트 $Port 를 다른 프로세스가 사용 중입니다. PID: $ownerIds"
}

$arguments = '"{0}" --host {1} --port {2} --refresh-on-start none' -f $launcherPath, $bindHost, $Port
$action = New-ScheduledTaskAction -Execute $pythonwExe -Argument $arguments -WorkingDirectory $projectRoot
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$recoveryTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$recoveryTrigger.Repetition.StopAtDurationEnd = $false
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
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
    -Description "Private token-protected S&P 500 Quant Signal server on the PC Tailscale IP." `
    -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

$healthUrl = "http://${bindHost}:$Port/api/health?token=$accessToken"
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
        # The Python service may still be importing its engine.
    }
}
if (-not $healthy) {
    throw "모바일 서버가 등록되었지만 상태 확인에 실패했습니다. .cache\mobile-server.stderr.log를 확인하세요."
}

if (-not (Test-Path -LiteralPath $privateDist -PathType Container)) {
    New-Item -ItemType Directory -Path $privateDist | Out-Null
}
$mobileUrl = "http://${bindHost}:$Port/?token=$accessToken#portfolio"
[System.IO.File]::WriteAllText($urlPath, $mobileUrl + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))

Write-Host "Galaxy 전용 모바일 서버 준비 완료" -ForegroundColor Green
Write-Host "Tailscale 주소: http://${bindHost}:$Port/#portfolio" -ForegroundColor Cyan
Write-Host "앱 자동 연결용 비공개 주소 파일: $urlPath"
Write-Host "작업 스케줄러: $TaskName | 상태: $((Get-ScheduledTask -TaskName $TaskName).State)"
Write-Warning "Galaxy에도 Tailscale을 설치하고 같은 계정으로 로그인해야 합니다. 인터넷 포트포워딩은 사용하지 마세요."
