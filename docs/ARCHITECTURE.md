# 아키텍처

## 목표와 경계

이 애플리케이션은 다섯 부분을 분리합니다.

| 계층 | 책임 | 직접 알지 않는 것 |
|---|---|---|
| `web/` | 화면 렌더링, 필터·정렬, API 호출 | 데이터 공급자와 점수 계산 내부 |
| `server.py` | 정적 파일, JSON API, 요청 검증, 갱신 작업 상태 | 지표 산식과 공급자별 파싱 |
| `quant_engine/` | 종목 구성, 데이터 수집·캐시, 지표·점수 계산 | HTTP 요청/응답과 화면 레이아웃 |
| `portfolio_service.py` | 미국·KOSPI·KRX 금 검색, 시세·환율 정규화, 원화 평가, 짧은 TTL 캐시 | S&P 500 점수 계산과 화면 레이아웃 |
| `android-app/` | 서버 주소 보관, WebView 표시, 연결 오류·외부 링크 처리 | 종목 데이터 수집과 점수 계산 |

브라우저는 같은 출처의 `/api/*`만 호출합니다. HTTP 서버는 `SignalServiceAdapter`를 거쳐 `quant_engine.service.SignalService`를 사용합니다. 엔진의 공개 메서드가 바뀌면 우선 어댑터만 수정합니다.

## 요청 흐름

일반 조회는 다음 순서로 처리됩니다.

1. `ThreadingHTTPServer`가 요청마다 처리 스레드를 할당합니다.
2. `RequestHandler`가 경로·티커·JSON 본문을 검증합니다.
3. `SignalServiceAdapter`가 `screen()`, `get_signal()`, `get_chart_history()` 또는 규칙 메타데이터를 호출합니다. 대시보드와 단일 신호 조회는 캐시 전용(`allow_fetch=False`)이라 전체 네트워크 수집을 암묵적으로 시작하지 않습니다. 단, 사용자가 상세 화살표를 열어 `/api/stocks/{ticker}/history`를 요청하면 그 티커의 3년 가격 이력과 약 5년 주간 PER 계산 입력만 필요 시 수집해 각각 별도 캐시에 저장합니다.
4. dataclass, 날짜, Decimal 등은 JSON 안전 값으로 변환됩니다.
5. 서버가 UTF-8 JSON과 명시적 상태 코드를 반환합니다.

`GET /api/dashboard`는 엔진의 스크리닝 결과에 항상 `refresh` 상태를 합칩니다. 엔진 결과가 객체가 아니면 `data` 필드로 감쌉니다.

포트폴리오 요청은 점수 엔진과 독립적입니다. `/api/portfolio/search`는 시장별 검색 공급자를 호출하고, `/api/portfolio/compose`는 최대 50개 보유 항목과 선택적 `cash_krw`를 검증한 뒤 Yahoo 주식·환율 편의 시세와 네이버 국내 금 편의 시세를 원화로 정규화합니다. 입력 현금은 별도 공급자 호출 없이 `cash` 그룹의 원화 자산으로 합산합니다. 일부 종목의 시세만 실패하면 성공 종목과 현금으로 합계와 비중을 만들고 실패 항목을 `unpriced`로 반환하며, 평가 가능한 종목과 현금이 모두 없을 때만 안정된 오류를 냅니다. 따라서 점수 엔진 초기화가 실패해도 포트 검색은 계속 사용할 수 있습니다.

브라우저는 선택 종목·수량·현금 만원 값을 버전이 붙은 `localStorage` 키에 보관하고, 포트 화면의 최초 진입 때 최신 시세로 결과를 다시 구성합니다. 접근 토큰, 공급자 응답, 환율, 계산 결과는 보관하지 않습니다. 도넛과 그룹 트리맵은 서버가 반환한 평가액을 사용해 브라우저에서 결정적으로 만든 inline SVG이며, 자산별 색은 시장 계열 안에서 심볼에 따라 안정적으로 구분합니다.

## 새로고침 수명 주기

`POST /api/refresh`는 긴 데이터 수집이 HTTP 요청을 막지 않도록 별도 daemon 스레드를 시작하고 즉시 `202 Accepted`를 반환합니다.

- `quick`: 전체 종목 가격과 SPY/VIX/선택적 Fear & Greed 갱신, `fundamentals=False`, `prices=True`
- `full`: quick 범위에 종목별 펀더멘털 추가, `fundamentals=True`, `prices=True`
- `limit`: 현재 유니버스의 앞쪽 N개 심볼만 개발·점검용으로 전달
- 동시에 실행 가능한 작업: 1개

서버 CLI의 `--refresh-on-start` 기본값은 `quick`입니다. 리스닝 소켓을 연 뒤 같은 백그라운드 경로로 갱신하므로 HTTP 시작을 막지 않습니다. `none`은 기존 캐시만 사용할 때, `full`은 시작과 동시에 펀더멘털까지 받을 때 사용합니다.

`RefreshState`는 lock으로 `running`, 모드, 제한 수, 시작/완료 시각, 마지막 결과와 오류를 보호합니다. 프로세스를 재시작하면 이 메모리 상태는 초기화되지만 엔진의 영속 캐시는 유지될 수 있습니다. daemon 작업은 프로세스 종료 시 강제 중단될 수 있으므로 엔진은 부분 갱신과 재실행을 견뎌야 합니다.

## 엔진 어댑터 계약

권장 엔진 인터페이스는 다음과 같습니다.

```python
service = SignalService()
service.screen()
service.get_signal("AAPL")
service.get_chart_history("AAPL")
service.refresh(
    symbols=None,
    fundamentals=True,
    prices=True,
)
```

반환 객체는 `to_dict()`를 제공하는 것이 가장 좋습니다. 서버는 dataclass와 기본 컬렉션도 변환합니다. 날짜/시간은 ISO 8601, 결측치는 JSON `null`이어야 합니다. `NaN`과 `Infinity`는 올바른 JSON이 아니므로 엔진 경계에서 `None`으로 정규화해야 합니다.

현재 어댑터는 전환 편의를 위해 일부 대체 메서드 이름을 받아들이지만, 장기적으로 권장 계약으로 고정하는 편이 좋습니다. `/api/rules`는 서비스의 `get_rules`/`rules`를 먼저 사용하고, 그다음 `quant_engine.scoring`의 공개 규칙 메타데이터를 찾습니다. 둘 다 없을 때만 설명용 fallback을 반환하며 실제 점수는 언제나 엔진 계산이 기준입니다.

### 점수와 표시의 경계

`/api/rules`의 `company_rules`와 `StockSignal.components`가 회사 점수의 계약이며 현재 9개 규칙만 포함합니다. `sector_relative_value`와 `relative_momentum`은 규칙, score component, 대시보드 표시 항목에서 제외합니다. `FundamentalData.market_cap`은 공급자·모델 내부 입력으로 남을 수 있지만 회사 규칙이나 score/display factor가 아닙니다. 호환성을 위해 내부에 남는 원천·파생 필드도 점수 또는 화면 신호로 해석하거나 렌더링하지 않습니다.

단일 종목 응답에는 점수용 최대 5개 과거 Forward/Trailing PER 스냅샷, 연간 희석 EPS, 연간 희석주식수 등 작은 재무 이력이 포함됩니다. 전체 대시보드 응답에서는 503개 종목의 중복 전송을 막기 위해 이 이력 필드를 제거합니다. 상세 `/history` 응답은 3년 가격·RSI의 `chart_price_history_v1`과 StockAnalysis 분기 앵커 및 Yahoo 주간 종가로 계산한 약 5년 PER의 `chart_weekly_valuation_v2`를 합칩니다. StockAnalysis에 분기 기준가격이 없는 종목은 가장 가까운 이전 Yahoo 종가를 앵커 가격으로 사용합니다. 주간 Forward PER은 분기 예상 EPS를 다음 앵커까지 유지하는 차트용 프록시이며 점수 입력은 아닙니다. 펀더멘털은 schema version 4 캐시로 분리합니다.

`limit`을 지원하려면 `SignalService.universe` 또는 서비스 자체가 `get_symbols()`, `symbols`, `tickers`, `constituents` 중 하나로 현재 티커 목록을 공개하는 것이 가장 명확합니다. 전환 기간에는 `get_snapshot()`, `snapshot`, `load()`가 반환하는 snapshot의 `symbols`/`members`도 어댑터가 읽습니다.

## HTTP 및 오류 계약

| 상황 | 상태 코드 |
|---|---:|
| 정상 조회 | `200` |
| 갱신 접수 | `202` |
| 잘못된 티커·JSON·모드·limit | `400` |
| 개인 토큰 누락·불일치 | `401` |
| 정적 경로가 웹 루트 밖을 가리킴 | `403` |
| 경로 없음 | `404` |
| 갱신 중복 요청 | `409` |
| JSON 본문 크기 초과 | `413` |
| 잘못된 Content-Type | `415` |
| 엔진 import·초기화 실패 | `503` |
| 포트폴리오 편의 공급자가 전부 실패 | `502` |
| 예상하지 못한 서버 오류 | `500` |

예상된 오류의 본문 형식은 다음과 같습니다.

```json
{
  "error": {
    "code": "invalid_mode",
    "message": "mode must be either 'quick' or 'full'"
  },
  "time": "2026-07-19T13:00:00Z"
}
```

## 정적 파일 보안

- 기본 웹 루트는 프로젝트의 `web/`이며 디렉터리 목록은 제공하지 않습니다.
- URL을 디코딩한 뒤 `..`, 역슬래시, NUL을 거부하고 최종 `resolve()` 경로가 웹 루트 하위인지 다시 검사합니다.
- MIME 유형, 길이, `nosniff`, 동일 출처 referrer 정책을 명시합니다.
- 기본 바인드는 `127.0.0.1`입니다. 일반 실행은 인증을 활성화하지 않으므로 그대로 LAN이나 인터넷에 노출하면 안 됩니다.
- `run-mobile.ps1`은 192비트 임의 토큰을 생성해 `QUANT_ACCESS_TOKEN`으로 서버에 전달합니다. `/?token=...` 부트스트랩은 파생 세션값을 HttpOnly·SameSite 쿠키에 저장하고 토큰 없는 URL로 리다이렉트합니다. API 클라이언트는 별도 토큰 헤더도 사용할 수 있습니다.
- 요청 로그에서는 `token` 쿼리 값을 마스킹합니다. 토큰 원문 파일은 Git 제외 대상인 `.cache/` 아래에만 둡니다.
- 토큰 인증은 TLS를 대신하지 않습니다. 같은 집 Wi-Fi나 암호화된 개인 VPN 안에서만 사용하고 공유기 포트포워딩으로 공개하지 않습니다. 외부 공개용 TLS, 사용자 계정, rate limit은 구현 범위가 아닙니다.

## 종료와 장애 처리

`Ctrl+C`, `SIGINT`, `SIGTERM`은 `shutdown()`을 요청하고 리스닝 소켓을 닫습니다. 조회 요청의 예외는 해당 요청의 JSON 오류로 격리됩니다. 백그라운드 갱신 예외는 stderr에 traceback을 남기고 `RefreshState.last_error`에 요약해 다음 health/dashboard 조회에서 관찰할 수 있습니다.

운영 환경으로 확장할 때는 작업 큐와 영속 job 테이블, 구조화 로그, 인증/TLS reverse proxy, 공급자별 rate limit, 상태 메트릭을 추가해야 합니다.
