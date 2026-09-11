"""Local GS Quant risk-lab service.

The service deliberately keeps the data boundary small: it accepts a set of
US symbols and weights, obtains adjusted daily price history, and calculates
portfolio risk metrics locally.  ``gs_quant`` is used only through its public
open-source econometrics functions.  No Marquee session, client id, secret,
or network request is created by importing or constructing this module.

The default history provider is intentionally injectable.  Tests and callers
can provide a callable or an object with ``fetch_history``/``get_history``
which returns a mapping of symbol to ``pandas.Series`` or a yfinance-shaped
``pandas.DataFrame``.  Injected history is expected to be adjusted daily
history just like the default provider's ``auto_adjust=True`` output.
"""

from __future__ import annotations

import datetime as _dt
import inspect
import math
import numbers
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf
from gs_quant.timeseries import econometrics as _gs_econometrics
from gs_quant.timeseries.helper import Returns as _GsReturns
from gs_quant.timeseries.helper import SeriesType as _GsSeriesType


SCHEMA_VERSION = "gs-risk-lab-v1"
API_CONTRACT_VERSION = "gs-lab-api-v1"
METRIC_CONTRACT_VERSION = "gs-risk-metric-contract-v1"
SOURCE_DISCLOSURE_VERSION = "source-disclosure-v1"
FORMULA_VERSION = "gs-risk-formula-v1"
GS_QUANT_VERSION = "2.1.6"
TRADING_DAYS_PER_YEAR = 252
VAR_CONFIDENCE = 0.95
MIN_SYMBOLS = 2
MAX_SYMBOLS = 12
MIN_OVERLAP_PRICES = 60
LOOKBACKS = frozenset({"1y", "3y", "5y"})
US_MARKET_TIMEZONE = ZoneInfo("America/New_York")
US_SESSION_SETTLEMENT_TIME = _dt.time(16, 15)

# This follows the server's safe ticker alphabet while rejecting exchange
# prefixes, spaces, caret indexes, and other non-US-provider identifiers.
_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")
_NON_US_SUFFIXES = frozenset(
    {
        "AS",
        "AX",
        "BR",
        "DE",
        "HK",
        "JP",
        "KS",
        "KQ",
        "LN",
        "L",
        "PA",
        "SA",
        "SI",
        "TO",
    }
)


class GsQuantLabError(Exception):
    """Stable, safe-to-map service error.

    ``status`` is an HTTP-compatible integer, while ``code`` and ``message``
    are deliberately independent of provider exception text.  ``details``
    contains only safe, structured identifiers and is optional for callers
    that need diagnostics without exposing raw provider errors.
    """

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.status = int(status)
        self.code = str(code)
        self.message = str(message)
        self.details = dict(details or {})
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        """Return a server-safe error payload."""

        result: dict[str, Any] = {
            "status": self.status,
            "code": self.code,
            "message": self.message,
        }
        if self.details:
            result["details"] = dict(self.details)
        return result


@runtime_checkable
class HistoryProvider(Protocol):
    """Minimal injectable history-provider contract."""

    name: str

    def fetch_history(
        self, symbols: Sequence[str], lookback: str
    ) -> Mapping[str, pd.Series] | pd.DataFrame: ...


class YahooHistoryProvider:
    """Adjusted daily Yahoo history backed by ``yfinance.download``.

    Yahoo Finance/yfinance is a vendor convenience source, not an exchange or
    issuer filing source.  ``auto_adjust=True`` makes the returned ``Close``
    series split- and dividend-adjusted according to yfinance's policy.
    """

    name = "Yahoo Finance via yfinance"
    source_kind = "vendor"
    adjustment = "auto_adjust=True"
    source_url = "https://finance.yahoo.com/"

    def fetch_history(
        self, symbols: Sequence[str], lookback: str
    ) -> Mapping[str, pd.Series] | pd.DataFrame:
        """Fetch one daily adjusted history frame for the requested symbols."""

        # Keep all provider I/O inside the call made by ``analyze``.  In
        # particular, this method is never called from ``__init__``.
        return yf.download(
            tickers=list(symbols),
            period=lookback,
            interval="1d",
            auto_adjust=True,
            actions=False,
            progress=False,
            group_by="column",
            threads=False,
            timeout=10,
        )


@dataclass(frozen=True)
class _CleanSeries:
    symbol: str
    series: pd.Series
    original_count: int
    dropped_count: int


class GsQuantLabService:
    """Analyze a daily constant-weight US portfolio with local gs-quant.

    Construction is side-effect free.  Network I/O occurs only when
    ``analyze`` uses the default ``YahooHistoryProvider``.
    """

    def __init__(
        self,
        history_provider: Any | None = None,
        *,
        provider: Any | None = None,
        now_fn: Callable[[], Any] | None = None,
    ) -> None:
        if history_provider is not None and provider is not None:
            raise ValueError("Pass either history_provider or provider, not both")
        self.history_provider = (
            history_provider
            if history_provider is not None
            else provider
            if provider is not None
            else YahooHistoryProvider()
        )
        # ``provider`` is a convenient compatibility alias for callers that
        # inspect the injected dependency after construction.
        self.provider = self.history_provider
        self._now_fn = now_fn or (lambda: _dt.datetime.now(_dt.timezone.utc))

    def analyze(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Validate a request, fetch history, and return ``gs-risk-lab-v1``."""

        request = self._validate_request(payload)
        symbols = [item["symbol"] for item in request["positions"]]
        all_symbols = list(dict.fromkeys([*symbols, request["benchmark"]]))
        generated_at = _now_iso(self._now_fn)

        try:
            raw_history = self._fetch_history(all_symbols, request["lookback"])
        except Exception as exc:
            # Do not leak requests/yfinance exception text into a public API.
            raise GsQuantLabError(
                502,
                "provider_failure",
                "Adjusted daily history provider failed",
            ) from exc

        clean, data_warnings = self._normalize_history(raw_history, all_symbols)
        clean, session_warnings = _exclude_incomplete_us_session(
            clean, generated_at
        )
        data_warnings.extend(session_warnings)
        missing = [symbol for symbol in all_symbols if symbol not in clean]
        if missing:
            code = "missing_data" if len(missing) == len(all_symbols) else "partial_data"
            message = (
                "No adjusted daily history was returned for the requested symbols"
                if code == "missing_data"
                else "Adjusted daily history is missing for part of the request"
            )
            raise GsQuantLabError(
                422,
                code,
                message,
                details={"symbols": missing},
            )

        short_symbols = [
            symbol
            for symbol in all_symbols
            if len(clean[symbol].series) < MIN_OVERLAP_PRICES
        ]
        if short_symbols and len(short_symbols) < len(all_symbols):
            raise GsQuantLabError(
                422,
                "partial_data",
                "Adjusted daily history is too short for part of the request",
                details={
                    "symbols": short_symbols,
                    "minimum": MIN_OVERLAP_PRICES,
                },
            )

        frame = pd.concat(
            [clean[symbol].series.rename(symbol) for symbol in all_symbols],
            axis=1,
            join="inner",
        ).dropna(how="any")
        frame = frame.replace([np.inf, -np.inf], np.nan).dropna(how="any")
        if len(frame) < MIN_OVERLAP_PRICES:
            raise GsQuantLabError(
                422,
                "insufficient_overlap",
                "Not enough common adjusted daily observations for the requested portfolio",
                details={"observations": int(len(frame)), "minimum": MIN_OVERLAP_PRICES},
            )

        position_symbols = symbols
        benchmark = request["benchmark"]
        weight_values = np.array(
            [item["weight_pct"] / 100.0 for item in request["positions"]],
            dtype=float,
        )

        warnings = list(data_warnings)
        date_strings = [_date_iso(value) for value in frame.index]
        prices = {symbol: frame[symbol] for symbol in all_symbols}

        returns_by_symbol: dict[str, pd.Series] = {}
        for symbol in all_symbols:
            result = self._gs_returns(prices[symbol], warnings)
            if result is None:
                # A valid price frame should normally make this impossible;
                # preserve a null metric rather than inventing observations.
                result = pd.Series(np.nan, index=frame.index, dtype=float)
            returns_by_symbol[symbol] = result.reindex(frame.index)

        returns_frame = pd.concat(
            [returns_by_symbol[symbol].rename(symbol) for symbol in all_symbols],
            axis=1,
        ).replace([np.inf, -np.inf], np.nan)
        aligned_returns = returns_frame.dropna(how="any")
        if len(aligned_returns) < 2:
            raise GsQuantLabError(
                422,
                "insufficient_overlap",
                "Not enough common daily returns for risk calculations",
                details={"returns": int(len(aligned_returns)), "minimum": 2},
            )

        portfolio_returns = pd.Series(
            aligned_returns[position_symbols].to_numpy(dtype=float) @ weight_values,
            index=aligned_returns.index,
            name="portfolio",
        )
        benchmark_returns = aligned_returns[benchmark].rename(benchmark)
        portfolio_index = _level_series(portfolio_returns, frame.index[0])
        benchmark_index = _level_series(benchmark_returns, frame.index[0])

        portfolio_metrics = self._portfolio_metrics(
            portfolio_index,
            portfolio_returns,
            benchmark_index,
            benchmark_returns,
            request["risk_free_rate_pct"],
            warnings,
        )
        benchmark_metrics = self._asset_metrics(
            prices[benchmark],
            prices[benchmark],
            benchmark,
            warnings,
        )

        per_position = self._position_metrics(
            prices,
            position_symbols,
            benchmark,
            request["positions"],
            warnings,
        )
        correlation_matrix = self._correlation_matrix(
            prices,
            position_symbols,
            warnings,
        )
        risk_contributions, covariance_matrix = self._risk_contributions(
            aligned_returns[position_symbols],
            weight_values,
            position_symbols,
            warnings,
        )
        for symbol, contribution in risk_contributions.items():
            per_position[symbol]["risk_contribution_pct"] = contribution
            per_position[symbol]["risk_contribution"] = contribution
            per_position[symbol]["metrics"]["risk_contribution_pct"] = contribution
            per_position[symbol]["metrics"]["risk_contribution"] = contribution

        # Keep both the object-shaped map and the list-shaped form convenient
        # for existing clients.  They contain the same calculated values.
        per_position_list = [per_position[symbol] for symbol in position_symbols]
        chart = self._chart_series(
            frame,
            position_symbols,
            benchmark,
            portfolio_index,
            benchmark_index,
            date_strings,
        )
        as_of = date_strings[-1]
        if _stale_from_retrieval(as_of, generated_at) is True:
            warnings.append(
                "Aligned history is more than seven calendar days older than retrieval time"
            )
        warnings = _dedupe(warnings)
        data = self._data_metadata(
            clean,
            all_symbols,
            as_of,
            len(frame),
            len(aligned_returns),
            generated_at,
            request["lookback"],
            warnings,
        )
        metrics = self._metrics_payload(
            portfolio_metrics,
            benchmark_metrics,
            per_position_list,
            correlation_matrix,
            covariance_matrix,
        )
        formula = _formula_metadata(request["risk_free_rate_pct"])

        return {
            "schema_version": SCHEMA_VERSION,
            "contract_versions": {
                "api": API_CONTRACT_VERSION,
                "metrics": METRIC_CONTRACT_VERSION,
                "source_disclosure": SOURCE_DISCLOSURE_VERSION,
            },
            "generated_at": generated_at,
            "as_of": as_of,
            # ``request`` is retained for the existing HTTP/UI contract;
            # ``normalized_request`` makes normalization explicit to new
            # callers.
            "request": request,
            "normalized_request": request,
            "engine": _engine_metadata(),
            "data": data,
            "data_provenance": data,
            "metrics": metrics,
            "chart": chart,
            "per_position": per_position_list,
            "per_position_metrics": per_position_list,
            "per_position_by_symbol": per_position,
            "risk_contributions": {
                symbol: per_position[symbol]["risk_contribution_pct"]
                for symbol in position_symbols
            },
            "positions": per_position_list,
            "correlation_matrix": correlation_matrix,
            "formula": formula,
            "formula_metadata": formula,
            "warnings": warnings,
        }

    def _validate_request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise GsQuantLabError(400, "validation_error", "Request must be a JSON object")
        unknown = sorted(
            set(payload) - {"positions", "benchmark", "lookback", "risk_free_rate_pct"}
        )
        if unknown:
            raise GsQuantLabError(
                400,
                "validation_error",
                "Request contains unsupported fields",
                details={"fields": [str(field) for field in unknown]},
            )

        raw_positions = payload.get("positions")
        if not isinstance(raw_positions, list):
            raise GsQuantLabError(400, "validation_error", "positions must be an array")
        if not MIN_SYMBOLS <= len(raw_positions) <= MAX_SYMBOLS:
            raise GsQuantLabError(
                400,
                "validation_error",
                "positions must contain 2 to 12 unique US symbols",
            )

        positions: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw_position in raw_positions:
            if not isinstance(raw_position, Mapping):
                raise GsQuantLabError(
                    400,
                    "validation_error",
                    "Each position must be an object with symbol and weight_pct",
                )
            symbol = _normalize_symbol(raw_position.get("symbol"))
            if symbol is None:
                raise GsQuantLabError(
                    400,
                    "validation_error",
                    "Each position must contain a valid US symbol",
                )
            if symbol in seen:
                raise GsQuantLabError(
                    400,
                    "validation_error",
                    "positions must contain unique symbols",
                )
            seen.add(symbol)
            weight = _numeric_input(raw_position.get("weight_pct"))
            if weight is None or weight <= 0:
                raise GsQuantLabError(
                    400,
                    "validation_error",
                    "Each position weight_pct must be finite and positive",
                )
            positions.append({"symbol": symbol, "weight_pct": weight})

        total_weight = sum(item["weight_pct"] for item in positions)
        if not math.isclose(total_weight, 100.0, abs_tol=0.5):
            raise GsQuantLabError(
                400,
                "validation_error",
                "Position weights must total 100% within 0.5 percentage points",
            )
        # Normalize the accepted tolerance so the actual covariance math uses
        # weights that sum to one.  Preserve the input at ordinary exact sums.
        normalized_weights = [weight * 100.0 / total_weight for weight in (item["weight_pct"] for item in positions)]
        normalized_weights[-1] = 100.0 - sum(normalized_weights[:-1])
        for item, normalized_weight in zip(positions, normalized_weights):
            item["weight_pct"] = _finite_number(normalized_weight)

        benchmark = _normalize_symbol(payload.get("benchmark"))
        if benchmark is None:
            raise GsQuantLabError(
                400,
                "validation_error",
                "benchmark must be a valid US symbol",
            )

        lookback = payload.get("lookback")
        if not isinstance(lookback, str) or lookback not in LOOKBACKS:
            raise GsQuantLabError(
                400,
                "validation_error",
                "lookback must be one of 1y, 3y, or 5y",
            )

        risk_free_rate_pct = _numeric_input(payload.get("risk_free_rate_pct"))
        if (
            risk_free_rate_pct is None
            or risk_free_rate_pct < 0.0
            or risk_free_rate_pct > 20.0
        ):
            raise GsQuantLabError(
                400,
                "validation_error",
                "risk_free_rate_pct must be between 0 and 20",
            )

        return {
            "positions": positions,
            "benchmark": benchmark,
            "lookback": lookback,
            "risk_free_rate_pct": _finite_number(risk_free_rate_pct),
        }

    def _fetch_history(
        self, symbols: Sequence[str], lookback: str
    ) -> Mapping[str, Any] | pd.DataFrame:
        provider = self.history_provider
        if isinstance(provider, (Mapping, pd.DataFrame)):
            return provider

        target: Any = None
        for method_name in (
            "fetch_history",
            "get_history",
            "fetch_price_history",
            "fetch_adjusted_history",
            "get_price_history",
            "history",
            "fetch",
            "download",
        ):
            candidate = getattr(provider, method_name, None)
            if callable(candidate):
                target = candidate
                break
        if target is None and callable(provider):
            target = provider
        if target is None:
            raise TypeError("history provider has no supported fetch method")

        if _provider_is_single_symbol(target):
            return {
                symbol: _invoke_provider(target, [symbol], lookback)
                for symbol in symbols
            }
        return _invoke_provider(target, symbols, lookback)

    def _normalize_history(
        self, raw_history: Mapping[str, Any] | pd.DataFrame, symbols: Sequence[str]
    ) -> tuple[dict[str, _CleanSeries], list[str]]:
        if raw_history is None:
            return {}, []

        metadata: Mapping[str, Any] = {}
        raw: Any = raw_history
        if isinstance(raw_history, tuple) and len(raw_history) == 2:
            raw, possible_metadata = raw_history
            if isinstance(possible_metadata, Mapping):
                metadata = possible_metadata
        if isinstance(raw, Mapping):
            for key in ("data", "history", "prices"):
                candidate = _lookup_mapping(raw, key)
                if isinstance(candidate, (Mapping, pd.DataFrame)):
                    metadata = raw
                    raw = candidate
                    break
        if not isinstance(raw, (Mapping, pd.DataFrame)):
            raise GsQuantLabError(
                502,
                "provider_failure",
                "Adjusted daily history provider returned an unsupported payload",
            )

        result: dict[str, _CleanSeries] = {}
        warnings: list[str] = []
        for symbol in symbols:
            value = _extract_symbol_history(raw, symbol, symbols)
            if value is None:
                continue
            try:
                cleaned = _clean_series(symbol, value)
            except Exception:
                # A supplied provider can contain an invalid individual
                # series.  Leave it missing so the stable error path names the
                # symbol without leaking conversion details.
                continue
            if cleaned.series.empty:
                continue
            result[symbol] = cleaned
            if cleaned.dropped_count:
                warnings.append(
                    f"Partial adjusted history for {symbol}: "
                    f"{cleaned.dropped_count} invalid observation(s) omitted"
                )
            if cleaned.original_count < MIN_OVERLAP_PRICES:
                warnings.append(
                    f"Partial adjusted history for {symbol}: "
                    f"only {cleaned.original_count} observation(s) returned"
                )

        # Provider metadata is intentionally not trusted as numeric data, but
        # a concise source note can be retained when a fixture supplies one.
        note = metadata.get("warning") if isinstance(metadata, Mapping) else None
        if isinstance(note, str) and note.strip():
            warnings.append(note.strip()[:240])
        return result, _dedupe(warnings)

    def _gs_returns(
        self, prices: pd.Series, warnings: list[str]
    ) -> pd.Series | None:
        try:
            result = _gs_econometrics.returns(prices, type=_GsReturns.SIMPLE)
            return _coerce_numeric_series(result, prices.index)
        except Exception:
            warnings.append("gs-quant returns calculation was unavailable for part of the data")
            return None

    def _gs_last(
        self,
        function: Callable[..., Any],
        *args: Any,
        warnings: list[str],
        warning_name: str,
        **kwargs: Any,
    ) -> float | None:
        try:
            series = function(*args, **kwargs)
            return _last_finite(series)
        except Exception:
            warnings.append(f"gs-quant {warning_name} calculation was unavailable")
            return None

    def _asset_metrics(
        self,
        prices: pd.Series,
        benchmark_prices: pd.Series,
        symbol: str,
        warnings: list[str],
    ) -> dict[str, Any]:
        returns = self._gs_returns(prices, warnings)
        return_values = returns.dropna().to_numpy(dtype=float) if returns is not None else np.array([], dtype=float)
        total_return_pct = _pct(_price_total_return(prices))
        annualized_return_pct = _pct(_annualized_geometric_return(prices))
        volatility_pct = self._gs_last(
            _gs_econometrics.volatility,
            prices,
            warnings=warnings,
            warning_name="volatility",
            returns_type=_GsReturns.SIMPLE,
            annualization_factor=TRADING_DAYS_PER_YEAR,
        )
        beta = self._gs_last(
            _gs_econometrics.beta,
            prices,
            benchmark_prices,
            warnings=warnings,
            warning_name="beta",
            prices=True,
        )
        correlation = self._gs_last(
            _gs_econometrics.correlation,
            prices,
            benchmark_prices,
            warnings=warnings,
            warning_name="correlation",
            type_=_GsSeriesType.PRICES,
            returns_type=_GsReturns.SIMPLE,
        )
        max_drawdown_pct = self._gs_last(
            _gs_econometrics.max_drawdown,
            prices,
            warnings=warnings,
            warning_name="max drawdown",
        )
        max_drawdown_pct = _scale_if_finite(max_drawdown_pct, 100.0)
        return {
            "symbol": symbol,
            "observations": int(len(prices)),
            "return_observations": int(len(return_values)),
            "total_return_pct": _finite_number(total_return_pct),
            "total_return": _finite_number(total_return_pct),
            "annualized_return_pct": _finite_number(annualized_return_pct),
            "annualized_return": _finite_number(annualized_return_pct),
            "volatility_pct": _finite_number(volatility_pct),
            "volatility": _finite_number(volatility_pct),
            "beta": _finite_number(beta),
            "correlation": _finite_number(correlation),
            "max_drawdown_pct": _finite_number(max_drawdown_pct),
            "max_drawdown": _finite_number(max_drawdown_pct),
        }

    def _portfolio_metrics(
        self,
        portfolio_index: pd.Series,
        portfolio_returns: pd.Series,
        benchmark_index: pd.Series,
        benchmark_returns: pd.Series,
        risk_free_rate_pct: float,
        warnings: list[str],
    ) -> dict[str, Any]:
        volatility_pct = self._gs_last(
            _gs_econometrics.volatility,
            portfolio_index,
            warnings=warnings,
            warning_name="portfolio volatility",
            returns_type=_GsReturns.SIMPLE,
            annualization_factor=TRADING_DAYS_PER_YEAR,
        )
        max_drawdown_pct = self._gs_last(
            _gs_econometrics.max_drawdown,
            portfolio_index,
            warnings=warnings,
            warning_name="portfolio max drawdown",
        )
        max_drawdown_pct = _scale_if_finite(max_drawdown_pct, 100.0)
        beta = self._gs_last(
            _gs_econometrics.beta,
            portfolio_index,
            benchmark_index,
            warnings=warnings,
            warning_name="portfolio beta",
            prices=True,
        )
        correlation = self._gs_last(
            _gs_econometrics.correlation,
            portfolio_index,
            benchmark_index,
            warnings=warnings,
            warning_name="portfolio correlation",
            type_=_GsSeriesType.PRICES,
            returns_type=_GsReturns.SIMPLE,
        )
        annualized_return = _annualized_arithmetic_return(portfolio_returns)
        rf_decimal = risk_free_rate_pct / 100.0
        vol_decimal = (
            volatility_pct / 100.0 if volatility_pct is not None and math.isfinite(volatility_pct) else None
        )
        sharpe = (
            (annualized_return - rf_decimal) / vol_decimal
            if annualized_return is not None
            and vol_decimal is not None
            and vol_decimal > 0.0
            else None
        )
        q05 = _quantile(portfolio_returns.to_numpy(dtype=float), 1.0 - VAR_CONFIDENCE)
        tail = (
            portfolio_returns[portfolio_returns <= q05].to_numpy(dtype=float)
            if q05 is not None
            else np.array([], dtype=float)
        )
        var_pct = max(0.0, -q05 * 100.0) if q05 is not None else None
        es_pct = max(0.0, -float(np.mean(tail)) * 100.0) if len(tail) and np.all(np.isfinite(tail)) else None
        return {
            "observations": int(len(portfolio_returns)),
            "total_return_pct": _finite_number(_pct(_price_total_return(portfolio_index))),
            "total_return": _finite_number(_pct(_price_total_return(portfolio_index))),
            "annualized_return_pct": _finite_number(_pct(annualized_return)),
            "annualized_return": _finite_number(_pct(annualized_return)),
            "volatility_pct": _finite_number(volatility_pct),
            "volatility": _finite_number(volatility_pct),
            "max_drawdown_pct": _finite_number(max_drawdown_pct),
            "max_drawdown": _finite_number(max_drawdown_pct),
            "beta": _finite_number(beta),
            "correlation": _finite_number(correlation),
            "sharpe": _finite_number(sharpe),
            "sharpe_ratio": _finite_number(sharpe),
            "var_95_1d_pct": _finite_number(var_pct),
            "historical_var_95_1d_pct": _finite_number(var_pct),
            "var_95_1d": _finite_number(var_pct),
            "expected_shortfall_95_1d_pct": _finite_number(es_pct),
            "es_95_1d_pct": _finite_number(es_pct),
            "expected_shortfall_95_1d": _finite_number(es_pct),
            "risk_free_rate_pct": _finite_number(risk_free_rate_pct),
        }

    def _position_metrics(
        self,
        prices: Mapping[str, pd.Series],
        position_symbols: Sequence[str],
        benchmark: str,
        positions: Sequence[Mapping[str, Any]],
        warnings: list[str],
    ) -> dict[str, dict[str, Any]]:
        weight_by_symbol = {item["symbol"]: item["weight_pct"] for item in positions}
        result: dict[str, dict[str, Any]] = {}
        for symbol in position_symbols:
            item = self._asset_metrics(prices[symbol], prices[benchmark], symbol, warnings)
            item["weight_pct"] = _finite_number(weight_by_symbol[symbol])
            item["risk_contribution_pct"] = None
            item["risk_contribution"] = None
            # A direct ``metrics`` child is useful to clients that keep the
            # allocation fields separate from the calculated measures.
            item["metrics"] = {
                key: value
                for key, value in item.items()
                if key
                not in {
                    "symbol",
                    "weight_pct",
                    "risk_contribution_pct",
                    "risk_contribution",
                    "metrics",
                }
            }
            result[symbol] = item
        return result

    def _correlation_matrix(
        self,
        prices: Mapping[str, pd.Series],
        symbols: Sequence[str],
        warnings: list[str],
    ) -> dict[str, Any]:
        values: list[list[float | None]] = []
        for left in symbols:
            row: list[float | None] = []
            for right in symbols:
                value = self._gs_last(
                    _gs_econometrics.correlation,
                    prices[left],
                    prices[right],
                    warnings=warnings,
                    warning_name="correlation matrix",
                    type_=_GsSeriesType.PRICES,
                    returns_type=_GsReturns.SIMPLE,
                )
                row.append(_finite_number(value))
            values.append(row)
        return {
            "symbols": list(symbols),
            "values": values,
            "matrix": {
                left: {right: values[i][j] for j, right in enumerate(symbols)}
                for i, left in enumerate(symbols)
            },
            "rows": [
                {"symbol": symbol, "values": row}
                for symbol, row in zip(symbols, values)
            ],
        }

    def _risk_contributions(
        self,
        returns: pd.DataFrame,
        weights: np.ndarray,
        symbols: Sequence[str],
        warnings: list[str],
    ) -> tuple[dict[str, float | None], list[list[float | None]]]:
        try:
            covariance = np.asarray(returns.to_numpy(dtype=float), dtype=float)
            covariance = np.cov(covariance, rowvar=False, ddof=1)
            covariance = np.atleast_2d(covariance)
            if covariance.shape != (len(symbols), len(symbols)) or not np.all(np.isfinite(covariance)):
                raise ValueError
            portfolio_variance = float(weights @ covariance @ weights)
            marginal = covariance @ weights
            if not math.isfinite(portfolio_variance) or portfolio_variance <= 0.0:
                raise ValueError
            contributions = weights * marginal / portfolio_variance * 100.0
            contribution_map = {
                symbol: _finite_number(value) for symbol, value in zip(symbols, contributions)
            }
            matrix = [
                [_finite_number(value) for value in row]
                for row in covariance.tolist()
            ]
            return contribution_map, matrix
        except Exception:
            warnings.append("Covariance risk contribution is unavailable because portfolio variance is not positive")
            return {symbol: None for symbol in symbols}, [
                [None for _ in symbols] for _ in symbols
            ]

    def _chart_series(
        self,
        prices: pd.DataFrame,
        position_symbols: Sequence[str],
        benchmark: str,
        portfolio_index: pd.Series,
        benchmark_index: pd.Series,
        date_strings: Sequence[str],
    ) -> dict[str, Any]:
        positions_index = {
            symbol: [_finite_number(value) for value in _normalize_index(prices[symbol]).tolist()]
            for symbol in position_symbols
        }
        portfolio_values = [
            _finite_number(value * 100.0) for value in portfolio_index.tolist()
        ]
        benchmark_values = [
            _finite_number(value * 100.0) for value in benchmark_index.tolist()
        ]
        # The level series includes the common-date initial level, so it maps
        # one-to-one to the aligned price dates.
        return_dates = list(date_strings)
        portfolio_values = _resize_with_nulls(portfolio_values, len(return_dates))
        benchmark_values = _resize_with_nulls(benchmark_values, len(return_dates))
        # Prices are already indexed to 100 using the first common price.
        positions_index = {
            symbol: _resize_with_nulls(values, len(return_dates))
            for symbol, values in positions_index.items()
        }
        # One aligned columnar representation is enough for the browser and
        # avoids serializing the same daily values again as series, points,
        # aligned points, and two top-level aliases. This matters on the
        # Tailscale/mobile path for 3y and 5y requests.
        return {
            "frequency": "daily",
            "unit": "indexed_level",
            "base": 100.0,
            "dates": return_dates,
            "portfolio": portfolio_values,
            "benchmark": benchmark_values,
            "positions": positions_index,
        }

    def _data_metadata(
        self,
        clean: Mapping[str, _CleanSeries],
        symbols: Sequence[str],
        as_of: str,
        observations: int,
        return_observations: int,
        retrieved_at: str,
        lookback: str,
        warnings: Sequence[str],
    ) -> dict[str, Any]:
        provider = self.history_provider
        if isinstance(provider, (Mapping, pd.DataFrame)):
            provider_name = "Injected history provider"
        elif callable(provider) and not getattr(provider, "name", None):
            provider_name = "Injected history provider"
        else:
            provider_name = str(getattr(provider, "name", provider.__class__.__name__))
        provider_source_kind = str(getattr(provider, "source_kind", "injected"))
        adjustment = str(getattr(provider, "adjustment", "provider-supplied adjusted history"))
        source_url = getattr(provider, "source_url", None)
        latest_by_symbol = {
            symbol: _date_iso(clean[symbol].series.index[-1]) for symbol in symbols
        }
        earliest_by_symbol = {
            symbol: _date_iso(clean[symbol].series.index[0]) for symbol in symbols
        }
        count_by_symbol = {symbol: int(len(clean[symbol].series)) for symbol in symbols}
        stale = _stale_from_retrieval(as_of, retrieved_at)
        freshness_status = "unknown" if stale is None else "stale" if stale else "fresh"
        return {
            "provider": provider_name,
            "source_disclosure_version": SOURCE_DISCLOSURE_VERSION,
            "contract_version": API_CONTRACT_VERSION,
            "source_name": provider_name,
            "source_kind": provider_source_kind,
            "source_role": "adjusted daily price history for risk calculations",
            "source_url": source_url,
            "adjustment": adjustment,
            "frequency": "daily",
            "session_policy": "completed_us_market_sessions_only",
            "lookback": lookback,
            "period_start": min(earliest_by_symbol.values()),
            "period_end": as_of,
            "as_of": as_of,
            "source": provider_name,
            "price_adjustment": adjustment,
            "retrieved_at": retrieved_at,
            "freshness": {
                "status": freshness_status,
                "stale": stale,
                "as_of": as_of,
                "retrieved_at": retrieved_at,
                "note": "Stale means the latest common trading date is more than seven calendar days before retrieval; Yahoo does not provide an exchange timestamp here.",
            },
            "symbols": list(symbols),
            "latest_by_symbol": latest_by_symbol,
            "observations_by_symbol": count_by_symbol,
            "observations": observations,
            "return_observations": return_observations,
            "aligned_observations": observations,
            "aligned_return_observations": return_observations,
            "partial": any(
                clean[symbol].dropped_count or clean[symbol].original_count < len(clean[symbol].series)
                for symbol in symbols
            ),
            "provenance": [
                {
                    "source_name": provider_name,
                    "source_kind": provider_source_kind,
                    "source_url": source_url,
                    "adjustment": adjustment,
                    "as_of": as_of,
                    "retrieved_at": retrieved_at,
                    "status": freshness_status,
                    "value_kind": "fact",
                }
            ],
            "source_disclosure": {
                "version": SOURCE_DISCLOSURE_VERSION,
                "source_name": provider_name,
                "source_kind": provider_source_kind,
                "source_url": source_url,
                "adjustment": adjustment,
                "role": "vendor adjusted daily prices used for derived risk metrics; not official exchange or filing data",
            },
            "warnings_count": len(warnings),
        }

    def _metrics_payload(
        self,
        portfolio: Mapping[str, Any],
        benchmark: Mapping[str, Any],
        positions: Sequence[Mapping[str, Any]],
        correlation_matrix: Mapping[str, Any],
        covariance_matrix: Sequence[Sequence[float | None]],
    ) -> dict[str, Any]:
        payload = {
            "contract_version": METRIC_CONTRACT_VERSION,
            "portfolio": dict(portfolio),
            "benchmark": dict(benchmark),
            "positions": list(positions),
            "position_metrics_by_symbol": {
                item["symbol"]: dict(item) for item in positions
            },
            "risk_contributions": {
                item["symbol"]: item.get("risk_contribution_pct")
                for item in positions
            },
            "correlation_matrix": dict(correlation_matrix),
            "covariance_matrix": {
                "symbols": [item["symbol"] for item in positions],
                "values": [list(row) for row in covariance_matrix],
            },
        }
        # Stable aliases make the aggregate fields directly consumable by a
        # small chart/UI client while retaining the grouped contract above.
        for key, value in portfolio.items():
            payload[f"portfolio_{key}"] = value
        return payload


def _engine_metadata() -> dict[str, Any]:
    return {
        "name": "gs-quant",
        "library": "gs_quant.timeseries.econometrics",
        "version": GS_QUANT_VERSION,
        "mode": "open_source_local",
        "marquee_connected": False,
        "marquee_credentials_required": False,
        "functions": ["returns", "volatility", "beta", "correlation", "max_drawdown"],
    }


def _formula_metadata(risk_free_rate_pct: float) -> dict[str, Any]:
    return {
        "version": FORMULA_VERSION,
        "trading_days_per_year": TRADING_DAYS_PER_YEAR,
        "confidence_level": VAR_CONFIDENCE,
        "risk_free_rate_pct": _finite_number(risk_free_rate_pct),
        "portfolio_method": "daily_constant_weight",
        "minimum_common_price_observations": MIN_OVERLAP_PRICES,
        "session_policy": "completed_us_market_sessions_only",
        "assumptions": [
            "Adjusted daily close history is used on the common trading-date intersection.",
            "At least 60 common completed daily price observations are required; an updating same-day US bar is omitted until 16:15 America/New_York.",
            "Input weights are rebalanced to the normalized request weights every trading day.",
            "252 trading days per year is used for annualization.",
            "Historical one-day 95% VaR is max(0, negative empirical 5th-percentile return); ES is max(0, negative mean of returns at or below that boundary).",
            "Sharpe uses annualized arithmetic mean daily return minus the supplied annual risk-free rate, divided by annualized volatility.",
            "Risk contribution is covariance variance contribution: w_i * (Sigma w)_i / (w' Sigma w).",
        ],
        "measures": {
            "returns": {
                "engine": "gs_quant.timeseries.econometrics.returns",
                "formula": "P_t / P_(t-1) - 1",
                "type": "simple",
            },
            "volatility": {
                "engine": "gs_quant.timeseries.econometrics.volatility",
                "formula": "sample_std(daily_returns) * sqrt(252)",
                "unit": "percent",
            },
            "beta": {
                "engine": "gs_quant.timeseries.econometrics.beta",
                "formula": "cov(asset_returns, benchmark_returns) / var(benchmark_returns)",
            },
            "correlation": {
                "engine": "gs_quant.timeseries.econometrics.correlation",
                "formula": "Pearson correlation of simple daily returns",
            },
            "max_drawdown": {
                "engine": "gs_quant.timeseries.econometrics.max_drawdown",
                "formula": "minimum of indexed level / running peak - 1",
                "unit": "percent",
            },
            "portfolio_return": {
                "engine": "local_aggregate",
                "formula": "sum_i(weight_i * return_i,t)",
            },
            "risk_contribution": {
                "engine": "local_aggregate",
                "formula": "weight_i * (Sigma w)_i / (w' Sigma w)",
                "unit": "percent_of_portfolio_variance",
            },
        },
    }


def _numeric_input(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _normalize_symbol(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    symbol = value.strip().upper()
    if not _SYMBOL_RE.fullmatch(symbol):
        return None
    if symbol.startswith(".") or symbol.startswith("-"):
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", symbol):
        return None
    if "." in symbol:
        suffix = symbol.rsplit(".", 1)[1]
        if suffix in _NON_US_SUFFIXES:
            return None
    return symbol


def _now_iso(value_or_fn: Any) -> str:
    value = value_or_fn() if callable(value_or_fn) else value_or_fn
    if isinstance(value, _dt.date) and not isinstance(value, _dt.datetime):
        return value.isoformat()
    if not isinstance(value, _dt.datetime):
        return str(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=_dt.timezone.utc)
    value = value.astimezone(_dt.timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def _date_iso(value: Any) -> str:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert("UTC").tz_localize(None)
    return timestamp.date().isoformat()


def _exclude_incomplete_us_session(
    clean: Mapping[str, _CleanSeries], retrieved_at: str
) -> tuple[dict[str, _CleanSeries], list[str]]:
    """Remove a same-day US daily bar until the regular session has settled.

    Yahoo can expose an updating daily bar while the US regular session is
    still open. Mixing that partial return with completed sessions biases
    volatility, drawdown, beta, VaR, and expected-shortfall estimates. The
    date label is treated as a daily-session label even when a provider adds a
    timezone to midnight.
    """

    try:
        retrieved = pd.Timestamp(retrieved_at)
        if retrieved.tzinfo is None:
            retrieved = retrieved.tz_localize("UTC")
        market_now = retrieved.tz_convert(US_MARKET_TIMEZONE)
    except (TypeError, ValueError, KeyError):
        return dict(clean), []

    if (
        market_now.weekday() >= 5
        or market_now.time().replace(tzinfo=None) >= US_SESSION_SETTLEMENT_TIME
    ):
        return dict(clean), []

    market_date = market_now.date()
    result: dict[str, _CleanSeries] = {}
    removed: list[str] = []
    for symbol, item in clean.items():
        keep = np.array(
            [pd.Timestamp(value).date() != market_date for value in item.series.index],
            dtype=bool,
        )
        if bool(np.all(keep)):
            result[symbol] = item
            continue
        removed.append(symbol)
        result[symbol] = _CleanSeries(
            symbol=item.symbol,
            series=item.series.iloc[keep],
            original_count=item.original_count,
            dropped_count=item.dropped_count,
        )

    warnings: list[str] = []
    if removed:
        warnings.append(
            "뉴욕 정규장 미완료 일봉은 16:15(미 동부시간)까지 제외했습니다: "
            + ", ".join(removed)
        )
    return result, warnings


def _stale_from_retrieval(as_of: str, retrieved_at: str) -> bool | None:
    try:
        retrieved_date = pd.Timestamp(retrieved_at).date()
        as_of_date = pd.Timestamp(as_of).date()
    except (TypeError, ValueError):
        return None
    return (retrieved_date - as_of_date).days > 7


def _finite_number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _scale_if_finite(value: float | None, scale: float) -> float | None:
    return _finite_number(value * scale) if value is not None else None


def _pct(value: float | None) -> float | None:
    return _scale_if_finite(value, 100.0)


def _price_total_return(series: pd.Series) -> float | None:
    if len(series) < 2:
        return None
    first = _finite_number(series.iloc[0])
    last = _finite_number(series.iloc[-1])
    if first is None or last is None or first <= 0.0:
        return None
    return _finite_number(last / first - 1.0)


def _annualized_geometric_return(series: pd.Series) -> float | None:
    total = _price_total_return(series)
    if total is None or len(series) < 2:
        return None
    days = (pd.Timestamp(series.index[-1]) - pd.Timestamp(series.index[0])).days
    if days <= 0 or 1.0 + total <= 0.0:
        return None
    return _finite_number((1.0 + total) ** (365.25 / days) - 1.0)


def _annualized_arithmetic_return(returns: pd.Series) -> float | None:
    values = returns.to_numpy(dtype=float)
    if len(values) == 0 or not np.all(np.isfinite(values)):
        return None
    return _finite_number(float(np.mean(values)) * TRADING_DAYS_PER_YEAR)


def _quantile(values: np.ndarray, probability: float) -> float | None:
    if values.size == 0 or not np.all(np.isfinite(values)):
        return None
    return _finite_number(np.quantile(values, probability))


def _level_series(returns: pd.Series, initial_date: Any) -> pd.Series:
    """Build an index level that includes the pre-return common-date level."""

    cumulative = (1.0 + returns.astype(float)).cumprod()
    values = np.concatenate(([1.0], cumulative.to_numpy(dtype=float)))
    index = pd.Index([initial_date, *list(returns.index)])
    return pd.Series(values, index=index, dtype=float).replace(
        [np.inf, -np.inf], np.nan
    )


def _normalize_index(prices: pd.Series) -> pd.Series:
    first = _finite_number(prices.iloc[0]) if len(prices) else None
    if first is None or first <= 0.0:
        return pd.Series(np.nan, index=prices.index, dtype=float)
    return (prices / first * 100.0).astype(float)


def _coerce_numeric_series(value: Any, index: pd.Index) -> pd.Series:
    if isinstance(value, pd.Series):
        result = pd.to_numeric(value, errors="coerce")
        return result.reindex(index)
    result = pd.Series(value, index=index, dtype=float)
    return pd.to_numeric(result, errors="coerce")


def _last_finite(value: Any) -> float | None:
    if isinstance(value, pd.Series):
        values = pd.to_numeric(value, errors="coerce").to_numpy(dtype=float)
    elif isinstance(value, (list, tuple, np.ndarray, pd.Index)):
        values = pd.to_numeric(pd.Series(value), errors="coerce").to_numpy(dtype=float)
    else:
        return _finite_number(value)
    values = values[np.isfinite(values)]
    return _finite_number(values[-1]) if values.size else None


def _clean_series(symbol: str, value: Any) -> _CleanSeries:
    if isinstance(value, pd.DataFrame):
        value = _extract_frame_column(value, symbol, [symbol])
    elif isinstance(value, Mapping):
        value = pd.Series(value, dtype=float)
    if not isinstance(value, pd.Series):
        raise TypeError("history must be a pandas Series")
    original_count = len(value)
    if original_count == 0:
        return _CleanSeries(symbol, pd.Series(dtype=float), 0, 0)
    index = pd.to_datetime(value.index, errors="coerce", utc=True)
    numeric = pd.to_numeric(value, errors="coerce").to_numpy(dtype=float)
    valid = (~pd.isna(index)) & np.isfinite(numeric) & (numeric > 0.0)
    if not np.any(valid):
        return _CleanSeries(symbol, pd.Series(dtype=float), original_count, original_count)
    clean_index = pd.DatetimeIndex(index[valid]).tz_convert(None).normalize()
    clean_values = numeric[valid]
    series = pd.Series(clean_values, index=clean_index, dtype=float)
    duplicate_count = int(series.index.duplicated(keep="last").sum())
    series = series[~series.index.duplicated(keep="last")].sort_index()
    dropped_count = original_count - len(series)
    dropped_count = max(dropped_count, duplicate_count)
    return _CleanSeries(symbol, series, original_count, dropped_count)


def _extract_symbol_history(raw: Any, symbol: str, symbols: Sequence[str]) -> Any | None:
    if isinstance(raw, pd.DataFrame):
        return _extract_frame_column(raw, symbol, symbols)
    if not isinstance(raw, Mapping):
        return None
    value = _lookup_mapping(raw, symbol)
    if value is not None:
        if isinstance(value, Mapping) and not isinstance(value, pd.Series):
            for key in ("Close", "Adj Close", "close", "adj_close"):
                candidate = _lookup_mapping(value, key)
                if candidate is not None:
                    return candidate
        if isinstance(value, pd.DataFrame):
            return _extract_frame_column(value, symbol, [symbol])
        return value
    # A mapping of yfinance fields to symbol mappings is also accepted.
    for field in ("Close", "Adj Close", "close", "adj_close"):
        field_value = _lookup_mapping(raw, field)
        if isinstance(field_value, Mapping):
            candidate = _lookup_mapping(field_value, symbol)
            if candidate is not None:
                return candidate
        elif isinstance(field_value, pd.DataFrame):
            candidate = _extract_frame_column(field_value, symbol, symbols)
            if candidate is not None:
                return candidate
    # A small deterministic fixture may use a date-to-price mapping rather
    # than a Series.  Do not reinterpret field-shaped mappings as dates.
    raw_keys = list(raw) if raw else []
    looks_like_date_mapping = bool(raw_keys) and all(
        isinstance(key, (str, _dt.date, _dt.datetime, pd.Timestamp))
        and _normalize_symbol(key) is None
        for key in raw_keys
    )
    if looks_like_date_mapping:
        try:
            return pd.Series(raw, dtype=float)
        except (TypeError, ValueError):
            pass
    return None


def _extract_frame_column(frame: pd.DataFrame, symbol: str, symbols: Sequence[str]) -> pd.Series | None:
    if frame.empty:
        return None
    columns = frame.columns
    if isinstance(columns, pd.MultiIndex):
        candidates = [
            ("Close", symbol),
            ("Adj Close", symbol),
            (symbol, "Close"),
            (symbol, "Adj Close"),
        ]
        for candidate in candidates:
            if candidate in columns:
                return frame[candidate]
        for level in range(columns.nlevels):
            for value in columns.get_level_values(level).unique():
                if str(value).upper() == symbol:
                    subset = frame.xs(value, axis=1, level=level)
                    return _extract_single_field(subset)
        return None
    for candidate in (symbol, "Close", "Adj Close", "close", "adj_close"):
        if candidate in columns:
            # A single-symbol frame's Close column is the adjusted output from
            # auto_adjust=True; a fixture may use Adj Close explicitly.
            if candidate == "Close" and len(symbols) > 1 and symbol not in columns:
                continue
            return frame[candidate]
    for column in columns:
        if str(column).upper() == symbol:
            return frame[column]
    if len(symbols) == 1 and len(columns) == 1:
        return frame.iloc[:, 0]
    return None


def _extract_single_field(frame: pd.DataFrame) -> pd.Series | None:
    for column in ("Close", "Adj Close", "close", "adj_close"):
        if column in frame.columns:
            return frame[column]
    if frame.shape[1] == 1:
        return frame.iloc[:, 0]
    return None


def _lookup_mapping(mapping: Mapping[Any, Any], key: Any) -> Any | None:
    if key in mapping:
        return mapping[key]
    wanted = str(key).casefold()
    for current_key, value in mapping.items():
        if str(current_key).casefold() == wanted:
            return value
    return None


def _invoke_provider(target: Callable[..., Any], symbols: Sequence[str], lookback: str) -> Any:
    symbol_argument: Any = (
        symbols[0] if _provider_is_single_symbol(target) and len(symbols) == 1 else symbols
    )
    candidates = [
        ((symbol_argument, lookback), {}),
        ((symbol_argument,), {"lookback": lookback}),
        ((symbol_argument,), {"period": lookback}),
        ((), {"symbols": symbol_argument, "lookback": lookback}),
        ((), {"symbols": symbol_argument, "period": lookback}),
        ((), {"tickers": symbol_argument, "lookback": lookback}),
        ((), {"tickers": symbol_argument, "period": lookback}),
        ((symbol_argument,), {}),
    ]
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        return target(symbol_argument, lookback)
    for args, kwargs in candidates:
        try:
            signature.bind(*args, **kwargs)
        except TypeError:
            continue
        return target(*args, **kwargs)
    raise TypeError("history provider has an unsupported signature")


def _provider_is_single_symbol(target: Callable[..., Any]) -> bool:
    """Identify the optional per-symbol provider form without calling it."""

    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        return False
    parameters = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.name not in {"self", "cls"}
        and parameter.kind
        in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        }
    ]
    if not parameters:
        return False
    return parameters[0].name.casefold() in {
        "symbol",
        "ticker",
        "security",
        "instrument",
    }


def _dedupe(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _resize_with_nulls(values: Sequence[Any], size: int) -> list[float | None]:
    result = [_finite_number(value) for value in values[:size]]
    if len(result) < size:
        result.extend([None] * (size - len(result)))
    return result


__all__ = [
    "API_CONTRACT_VERSION",
    "FORMULA_VERSION",
    "GS_QUANT_VERSION",
    "GsQuantLabError",
    "GsQuantLabService",
    "HistoryProvider",
    "METRIC_CONTRACT_VERSION",
    "SCHEMA_VERSION",
    "SOURCE_DISCLOSURE_VERSION",
    "YahooHistoryProvider",
]
