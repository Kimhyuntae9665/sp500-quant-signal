# S&P 500 퀀트 시그널 Android 앱

개인 PC에서 실행되는 퀀트 서버를 Samsung Android 휴대폰의 WebView로 여는 전용 앱입니다. 무거운 계산은 PC가 담당하고 Galaxy는 화면만 표시하므로 설치 파일이 작고, 저사양 기기까지 고려한 Android 7.0(API 24) 이상에서 실행됩니다. Galaxy S23과 같은 Android 14 환경을 기준으로 세로·가로 화면을 검증했습니다.

## 앱 동작

- 개인 release APK에는 이 PC의 Tailscale 전용 주소가 빌드 시 포함되어 첫 실행부터 자동 접속합니다.
- 내장 주소가 없거나 서버를 바꿀 때만 첫 연결 화면에서 URL을 입력하고 앱 전용 저장소에 보관합니다.
- 이후 실행부터 저장된 서버로 자동 접속합니다.
- 웹 화면 하단의 `연결` 탭이나 연결 오류 화면의 `서버 설정` 버튼으로 URL을 언제든 바꿀 수 있습니다.
- `http://IP:8765/?token=...` 전체 주소를 저장하며, 서버가 만든 세션 쿠키를 유지합니다.
- HTTP는 LAN 사설 IP, Tailscale IP/호스트명만 허용합니다. 공인 인터넷 주소는 HTTPS가 필요합니다.
- 같은 서버의 링크만 WebView 안에서 열고 외부 링크는 Android 기본 브라우저로 보냅니다.
- 연결 실패, HTTP 인증 실패, TLS 오류 화면에서 재시도하거나 서버 설정을 열 수 있습니다.
- Android 뒤로가기는 먼저 WebView 방문 기록을 이동하고, 기록이 없으면 앱을 종료합니다.

## PC 서버 준비 — 권장 Tailscale 방식

1. PC와 Galaxy에 Tailscale을 설치하고 같은 계정으로 로그인합니다.
2. PC에서 아래 명령을 한 번 실행합니다.

```powershell
Set-Location .\sp500-quant-signal
powershell -NoProfile -ExecutionPolicy Bypass -File .\install-mobile-autostart.ps1
```

작업 스케줄러의 `SP500 Quant Signal Mobile`이 PC 로그인 후 Tailscale IPv4의 `8766` 포트에만 개인 토큰 서버를 실행합니다. Wi-Fi나 외부 접속망이 바뀌어도 Tailscale 주소는 유지됩니다. PC가 켜져 있고 인터넷과 Tailscale에 연결되어 있어야 하며, 공유기 포트포워딩은 사용하지 않습니다.

자동 연결용 전체 주소는 다음 비공개 파일에만 저장됩니다.

```text
dist\private\Galaxy-S23-server-url.txt
```

이 파일과 접근 토큰은 다른 사람에게 보내지 마세요. 휴대폰의 `127.0.0.1`은 휴대폰 자신이므로 PC 서버 주소로 사용할 수 없습니다.

## 빌드

요구 사항은 JDK 17, Android SDK Platform 34, Build Tools 34.0.0입니다. 각 PC의 Android SDK 위치는 Git 제외 파일인 `local.properties`에 설정합니다.

개인용 최적화 release APK를 빌드합니다. 스크립트가 위 비공개 주소를 자동 포함하고, 린트·코드 축소·리소스 축소·APK 서명 검증·SHA-256 생성을 함께 수행합니다.

```powershell
Set-Location .\android-app
.\build-apk.ps1 release
```

설치 파일:

```text
..\dist\SP500-Quant-Signal-Galaxy-S23.apk
..\dist\SP500-Quant-Signal-Galaxy-S23.apk.sha256
```

## Samsung 휴대폰에 설치

가장 단순하고 외부 업로드가 없는 방법은 USB 복사입니다.

1. Galaxy S23을 USB-C 케이블로 PC에 연결하고 휴대폰 알림에서 USB 용도를 `파일 전송 / Android Auto`로 선택합니다.
2. Windows 파일 탐색기에서 `Galaxy S23 → Internal storage → Download`를 열어 `SP500-Quant-Signal-Galaxy-S23.apk`를 복사합니다.
3. Galaxy에서 Tailscale 앱을 설치하고 이 PC와 같은 계정으로 로그인한 뒤 VPN을 켭니다.
4. Samsung `내 파일 → 다운로드`에서 APK를 누릅니다.
5. 차단 안내가 나오면 해당 화면의 `설정`에서 **내 파일**에 대해서만 `이 출처 허용`을 잠시 켭니다.
6. 그래도 차단되고 Galaxy의 `자동 차단 기능(Auto Blocker)`이 켜져 있다면, 체크섬과 파일 출처를 다시 확인한 후 설치하는 동안만 `설정 → 보안 및 개인정보 보호 → 자동 차단 기능`을 끕니다.
7. 설치 직후 `내 파일`의 알 수 없는 앱 설치 권한과 자동 차단 기능을 원래대로 되돌립니다.
8. 앱 목록에서 `퀀트 시그널`을 실행합니다. 서버 주소를 묻지 않고 `내 포트`가 열리면 정상입니다.

Samsung Quick Share가 PC와 Galaxy 양쪽에 설정되어 있다면 같은 APK를 `Download`로 보내도 됩니다. 비공개 주소 파일이나 `.cache` 폴더는 옮기지 마세요.

APK는 이 PC에서 직접 빌드한 개인용 파일만 설치하세요. Samsung의 최신 안내는 [알 수 없는 출처 앱 설치 도움말](https://www.samsung.com/ae/support/mobile-devices/how-to-enable-permission-to-install-apps-from-unknown-source-on-my-samsung-phone/)에서 확인할 수 있습니다.

개발자 옵션의 USB 디버깅이 켜진 휴대폰은 PC에서 바로 설치·업데이트할 수도 있습니다.

```powershell
& "$env:LOCALAPPDATA\Android\Sdk\platform-tools\adb.exe" install -r "..\dist\SP500-Quant-Signal-Galaxy-S23.apk"
```

## 개인 서명 Release APK

현재 개인 release APK는 별도 `keystore.properties`가 없을 때 이 PC의 안정된 Android debug 키로 서명됩니다. Play Store 배포용은 아니지만 본인 휴대폰에 설치하고 같은 PC에서 만든 다음 버전으로 덮어쓰는 용도에는 충분합니다. 업데이트 호환을 유지하려면 `%USERPROFILE%\.android\debug.keystore`를 삭제하지 마세요.

장기 보관용 전용 release 키를 쓰려면 본인 키를 한 번 만들고 계속 보관할 수 있습니다.

```powershell
keytool -genkeypair -v -keystore quant-signal-release.jks -alias quant-signal -keyalg RSA -keysize 2048 -validity 10000
Copy-Item .\keystore.properties.example .\keystore.properties
```

`keystore.properties`의 비밀번호를 수정한 뒤:

```powershell
.\build-apk.ps1 release
```

키 저장소와 `keystore.properties`는 Git 제외 대상입니다. 키를 잃으면 기존 설치본에 업데이트 서명을 할 수 없습니다.

## 연결이 안 될 때

- PC의 작업 스케줄러에서 `SP500 Quant Signal Mobile`이 `실행 중`인지 확인합니다.
- PC와 Galaxy의 Tailscale이 모두 온라인이고 같은 계정인지 확인합니다.
- Galaxy 앱의 연결 화면에서 `재시도`를 누릅니다.
- 토큰을 교체했다면 `install-mobile-autostart.ps1 -RotateToken` 실행 후 release APK도 다시 빌드·설치해야 합니다.
- 앱 데이터 삭제나 재설치는 WebView에 자동 저장된 포트폴리오도 지우므로, 보유 수량을 따로 기록해 두세요.

## JavaScript 브리지

동일 서버에서 로드된 웹 UI는 다음 두 메서드를 사용할 수 있습니다.

```javascript
window.AndroidQuant?.openSettings();
window.AndroidQuant?.reload();
```

브리지에는 파일·토큰 조회나 임의 명령 기능이 없으며 설정 열기와 현재 페이지 재로딩만 제공합니다.
