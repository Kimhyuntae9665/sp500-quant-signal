[CmdletBinding()]
param(
    [string]$BindHost = "127.0.0.1",
    [ValidateRange(1, 65535)]
    [int]$Port = 8765,
    [string]$WebRoot = "",
    [ValidateSet("none", "quick", "full")]
    [string]$RefreshOnStart = "quick"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$serverPath = Join-Path $projectRoot "server.py"
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $serverPath -PathType Leaf)) {
    throw "server.py를 찾을 수 없습니다: $serverPath"
}

$serverArgs = @(
    $serverPath,
    "--host", $BindHost,
    "--port", $Port.ToString(),
    "--refresh-on-start", $RefreshOnStart
)
if ($WebRoot) {
    $serverArgs += @("--web-root", $WebRoot)
}

if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
    & $venvPython @serverArgs
    if ($LASTEXITCODE -ne 0) {
        throw "서버가 종료 코드 $LASTEXITCODE로 끝났습니다."
    }
    return
}

$pyLauncher = Get-Command "py" -ErrorAction SilentlyContinue
if ($null -ne $pyLauncher) {
    & $pyLauncher.Source -3 @serverArgs
    if ($LASTEXITCODE -ne 0) {
        throw "서버가 종료 코드 $LASTEXITCODE로 끝났습니다."
    }
    return
}

$pythonCommand = Get-Command "python" -ErrorAction SilentlyContinue
if ($null -ne $pythonCommand) {
    & $pythonCommand.Source @serverArgs
    if ($LASTEXITCODE -ne 0) {
        throw "서버가 종료 코드 $LASTEXITCODE로 끝났습니다."
    }
    return
}

throw "Python 3를 찾지 못했습니다. Python을 설치하거나 .venv를 먼저 생성하세요."
