"""Portfolio search and valuation service for the local HTTP application.

The service deliberately keeps provider-specific code at the edge.  Yahoo
Finance and Naver are convenience providers, while the KOSPI catalog is read
from the FinanceDataReader KRX cache mirror.  None of those sources are
treated as an official market API in the response metadata.
"""

from __future__ import annotations

import csv
import datetime as dt
import html
import json
import math
import os
from collections import OrderedDict
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
import re
import threading
import time
from typing import Any, Iterable, Mapping, Sequence

import requests


MAX_HOLDINGS = 50
MAX_SEARCH_LIMIT = 20
DEFAULT_SEARCH_LIMIT = 10
MAX_QUERY_LENGTH = 80
MAX_NAME_LENGTH = 200
MAX_CASH_KRW = 10_000_000_000_000
DEFAULT_TIMEOUT_SECONDS = 8.0

YAHOO_SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart"
KRX_MAX_WORK_DT_URL = (
    "https://data.krx.co.kr/comm/bldAttendant/"
    "executeForResourceBundle.cmd?baseName=krx.mdc.i18n.component&key=B128.bld"
)
FDR_KRX_LISTING_URL = (
    "https://raw.githubusercontent.com/FinanceData/fdr_krx_data_cache/"
    "refs/heads/master/data/listing/krx/{date}.csv"
)
NAVER_GOLD_URL = "https://m.stock.naver.com/marketindex/metals/M04020000"

GOLD_SYMBOL = "M04020000"
GOLD_NAME = "KRX 금 99.99_1kg"
GOLD_SOURCE_NOTE = (
    "Naver 모바일 market-index 편의 시세로 KRX 국내 금을 나타냅니다. "
    "공식 KRX API가 아닙니다."
)
KOSPI_SOURCE = (
    "FinanceDataReader fdr_krx_data_cache GitHub mirror "
    "(third-party mirror of KRX listing data)"
)
YAHOO_SOURCE = "Yahoo Finance convenience API (unofficial)"

GROUPS: dict[str, dict[str, str]] = {
    "us": {"label": "미국 포트", "color": "#3D73D9"},
    "kospi": {"label": "한국 포트", "color": "#D95567"},
    "gold": {"label": "금", "color": "#D99B18"},
    "cash": {"label": "현금", "color": "#7D8995"},
}

SEARCH_MARKETS = frozenset({"us", "kospi", "gold"})

_US_SYMBOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,14}$")
_KOSPI_SYMBOL_RE = re.compile(r"^\d{6}$")
_DATE_RE = re.compile(r"^\d{8}$")
_CACHE_FILE_RE = re.compile(r"^krx-(\d{4}-\d{2}-\d{2})\.csv$")
_NEXT_DATA_RE = re.compile(
    r"<script\b[^>]*\bid=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>",
    flags=re.IGNORECASE | re.DOTALL,
)


class PortfolioError(Exception):
    """Base error carrying a stable API-facing code and status."""

    def __init__(
        self,
        code: str,
        message: str,
        status: HTTPStatus = HTTPStatus.BAD_GATEWAY,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class PortfolioValidationError(PortfolioError):
    """A malformed portfolio request."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, HTTPStatus.BAD_REQUEST)


class PortfolioProviderError(PortfolioError):
    """A provider or quote failure which is safe to expose to API clients."""

    def __init__(
        self,
        code: str = "portfolio_provider_error",
        message: str = "Portfolio data provider is unavailable",
        status: HTTPStatus = HTTPStatus.BAD_GATEWAY,
    ) -> None:
        super().__init__(code, message, status)


@dataclass(frozen=True)
class SourceMeta:
    """Provenance attached to a quote or provider response."""

    provider: str
    source: str
    url: str | None = None
    official: bool = False
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "provider": self.provider,
            "source": self.source,
            "official": self.official,
        }
        if self.url:
            result["url"] = self.url
        if self.note:
            result["note"] = self.note
        return result


@dataclass(frozen=True)
class Quote:
    """A normalized local-currency quote."""

    price: float
    currency: str
    as_of: str | None
    source: SourceMeta


@dataclass(frozen=True)
class CatalogSnapshot:
    """Current or cached KRX catalog plus its provenance."""

    rows: tuple[Mapping[str, Any], ...]
    source: str = KOSPI_SOURCE
    warnings: tuple[str, ...] = ()


class TTLCache:
    """Small lock-protected bounded TTL cache used by request threads."""

    def __init__(self, *, max_entries: int, ttl_seconds: float) -> None:
        self.max_entries = max(1, int(max_entries))
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self._lock = threading.Lock()
        self._items: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    def get(self, key: str) -> Any | None:
        now = time.monotonic()
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            expires_at, value = item
            if expires_at <= now:
                self._items.pop(key, None)
                return None
            self._items.move_to_end(key)
            return value

    def put(self, key: str, value: Any) -> None:
        expires_at = time.monotonic() + self.ttl_seconds
        with self._lock:
            self._items[key] = (expires_at, value)
            self._items.move_to_end(key)
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


def _now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_from_epoch(value: Any) -> str | None:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(timestamp) or timestamp <= 0:
        return None
    try:
        return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
    except (OverflowError, OSError, ValueError):
        return None


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _finite_positive(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError, OverflowError):
        return False


def _parse_price(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = _clean_text(value).replace(",", "")
        if not text or text in {"-", "--", "N/A", "null", "None"}:
            return None
        match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
        if not match:
            return None
        try:
            number = float(match.group(0))
        except ValueError:
            return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _response_text(response: Any) -> str:
    if hasattr(response, "raise_for_status"):
        response.raise_for_status()
    else:
        status_code = getattr(response, "status_code", 200)
        if status_code >= 400:
            raise requests.HTTPError(f"HTTP {status_code}")
    text = getattr(response, "text", None)
    if text is not None:
        return str(text)
    content = getattr(response, "content", b"")
    if isinstance(content, bytes):
        return content.decode("utf-8-sig")
    return str(content)


def _response_json(response: Any) -> Any:
    if hasattr(response, "raise_for_status"):
        response.raise_for_status()
    else:
        status_code = getattr(response, "status_code", 200)
        if status_code >= 400:
            raise requests.HTTPError(f"HTTP {status_code}")
    json_method = getattr(response, "json", None)
    if callable(json_method):
        return json_method()
    return json.loads(_response_text(response))


def _request_get(
    session: Any,
    url: str,
    *,
    timeout: float,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> Any:
    request_headers = {
        "User-Agent": "sp500-quant-signal/1.0 (portfolio convenience data)",
    }
    if headers:
        request_headers.update(headers)
    try:
        return session.get(url, params=params, headers=request_headers, timeout=timeout)
    except TypeError:
        # Tiny test doubles often accept only (url, params=...).
        try:
            return session.get(url, params=params, timeout=timeout)
        except TypeError:
            return session.get(url, params=params)


def _meta_from_value(
    value: Any,
    *,
    default_provider: str,
    default_source: str,
    default_url: str | None = None,
    default_note: str | None = None,
) -> SourceMeta:
    if isinstance(value, SourceMeta):
        return value
    if isinstance(value, Mapping):
        return SourceMeta(
            provider=_clean_text(value.get("provider")) or default_provider,
            source=_clean_text(value.get("source")) or default_source,
            url=_clean_text(value.get("url")) or default_url,
            official=bool(value.get("official", False)),
            note=_clean_text(value.get("note")) or default_note,
        )
    if isinstance(value, str) and value.strip():
        return SourceMeta(
            provider=default_provider,
            source=value.strip(),
            url=default_url,
            note=default_note,
        )
    return SourceMeta(
        provider=default_provider,
        source=default_source,
        url=default_url,
        note=default_note,
    )


def normalize_quote(
    value: Any,
    *,
    default_currency: str,
    default_source: SourceMeta,
) -> Quote:
    """Normalize a real quote or a small fake-provider mapping for tests."""

    if isinstance(value, Quote):
        quote = Quote(
            value.price,
            value.currency,
            value.as_of,
            _meta_from_value(
                value.source,
                default_provider=default_source.provider,
                default_source=default_source.source,
                default_url=default_source.url,
                default_note=default_source.note,
            ),
        )
    else:
        if isinstance(value, Mapping):
            price_value = value.get(
                "price",
                value.get(
                    "regularMarketPrice",
                    value.get("close", value.get("value")),
                ),
            )
            currency = _clean_text(value.get("currency")) or default_currency
            as_of = _clean_text(value.get("as_of")) or _clean_text(
                value.get("asOf")
            )
            if not as_of:
                as_of = _iso_from_epoch(
                    value.get("regularMarketTime", value.get("timestamp"))
                )
            source = _meta_from_value(
                value.get("source"),
                default_provider=default_source.provider,
                default_source=default_source.source,
                default_url=default_source.url,
                default_note=default_source.note,
            )
        else:
            price_value = value
            currency = default_currency
            as_of = None
            source = default_source
        price = _parse_price(price_value)
        if price is None:
            raise PortfolioProviderError(
                "quote_invalid", "Quote provider returned no positive price"
            )
        quote = Quote(price, currency.upper(), as_of or None, source)

    if not _finite_positive(quote.price):
        raise PortfolioProviderError(
            "quote_invalid", "Quote provider returned no positive price"
        )
    currency = _clean_text(quote.currency).upper() or default_currency
    return Quote(float(quote.price), currency, quote.as_of, quote.source)


class YahooFinanceProvider:
    """Yahoo search/chart convenience provider backed by ``requests``."""

    source_meta = SourceMeta(
        provider="Yahoo Finance",
        source=YAHOO_SOURCE,
        official=False,
        note="Yahoo endpoints are an unofficial convenience source, not an exchange API.",
    )

    _US_EXCHANGES = {
        "NMS",
        "NAS",
        "NGM",
        "NCM",
        "NYQ",
        "NYS",
        "ASE",
        "PCX",
        "BTT",
        "CXI",
        "CBO",
        "OPR",
    }

    def __init__(
        self,
        *,
        session: Any | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = float(timeout)

    @classmethod
    def is_us_listed(cls, item: Mapping[str, Any]) -> bool:
        quote_type = _clean_text(item.get("quoteType")).upper()
        if quote_type not in {"EQUITY", "ETF"}:
            return False
        symbol = _clean_text(item.get("symbol"))
        if not _US_SYMBOL_RE.fullmatch(symbol) or "." in symbol and symbol.endswith(
            (".KS", ".KQ", ".T", ".HK", ".L")
        ):
            return False
        exchange = _clean_text(item.get("exchange")).upper()
        region = _clean_text(item.get("region")).upper()
        market = _clean_text(item.get("market")).upper()
        display = _clean_text(item.get("exchangeDisplayName")).lower()
        if exchange in cls._US_EXCHANGES:
            return True
        if region == "US" or market in {"US", "USA"}:
            return True
        return any(
            phrase in display
            for phrase in (
                "nasdaq",
                "new york",
                "nyse",
                "nysearca",
                "amex",
                "cboe",
            )
        )

    def search(self, query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> list[dict[str, Any]]:
        try:
            response = _request_get(
                self.session,
                YAHOO_SEARCH_URL,
                timeout=self.timeout,
                params={"q": query, "quotesCount": max(limit * 3, 20), "newsCount": 0},
            )
            payload = _response_json(response)
        except Exception as exc:
            raise PortfolioProviderError(
                "portfolio_provider_error", "Yahoo search provider is unavailable"
            ) from exc
        if not isinstance(payload, Mapping):
            raise PortfolioProviderError(
                "portfolio_provider_error", "Yahoo search returned an invalid response"
            )
        quotes = payload.get("quotes", [])
        if not isinstance(quotes, list):
            raise PortfolioProviderError(
                "portfolio_provider_error", "Yahoo search returned an invalid response"
            )
        results: list[dict[str, Any]] = []
        for item in quotes:
            if not isinstance(item, Mapping) or not self.is_us_listed(item):
                continue
            symbol = _clean_text(item.get("symbol")).upper()
            exchange = _clean_text(
                item.get("exchangeDisplayName") or item.get("exchange")
            ) or "US"
            quote_type = _clean_text(item.get("quoteType")).upper()
            results.append(
                {
                    "id": f"us:{symbol}",
                    "market": "us",
                    "symbol": symbol,
                    "name": _clean_text(
                        item.get("longname") or item.get("shortname") or symbol
                    ),
                    "exchange": exchange,
                    "asset_type": "ETF" if quote_type == "ETF" else "EQUITY",
                    "currency": _clean_text(item.get("currency")).upper() or "USD",
                    "quantity_unit": "shares",
                }
            )
            if len(results) >= limit:
                break
        return results

    def quote(self, symbol: str) -> Quote:
        chart_url = f"{YAHOO_CHART_URL}/{symbol}"
        try:
            response = _request_get(
                self.session,
                chart_url,
                timeout=self.timeout,
                params={"range": "5d", "interval": "1d", "includePrePost": "false"},
            )
            payload = _response_json(response)
        except Exception as exc:
            raise PortfolioProviderError(
                "portfolio_provider_error", "Yahoo quote provider is unavailable"
            ) from exc
        try:
            result = payload["chart"]["result"][0]
            if not isinstance(result, Mapping):
                raise ValueError("invalid chart result")
            meta = result.get("meta", {})
            if not isinstance(meta, Mapping):
                meta = {}
            price = _parse_price(meta.get("regularMarketPrice"))
            as_of = _iso_from_epoch(meta.get("regularMarketTime"))
            if price is None:
                timestamps = result.get("timestamp") or []
                quote_rows = (result.get("indicators", {}).get("quote") or [{}])[0]
                closes = quote_rows.get("close") or []
                for index in range(len(closes) - 1, -1, -1):
                    candidate = _parse_price(closes[index])
                    if candidate is not None:
                        price = candidate
                        if index < len(timestamps):
                            as_of = _iso_from_epoch(timestamps[index])
                        break
            if price is None:
                raise ValueError("missing price")
        except (AttributeError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise PortfolioProviderError(
                "quote_invalid", "Yahoo quote returned no usable price"
            ) from exc
        source = SourceMeta(
            provider=self.source_meta.provider,
            source=self.source_meta.source,
            url=chart_url,
            official=False,
            note=self.source_meta.note,
        )
        return Quote(
            price=price,
            currency=_clean_text(meta.get("currency")).upper()
            or ("KRW" if symbol.upper().endswith(".KS") else "USD"),
            as_of=as_of,
            source=source,
        )

    def get_quote(self, symbol: str) -> Quote:
        return self.quote(symbol)

    def get_usdkrw(self) -> Quote:
        quote = self.quote("KRW=X")
        return Quote(
            quote.price,
            "KRW",
            quote.as_of,
            quote.source,
        )


def _is_kospi_row(row: Mapping[str, Any]) -> bool:
    """Return true only for current KOSPI listing rows in the FDR mirror."""

    market_id = _clean_text(
        row.get("MarketId", row.get("market_id", row.get("MKT_ID")))
    ).upper()
    market = _clean_text(
        row.get("Market", row.get("market", row.get("MarketName")))
    ).upper()
    if market_id and market_id not in {"STK"}:
        return False
    if market and market not in {"KOSPI", "KOSPI MARKET"}:
        return False
    return market_id == "STK" or market == "KOSPI"


def _catalog_symbol(row: Mapping[str, Any]) -> str:
    value = row.get("Code", row.get("code", row.get("Symbol", row.get("symbol"))))
    text = _clean_text(value)
    if text.isdigit():
        return text.zfill(6)
    return text


def _catalog_name(row: Mapping[str, Any]) -> str:
    return _clean_text(row.get("Name", row.get("name", row.get("ISU_ABBRV"))))


def filter_kospi_rows(
    rows: Iterable[Mapping[str, Any]], query: str, limit: int
) -> list[dict[str, Any]]:
    """Filter current STK/KOSPI rows by Korean name or six-digit code."""

    needle = query.casefold().strip()
    results: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping) or not _is_kospi_row(row):
            continue
        symbol = _catalog_symbol(row)
        name = _catalog_name(row)
        if not _KOSPI_SYMBOL_RE.fullmatch(symbol) or not name:
            continue
        if needle and needle not in name.casefold() and needle not in symbol:
            continue
        results.append(
            {
                "id": f"kospi:{symbol}",
                "market": "kospi",
                "symbol": symbol,
                "name": name,
                "exchange": "KRX",
                "asset_type": "EQUITY",
                "currency": "KRW",
                "quantity_unit": "shares",
            }
        )
        if len(results) >= limit:
            break
    return results


class KrxCatalogProvider:
    """Load current KOSPI rows from the FinanceDataReader cache mirror."""

    def __init__(
        self,
        *,
        session: Any | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        cache_dir: Path | None = None,
        max_disk_entries: int = 3,
        stale_days: int = 7,
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = float(timeout)
        default_dir = Path.home() / ".cache" / "sp500-quant-signal" / "portfolio"
        self.cache_dir = Path(cache_dir) if cache_dir is not None else default_dir
        self.max_disk_entries = max(1, int(max_disk_entries))
        self.stale_days = max(0, int(stale_days))
        self._lock = threading.Lock()

    def _max_work_date(self) -> str:
        try:
            response = _request_get(
                self.session,
                KRX_MAX_WORK_DT_URL,
                timeout=self.timeout,
                headers={"Referer": "https://data.krx.co.kr/"},
            )
            payload = _response_json(response)
            value = payload["result"]["output"][0]["max_work_dt"]
            value = _clean_text(value)
            if not _DATE_RE.fullmatch(value):
                raise ValueError("invalid max_work_dt")
            parsed = dt.datetime.strptime(value, "%Y%m%d")
            return parsed.strftime("%Y-%m-%d")
        except Exception as exc:
            raise PortfolioProviderError(
                "portfolio_provider_error", "KRX catalog date provider is unavailable"
            ) from exc

    def _disk_path(self, date_value: str) -> Path:
        return self.cache_dir / f"krx-{date_value}.csv"

    def _read_csv(self, path: Path) -> tuple[Mapping[str, Any], ...]:
        try:
            text = path.read_text(encoding="utf-8-sig")
            reader = csv.DictReader(text.splitlines())
            rows = tuple(dict(row) for row in reader if row)
        except (OSError, UnicodeError, csv.Error) as exc:
            raise PortfolioProviderError(
                "portfolio_provider_error", "KRX catalog cache is unreadable"
            ) from exc
        if not rows:
            raise PortfolioProviderError(
                "portfolio_provider_error", "KRX catalog returned no rows"
            )
        return rows

    def _download(self, date_value: str) -> tuple[Mapping[str, Any], ...]:
        url = FDR_KRX_LISTING_URL.format(date=date_value)
        try:
            response = _request_get(
                self.session,
                url,
                timeout=self.timeout,
                headers={"Accept": "text/csv"},
            )
            text = _response_text(response)
            reader = csv.DictReader(text.splitlines())
            rows = tuple(dict(row) for row in reader if row)
        except Exception as exc:
            raise PortfolioProviderError(
                "portfolio_provider_error", "KRX catalog mirror is unavailable"
            ) from exc
        if not rows:
            raise PortfolioProviderError(
                "portfolio_provider_error", "KRX catalog mirror returned no rows"
            )
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path = self._disk_path(date_value)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, path)
            self._evict_disk_cache()
        except OSError:
            # A read-only home directory must not make a live catalog unusable.
            pass
        return rows

    def _evict_disk_cache(self) -> None:
        paths = sorted(
            (
                path
                for path in self.cache_dir.glob("krx-*.csv")
                if _CACHE_FILE_RE.fullmatch(path.name)
            ),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for path in paths[self.max_disk_entries :]:
            try:
                path.unlink()
            except OSError:
                pass

    def _newest_disk_cache(self) -> tuple[str, tuple[Mapping[str, Any], ...]] | None:
        try:
            paths = sorted(
                (
                    path
                    for path in self.cache_dir.glob("krx-*.csv")
                    if _CACHE_FILE_RE.fullmatch(path.name)
                ),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return None
        cutoff = dt.datetime.now().date() - dt.timedelta(days=self.stale_days)
        for path in paths:
            match = _CACHE_FILE_RE.fullmatch(path.name)
            if not match:
                continue
            try:
                cache_date = dt.date.fromisoformat(match.group(1))
                if cache_date < cutoff:
                    continue
                return match.group(1), self._read_csv(path)
            except (ValueError, PortfolioProviderError):
                continue
        return None

    def get_catalog(self) -> CatalogSnapshot:
        with self._lock:
            try:
                date_value = self._max_work_date()
                path = self._disk_path(date_value)
                if path.is_file():
                    rows = self._read_csv(path)
                    self._evict_disk_cache()
                else:
                    rows = self._download(date_value)
                return CatalogSnapshot(rows, KOSPI_SOURCE)
            except PortfolioProviderError as primary:
                stale = self._newest_disk_cache()
                if stale is None:
                    raise primary
                stale_date, rows = stale
                warning = (
                    f"KRX max_work_dt 조회 실패로 {stale_date}의 로컬 미러 캐시를 사용했습니다."
                )
                return CatalogSnapshot(rows, KOSPI_SOURCE, (warning,))

    def load(self) -> CatalogSnapshot:
        return self.get_catalog()


def _walk_matching_records(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        reuters = _clean_text(value.get("reutersCode"))
        symbol = _clean_text(value.get("symbolCode"))
        if reuters == GOLD_SYMBOL or symbol == GOLD_SYMBOL:
            yield value
        for child in value.values():
            yield from _walk_matching_records(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_matching_records(child)


def _next_data_payload(document: str | Mapping[str, Any]) -> Any:
    if isinstance(document, Mapping):
        return document
    match = _NEXT_DATA_RE.search(document)
    if not match:
        raise PortfolioProviderError(
            "portfolio_provider_error", "Naver page has no __NEXT_DATA__ payload"
        )
    try:
        return json.loads(html.unescape(match.group(1)))
    except (json.JSONDecodeError, TypeError) as exc:
        raise PortfolioProviderError(
            "portfolio_provider_error", "Naver __NEXT_DATA__ payload is invalid"
        ) from exc


def extract_naver_gold_quote(document: str | Mapping[str, Any]) -> Quote:
    """Extract the M04020000 record from Naver's nested ``__NEXT_DATA__``."""

    payload = _next_data_payload(document)
    record: Mapping[str, Any] | None = None
    price: float | None = None
    for candidate in _walk_matching_records(payload):
        for key in (
            "closePrice",
            "currentPrice",
            "nowPrice",
            "tradePrice",
            "price",
            "value",
        ):
            price = _parse_price(candidate.get(key))
            if price is not None:
                record = candidate
                break
        if record is not None:
            break
    if record is None or price is None:
        raise PortfolioProviderError(
            "portfolio_provider_error", "Naver domestic-gold record has no price"
        )
    as_of = _clean_text(
        record.get("localTradedAt")
        or record.get("tradedAt")
        or record.get("asOf")
    ) or None
    source = SourceMeta(
        provider="Naver",
        source="Naver mobile market-index __NEXT_DATA__",
        url=NAVER_GOLD_URL,
        official=False,
        note=GOLD_SOURCE_NOTE,
    )
    return Quote(price, "KRW", as_of, source)


class NaverGoldProvider:
    """Isolated Naver convenience provider for future official KRX integration."""

    def __init__(
        self,
        *,
        session: Any | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = float(timeout)

    def quote(self) -> Quote:
        try:
            response = _request_get(
                self.session,
                NAVER_GOLD_URL,
                timeout=self.timeout,
                headers={"Accept-Language": "ko-KR,ko;q=0.9"},
            )
            return extract_naver_gold_quote(_response_text(response))
        except PortfolioError:
            raise
        except Exception as exc:
            raise PortfolioProviderError(
                "portfolio_provider_error", "Naver domestic-gold provider is unavailable"
            ) from exc

    def get_quote(self) -> Quote:
        return self.quote()


def _validate_market(value: Any) -> str:
    market = _clean_text(value).lower()
    if market not in SEARCH_MARKETS:
        raise PortfolioValidationError(
            "invalid_market", "market must be one of us, kospi, or gold"
        )
    return market


def _provider_call(provider: Any, names: Sequence[str], *args: Any, **kwargs: Any) -> Any:
    for name in names:
        candidate = getattr(provider, name, None)
        if not callable(candidate):
            continue
        try:
            return candidate(*args, **kwargs)
        except TypeError as original:
            # Support simple fake providers and zero-argument gold/FX methods.
            if kwargs:
                try:
                    return candidate(*args)
                except TypeError:
                    pass
            if args:
                try:
                    return candidate()
                except TypeError:
                    pass
            raise original
    if callable(provider):
        try:
            return provider(*args, **kwargs)
        except TypeError as original:
            if kwargs:
                try:
                    return provider(*args)
                except TypeError:
                    pass
            if args:
                try:
                    return provider()
                except TypeError:
                    pass
            raise original
    names_text = ", ".join(names)
    raise AttributeError(f"Provider has no callable method ({names_text})")


def _unpack_search_result(value: Any) -> tuple[list[Mapping[str, Any]], str | None, list[str]]:
    if isinstance(value, Mapping):
        raw_results = value.get("results", [])
        source = _clean_text(value.get("source")) or None
        warnings = value.get("warnings", [])
    else:
        raw_results = value
        source = None
        warnings = []
    if not isinstance(raw_results, list):
        raise PortfolioProviderError(
            "portfolio_provider_error", "Search provider returned an invalid response"
        )
    warning_list = [str(item) for item in warnings] if isinstance(warnings, list) else []
    results = [item for item in raw_results if isinstance(item, Mapping)]
    return results, source, warning_list


def _result_with_defaults(market: str, item: Mapping[str, Any]) -> dict[str, Any]:
    symbol = _clean_text(item.get("symbol") or item.get("ticker")).upper()
    name = _clean_text(item.get("name") or item.get("shortname") or symbol)
    quote_type = _clean_text(item.get("asset_type") or item.get("quoteType")).upper()
    return {
        "id": _clean_text(item.get("id")) or f"{market}:{symbol}",
        "market": market,
        "symbol": symbol,
        "name": name,
        "exchange": _clean_text(item.get("exchange")) or ("KRX" if market == "kospi" else "US"),
        "asset_type": quote_type or ("GOLD" if market == "gold" else "EQUITY"),
        "currency": _clean_text(item.get("currency")).upper()
        or ("KRW" if market != "us" else "USD"),
        "quantity_unit": _clean_text(item.get("quantity_unit"))
        or ("g" if market == "gold" else "shares"),
    }


def _gold_result() -> dict[str, Any]:
    return {
        "id": f"gold:{GOLD_SYMBOL}",
        "market": "gold",
        "symbol": GOLD_SYMBOL,
        "name": GOLD_NAME,
        "exchange": "KRX 금시장",
        "asset_type": "GOLD",
        "currency": "KRW",
        "quantity_unit": "g",
    }


class PortfolioService:
    """Thread-safe search and valuation facade used by ``server.py``."""

    def __init__(
        self,
        *,
        search_provider: Any | None = None,
        catalog_provider: Any | None = None,
        quote_provider: Any | None = None,
        fx_provider: Any | None = None,
        gold_provider: Any | None = None,
        yahoo_provider: Any | None = None,
        cache_dir: Path | None = None,
        search_ttl_seconds: float = 60.0,
        catalog_ttl_seconds: float = 15 * 60.0,
        quote_ttl_seconds: float = 30.0,
        max_cache_entries: int = 256,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        yahoo = yahoo_provider or YahooFinanceProvider(timeout=timeout)
        self.search_provider = search_provider or yahoo
        self.catalog_provider = catalog_provider or KrxCatalogProvider(
            timeout=timeout, cache_dir=cache_dir
        )
        self.quote_provider = quote_provider or yahoo
        self.fx_provider = fx_provider or quote_provider or yahoo
        self.gold_provider = gold_provider or (
            quote_provider
            or yahoo_provider
            or NaverGoldProvider(timeout=timeout)
        )
        self._search_cache = TTLCache(
            max_entries=max_cache_entries, ttl_seconds=search_ttl_seconds
        )
        self._catalog_cache = TTLCache(max_entries=2, ttl_seconds=catalog_ttl_seconds)
        self._quote_cache = TTLCache(
            max_entries=max_cache_entries, ttl_seconds=quote_ttl_seconds
        )

    def search(
        self, market: Any, query: Any = "", limit: Any = DEFAULT_SEARCH_LIMIT
    ) -> dict[str, Any]:
        selected_market = _validate_market(market)
        normalized_query = _clean_text(query)
        if len(normalized_query) > MAX_QUERY_LENGTH:
            raise PortfolioValidationError(
                "invalid_query", f"q may not exceed {MAX_QUERY_LENGTH} characters"
            )
        if selected_market != "gold" and not normalized_query:
            raise PortfolioValidationError(
                "invalid_query", "q is required for US and KOSPI searches"
            )
        selected_limit = self._validate_limit(limit)
        cache_key = f"{selected_market}:{normalized_query.casefold()}:{selected_limit}"
        cached = self._search_cache.get(cache_key)
        if cached is not None:
            return cached

        warnings: list[str] = []
        if selected_market == "gold":
            results = []
            if not normalized_query or any(
                normalized_query.casefold() in value.casefold()
                for value in (GOLD_SYMBOL, GOLD_NAME, "금", "gold")
            ):
                results = [_gold_result()]
            payload = {
                "query": normalized_query,
                "market": selected_market,
                "results": results[:selected_limit],
                "source": "Canonical application instrument for KRX 금시장 (M04020000)",
                "warnings": [GOLD_SOURCE_NOTE],
            }
            self._search_cache.put(cache_key, payload)
            return payload

        if selected_market == "us":
            try:
                raw = _provider_call(
                    self.search_provider,
                    ("search", "search_us"),
                    normalized_query,
                    selected_limit,
                )
                items, provider_source, provider_warnings = _unpack_search_result(raw)
            except PortfolioError:
                raise
            except Exception as exc:
                raise PortfolioProviderError(
                    "portfolio_provider_error", "US search provider is unavailable"
                ) from exc
            results = []
            for item in items:
                # Re-apply the eligibility filter when a fake/custom provider is used.
                if not YahooFinanceProvider.is_us_listed(item) and not (
                    _clean_text(item.get("market")).lower() == "us"
                    and _clean_text(item.get("asset_type")).upper() in {"EQUITY", "ETF"}
                ):
                    continue
                results.append(_result_with_defaults("us", item))
                if len(results) >= selected_limit:
                    break
            source = provider_source or YAHOO_SOURCE
        else:
            snapshot_value = self._catalog_cache.get("catalog")
            if snapshot_value is None:
                try:
                    snapshot_value = _provider_call(
                        self.catalog_provider, ("get_catalog", "load", "catalog")
                    )
                    self._catalog_cache.put("catalog", snapshot_value)
                except PortfolioError:
                    raise
                except Exception as exc:
                    raise PortfolioProviderError(
                        "portfolio_provider_error", "KOSPI catalog provider is unavailable"
                    ) from exc
            if isinstance(snapshot_value, CatalogSnapshot):
                rows = snapshot_value.rows
                source = snapshot_value.source
                warnings.extend(snapshot_value.warnings)
            elif isinstance(snapshot_value, Mapping):
                rows = snapshot_value.get("rows", [])
                source = _clean_text(snapshot_value.get("source")) or KOSPI_SOURCE
                raw_warnings = snapshot_value.get("warnings", [])
                if isinstance(raw_warnings, list):
                    warnings.extend(str(item) for item in raw_warnings)
            else:
                rows = snapshot_value
                source = KOSPI_SOURCE
            if not isinstance(rows, Iterable):
                raise PortfolioProviderError(
                    "portfolio_provider_error", "KOSPI catalog provider returned invalid rows"
                )
            results = filter_kospi_rows(rows, normalized_query, selected_limit)

        warnings.extend(provider_warnings if selected_market == "us" else [])
        payload = {
            "query": normalized_query,
            "market": selected_market,
            "results": results,
            "source": source,
            "warnings": warnings,
        }
        self._search_cache.put(cache_key, payload)
        return payload

    @staticmethod
    def _validate_limit(value: Any) -> int:
        if isinstance(value, bool):
            raise PortfolioValidationError("invalid_limit", "limit must be an integer")
        try:
            result = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise PortfolioValidationError(
                "invalid_limit", "limit must be an integer between 1 and 20"
            ) from exc
        if str(value).strip() != str(result) and not isinstance(value, int):
            raise PortfolioValidationError(
                "invalid_limit", "limit must be an integer between 1 and 20"
            )
        if not 1 <= result <= MAX_SEARCH_LIMIT:
            raise PortfolioValidationError(
                "invalid_limit", "limit must be between 1 and 20"
            )
        return result

    @staticmethod
    def validate_holdings(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(payload, Mapping):
            raise PortfolioValidationError("invalid_holdings", "holdings must be an array")
        unknown_payload = sorted(set(payload) - {"holdings", "cash_krw"})
        if unknown_payload:
            raise PortfolioValidationError(
                "unknown_fields", f"Unknown JSON fields: {', '.join(unknown_payload)}"
            )
        holdings = payload.get("holdings", [])
        if not isinstance(holdings, list):
            raise PortfolioValidationError(
                "invalid_holdings", "holdings must be an array"
            )
        if len(holdings) > MAX_HOLDINGS:
            raise PortfolioValidationError(
                "too_many_holdings", f"holdings may not exceed {MAX_HOLDINGS} items"
            )
        normalized: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for index, item in enumerate(holdings):
            if not isinstance(item, Mapping):
                raise PortfolioValidationError(
                    "invalid_holding", f"holdings[{index}] must be an object"
                )
            unknown = sorted(set(item) - {"market", "symbol", "name", "quantity"})
            if unknown:
                raise PortfolioValidationError(
                    "unknown_fields",
                    f"Unknown holding fields at index {index}: {', '.join(unknown)}",
                )
            if not isinstance(item.get("symbol"), str):
                raise PortfolioValidationError(
                    "invalid_symbol", f"holdings[{index}].symbol must be text"
                )
            if not isinstance(item.get("name"), str):
                raise PortfolioValidationError(
                    "invalid_name", f"holdings[{index}].name must be text"
                )
            market = _validate_market(item.get("market"))
            symbol = item["symbol"].strip().upper()
            name = item["name"].strip()
            quantity = item.get("quantity")
            if not name or len(name) > MAX_NAME_LENGTH or any(
                ord(char) < 32 for char in name
            ):
                raise PortfolioValidationError(
                    "invalid_name", f"holdings[{index}].name is invalid"
                )
            if market == "us":
                if not _US_SYMBOL_RE.fullmatch(symbol):
                    raise PortfolioValidationError(
                        "invalid_symbol", f"holdings[{index}].symbol is not a valid US symbol"
                    )
                unit = "shares"
                if not _finite_positive(quantity):
                    raise PortfolioValidationError(
                        "invalid_quantity", f"holdings[{index}].quantity must be positive and finite"
                    )
                quantity_value: int | float = float(quantity)
            elif market == "kospi":
                if not _KOSPI_SYMBOL_RE.fullmatch(symbol):
                    raise PortfolioValidationError(
                        "invalid_symbol", f"holdings[{index}].symbol must be a six-digit KOSPI code"
                    )
                if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
                    raise PortfolioValidationError(
                        "invalid_quantity",
                        f"holdings[{index}].quantity must be a positive integer share count",
                    )
                unit = "shares"
                quantity_value = quantity
            else:
                if symbol != GOLD_SYMBOL:
                    raise PortfolioValidationError(
                        "invalid_symbol", f"holdings[{index}].symbol must be {GOLD_SYMBOL}"
                    )
                if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
                    raise PortfolioValidationError(
                        "invalid_quantity",
                        f"holdings[{index}].quantity must be a positive integer gram count",
                    )
                unit = "g"
                quantity_value = quantity
            identity = (market, symbol)
            if identity in seen:
                raise PortfolioValidationError(
                    "duplicate_holding",
                    f"Duplicate holding for {market}:{symbol}; combine quantities first",
                )
            seen.add(identity)
            normalized.append(
                {
                    "market": market,
                    "symbol": symbol,
                    "name": name,
                    "quantity": quantity_value,
                    "unit": unit,
                }
            )
        return normalized

    @staticmethod
    def validate_cash_krw(value: Any) -> int:
        if value is None:
            return 0
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PortfolioValidationError(
                "invalid_cash", "cash_krw must be a non-negative whole KRW amount"
            )
        numeric = float(value)
        if (
            not math.isfinite(numeric)
            or numeric < 0
            or numeric > MAX_CASH_KRW
            or not numeric.is_integer()
        ):
            raise PortfolioValidationError(
                "invalid_cash",
                f"cash_krw must be a whole KRW amount between 0 and {MAX_CASH_KRW}",
            )
        return int(numeric)

    def _cached_quote(self, cache_key: str, provider: Any, symbol: str, *, currency: str) -> Quote:
        cached = self._quote_cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            raw = _provider_call(provider, ("get_quote", "quote"), symbol)
            quote = normalize_quote(
                raw,
                default_currency=currency,
                default_source=SourceMeta(
                    provider="Portfolio quote provider", source="Configured quote provider"
                ),
            )
        except PortfolioError:
            raise
        except Exception as exc:
            raise PortfolioProviderError(
                "portfolio_provider_error", "Portfolio quote provider is unavailable"
            ) from exc
        self._quote_cache.put(cache_key, quote)
        return quote

    def _get_local_quote(self, holding: Mapping[str, Any]) -> Quote:
        market = str(holding["market"])
        symbol = str(holding["symbol"])
        if market == "us":
            provider_symbol, currency = symbol, "USD"
        elif market == "kospi":
            provider_symbol, currency = f"{symbol}.KS", "KRW"
        else:
            provider_symbol, currency = GOLD_SYMBOL, "KRW"
        provider = self.gold_provider if market == "gold" else self.quote_provider
        return self._cached_quote(f"{market}:{provider_symbol}", provider, provider_symbol, currency=currency)

    def _get_fx_quote(self) -> Quote:
        cached = self._quote_cache.get("fx:USD/KRW")
        if cached is not None:
            return cached
        try:
            fx_methods = ("get_usdkrw", "usdkrw")
            if any(callable(getattr(self.fx_provider, name, None)) for name in fx_methods):
                raw = _provider_call(self.fx_provider, fx_methods, "KRW=X")
            else:
                raw = _provider_call(self.fx_provider, ("get_quote", "quote"), "KRW=X")
            quote = normalize_quote(
                raw,
                default_currency="KRW",
                default_source=SourceMeta(
                    provider="Yahoo Finance",
                    source=YAHOO_SOURCE,
                    note="USD/KRW conversion via Yahoo KRW=X convenience quote.",
                ),
            )
        except PortfolioError:
            raise
        except Exception as exc:
            raise PortfolioProviderError(
                "portfolio_provider_error", "USD/KRW provider is unavailable"
            ) from exc
        self._quote_cache.put("fx:USD/KRW", quote)
        return quote

    @staticmethod
    def _holding_error(
        holding: Mapping[str, Any], error: PortfolioError | Exception
    ) -> dict[str, Any]:
        if isinstance(error, PortfolioError):
            code, message = error.code, error.message
        else:
            code, message = "quote_unavailable", "No usable quote was returned"
        return {
            "id": f"{holding['market']}:{holding['symbol']}",
            "market": holding["market"],
            "symbol": holding["symbol"],
            "name": holding["name"],
            "quantity": holding["quantity"],
            "unit": holding["unit"],
            "quantity_unit": holding["unit"],
            "error": {"code": code, "message": message},
        }

    @staticmethod
    def _quote_payload(quote: Quote) -> dict[str, Any]:
        return {
            "as_of": quote.as_of,
            "source": quote.source.to_dict(),
        }

    def compose(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        holdings = self.validate_holdings(payload)
        cash_krw = self.validate_cash_krw(payload.get("cash_krw", 0))
        if not holdings and cash_krw <= 0:
            raise PortfolioValidationError(
                "invalid_holdings",
                "Add at least one holding or a positive cash_krw amount",
            )
        warnings: list[str] = []
        priced: list[dict[str, Any]] = []
        unpriced: list[dict[str, Any]] = []
        fx_quote: Quote | None = None

        if any(item["market"] == "us" for item in holdings):
            try:
                fx_quote = self._get_fx_quote()
            except PortfolioError as exc:
                warnings.append(f"USD/KRW 환율을 가져오지 못했습니다: {exc.message}")
                fx_error: PortfolioError | None = exc
            else:
                fx_error = None
        else:
            fx_error = None

        for holding in holdings:
            try:
                local_quote = self._get_local_quote(holding)
                if holding["market"] == "us":
                    if fx_quote is None:
                        raise fx_error or PortfolioProviderError(
                            "portfolio_provider_error", "USD/KRW provider is unavailable"
                        )
                    fx_rate = fx_quote.price
                else:
                    fx_rate = 1.0
                value_krw = float(holding["quantity"]) * local_quote.price * fx_rate
                if not math.isfinite(value_krw) or value_krw <= 0:
                    raise PortfolioProviderError(
                        "quote_invalid", "Calculated holding value is not finite and positive"
                    )
                group = GROUPS[holding["market"]]
                priced.append(
                    {
                        "id": f"{holding['market']}:{holding['symbol']}",
                        "market": holding["market"],
                        "symbol": holding["symbol"],
                        "name": holding["name"],
                        "quantity": holding["quantity"],
                        "unit": holding["unit"],
                        "quantity_unit": holding["unit"],
                        "local_price": round(local_quote.price, 8),
                        "price": round(local_quote.price, 8),
                        "currency": local_quote.currency,
                        "price_currency": local_quote.currency,
                        "fx_rate": round(fx_rate, 8),
                        "value_krw": value_krw,
                        "value_manwon": value_krw / 10000.0,
                        "weight_pct": 0.0,
                        "group_key": holding["market"],
                        "group_label": group["label"],
                        "color": group["color"],
                        "quote": self._quote_payload(local_quote),
                        "quote_as_of": local_quote.as_of,
                        "as_of": local_quote.as_of,
                        "source": local_quote.source.to_dict(),
                    }
                )
            except Exception as exc:
                unpriced.append(self._holding_error(holding, exc))

        if cash_krw > 0:
            cash_as_of = _now_utc()
            cash_source = SourceMeta(
                provider="User input",
                source="사용자 입력 현금",
                note="시세 조회 없이 사용자가 직접 입력한 원화 현금 금액입니다.",
            )
            cash_group = GROUPS["cash"]
            priced.append(
                {
                    "id": "cash:KRW",
                    "market": "cash",
                    "symbol": "KRW",
                    "name": "현금",
                    "quantity": cash_krw,
                    "unit": "KRW",
                    "quantity_unit": "KRW",
                    "local_price": 1.0,
                    "price": 1.0,
                    "currency": "KRW",
                    "price_currency": "KRW",
                    "fx_rate": 1.0,
                    "value_krw": float(cash_krw),
                    "value_manwon": cash_krw / 10000.0,
                    "weight_pct": 0.0,
                    "group_key": "cash",
                    "group_label": cash_group["label"],
                    "color": cash_group["color"],
                    "quote": {"as_of": cash_as_of, "source": cash_source.to_dict()},
                    "quote_as_of": cash_as_of,
                    "as_of": cash_as_of,
                    "source": cash_source.to_dict(),
                }
            )

        if not priced:
            raise PortfolioProviderError(
                "portfolio_no_priced_holdings",
                "No portfolio holdings could be priced",
            )

        total_value_krw = sum(item["value_krw"] for item in priced)
        for item in priced:
            item["weight_pct"] = (
                round(item["value_krw"] / total_value_krw * 100.0, 6)
                if total_value_krw
                else 0.0
            )

        group_rows: list[dict[str, Any]] = []
        for key, definition in GROUPS.items():
            group_value = sum(item["value_krw"] for item in priced if item["group_key"] == key)
            group_rows.append(
                {
                    "key": key,
                    "group_key": key,
                    "label": definition["label"],
                    "color": definition["color"],
                    "value_krw": group_value,
                    "value_manwon": group_value / 10000.0,
                    "weight_pct": round(group_value / total_value_krw * 100.0, 6),
                }
            )

        if unpriced:
            warnings.append(
                f"{len(unpriced)}개 보유 종목은 시세를 받지 못해 합계에서 제외했습니다."
            )
        if any(item["market"] == "gold" for item in priced):
            warnings.append(GOLD_SOURCE_NOTE)
        if fx_quote is not None:
            fx = {
                "pair": "USD/KRW",
                "rate": fx_quote.price,
                "usdkrw": fx_quote.price,
                "as_of": fx_quote.as_of,
                "source": fx_quote.source.to_dict(),
            }
        else:
            fx = {"pair": "USD/KRW", "rate": None, "usdkrw": None, "as_of": None, "source": None}

        return {
            "generated_at": _now_utc(),
            "base_currency": "KRW",
            "unit": "만원",
            "total_value_krw": total_value_krw,
            "total_value_manwon": total_value_krw / 10000.0,
            "fx": fx,
            "groups": group_rows,
            "holdings": priced,
            "priced": priced,
            "priced_holdings": priced,
            "unpriced": unpriced,
            "errors": unpriced,
            "warnings": warnings,
        }


__all__ = [
    "CatalogSnapshot",
    "FDR_KRX_LISTING_URL",
    "GOLD_NAME",
    "GOLD_SOURCE_NOTE",
    "GOLD_SYMBOL",
    "KOSPI_SOURCE",
    "KrxCatalogProvider",
    "NAVER_GOLD_URL",
    "NaverGoldProvider",
    "PortfolioError",
    "PortfolioProviderError",
    "PortfolioService",
    "PortfolioValidationError",
    "Quote",
    "SourceMeta",
    "YahooFinanceProvider",
    "extract_naver_gold_quote",
    "filter_kospi_rows",
    "normalize_quote",
]
