param(
    [ValidateSet("debug", "release")]
    [string]$Variant = "debug",
    [string]$ServerUrlOverride = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$AndroidStudioJbr = "C:\Program Files\Android\Android Studio\jbr"

if (Test-Path -LiteralPath $AndroidStudioJbr) {
    $env:JAVA_HOME = $AndroidStudioJbr
}

if (-not $env:JAVA_HOME -or -not (Test-Path -LiteralPath (Join-Path $env:JAVA_HOME "bin\java.exe"))) {
    throw "JDK 17을 찾을 수 없습니다. JAVA_HOME을 JDK 17 경로로 설정해 주세요."
}

$RepositoryRoot = Split-Path -Parent $ProjectRoot
$PrivateUrlPath = Join-Path $RepositoryRoot "dist\private\Galaxy-S23-server-url.txt"
$PrivatePropertiesPath = Join-Path $ProjectRoot "private.properties"
$DefaultServerUrl = $ServerUrlOverride.Trim()
if (-not $DefaultServerUrl -and (Test-Path -LiteralPath $PrivateUrlPath -PathType Leaf)) {
    $DefaultServerUrl = (Get-Content -LiteralPath $PrivateUrlPath -Raw).Trim()
}
if ($DefaultServerUrl) {
    [System.IO.File]::WriteAllText(
        $PrivatePropertiesPath,
        ("defaultServerUrl={0}{1}" -f $DefaultServerUrl, [Environment]::NewLine),
        [System.Text.UTF8Encoding]::new($false)
    )
}

$Tasks = if ($Variant -eq "release") { @("lintRelease", "assembleRelease") } else { @("assembleDebug") }
$GradleArguments = @("--no-daemon") + @($Tasks)
& (Join-Path $ProjectRoot "gradlew.bat") @GradleArguments
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$ApkDirectory = Join-Path $ProjectRoot "app\build\outputs\apk\$Variant"
$Apk = Get-ChildItem -LiteralPath $ApkDirectory -Filter "*.apk" -File |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
if ($null -eq $Apk) {
    throw "빌드 결과 APK를 찾을 수 없습니다: $ApkDirectory"
}

$DistDirectory = Join-Path $RepositoryRoot "dist"
if (-not (Test-Path -LiteralPath $DistDirectory -PathType Container)) {
    New-Item -ItemType Directory -Path $DistDirectory | Out-Null
}
$ArtifactName = if ($Variant -eq "release") {
    "SP500-Quant-Signal-Galaxy-S23.apk"
} else {
    "SP500-Quant-Signal-Galaxy-S23-debug.apk"
}
$ArtifactPath = Join-Path $DistDirectory $ArtifactName
Copy-Item -LiteralPath $Apk.FullName -Destination $ArtifactPath -Force

$Hash = Get-FileHash -Algorithm SHA256 -LiteralPath $ArtifactPath
$HashPath = "$ArtifactPath.sha256"
[System.IO.File]::WriteAllText(
    $HashPath,
    ("{0} *{1}{2}" -f $Hash.Hash, $ArtifactName, [Environment]::NewLine),
    [System.Text.Encoding]::ASCII
)

$LocalProperties = Join-Path $ProjectRoot "local.properties"
$SdkRoot = $null
if (Test-Path -LiteralPath $LocalProperties -PathType Leaf) {
    $SdkLine = Get-Content -LiteralPath $LocalProperties |
        Where-Object { $_ -match '^sdk\.dir=' } |
        Select-Object -First 1
    if ($SdkLine) {
        $SdkRoot = ($SdkLine -replace '^sdk\.dir=', '') -replace '\\:', ':' -replace '\\\\', '\'
    }
}
$ApkSigner = if ($SdkRoot) { Join-Path $SdkRoot "build-tools\34.0.0\apksigner.bat" } else { $null }
if ($ApkSigner -and (Test-Path -LiteralPath $ApkSigner -PathType Leaf)) {
    & $ApkSigner verify --verbose --print-certs $ArtifactPath
    if ($LASTEXITCODE -ne 0) {
        throw "APK 서명 검증에 실패했습니다: $ArtifactPath"
    }
}

Write-Host "APK 생성 완료: $ArtifactPath" -ForegroundColor Green
Write-Host "SHA-256: $($Hash.Hash)" -ForegroundColor Cyan
Write-Host "체크섬 파일: $HashPath"
