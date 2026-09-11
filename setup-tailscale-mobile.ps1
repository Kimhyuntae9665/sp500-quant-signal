[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$LocalPort = 8765
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$tailscale = Get-Command "tailscale.exe" -ErrorAction SilentlyContinue
if ($null -eq $tailscale) {
    $fallback = "C:\Program Files\Tailscale\tailscale.exe"
    if (Test-Path -LiteralPath $fallback -PathType Leaf) {
        $tailscale = Get-Item -LiteralPath $fallback
    }
}
if ($null -eq $tailscale) {
    throw "Tailscale을 찾을 수 없습니다. PC에 Tailscale을 설치하고 로그인해 주세요."
}
$tailscalePath = if ($tailscale.PSObject.Properties.Name -contains "Path") {
    [string]$tailscale.Path
} elseif ($tailscale.PSObject.Properties.Name -contains "Source") {
    [string]$tailscale.Source
} else {
    [string]$tailscale.FullName
}
if (-not (Test-Path -LiteralPath $tailscalePath -PathType Leaf)) {
    throw "Tailscale 실행 파일 경로를 확인할 수 없습니다: $tailscalePath"
}

try {
    $localHealth = Invoke-RestMethod -Uri "http://127.0.0.1:$LocalPort/api/health" -TimeoutSec 5
} catch {
    throw "로컬 퀀트 서버가 127.0.0.1:$LocalPort 에서 실행 중이 아닙니다."
}
if (-not $localHealth.ok) {
    throw "로컬 퀀트 서버 상태가 정상이 아닙니다."
}

$status = (& $tailscalePath status --json | ConvertFrom-Json)
if ($status.BackendState -ne "Running" -or -not $status.Self.Online) {
    throw "PC Tailscale이 온라인 상태가 아닙니다. Tailscale에 로그인한 뒤 다시 실행하세요."
}
$dnsName = [string]$status.Self.DNSName
$dnsName = $dnsName.Trim().TrimEnd('.')
if (-not $dnsName) {
    throw "Tailscale MagicDNS 이름을 확인할 수 없습니다. MagicDNS를 켜 주세요."
}

$target = "http://127.0.0.1:$LocalPort"
& $tailscalePath serve --bg --yes $target
if ($LASTEXITCODE -ne 0) {
    throw "Tailscale Serve 설정에 실패했습니다."
}

$mobileUrl = "https://$dnsName/#portfolio"
$healthUrl = "https://$dnsName/api/health"
$healthy = $false
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    try {
        $response = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 3
        if ($response.ok) {
            $healthy = $true
            break
        }
    } catch {
        Start-Sleep -Milliseconds 500
    }
}
if (-not $healthy) {
    throw "Tailscale HTTPS 주소가 열렸지만 상태 확인에 실패했습니다: $healthUrl"
}

$distDirectory = Join-Path $PSScriptRoot "dist"
if (-not (Test-Path -LiteralPath $distDirectory -PathType Container)) {
    New-Item -ItemType Directory -Path $distDirectory | Out-Null
}
$urlPath = Join-Path $distDirectory "Galaxy-S23-server-url.txt"
[System.IO.File]::WriteAllText($urlPath, $mobileUrl + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))

Write-Host "Galaxy 전용 Tailscale HTTPS 연결 준비 완료" -ForegroundColor Green
Write-Host $mobileUrl -ForegroundColor Cyan
Write-Host "주소 파일: $urlPath"
Write-Host "이 주소는 Tailscale에 로그인한 기기에서만 열립니다. Funnel은 사용하지 않았습니다."
