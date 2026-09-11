[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8765,
    [ValidateSet("none", "quick", "full")]
    [string]$RefreshOnStart = "quick",
    [string]$BindHost = "",
    [switch]$RotateToken
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$runner = Join-Path $projectRoot "run.ps1"
$cacheRoot = Join-Path $projectRoot ".cache"
$tokenPath = Join-Path $cacheRoot "mobile-access-token.txt"

if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
    throw "run.ps1을 찾을 수 없습니다: $runner"
}

if (-not $BindHost) {
    $network = Get-NetIPConfiguration |
        Where-Object {
            $_.NetAdapter.Status -eq "Up" -and
            $null -ne $_.IPv4DefaultGateway -and
            $null -ne $_.IPv4Address
        } |
        Select-Object -First 1

    if ($null -eq $network) {
        throw "휴대폰과 연결할 활성 IPv4 네트워크를 찾지 못했습니다. -BindHost에 PC 주소를 지정하세요."
    }

    $address = $network.IPv4Address |
        Where-Object {
            $_.IPAddress -notlike "127.*" -and
            $_.IPAddress -notlike "169.254.*"
        } |
        Select-Object -First 1

    if ($null -eq $address) {
        throw "사용 가능한 LAN IPv4 주소를 찾지 못했습니다."
    }
    $BindHost = $address.IPAddress
    $profile = Get-NetConnectionProfile -InterfaceIndex $network.InterfaceIndex -ErrorAction SilentlyContinue
} else {
    $profile = $null
}

if (-not (Test-Path -LiteralPath $cacheRoot -PathType Container)) {
    New-Item -ItemType Directory -Path $cacheRoot | Out-Null
}

if ((Test-Path -LiteralPath $tokenPath -PathType Leaf) -and -not $RotateToken) {
    $accessToken = (Get-Content -LiteralPath $tokenPath -Raw).Trim()
} else {
    $random = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $bytes = New-Object byte[] 24
        $random.GetBytes($bytes)
    } finally {
        $random.Dispose()
    }
    $accessToken = [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
    Set-Content -LiteralPath $tokenPath -Value $accessToken -Encoding Ascii -NoNewline
}

if ($accessToken.Length -lt 16) {
    throw "모바일 접근 토큰 파일이 손상되었습니다: $tokenPath"
}

$mobileUrl = "http://${BindHost}:$Port/?token=$accessToken"

Write-Host ""
Write-Host "S&P 500 퀀트 모바일 서버" -ForegroundColor Green
Write-Host "휴대폰 앱의 서버 주소에 아래 전체 주소를 입력하세요."
Write-Host $mobileUrl -ForegroundColor Cyan
Write-Host ""
Write-Warning "이 주소에는 개인 접근 토큰이 포함됩니다. 다른 사람에게 공유하거나 인터넷 포트포워딩에 사용하지 마세요."
if ($null -ne $profile -and $profile.NetworkCategory -eq "Public") {
    Write-Warning "현재 네트워크가 Windows에서 '공용'으로 설정되어 있어 휴대폰 접속이 방화벽에 막힐 수 있습니다. 신뢰하는 집 Wi-Fi일 때만 네트워크 프로필을 '개인'으로 바꾸고 Python의 개인 네트워크 접근을 허용하세요."
}
Write-Host "PC와 휴대폰은 같은 Wi-Fi 또는 동일한 개인 VPN에 있어야 합니다. 종료: Ctrl+C"
Write-Host ""

$previousToken = $env:QUANT_ACCESS_TOKEN
$env:QUANT_ACCESS_TOKEN = $accessToken
try {
    & $runner -BindHost $BindHost -Port $Port -RefreshOnStart $RefreshOnStart
} finally {
    $env:QUANT_ACCESS_TOKEN = $previousToken
}
