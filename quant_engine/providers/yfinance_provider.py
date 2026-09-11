"""Free-data provider backed by the unofficial yfinance/Yahoo interface."""

from __future__ import annotations

from bisect import bisect_left
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from math import isfinite, log
import os
from pathlib import Path
from statistics import median
import time
from typing import Any, Mapping, Sequence

from ..models import (
    DatedValue,
    FundamentalData,
    MetricStatus,
    MetricValue,
    PriceHistory,
    Provenance,
    utc_now_iso,
)
from .legacy_forward_pe import LegacyForwardPECache, load_legacy_forward_pe_cache
from .stockanalysis_valuation import (
    fetch_weekly_valuation_history as fetch_stockanalysis_weekly_valuation_history,
)


YAHOO_HISTORY_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbol}"
CNN_FEAR_GREED_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"


class YFinanceProvider:
    """Fetch prices in batches and fundamentals concurrently by ticker.

    Yahoo Finance is a convenient, unofficial vendor source.  It is not an
    exchange or issuer filing system and may change fields or adjustment logic
    without notice.  Every emitted metric records that limitation.
    """

    name = "Yahoo Finance via yfinance"

    def __init__(
        self,
        *,
        max_workers: int = 8,
        price_batch_size: int = 600,
        historical_forward_pe: Mapping[str, Sequence[DatedValue]] | None = None,
        historical_trailing_pe: Mapping[str, Sequence[DatedValue]] | None = None,
        historical_dividend_yield: Mapping[str, Sequence[DatedValue]] | None = None,
        legacy_forward_pe_path: str | Path | None = None,
        auto_detect_legacy: bool = True,
    ) -> None:
        self.max_workers = max(1, min(int(max_workers), 16))
        self.price_batch_size = max(1, int(price_batch_size))
        # This extension point accepts only already-dated observations.  The
        # provider never assigns dates to an undated legacy array.
        loaded_history: dict[str, tuple[DatedValue, ...]] = {}
        loaded_trailing_history: dict[str, tuple[DatedValue, ...]] = {}
        loaded_dividend_history: dict[str, tuple[DatedValue, ...]] = {}
        self.legacy_forward_pe_cache: LegacyForwardPECache | None = None
        self.data_warnings: list[str] = []
        candidate: Path | None = None
        override = os.getenv("SP500_FORWARD_PE_CACHE")
        if legacy_forward_pe_path is not None:
            candidate = Path(legacy_forward_pe_path).expanduser()
        elif override:
            candidate = Path(override).expanduser()
        elif auto_detect_legacy:
            candidate = Path.home() / "sp500_stockanalysis_ratio_cache.json"
        if candidate is not None and candidate.exists():
            try:
                loaded = load_legacy_forward_pe_cache(candidate)
                self.legacy_forward_pe_cache = loaded
                loaded_history.update(loaded.history_by_symbol)
                loaded_trailing_history.update(loaded.trailing_pe_by_symbol)
                loaded_dividend_history.update(loaded.dividend_yield_by_symbol)
                self.data_warnings.extend(loaded.warnings)
            except Exception as exc:
                self.data_warnings.append(
                    f"Legacy forward-P/E cache was ignored: {type(exc).__name__}: {exc}"
                )
        elif candidate is not None and (legacy_forward_pe_path is not None or override):
            self.data_warnings.append(
                f"Configured legacy forward-P/E cache does not exist: {candidate}"
            )
        loaded_history.update({
            str(symbol).upper(): tuple(values)
            for symbol, values in (historical_forward_pe or {}).items()
        })
        loaded_trailing_history.update({
            str(symbol).upper(): tuple(values)
            for symbol, values in (historical_trailing_pe or {}).items()
        })
        loaded_dividend_history.update({
            str(symbol).upper(): tuple(values)
            for symbol, values in (historical_dividend_yield or {}).items()
        })
        self.historical_forward_pe = loaded_history
        self.historical_trailing_pe = loaded_trailing_history
        self.historical_dividend_yield = loaded_dividend_history

    def fetch_price_history(
        self, symbols: Sequence[str], *, period: str = "1y"
    ) -> Mapping[str, PriceHistory]:
        requested = list(dict.fromkeys(str(item).strip().upper() for item in symbols if item))
        if not requested:
            return {}
        output: dict[str, PriceHistory] = {}
        for start in range(0, len(requested), self.price_batch_size):
            chunk = requested[start : start + self.price_batch_size]
            output.update(self._download_price_chunk(chunk, period=period))
        return output

    def fetch_fundamentals(
        self, symbols: Sequence[str]
    ) -> Mapping[str, FundamentalData]:
        requested = list(dict.fromkeys(str(item).strip().upper() for item in symbols if item))
        if not requested:
            return {}
        output: dict[str, FundamentalData] = {}
        with ThreadPoolExecutor(
            max_workers=min(self.max_workers, len(requested)),
            thread_name_prefix="yf-fundamental",
        ) as executor:
            futures = {executor.submit(self._fetch_one_fundamental, symbol): symbol for symbol in requested}
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    output[symbol] = future.result()
                except Exception as exc:  # One ticker must not abort the index refresh.
                    retrieved_at = utc_now_iso()
                    provenance = self._provenance(
                        symbol,
                        retrieved_at=retrieved_at,
                        basis="quote summary, annual income statement, and annual cash-flow statement",
                        notes=(f"Provider request failed: {type(exc).__name__}: {exc}",),
                    )
                    output[symbol] = _missing_fundamentals(
                        symbol,
                        provenance,
                        f"provider request failed: {type(exc).__name__}: {exc}",
                    )
        return output

    def fetch_weekly_valuation_history(self, symbol: str) -> Mapping[str, Any]:
        """Fetch the five-year weekly chart and preserve its proxy limitation.

        StockAnalysis supplies quarterly trailing/forward ratio anchors and
        Yahoo supplies each week's last trading close.  The resulting Forward
        P/E series is therefore a quarterly-anchor proxy, not historical weekly
        analyst consensus.
        """

        payload = fetch_stockanalysis_weekly_valuation_history(symbol)
        if not isinstance(payload, Mapping):
            raise TypeError("weekly valuation provider returned a non-object payload")
        return payload

    def fetch_fear_greed(self) -> MetricValue:
        """Fetch CNN's optional convenience index; return N/A on any failure."""

        retrieved_at = utc_now_iso()
        provenance = Provenance(
            source="CNN Fear & Greed (unofficial convenience endpoint)",
            source_url=CNN_FEAR_GREED_URL,
            retrieved_at=retrieved_at,
            as_of=None,
            official=False,
            notes=("Optional market-sentiment input; methodology and endpoint may change.",),
        )
        try:
            import requests

            session = requests.Session()
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/126 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml",
            }
            landing = session.get(
                "https://edition-prod-cf.sitemirror.cnn.com/markets/fear-and-greed",
                headers=headers,
                timeout=15,
            )
            landing.raise_for_status()
            response = session.get(
                CNN_FEAR_GREED_URL,
                headers={
                    **headers,
                    "Accept": "application/json",
                    "Referer": landing.url,
                },
                timeout=15,
            )
            response.raise_for_status()
            payload = response.json().get("fear_and_greed", {})
            score = _finite_float(payload.get("score"))
            as_of = _timestamp_to_iso(payload.get("timestamp")) or retrieved_at
            if score is None or not 0 <= score <= 100:
                raise ValueError("score is absent or outside 0..100")
            dated_provenance = Provenance(
                source=provenance.source,
                source_url=provenance.source_url,
                retrieved_at=retrieved_at,
                as_of=as_of,
                official=False,
                notes=provenance.notes,
            )
            return MetricValue(
                score,
                "index",
                as_of=as_of,
                provenance=(dated_provenance,),
                raw_value=str(payload.get("rating", "")) or None,
            )
        except Exception as exc:
            return MetricValue.missing(
                "index",
                f"optional Fear & Greed fetch failed: {type(exc).__name__}: {exc}",
                provenance=(provenance,),
                as_of=retrieved_at,
            )

    def _download_price_chunk(
        self, symbols: Sequence[str], *, period: str
    ) -> dict[str, PriceHistory]:
        import yfinance as yf

        retrieved_at = utc_now_iso()
        vendor_symbols = {symbol: _to_yahoo_symbol(symbol) for symbol in symbols}
        reverse = {vendor: canonical for canonical, vendor in vendor_symbols.items()}
        price_basis = (
            f"daily Yahoo Adj Close, period={period}, interval=1d, "
            "auto_adjust=False, actions=True; Adj Close retains Yahoo "
            "dividend-adjustment semantics"
        )
        price_notes = (
            "Reported Stock Splits actions are used only for a conditional "
            "continuity repair when robust pre/post Adj Close samples materially "
            "match the split factor.",
            "Yahoo action history and adjustment behavior are unofficial and may "
            "be incomplete or change.",
        )
        try:
            frame = yf.download(
                tickers=list(vendor_symbols.values()),
                period=period,
                interval="1d",
                auto_adjust=False,
                actions=True,
                threads=True,
                progress=False,
                group_by="column",
                timeout=30,
            )
        except Exception as exc:
            return {
                symbol: PriceHistory(
                    symbol=symbol,
                    dates=(),
                    adjusted_closes=(),
                    provenance=self._provenance(
                        vendor_symbols[symbol],
                        retrieved_at=retrieved_at,
                        basis=price_basis,
                        notes=price_notes,
                    ),
                    error=f"batch download failed: {type(exc).__name__}: {exc}",
                )
                for symbol in symbols
            }

        output: dict[str, PriceHistory] = {}
        for vendor_symbol, canonical in reverse.items():
            provenance = self._provenance(
                vendor_symbol,
                retrieved_at=retrieved_at,
                basis=price_basis,
                notes=price_notes,
            )
            try:
                series = _extract_series(
                    frame, vendor_symbol, len(symbols), field="Adj Close"
                )
                split_series = _extract_optional_series(
                    frame, vendor_symbol, len(symbols), field="Stock Splits"
                )
                clean = _clean_price_series(series)
                if not clean:
                    raise ValueError("no valid adjusted closes returned")
                dates = [day for day, _ in clean]
                closes = [close for _, close in clean]
                split_events = _extract_split_events(split_series)
                repaired, repair_notes = _repair_split_discontinuities(
                    dates, closes, split_events
                )
                dated_provenance = Provenance(
                    source=provenance.source,
                    source_url=provenance.source_url,
                    retrieved_at=provenance.retrieved_at,
                    as_of=dates[-1],
                    basis=provenance.basis,
                    official=False,
                    notes=(*provenance.notes, *repair_notes),
                )
                output[canonical] = PriceHistory(
                    symbol=canonical,
                    dates=tuple(dates),
                    adjusted_closes=repaired,
                    provenance=dated_provenance,
                )
            except Exception as exc:
                output[canonical] = PriceHistory(
                    symbol=canonical,
                    dates=(),
                    adjusted_closes=(),
                    provenance=provenance,
                    error=f"no usable price series: {type(exc).__name__}: {exc}",
                )
        return output

    def _fetch_one_fundamental(self, symbol: str) -> FundamentalData:
        import yfinance as yf

        vendor_symbol = _to_yahoo_symbol(symbol)
        ticker = yf.Ticker(vendor_symbol)
        errors: list[str] = []
        info: Mapping[str, Any] = {}
        statement: Any = None
        cash_statement: Any = None
        try:
            result = _retry(lambda: ticker.get_info(), attempts=2)
            if isinstance(result, Mapping):
                info = result
            else:
                errors.append("quote summary returned a non-object payload")
        except Exception as exc:
            errors.append(f"quote summary: {type(exc).__name__}: {exc}")
        try:
            statement = _retry(
                lambda: ticker.get_income_stmt(freq="yearly"), attempts=2
            )
        except Exception as exc:
            errors.append(f"annual income statement: {type(exc).__name__}: {exc}")
        try:
            cash_statement = _retry(
                lambda: ticker.get_cash_flow(freq="yearly"), attempts=2
            )
        except Exception as exc:
            errors.append(f"annual cash-flow statement: {type(exc).__name__}: {exc}")

        retrieved_at = utc_now_iso()
        as_of = retrieved_at[:10]
        quote_provenance = self._provenance(
            vendor_symbol,
            retrieved_at=retrieved_at,
            as_of=as_of,
            basis="Yahoo quote summary fields",
        )
        statement_provenance = self._provenance(
            vendor_symbol,
            retrieved_at=retrieved_at,
            as_of=as_of,
            basis="Yahoo annual income statement, DilutedEPS and DilutedAverageShares rows",
        )
        cash_provenance = self._provenance(
            vendor_symbol,
            retrieved_at=retrieved_at,
            as_of=as_of,
            basis="Yahoo annual cash-flow statement, OperatingCashFlow and FreeCashFlow rows",
        )

        forward_pe = _positive_metric(
            info.get("forwardPE"),
            "multiple",
            "forwardPE",
            quote_provenance,
            as_of,
        )
        trailing_pe = _positive_metric(
            info.get("trailingPE"),
            "multiple",
            "trailingPE",
            quote_provenance,
            as_of,
        )
        market_cap = _positive_metric(
            info.get("marketCap"),
            str(info.get("currency") or "USD"),
            "marketCap",
            quote_provenance,
            as_of,
        )

        # Yahoo's debtToEquity is percentage-form: 79.548 means 0.79548x.
        raw_debt_equity = _finite_float(info.get("debtToEquity"))
        if raw_debt_equity is None:
            debt_equity = MetricValue.missing(
                "ratio",
                "Yahoo debtToEquity is unavailable",
                provenance=(quote_provenance,),
                as_of=as_of,
            )
        elif raw_debt_equity < 0:
            debt_equity = MetricValue(
                None,
                "ratio",
                MetricStatus.INVALID,
                as_of=as_of,
                provenance=(quote_provenance,),
                raw_value=raw_debt_equity,
                note="negative Yahoo debtToEquity usually reflects negative equity; not scored",
            )
        else:
            debt_equity = MetricValue(
                normalize_yahoo_debt_to_equity(raw_debt_equity),
                "ratio",
                as_of=as_of,
                provenance=(quote_provenance,),
                raw_value=raw_debt_equity,
                note="Normalized from Yahoo percentage-form debtToEquity by dividing by 100",
            )

        annual_eps = _annual_diluted_eps(statement, statement_provenance)
        annual_shares = _annual_diluted_shares(statement, statement_provenance)
        annual_net_income = _annual_statement_values(
            statement,
            statement_provenance,
            row_key="netincome",
            period_type="annual_net_income",
        )
        annual_operating_cash_flow = _annual_statement_values(
            cash_statement,
            cash_provenance,
            row_key="operatingcashflow",
            period_type="annual_operating_cash_flow",
        )
        annual_free_cash_flow = _annual_statement_values(
            cash_statement,
            cash_provenance,
            row_key="freecashflow",
            period_type="annual_free_cash_flow",
        )
        share_continuity_error = _annual_share_continuity_error(annual_shares)
        if share_continuity_error:
            # Do not cache a vendor/unit discontinuity as if it were a real
            # dilution event. The scoring layer repeats this guard for cache
            # rows created before this validation existed.
            annual_shares = ()
            errors.append(share_continuity_error)
        if not annual_eps:
            errors.append("annual DilutedEPS has fewer than one usable observation")
        if len(annual_shares) < 4:
            errors.append("annual DilutedAverageShares has fewer than four usable observations")
        if len(set(item.period_end for item in annual_net_income) & set(
            item.period_end for item in annual_operating_cash_flow
        )) < 3:
            errors.append(
                "annual OperatingCashFlow and NetIncome have fewer than three comparable observations"
            )
        if not annual_free_cash_flow:
            errors.append("annual FreeCashFlow has no usable observation")
        history = tuple(self.historical_forward_pe.get(symbol, ()))
        trailing_history = tuple(self.historical_trailing_pe.get(symbol, ()))
        dividend_history = tuple(self.historical_dividend_yield.get(symbol, ()))
        return FundamentalData(
            symbol=symbol,
            forward_pe=forward_pe,
            forward_pe_history=history,
            trailing_pe=trailing_pe,
            trailing_pe_history=trailing_history,
            annual_diluted_eps=annual_eps,
            debt_to_equity=debt_equity,
            market_cap=market_cap,
            annual_diluted_shares=annual_shares,
            dividend_yield_history=dividend_history,
            annual_net_income=annual_net_income,
            annual_operating_cash_flow=annual_operating_cash_flow,
            annual_free_cash_flow=annual_free_cash_flow,
            currency=str(info.get("currency")) if info.get("currency") else None,
            errors=tuple(errors),
        )

    def _provenance(
        self,
        vendor_symbol: str,
        *,
        retrieved_at: str,
        as_of: str | None = None,
        basis: str,
        notes: Sequence[str] = (),
    ) -> Provenance:
        return Provenance(
            source=self.name,
            source_url=YAHOO_HISTORY_URL.format(symbol=vendor_symbol),
            retrieved_at=retrieved_at,
            as_of=as_of,
            basis=basis,
            official=False,
            notes=(
                "Unofficial convenience source; fields, adjustments, and availability may change.",
                *tuple(notes),
            ),
        )


_SPLIT_SAMPLE_WINDOW = 3
_SPLIT_MIN_SAMPLES = 2
_SPLIT_MATERIAL_CLOSENESS = 0.75


def _extract_series(
    frame: Any, vendor_symbol: str, symbol_count: int, *, field: str
) -> Any:
    """Extract one yfinance field from either supported column orientation."""

    columns = getattr(frame, "columns", None)
    if columns is None:
        raise TypeError("yfinance download returned an object without columns")
    if getattr(columns, "nlevels", 1) > 1:
        candidates = [
            (field, vendor_symbol),
            (vendor_symbol, field),
        ]
        for candidate in candidates:
            if candidate in columns:
                return _as_series(frame[candidate], field, vendor_symbol)
        # A one-ticker yfinance frame can retain a MultiIndex with a provider-
        # normalized symbol.  Selecting the only matching field is unambiguous.
        field_columns = [
            item for item in columns if field in tuple(str(part) for part in item)
        ]
        if symbol_count == 1 and len(field_columns) == 1:
            return _as_series(frame[field_columns[0]], field, vendor_symbol)
        raise KeyError(f"{field} column for {vendor_symbol} is absent")
    if field in columns and symbol_count == 1:
        return _as_series(frame[field], field, vendor_symbol)
    # Preserve the old one-ticker flat-frame compatibility path, where the
    # selected field itself may be held under the vendor symbol.
    if vendor_symbol in columns and symbol_count == 1:
        return _as_series(frame[vendor_symbol], field, vendor_symbol)
    raise KeyError(f"{field} column for {vendor_symbol} is absent")


def _extract_optional_series(
    frame: Any, vendor_symbol: str, symbol_count: int, *, field: str
) -> Any | None:
    try:
        return _extract_series(frame, vendor_symbol, symbol_count, field=field)
    except KeyError:
        return None


def _as_series(value: Any, field: str, vendor_symbol: str) -> Any:
    if getattr(value, "ndim", 1) == 1:
        return value
    if getattr(value, "shape", (0, 0))[1] == 1:
        return value.iloc[:, 0]
    raise TypeError(f"{field} column for {vendor_symbol} is not a single series")


def _clean_price_series(series: Any) -> tuple[tuple[str, float], ...]:
    if not hasattr(series, "items"):
        raise TypeError("Yahoo Adj Close is not a series")
    points: list[tuple[str, float]] = []
    for index, raw in series.items():
        number = _finite_float(raw)
        if number is None or number <= 0:
            continue
        points.append((_date_key(index), number))
    return tuple(sorted(points, key=lambda item: item[0]))


def _extract_split_events(series: Any | None) -> tuple[tuple[str, float], ...]:
    if series is None:
        return ()
    if not hasattr(series, "items"):
        return ()
    events: dict[str, float] = {}
    for index, raw in series.items():
        factor = _finite_float(raw)
        if factor is None or factor <= 0 or factor == 1.0:
            continue
        day = _date_key(index)
        events[day] = events.get(day, 1.0) * factor
    return tuple(sorted(events.items()))


def _repair_split_discontinuities(
    dates: Sequence[str],
    closes: Sequence[float],
    split_events: Sequence[tuple[str, float]],
) -> tuple[tuple[float, ...], tuple[str, ...]]:
    """Repair only split discontinuities that are visible in Yahoo Adj Close.

    Yahoo's Adj Close is retained as the source of dividend-adjusted values.
    For each reported action, robust local medians are compared in log space:
    a repair is made only when the pre/post ratio is materially closer to the
    reported split factor than to 1.0.  Historical values before the event are
    then multiplied by ``1 / factor``.  This works for both forward and reverse
    splits and avoids double-adjusting a series Yahoo already normalized.
    """

    values = [float(value) for value in closes]
    if not split_events:
        return tuple(values), (
            "No usable Stock Splits action was reported; no split continuity "
            "repair was applied.",
        )

    notes: list[str] = []
    for split_day, factor in split_events:
        if factor <= 0 or factor == 1.0:
            continue
        boundary = bisect_left(dates, split_day)
        pre = values[max(0, boundary - _SPLIT_SAMPLE_WINDOW) : boundary]
        post = values[boundary : boundary + _SPLIT_SAMPLE_WINDOW]
        if len(pre) < _SPLIT_MIN_SAMPLES or len(post) < _SPLIT_MIN_SAMPLES:
            notes.append(
                f"Reported Stock Splits factor {factor:g} on {split_day} was not "
                "applied because robust pre/post Adj Close samples were "
                "insufficient."
            )
            continue
        pre_median = median(pre)
        post_median = median(post)
        if pre_median <= 0 or post_median <= 0:
            notes.append(
                f"Reported Stock Splits factor {factor:g} on {split_day} was not "
                "applied because its pre/post Adj Close samples were invalid."
            )
            continue
        observed_ratio = pre_median / post_median
        split_distance = abs(log(observed_ratio / factor))
        continuity_distance = abs(log(observed_ratio))
        if (
            split_distance >= continuity_distance * _SPLIT_MATERIAL_CLOSENESS
            or split_distance > log(1.5)
        ):
            notes.append(
                f"Reported Stock Splits factor {factor:g} on {split_day} was not "
                f"applied: robust pre/post Adj Close ratio {observed_ratio:.4g} "
                "was not materially closer to the split factor than to continuity."
            )
            continue
        multiplier = 1.0 / factor
        for position in range(boundary):
            values[position] *= multiplier
        notes.append(
            f"Applied conditional split continuity repair before {split_day}: "
            f"Stock Splits factor {factor:g}, robust pre/post Adj Close ratio "
            f"{observed_ratio:.4g}; historical values multiplied by {multiplier:.4g}."
        )
    return tuple(values), tuple(notes)


def _date_key(index: Any) -> str:
    if hasattr(index, "date"):
        try:
            return index.date().isoformat()
        except (AttributeError, TypeError, ValueError):
            pass
    return str(index)[:10]


def _extract_close_series(frame: Any, vendor_symbol: str, symbol_count: int) -> Any:
    """Backward-compatible helper for callers that still need raw Close."""

    return _extract_series(frame, vendor_symbol, symbol_count, field="Close")


def _annual_statement_values(
    statement: Any,
    provenance: Provenance,
    *,
    row_key: str,
    period_type: str,
    positive_only: bool = False,
) -> tuple[DatedValue, ...]:
    if statement is None or getattr(statement, "empty", True):
        return ()
    row_name = next(
        (
            item
            for item in statement.index
            if str(item).replace(" ", "").replace("_", "").casefold()
            == row_key.casefold()
        ),
        None,
    )
    if row_name is None:
        return ()
    values: list[DatedValue] = []
    series = statement.loc[row_name]
    for period_end, raw in series.items():
        number = _finite_float(raw)
        if number is None or (positive_only and number <= 0):
            continue
        if hasattr(period_end, "date"):
            day = period_end.date().isoformat()
        else:
            day = str(period_end)[:10]
        dated_provenance = Provenance(
            source=provenance.source,
            source_url=provenance.source_url,
            retrieved_at=provenance.retrieved_at,
            as_of=day,
            basis=provenance.basis,
            official=False,
            notes=provenance.notes,
        )
        values.append(
            DatedValue(
                period_end=day,
                value=number,
                period_type=period_type,
                provenance=(dated_provenance,),
            )
        )
    return tuple(sorted(values, key=lambda item: item.period_end, reverse=True))


def _annual_diluted_eps(statement: Any, provenance: Provenance) -> tuple[DatedValue, ...]:
    return _annual_statement_values(
        statement,
        provenance,
        row_key="dilutedeps",
        period_type="annual",
    )


def _annual_diluted_shares(
    statement: Any, provenance: Provenance
) -> tuple[DatedValue, ...]:
    return _annual_statement_values(
        statement,
        provenance,
        row_key="dilutedaverageshares",
        period_type="annual_diluted_shares",
        positive_only=True,
    )


def _annual_share_continuity_error(
    values: Sequence[DatedValue], *, maximum_adjacent_factor: float = 10.0
) -> str | None:
    """Flag likely Yahoo unit errors in consecutive annual share rows."""

    ordered = sorted(values, key=lambda item: item.period_end, reverse=True)
    for current, prior in zip(ordered, ordered[1:]):
        try:
            current_day = datetime.fromisoformat(current.period_end).date()
            prior_day = datetime.fromisoformat(prior.period_end).date()
        except ValueError:
            continue
        spacing = (current_day - prior_day).days
        if not 300 <= spacing <= 430:
            continue
        factor = max(float(current.value), float(prior.value)) / min(
            float(current.value), float(prior.value)
        )
        if factor >= maximum_adjacent_factor:
            return (
                "annual DilutedAverageShares continuity outlier: "
                f"{current.period_end} vs {prior.period_end} differs by {factor:.1f}x; "
                "share history was withheld"
            )
    return None


def _positive_metric(
    raw: Any,
    unit: str,
    field_name: str,
    provenance: Provenance,
    as_of: str,
) -> MetricValue:
    number = _finite_float(raw)
    if number is None:
        return MetricValue.missing(
            unit,
            f"Yahoo {field_name} is unavailable",
            provenance=(provenance,),
            as_of=as_of,
        )
    if number <= 0:
        return MetricValue(
            None,
            unit,
            MetricStatus.INVALID,
            as_of=as_of,
            provenance=(provenance,),
            raw_value=number,
            note=f"Yahoo {field_name} must be positive to be comparable",
        )
    return MetricValue(
        number,
        unit,
        as_of=as_of,
        provenance=(provenance,),
        raw_value=number,
    )


def _missing_fundamentals(
    symbol: str, provenance: Provenance, note: str
) -> FundamentalData:
    lineage = (provenance,)
    return FundamentalData(
        symbol=symbol,
        forward_pe=MetricValue.missing("multiple", note, provenance=lineage),
        forward_pe_history=(),
        trailing_pe=MetricValue.missing("multiple", note, provenance=lineage),
        trailing_pe_history=(),
        annual_diluted_eps=(),
        debt_to_equity=MetricValue.missing("ratio", note, provenance=lineage),
        market_cap=MetricValue.missing("USD", note, provenance=lineage),
        annual_diluted_shares=(),
        dividend_yield_history=(),
        annual_net_income=(),
        annual_operating_cash_flow=(),
        annual_free_cash_flow=(),
        errors=(note,),
    )


def _to_yahoo_symbol(symbol: str) -> str:
    return symbol.replace(".", "-") if symbol in {"BRK.B", "BF.B"} else symbol


def normalize_yahoo_debt_to_equity(raw_value: Any) -> float | None:
    """Convert Yahoo's percentage-form debtToEquity field into a ratio.

    For example, Yahoo ``79.548`` is returned as ``0.79548``.  Missing,
    non-finite, and negative values are invalid and remain ``None``.
    """

    number = _finite_float(raw_value)
    if number is None or number < 0:
        return None
    return number / 100.0


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _retry(function: Any, *, attempts: int) -> Any:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return function()
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.25 * (attempt + 1))
    assert last_error is not None
    raise last_error


def _timestamp_to_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(
                timezone.utc
            ).isoformat().replace("+00:00", "Z")
        except ValueError:
            try:
                value = float(text)
            except ValueError:
                return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 10_000_000_000:  # milliseconds
        number /= 1000.0
    try:
        return datetime.fromtimestamp(number, timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None
