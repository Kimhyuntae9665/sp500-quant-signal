#!/usr/bin/env python3
"""Local HTTP server for the S&P 500 Quant Signal application.

The HTTP layer intentionally depends only on Python's standard library.  Calls
into ``quant_engine`` are kept in ``SignalServiceAdapter`` so the public engine
API can evolve without leaking those changes into request handling.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import decimal
import enum
import hashlib
import hmac
import inspect
import json
import mimetypes
import os
from pathlib import Path
import re
import signal
import sys
import threading
import traceback
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from portfolio_service import (
    PortfolioError,
    PortfolioService,
)

try:
    from gs_quant_lab import GsQuantLabError, GsQuantLabService
except Exception as exc:  # Keep the existing screener usable if the optional lab fails.
    GsQuantLabError = None  # type: ignore[assignment,misc]
    GsQuantLabService = None  # type: ignore[assignment,misc]
    GS_QUANT_LAB_IMPORT_ERROR: Exception | None = exc
else:
    GS_QUANT_LAB_IMPORT_ERROR = None


APP_NAME = "sp500-quant-signal"
PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_WEB_ROOT = PROJECT_ROOT / "web"
MAX_JSON_BODY_BYTES = 64 * 1024
TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")
AUTH_COOKIE_NAME = "quant_session"
AUTH_COOKIE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
MIN_ACCESS_TOKEN_LENGTH = 16


try:
    from quant_engine.service import SignalService
except Exception as exc:  # Keep the static UI and /api/health diagnosable.
    SignalService = None  # type: ignore[assignment,misc]
    ENGINE_IMPORT_ERROR: Exception | None = exc
else:
    ENGINE_IMPORT_ERROR = None


def utc_now() -> str:
    """Return a compact, unambiguous timestamp for API metadata."""

    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def to_jsonable(value: Any) -> Any:
    """Convert engine/dataclass values to objects accepted by ``json.dumps``."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return to_jsonable(value.to_dict())
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return to_jsonable(dataclasses.asdict(value))
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (decimal.Decimal, Path)):
        return str(value)
    if isinstance(value, enum.Enum):
        return to_jsonable(value.value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


class ApiError(Exception):
    """An expected client/service error with an HTTP status and stable code."""

    def __init__(self, status: HTTPStatus, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class RefreshState:
    """Thread-safe snapshot of the single background refresh job."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False
        self._mode: str | None = None
        self._limit: int | None = None
        self._started_at: str | None = None
        self._finished_at: str | None = None
        self._last_error: str | None = None
        self._last_result: Any = None

    def start(self, *, mode: str, limit: int | None) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._mode = mode
            self._limit = limit
            self._started_at = utc_now()
            self._finished_at = None
            self._last_error = None
            self._last_result = None
            return True

    def finish(self, *, result: Any = None, error: str | None = None) -> None:
        with self._lock:
            self._running = False
            self._finished_at = utc_now()
            self._last_error = error
            self._last_result = to_jsonable(result) if result is not None else None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._running,
                "mode": self._mode,
                "limit": self._limit,
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "last_error": self._last_error,
                "last_result": self._last_result,
            }


class SignalServiceAdapter:
    """Small compatibility boundary around ``quant_engine.service``.

    The preferred engine contract is:

    * ``screen()`` -> dashboard result
    * ``get_signal(symbol)`` -> one stock result
    * ``refresh(symbols=None, fundamentals=..., prices=True)`` -> report

    Alternative method names are accepted to make integration failures easier
    to fix while the engine is under active development.
    """

    def __init__(self, service: Any) -> None:
        self.service = service

    def dashboard(self) -> Any:
        method = self._method("screen", "get_dashboard", "dashboard")
        return to_jsonable(method())

    def stock(self, ticker: str) -> Any:
        method = self._method("get_signal", "get_stock", "stock")
        return to_jsonable(method(ticker))

    def stock_history(self, ticker: str) -> Any:
        method = self._method("get_chart_history")
        return to_jsonable(method(ticker))

    def rules(self) -> Any:
        for name in ("get_rules", "rules", "rule_definitions"):
            candidate = getattr(self.service, name, None)
            if callable(candidate):
                return to_jsonable(candidate())
            if candidate is not None:
                return to_jsonable(candidate)

        # Scoring modules may expose their schema independently of the service.
        try:
            from quant_engine import scoring
        except Exception:
            return _fallback_rule_definitions()

        for name in ("get_rule_definitions", "get_rules"):
            candidate = getattr(scoring, name, None)
            if callable(candidate):
                return to_jsonable(candidate())
        for name in ("RULE_DEFINITIONS", "SCORING_RULES", "RULES"):
            candidate = getattr(scoring, name, None)
            if candidate is not None:
                return to_jsonable(candidate)
        return _fallback_rule_definitions()

    def refresh(self, *, mode: str, limit: int | None) -> Any:
        method = self._method("refresh", "refresh_all")
        symbols = self._symbols(limit) if limit is not None else None

        preferred_kwargs = {
            "symbols": symbols,
            "fundamentals": mode == "full",
            "prices": True,
        }
        kwargs = _supported_kwargs(method, preferred_kwargs)

        # A generic/older engine may expose ``mode`` and ``limit`` directly.
        signature = _safe_signature(method)
        if signature is not None:
            names = signature.parameters
            accepts_any = any(
                item.kind == inspect.Parameter.VAR_KEYWORD
                for item in names.values()
            )
            if "mode" in names or accepts_any:
                kwargs.setdefault("mode", mode)
            if "limit" in names or accepts_any:
                kwargs.setdefault("limit", limit)

        return to_jsonable(method(**kwargs))

    def _symbols(self, limit: int) -> list[str]:
        """Read the current universe without embedding its implementation here."""

        candidates: list[Any] = []
        universe = getattr(self.service, "universe", None)
        if universe is not None:
            candidates.append(universe)
        candidates.append(self.service)

        for owner in candidates:
            for name in (
                "get_symbols",
                "symbols",
                "tickers",
                "constituents",
                "get_snapshot",
                "snapshot",
                "load",
            ):
                value = getattr(owner, name, None)
                if callable(value):
                    value = value()
                symbols = _coerce_symbols(value)
                if symbols:
                    return symbols[:limit]

        raise RuntimeError(
            "The engine does not expose its universe symbols; "
            "add get_symbols()/symbols to SignalService or its universe."
        )

    def _method(self, *names: str) -> Callable[..., Any]:
        for name in names:
            candidate = getattr(self.service, name, None)
            if callable(candidate):
                return candidate
        joined = ", ".join(names)
        raise RuntimeError(f"SignalService is missing a required method ({joined})")


def _safe_signature(method: Callable[..., Any]) -> inspect.Signature | None:
    try:
        return inspect.signature(method)
    except (TypeError, ValueError):
        return None


def _supported_kwargs(
    method: Callable[..., Any], preferred: Mapping[str, Any]
) -> dict[str, Any]:
    signature = _safe_signature(method)
    if signature is None:
        return dict(preferred)
    accepts_any = any(
        item.kind == inspect.Parameter.VAR_KEYWORD
        for item in signature.parameters.values()
    )
    if accepts_any:
        return dict(preferred)
    return {
        key: value
        for key, value in preferred.items()
        if key in signature.parameters
    }


def _coerce_symbols(value: Any) -> list[str]:
    if value is None or isinstance(value, (str, bytes)):
        return []
    if isinstance(value, Mapping):
        for key in ("symbols", "members", "constituents", "tickers"):
            if key in value:
                symbols = _coerce_symbols(value[key])
                if symbols:
                    return symbols
        return []

    for name in ("symbols", "members", "constituents", "tickers"):
        nested = getattr(value, name, None)
        if nested is not None and nested is not value:
            symbols = _coerce_symbols(nested)
            if symbols:
                return symbols
    if not isinstance(value, Iterable):
        return []

    result: list[str] = []
    for item in value:
        if isinstance(item, str):
            symbol = item
        elif isinstance(item, Mapping):
            symbol = str(item.get("symbol") or item.get("ticker") or "")
        else:
            symbol = str(
                getattr(item, "symbol", None)
                or getattr(item, "ticker", None)
                or ""
            )
        symbol = symbol.strip().upper()
        if TICKER_RE.fullmatch(symbol):
            result.append(symbol)
    return result


def _fallback_rule_definitions() -> dict[str, Any]:
    """UI-safe core schema used only when the engine exposes no rules metadata."""

    return {
        "source": "server_fallback",
        "authoritative": False,
        "version": "fallback-1.2",
        "minimum_company_metrics": 5,
        "note": "실제 점수 계산은 quant_engine이 기준이며 이 목록은 UI 설명용입니다.",
        "rules": [
            {
                "id": "drawdown_from_52w_high",
                "label": "52주 고점 대비 하락률",
                "unit": "%",
                "period": "52주",
                "direction": "lower_is_more_attractive",
            },
            {
                "id": "pe_gap_3y",
                "label": "현재 PER과 3년 기준 PER의 괴리",
                "unit": "%",
                "period": "3년",
                "direction": "lower_is_more_attractive",
            },
            {
                "id": "sector_relative_value",
                "label": "섹터 내 상대가치",
                "unit": "백분위",
                "period": "현재 S&P 500 섹터",
                "direction": "higher_is_more_attractive",
            },
            {
                "id": "distance_from_ma200",
                "label": "200거래일 이동평균 이격도",
                "unit": "%",
                "period": "200거래일",
                "direction": "lower_is_more_attractive",
            },
            {
                "id": "eps_growth_yoy",
                "label": "최근 1년 EPS 성장률",
                "unit": "%",
                "period": "최근 1년",
                "direction": "higher_is_more_attractive",
            },
            {
                "id": "relative_momentum",
                "label": "S&P 500 상대 모멘텀",
                "unit": "백분위",
                "period": "6-1개월·12-1개월",
                "direction": "higher_is_more_attractive",
            },
            {
                "id": "shareholder_return_3y",
                "label": "3년 평균 주주환원율 프록시",
                "unit": "%",
                "period": "3년",
                "direction": "higher_is_more_attractive",
            },
            {
                "id": "shares_dilution_3y",
                "label": "최근 3년 희석주식수 증감",
                "unit": "%",
                "period": "3년",
                "direction": "lower_is_more_attractive",
            },
            {
                "id": "cash_flow_quality",
                "label": "3년 현금흐름 품질",
                "unit": "배",
                "period": "최근 3개 회계연도",
                "direction": "higher_is_more_attractive",
            },
        ],
    }


def create_service_adapter() -> SignalServiceAdapter | None:
    if SignalService is None:
        return None
    return SignalServiceAdapter(SignalService())


def merge_refresh(payload: Any, refresh: Mapping[str, Any]) -> dict[str, Any]:
    """Guarantee that dashboard responses expose background refresh state."""

    converted = to_jsonable(payload)
    if isinstance(converted, dict):
        return {**converted, "refresh": dict(refresh)}
    return {"data": converted, "refresh": dict(refresh)}


class QuantHTTPServer(ThreadingHTTPServer):
    """HTTP server carrying application dependencies for each request thread."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        *,
        adapter: SignalServiceAdapter | None,
        portfolio_service: PortfolioService | None = None,
        gs_quant_lab_service: Any | None = None,
        web_root: Path,
        engine_error: Exception | None = ENGINE_IMPORT_ERROR,
        access_token: str | None = None,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.adapter = adapter
        self.portfolio_service = (
            portfolio_service if portfolio_service is not None else PortfolioService()
        )
        self.gs_quant_lab_service = (
            gs_quant_lab_service
            if gs_quant_lab_service is not None
            else GsQuantLabService() if GsQuantLabService is not None else None
        )
        self.web_root = web_root.resolve()
        self.refresh_state = RefreshState()
        self.engine_error = engine_error
        self.access_token = access_token
        self.auth_session = _session_digest(access_token) if access_token else None


def run_refresh_job(
    server: QuantHTTPServer,
    adapter: SignalServiceAdapter,
    *,
    mode: str,
    limit: int | None,
) -> None:
    """Execute one accepted refresh and persist its observable final state."""

    try:
        result = adapter.refresh(mode=mode, limit=limit)
    except Exception as exc:  # The error is observable through health/dashboard.
        traceback.print_exc(file=sys.stderr)
        server.refresh_state.finish(error=f"{type(exc).__name__}: {exc}")
    else:
        server.refresh_state.finish(result=result)


def start_background_refresh(
    server: QuantHTTPServer,
    adapter: SignalServiceAdapter,
    *,
    mode: str,
    limit: int | None = None,
) -> bool:
    """Atomically accept one job and start it without blocking the HTTP server."""

    if not server.refresh_state.start(mode=mode, limit=limit):
        return False
    threading.Thread(
        target=run_refresh_job,
        name="quant-data-refresh",
        args=(server, adapter),
        kwargs={"mode": mode, "limit": limit},
        daemon=True,
    ).start()
    return True


class RequestHandler(BaseHTTPRequestHandler):
    """JSON API and safe static-file handler."""

    server: QuantHTTPServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        try:
            if self._finish_authentication_response():
                return
            path = urlsplit(self.path).path
            if path == "/api/health":
                self._get_health()
            elif path == "/api/portfolio/search":
                self._get_portfolio_search()
            elif path == "/api/dashboard":
                self._get_dashboard()
            elif path == "/api/rules":
                self._get_rules()
            elif path.startswith("/api/stocks/") and path.endswith("/history"):
                self._get_stock_history(path)
            elif path.startswith("/api/stocks/"):
                self._get_stock(path)
            elif path.startswith("/api/"):
                raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "API endpoint not found")
            else:
                self._serve_static(path, head_only=False)
        except Exception as exc:
            self._handle_exception(exc)

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler contract
        try:
            if self._finish_authentication_response(head_only=True):
                return
            path = urlsplit(self.path).path
            if path.startswith("/api/"):
                raise ApiError(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "method_not_allowed",
                    "HEAD is not supported for API endpoints",
                )
            self._serve_static(path, head_only=True)
        except Exception as exc:
            self._handle_exception(exc, head_only=True)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
        try:
            if self._finish_authentication_response():
                return
            path = urlsplit(self.path).path
            if path == "/api/portfolio/compose":
                self._post_portfolio_compose()
            elif path == "/api/gs-quant/analyze":
                self._post_gs_quant_analyze()
            elif path == "/api/refresh":
                self._post_refresh()
            else:
                raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "API endpoint not found")
        except Exception as exc:
            self._handle_exception(exc)

    def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib handler contract
        if self._finish_authentication_response(head_only=True):
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Allow", "GET, HEAD, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _finish_authentication_response(self, *, head_only: bool = False) -> bool:
        """Authorize a request or finish it with a bootstrap/401 response.

        Mobile clients may open ``/?token=...`` once. A successful bootstrap
        stores only a derived session value in an HttpOnly cookie, then redirects
        to a clean URL so the access token does not remain in browser history.
        API clients can alternatively send ``X-Quant-Token`` or a Bearer token.
        """

        expected = self.server.access_token
        if not expected:
            return False

        parsed = urlsplit(self.path)
        query_items = parse_qsl(parsed.query, keep_blank_values=True)
        supplied_query_tokens = [
            value for key, value in query_items if key.lower() == "token"
        ]
        if len(supplied_query_tokens) == 1 and _secure_equal(
            supplied_query_tokens[0], expected
        ):
            remaining_query = [
                (key, value) for key, value in query_items if key.lower() != "token"
            ]
            clean_location = urlunsplit(
                ("", "", parsed.path or "/", urlencode(remaining_query), "")
            )
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", clean_location)
            self.send_header("Set-Cookie", self._auth_cookie_header())
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True

        cookie_value = self._cookie_value(AUTH_COOKIE_NAME)
        if cookie_value and self.server.auth_session and _secure_equal(
            cookie_value, self.server.auth_session
        ):
            return False

        header_token = self.headers.get("X-Quant-Token", "").strip()
        authorization = self.headers.get("Authorization", "").strip()
        if authorization.lower().startswith("bearer "):
            header_token = authorization[7:].strip()
        if header_token and _secure_equal(header_token, expected):
            return False

        path = parsed.path
        if path.startswith("/api/"):
            # Authentication is decided before a POST body is parsed. Close
            # unauthorized POST connections so unread bytes cannot be
            # mistaken for the next HTTP request on a keep-alive socket.
            if self.command == "POST":
                self.close_connection = True
            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                {
                    "error": {
                        "code": "authentication_required",
                        "message": "A valid personal access token is required",
                    },
                    "time": utc_now(),
                },
            )
        else:
            self._send_auth_required_page(head_only=head_only)
        return True

    def _auth_cookie_header(self) -> str:
        value = self.server.auth_session or ""
        return (
            f"{AUTH_COOKIE_NAME}={value}; Path=/; HttpOnly; SameSite=Strict; "
            f"Max-Age={AUTH_COOKIE_MAX_AGE_SECONDS}"
        )

    def _cookie_value(self, name: str) -> str | None:
        raw = self.headers.get("Cookie", "")
        if not raw:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except CookieError:
            return None
        morsel = cookie.get(name)
        return morsel.value if morsel else None

    def _send_auth_required_page(self, *, head_only: bool) -> None:
        body = (
            "<!doctype html><html lang=\"ko\"><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>연결 인증 필요</title>"
            "<style>body{margin:0;background:#f7f5ef;color:#17251f;font-family:"
            "system-ui,sans-serif;display:grid;min-height:100vh;place-items:center}"
            "main{max-width:32rem;margin:24px;padding:28px;border:1px solid #d9dfd8;"
            "border-radius:20px;background:#fff}h1{font-size:1.35rem}p{line-height:1.65;"
            "color:#58635e}</style><main><h1>개인 연결 인증이 필요합니다</h1>"
            "<p>Android 앱의 연결 설정에서 PC 실행창에 표시된 전체 주소를 "
            "입력해 주세요. 주소에는 개인 접근 토큰이 포함됩니다.</p></main></html>"
        ).encode("utf-8")
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _get_health(self) -> None:
        refresh = self.server.refresh_state.snapshot()
        engine_ready = self.server.adapter is not None
        payload: dict[str, Any] = {
            "ok": engine_ready,
            "service": APP_NAME,
            "time": utc_now(),
            "engine": {
                "ready": engine_ready,
                "type": "SignalService" if engine_ready else None,
            },
            "gs_quant_lab": {
                "ready": self.server.gs_quant_lab_service is not None,
                "mode": "open_source_local",
                "marquee_connected": False,
            },
            "refresh": refresh,
        }
        if not engine_ready and self.server.engine_error is not None:
            payload["engine"]["error"] = str(self.server.engine_error)
        if (
            self.server.gs_quant_lab_service is None
            and GS_QUANT_LAB_IMPORT_ERROR is not None
        ):
            payload["gs_quant_lab"]["error"] = str(GS_QUANT_LAB_IMPORT_ERROR)
        status = HTTPStatus.OK if engine_ready else HTTPStatus.SERVICE_UNAVAILABLE
        self._send_json(status, payload)

    def _get_portfolio_search(self) -> None:
        parsed = urlsplit(self.path)
        query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
        allowed_fields = {"market", "q", "limit"}
        unknown_fields = sorted({key for key, _value in query_pairs} - allowed_fields)
        if unknown_fields:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "invalid_query",
                f"Unknown query fields: {', '.join(unknown_fields)}",
            )
        duplicate_fields = sorted(
            key
            for key in allowed_fields
            if sum(1 for item, _value in query_pairs if item == key) > 1
        )
        if duplicate_fields:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "invalid_query",
                f"Duplicate query fields: {', '.join(duplicate_fields)}",
            )
        query = dict(query_pairs)
        if "market" not in query:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "invalid_market",
                "market is required",
            )
        try:
            payload = self.server.portfolio_service.search(
                market=query.get("market"),
                query=query.get("q", ""),
                limit=query.get("limit", 10),
            )
        except PortfolioError as exc:
            raise ApiError(exc.status, exc.code, exc.message) from exc
        self._send_json(HTTPStatus.OK, payload)

    def _post_portfolio_compose(self) -> None:
        body = self._read_json_body(allowed_fields={"holdings", "cash_krw"})
        try:
            payload = self.server.portfolio_service.compose(body)
        except PortfolioError as exc:
            raise ApiError(exc.status, exc.code, exc.message) from exc
        self._send_json(HTTPStatus.OK, payload)

    def _post_gs_quant_analyze(self) -> None:
        body = self._read_json_body(
            allowed_fields={
                "positions",
                "benchmark",
                "lookback",
                "risk_free_rate_pct",
            }
        )
        service = self.server.gs_quant_lab_service
        if service is None:
            raise ApiError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "gs_quant_lab_unavailable",
                "GS Quant Risk Lab is unavailable on this server",
            )
        try:
            payload = service.analyze(body)
        except Exception as exc:
            if GsQuantLabError is not None and isinstance(exc, GsQuantLabError):
                try:
                    status = HTTPStatus(int(exc.status))
                except (TypeError, ValueError):
                    status = HTTPStatus.BAD_REQUEST
                raise ApiError(status, exc.code, exc.message) from exc
            raise
        self._send_json(HTTPStatus.OK, payload)

    def _get_dashboard(self) -> None:
        adapter = self._require_adapter()
        payload = merge_refresh(
            adapter.dashboard(), self.server.refresh_state.snapshot()
        )
        self._send_json(HTTPStatus.OK, payload)

    def _get_rules(self) -> None:
        adapter = self._require_adapter()
        self._send_json(HTTPStatus.OK, adapter.rules())

    def _get_stock(self, path: str) -> None:
        encoded = path.removeprefix("/api/stocks/")
        if not encoded or "/" in encoded:
            raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "Stock endpoint not found")
        ticker = unquote(encoded).strip().upper()
        if not TICKER_RE.fullmatch(ticker):
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "invalid_ticker",
                "Ticker must contain 1-15 letters, digits, dots, or hyphens",
            )
        try:
            payload = self._require_adapter().stock(ticker)
        except KeyError as exc:
            detail = str(exc).strip("'") or f"{ticker} was not found"
            raise ApiError(
                HTTPStatus.NOT_FOUND, "stock_not_found", detail
            ) from exc
        if payload is None:
            raise ApiError(
                HTTPStatus.NOT_FOUND,
                "stock_not_found",
                f"{ticker} was not found",
            )
        self._send_json(HTTPStatus.OK, payload)

    def _get_stock_history(self, path: str) -> None:
        encoded = path.removeprefix("/api/stocks/").removesuffix("/history")
        if not encoded or "/" in encoded:
            raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "Stock history endpoint not found")
        ticker = unquote(encoded).strip().upper()
        if not TICKER_RE.fullmatch(ticker):
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "invalid_ticker",
                "Ticker must contain 1-15 letters, digits, dots, or hyphens",
            )
        try:
            payload = self._require_adapter().stock_history(ticker)
        except KeyError as exc:
            detail = str(exc).strip("'") or f"{ticker} was not found"
            raise ApiError(
                HTTPStatus.NOT_FOUND, "stock_not_found", detail
            ) from exc
        self._send_json(HTTPStatus.OK, payload)

    def _post_refresh(self) -> None:
        adapter = self._require_adapter()
        body = self._read_json_body(allowed_fields={"mode", "limit"})
        mode = body.get("mode", "quick")
        limit = body.get("limit")

        if mode not in {"quick", "full"}:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "invalid_mode",
                "mode must be either 'quick' or 'full'",
            )
        if isinstance(limit, bool) or (
            limit is not None and not isinstance(limit, int)
        ):
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "invalid_limit",
                "limit must be an integer or null",
            )
        if limit is not None and not 1 <= limit <= 1000:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "invalid_limit",
                "limit must be between 1 and 1000",
            )

        state = self.server.refresh_state
        if not start_background_refresh(
            self.server, adapter, mode=mode, limit=limit
        ):
            raise ApiError(
                HTTPStatus.CONFLICT,
                "refresh_in_progress",
                "A refresh job is already running",
            )
        self._send_json(
            HTTPStatus.ACCEPTED,
            {"accepted": True, "refresh": state.snapshot()},
        )

    def _read_json_body(
        self, *, allowed_fields: set[str] | frozenset[str] | None = None
    ) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "")
        if content_type and content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise ApiError(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "unsupported_media_type",
                "Content-Type must be application/json",
            )
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return {}
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ApiError(
                HTTPStatus.BAD_REQUEST, "invalid_content_length", "Invalid Content-Length"
            ) from exc
        if length < 0 or length > MAX_JSON_BODY_BYTES:
            raise ApiError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "body_too_large",
                f"JSON body may not exceed {MAX_JSON_BODY_BYTES} bytes",
            )
        raw = self.rfile.read(length)
        if not raw.strip():
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(
                HTTPStatus.BAD_REQUEST, "invalid_json", "Request body is not valid UTF-8 JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise ApiError(
                HTTPStatus.BAD_REQUEST, "invalid_json", "JSON body must be an object"
            )
        accepted_fields = {"mode", "limit"} if allowed_fields is None else set(allowed_fields)
        unknown = sorted(set(payload) - accepted_fields)
        if unknown:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "unknown_fields",
                f"Unknown JSON fields: {', '.join(unknown)}",
            )
        return payload

    def _serve_static(self, url_path: str, *, head_only: bool) -> None:
        try:
            decoded = unquote(url_path, errors="strict")
        except UnicodeDecodeError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_path", "Invalid URL path") from exc
        if "\x00" in decoded or "\\" in decoded:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_path", "Invalid URL path")

        relative = decoded.lstrip("/") or "index.html"
        parts = Path(relative).parts
        if any(part in {"..", "."} for part in parts):
            raise ApiError(HTTPStatus.FORBIDDEN, "path_traversal", "Path is outside web root")

        candidate = (self.server.web_root / relative).resolve()
        try:
            candidate.relative_to(self.server.web_root)
        except ValueError as exc:
            raise ApiError(
                HTTPStatus.FORBIDDEN, "path_traversal", "Path is outside web root"
            ) from exc
        if not candidate.is_file():
            raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "Static file not found")

        try:
            size = candidate.stat().st_size
            content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("Cache-Control", self._cache_control(candidate.name))
            self.end_headers()
            if not head_only:
                with candidate.open("rb") as file_handle:
                    while chunk := file_handle.read(64 * 1024):
                        self.wfile.write(chunk)
        except OSError as exc:
            raise ApiError(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "static_read_failed",
                "Unable to read static file",
            ) from exc

    @staticmethod
    def _cache_control(filename: str) -> str:
        lower_name = filename.lower()
        if lower_name.endswith(".html") or lower_name == "sw.js":
            return "no-cache, no-store, must-revalidate"
        if lower_name.endswith((".js", ".css", ".webmanifest")):
            return "no-cache, must-revalidate"
        return "public, max-age=3600"

    def _require_adapter(self) -> SignalServiceAdapter:
        if self.server.adapter is None:
            message = "Quant engine is unavailable"
            if self.server.engine_error is not None:
                message += f": {self.server.engine_error}"
            raise ApiError(
                HTTPStatus.SERVICE_UNAVAILABLE, "engine_unavailable", message
            )
        return self.server.adapter

    def _send_json(self, status: HTTPStatus, payload: Any) -> None:
        body = json.dumps(
            to_jsonable(payload),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _handle_exception(self, exc: Exception, *, head_only: bool = False) -> None:
        if isinstance(exc, ApiError):
            status, code, message = exc.status, exc.code, exc.message
        else:
            traceback.print_exc(file=sys.stderr)
            status = HTTPStatus.INTERNAL_SERVER_ERROR
            code = "internal_error"
            message = "Unexpected server error"

        payload = {
            "error": {"code": code, "message": message},
            "time": utc_now(),
        }
        try:
            if head_only:
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
            else:
                self._send_json(status, payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format: str, *args: Any) -> None:
        timestamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        message = format % args
        message = re.sub(
            r"(?i)([?&]token=)[^&\s\"]+", r"\1<redacted>", message
        )
        print(f"[{timestamp}] {self.client_address[0]} {message}", file=sys.stderr)


def _secure_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _session_digest(access_token: str) -> str:
    return hmac.new(
        access_token.encode("utf-8"),
        b"sp500-quant-signal/mobile-session/v1",
        hashlib.sha256,
    ).hexdigest()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Serve the local S&P 500 Quant Signal web application."
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("QUANT_HOST", "127.0.0.1"),
        help="bind address (default: 127.0.0.1; env: QUANT_HOST)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("QUANT_PORT", "8765")),
        help="bind port (default: 8765; env: QUANT_PORT)",
    )
    parser.add_argument(
        "--web-root",
        type=Path,
        default=DEFAULT_WEB_ROOT,
        help="static web directory (default: ./web)",
    )
    parser.add_argument(
        "--refresh-on-start",
        choices=("none", "quick", "full"),
        default=os.environ.get("QUANT_REFRESH_ON_START", "quick"),
        help=(
            "background refresh after binding the server "
            "(default: quick; env: QUANT_REFRESH_ON_START)"
        ),
    )
    parser.add_argument(
        "--access-token",
        default=os.environ.get("QUANT_ACCESS_TOKEN"),
        help=(
            "optional personal access token; required for every request when set "
            "(env: QUANT_ACCESS_TOKEN)"
        ),
    )
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if not args.web_root.resolve().is_dir():
        parser.error(f"--web-root is not a directory: {args.web_root}")
    if args.access_token is not None:
        args.access_token = args.access_token.strip()
        if not args.access_token:
            args.access_token = None
        elif len(args.access_token) < MIN_ACCESS_TOKEN_LENGTH:
            parser.error(
                f"--access-token must contain at least {MIN_ACCESS_TOKEN_LENGTH} characters"
            )
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    engine_error = ENGINE_IMPORT_ERROR
    try:
        adapter = create_service_adapter()
    except Exception as exc:
        print(f"Failed to initialize SignalService: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        adapter = None
        engine_error = exc

    portfolio_service = PortfolioService()

    server = QuantHTTPServer(
        (args.host, args.port),
        RequestHandler,
        adapter=adapter,
        portfolio_service=portfolio_service,
        web_root=args.web_root,
        engine_error=engine_error,
        access_token=args.access_token,
    )

    stopping = threading.Event()

    def request_shutdown(signum: int, _frame: Any) -> None:
        if stopping.is_set():
            return
        stopping.set()
        print(f"\nReceived signal {signum}; shutting down...", file=sys.stderr)
        threading.Thread(target=server.shutdown, daemon=True).start()

    for signal_name in ("SIGINT", "SIGTERM"):
        signal_value = getattr(signal, signal_name, None)
        if signal_value is not None:
            signal.signal(signal_value, request_shutdown)

    host, port = server.server_address[:2]
    print(f"{APP_NAME} listening on http://{host}:{port}")
    if args.access_token:
        print("Personal access-token protection is enabled.")
    if adapter is None:
        print(
            f"WARNING: quant engine unavailable: {engine_error}",
            file=sys.stderr,
        )
    elif args.refresh_on_start != "none":
        start_background_refresh(
            server, adapter, mode=args.refresh_on_start, limit=None
        )
        print(f"Background {args.refresh_on_start} refresh started.")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        request_shutdown(signal.SIGINT, None)
    finally:
        server.server_close()
    print("Server stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
