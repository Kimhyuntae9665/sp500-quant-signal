"""Application service coordinating universe, provider, cache, and scoring."""

from __future__ import annotations

from datetime import date, timedelta
from math import isfinite
from pathlib import Path
from typing import Any, Mapping, Sequence

from .cache import CacheRecord, SQLiteCache
from .models import (
    DatedValue,
    FundamentalData,
    MarketOverlay,
    MetricStatus,
    MetricValue,
    PriceHistory,
    PriceMetrics,
    Provenance,
    RefreshReport,
    ScreeningResult,
    StockSignal,
    UniverseMember,
    utc_now_iso,
)
from .providers import MarketDataProvider, YFinanceProvider
from .scoring import (
    calculate_price_metrics,
    compose_stock_metrics,
    get_rule_definitions,
    score_market_overlay,
    score_stock,
    wilder_rsi_series,
)
from .universe import SP500Universe, normalize_symbol


# ``v1`` may contain price metrics calculated from a temporarily mixed
# pre/post-split Yahoo series.  Keep those rows unreachable after the provider
# starts enforcing split continuity instead of trusting their normal TTL.
PRICE_NAMESPACE = "price_metrics_v3"
PRICE_SCHEMA_VERSION = 4
FUNDAMENTAL_NAMESPACE = "fundamentals_v1"
FUNDAMENTAL_SCHEMA_VERSION = 4
SENTIMENT_NAMESPACE = "market_sentiment_v1"
CHART_HISTORY_NAMESPACE = "chart_price_history_v1"
# Cache namespace for the explicit five-year weekly valuation contract.  The
# previous ``..._v2`` namespace also held sparse/legacy valuation snapshots;
# using a new namespace means those rows are never presented as weekly data.
VALUATION_HISTORY_NAMESPACE = "chart_weekly_valuation_v3"
VALUATION_HISTORY_SCHEMA_VERSION = 2

# A weekly payload may contain a long interval when a quarterly P/E
# denominator is undefined.  Keep the cadence contract strict enough to
# reject annual/sparse snapshots: one nonempty series must have at least eight
# observations and at least 75% of its adjacent intervals in the normal
# 3-to-14-day trading-week band.  For the existing eight-point fixtures this
# is six of seven intervals.  Every nonempty series with enough points must
# satisfy that density independently, while one qualifying series establishes
# cadence for the payload.  This lets the companion series contain legitimate
# long gaps without admitting a mixed annual legacy series.
_WEEKLY_VALUATION_MIN_CADENCE_POINTS = 8
_WEEKLY_VALUATION_MIN_REGULAR_SPACING_RATIO = 0.75
_WEEKLY_VALUATION_MIN_SPACING_DAYS = 3
_WEEKLY_VALUATION_MAX_REGULAR_SPACING_DAYS = 14


class SignalService:
    """Public synchronous API for the local S&P 500 application.

    Parameters may be replaced with fixture implementations in tests.  By
    default, prices are refreshed after six hours and fundamentals after 24
    hours.  Refresh selection happens per cache key, so a partial run fetches
    only absent/expired ticker fundamentals.
    """

    def __init__(
        self,
        provider: MarketDataProvider | None = None,
        cache: SQLiteCache | None = None,
        universe: SP500Universe | None = None,
        *,
        cache_path: str | Path | None = None,
        price_ttl: timedelta | float = timedelta(hours=6),
        fundamentals_ttl: timedelta | float = timedelta(hours=24),
        sentiment_ttl: timedelta | float = timedelta(hours=1),
    ) -> None:
        self.provider = provider or YFinanceProvider()
        default_cache = Path(__file__).resolve().parent.parent / ".cache" / "quant_engine.sqlite3"
        self.cache = cache or SQLiteCache(cache_path or default_cache)
        self.universe = universe or SP500Universe()
        self.price_ttl_seconds = _seconds(price_ttl)
        self.fundamentals_ttl_seconds = _seconds(fundamentals_ttl)
        self.sentiment_ttl_seconds = _seconds(sentiment_ttl)

    def get_symbols(self) -> list[str]:
        return self.universe.get_symbols()

    def get_rules(self) -> dict[str, Any]:
        return get_rule_definitions()

    @property
    def rules(self) -> dict[str, Any]:
        return self.get_rules()

    def screen(
        self,
        symbols: Sequence[str] | None = None,
        *,
        force_refresh: bool = False,
        allow_fetch: bool = False,
        market_overlay: MarketOverlay | None = None,
    ) -> ScreeningResult:
        universe_snapshot = self.universe.get_snapshot()
        selected = self._select_members(symbols, universe_snapshot.members)
        requested = [member.symbol for member in selected]

        fetch = allow_fetch or force_refresh
        price_data, price_stats = self._load_prices(
            requested, force=force_refresh, allow_fetch=fetch
        )
        fundamental_data, fundamental_stats = self._load_fundamentals(
            requested, force=force_refresh, allow_fetch=fetch
        )
        overlay_stats: Mapping[str, Any] = {}
        if market_overlay is None:
            market_overlay, overlay_stats = self._load_market_overlay(
                force=force_refresh, allow_fetch=fetch
            )

        composed: list[Any] = []
        for member in selected:
            price = price_data.get(member.symbol) or _missing_price_metrics(
                member.symbol, "provider returned no price object"
            )
            fundamentals = fundamental_data.get(member.symbol) or _missing_fundamentals(
                member.symbol, "provider returned no fundamentals object"
            )
            metrics = compose_stock_metrics(
                member,
                price,
                fundamentals,
            )
            composed.append(metrics)

        signals: list[StockSignal] = [
            score_stock(metrics, market_overlay) for metrics in composed
        ]

        signals.sort(
            key=lambda item: (
                not item.eligible_for_ranking,
                -item.total_score,
                item.company.symbol,
            )
        )
        price_as_of_candidates = [
            signal.metrics["current_price"].as_of
            for signal in signals
            if signal.metrics.get("current_price")
            and signal.metrics["current_price"].as_of
        ]
        fundamental_as_of_candidates = [
            metric.as_of
            for signal in signals
            for key, metric in signal.metrics.items()
            if key
            not in {
                "current_price",
                "high_52_week",
                "drawdown_52_week_pct",
                "sma_200",
                "distance_200dma_pct",
                "rsi_14",
                "return_6_1_pct",
                "return_12_1_pct",
                "volatility_12m_pct",
            }
            and metric.as_of
        ]
        overlay_as_of_candidates = (
            [metric.as_of for metric in market_overlay.metrics.values() if metric.as_of]
            if market_overlay
            else []
        )
        coverage = [item.coverage_pct for item in signals]
        minimum_company_metrics = int(
            get_rule_definitions()["minimum_company_metrics"]
        )
        data_quality = {
            "missing_values_are_null": True,
            "partial_scores_are_not_ranked_below_minimum_factors": True,
            "minimum_company_metrics": minimum_company_metrics,
            "average_company_factor_coverage_pct": (
                round(sum(coverage) / len(coverage), 1) if coverage else 0.0
            ),
            "price_as_of": (
                max(price_as_of_candidates) if price_as_of_candidates else None
            ),
            "fundamentals_as_of": (
                max(fundamental_as_of_candidates)
                if fundamental_as_of_candidates
                else None
            ),
            "price_cache": price_stats,
            "fundamentals_cache": fundamental_stats,
            "market_overlay": dict(overlay_stats),
            "provider": getattr(self.provider, "name", type(self.provider).__name__),
            "provider_notice": (
                "Yahoo/yfinance is an unofficial convenience source; inspect each metric's "
                "provenance and as-of before acting."
            ),
            "provider_warnings": list(getattr(self.provider, "data_warnings", ())),
        }
        legacy_cache = getattr(self.provider, "legacy_forward_pe_cache", None)
        if legacy_cache is not None:
            data_quality["legacy_forward_pe_cache"] = {
                "source_path": legacy_cache.source_path,
                "file_mtime": legacy_cache.file_mtime,
                "records_seen": legacy_cache.records_seen,
                "records_loaded": legacy_cache.records_loaded,
                "dividend_yield_records_loaded": len(
                    legacy_cache.dividend_yield_by_symbol
                ),
                "warnings": list(legacy_cache.warnings),
            }
        return ScreeningResult(
            generated_at=utc_now_iso(),
            # The headline dashboard date represents the latest tradable market
            # close. Fundamental retrieval dates remain explicit in data_quality.
            as_of=(
                max(price_as_of_candidates)
                if price_as_of_candidates
                else (max(overlay_as_of_candidates) if overlay_as_of_candidates else None)
            ),
            universe=universe_snapshot,
            signals=tuple(signals),
            market_overlay=market_overlay,
            data_quality=data_quality,
        )

    def get_signal(
        self,
        symbol: str,
        *,
        force_refresh: bool = False,
        allow_fetch: bool = False,
        market_overlay: MarketOverlay | None = None,
    ) -> StockSignal:
        canonical = normalize_symbol(symbol)
        result = self.screen(
            [canonical],
            force_refresh=force_refresh,
            allow_fetch=allow_fetch,
            market_overlay=market_overlay,
        )
        signal = next(
            (item for item in result.signals if item.company.symbol == canonical),
            None,
        )
        if signal is None:
            raise KeyError(f"{canonical} is not in the current S&P 500 roster")
        return signal

    def get_chart_history(self, symbol: str) -> dict[str, Any]:
        """Return a cached/on-demand three-year price and RSI chart series."""

        canonical = normalize_symbol(symbol)
        snapshot = self.universe.get_snapshot()
        self._select_members([canonical], snapshot.members)
        record = self.cache.get(
            CHART_HISTORY_NAMESPACE,
            canonical,
            max_age_seconds=self.price_ttl_seconds,
        )
        history: PriceHistory | None = None
        cache_status = "miss"
        if record and record.fresh:
            history = PriceHistory.from_dict(record.payload)
            cache_status = "fresh"
        else:
            try:
                fetched = self.provider.fetch_price_history([canonical], period="3y")
                candidate = fetched.get(canonical)
            except Exception as exc:
                candidate = None
                fetch_error = f"{type(exc).__name__}: {exc}"
            else:
                fetch_error = None
            if candidate and candidate.adjusted_closes:
                history = candidate
                cache_status = "updated"
                self.cache.put(
                    CHART_HISTORY_NAMESPACE,
                    canonical,
                    history.to_dict(),
                    source_as_of=(history.dates[-1] if history.dates else None),
                    schema_version=1,
                )
            elif record:
                history = PriceHistory.from_dict(record.payload)
                cache_status = "stale"
            else:
                provenance = _missing_provenance(
                    fetch_error or "provider returned no chart history"
                )
                history = PriceHistory(
                    symbol=canonical,
                    dates=(),
                    adjusted_closes=(),
                    provenance=provenance,
                    error=fetch_error or "provider returned no chart history",
                )
        valuation_history, valuation_cache_status = (
            self._get_weekly_valuation_history(canonical)
        )
        return _chart_history_payload(
            history,
            cache_status=cache_status,
            valuation_history=valuation_history,
            valuation_cache_status=valuation_cache_status,
        )

    def _get_weekly_valuation_history(
        self, canonical: str
    ) -> tuple[dict[str, Any], str]:
        """Load or fetch the on-demand five-year weekly valuation proxy."""

        record = self.cache.get(
            VALUATION_HISTORY_NAMESPACE,
            canonical,
            max_age_seconds=self.price_ttl_seconds,
        )
        cache_error: str | None = None
        if record and not _is_weekly_valuation_cache_record(record):
            cache_error = _weekly_valuation_payload_error(
                record.payload,
                require_schema=True,
            ) or (
                "cache schema version "
                f"{record.schema_version} is older than "
                f"{VALUATION_HISTORY_SCHEMA_VERSION}"
            )
        if record and record.fresh and _is_weekly_valuation_cache_record(record):
            return dict(record.payload), "fresh"

        fetch_error: str | None = None
        candidate: Mapping[str, Any] | None = None
        method = getattr(self.provider, "fetch_weekly_valuation_history", None)
        if callable(method):
            try:
                fetched = method(canonical)
                candidate = fetched if isinstance(fetched, Mapping) else None
                if candidate is None:
                    fetch_error = "provider returned a non-object valuation payload"
            except Exception as exc:
                fetch_error = f"{type(exc).__name__}: {exc}"
        else:
            fetch_error = "provider does not support weekly valuation history"

        if candidate:
            candidate_error = _weekly_valuation_payload_error(candidate)
            if candidate_error is None:
                payload = dict(candidate)
                payload.setdefault("schema_version", VALUATION_HISTORY_SCHEMA_VERSION)
                self.cache.put(
                    VALUATION_HISTORY_NAMESPACE,
                    canonical,
                    payload,
                    source_as_of=(
                        str(payload.get("as_of"))
                        if payload.get("as_of") is not None
                        else None
                    ),
                    schema_version=VALUATION_HISTORY_SCHEMA_VERSION,
                )
                return payload, "updated"
            fetch_error = f"provider returned invalid weekly valuation payload: {candidate_error}"
        if record and _is_weekly_valuation_cache_record(record):
            payload = dict(record.payload)
            if fetch_error:
                payload["error"] = (
                    "Latest weekly valuation refresh failed; showing the last "
                    f"successful cache. {fetch_error}"
                )
            return payload, "stale"
        if cache_error:
            fetch_error = (
                f"{fetch_error}; " if fetch_error else ""
            ) + f"ignored invalid weekly valuation cache: {cache_error}"
        return _empty_weekly_valuation_payload(
            canonical,
            error=fetch_error or "provider returned no weekly valuation points",
        ), "miss"

    def refresh(
        self,
        symbols: Sequence[str] | None = None,
        *,
        fundamentals: bool = True,
        prices: bool = True,
    ) -> RefreshReport:
        started_at = utc_now_iso()
        snapshot = self.universe.get_snapshot()
        selected = self._select_members(symbols, snapshot.members)
        requested = [member.symbol for member in selected]
        price_stats: Mapping[str, Any] = {
            "updated": 0,
            "failures": {},
        }
        fundamental_stats: Mapping[str, Any] = {
            "updated": 0,
            "failures": {},
        }
        overlay_stats: Mapping[str, Any] = {"failures": {}}
        if prices:
            _, price_stats = self._load_prices(requested, force=True, allow_fetch=True)
            _, overlay_stats = self._load_market_overlay(force=True, allow_fetch=True)
        if fundamentals:
            _, fundamental_stats = self._load_fundamentals(
                requested, force=True, allow_fetch=True
            )
        failures = {
            "prices": tuple(
                f"{key}: {value}" for key, value in price_stats.get("failures", {}).items()
            ),
            "fundamentals": tuple(
                f"{key}: {value}"
                for key, value in fundamental_stats.get("failures", {}).items()
            ),
            "market_overlay": tuple(
                f"{key}: {value}"
                for key, value in overlay_stats.get("failures", {}).items()
            ),
        }
        return RefreshReport(
            started_at=started_at,
            finished_at=utc_now_iso(),
            requested=len(requested),
            prices_requested=prices,
            fundamentals_requested=fundamentals,
            price_updated=int(price_stats.get("updated", 0)),
            fundamentals_updated=int(fundamental_stats.get("updated", 0)),
            failures=failures,
        )

    def _select_members(
        self,
        symbols: Sequence[str] | None,
        members: Sequence[UniverseMember],
    ) -> tuple[UniverseMember, ...]:
        if symbols is None:
            return tuple(members)
        lookup = {member.symbol: member for member in members}
        requested = list(dict.fromkeys(normalize_symbol(item) for item in symbols))
        unknown = [symbol for symbol in requested if symbol not in lookup]
        if unknown:
            raise KeyError(
                "not in the current S&P 500 roster: " + ", ".join(sorted(unknown))
            )
        return tuple(lookup[symbol] for symbol in requested)

    def _load_prices(
        self, symbols: Sequence[str], *, force: bool, allow_fetch: bool
    ) -> tuple[dict[str, PriceMetrics], dict[str, Any]]:
        records = self.cache.get_many(
            PRICE_NAMESPACE,
            symbols,
            max_age_seconds=self.price_ttl_seconds,
        )
        output: dict[str, PriceMetrics] = {}
        to_fetch: list[str] = []
        fresh_cache_hits = 0
        stale_cache_reads = 0
        cache_misses = 0
        expired_cache_entries = 0
        forced_cache_refreshes = 0
        stale_fallbacks = 0
        for symbol in symbols:
            record = records.get(symbol)
            if record and record.fresh and not force:
                output[symbol] = PriceMetrics.from_dict(record.payload)
                fresh_cache_hits += 1
            elif not allow_fetch:
                if record:
                    stale_cache_reads += 1
                    stale_fallbacks += 1
                    output[symbol] = _stale_price_metrics(
                        PriceMetrics.from_dict(record.payload),
                        "cache TTL expired; run a price refresh",
                    )
                else:
                    cache_misses += 1
                    output[symbol] = _missing_price_metrics(
                        symbol, "not cached; run a price refresh"
                    )
            else:
                if record:
                    if record.fresh:
                        forced_cache_refreshes += 1
                    else:
                        expired_cache_entries += 1
                else:
                    cache_misses += 1
                to_fetch.append(symbol)

        failures: dict[str, str] = {}
        updated_payloads: dict[str, Mapping[str, Any]] = {}
        source_as_of: dict[str, str | None] = {}
        if to_fetch:
            try:
                # Two years provides the 273+ sessions needed for standard
                # 12-1 momentum while still keeping the batch download small.
                histories = self.provider.fetch_price_history(to_fetch, period="2y")
            except Exception as exc:
                histories = {}
                provider_error = f"{type(exc).__name__}: {exc}"
                failures.update({symbol: provider_error for symbol in to_fetch})
            for symbol in to_fetch:
                history = histories.get(symbol)
                fresh = calculate_price_metrics(history) if history else None
                if fresh and fresh.current_price.usable:
                    output[symbol] = fresh
                    updated_payloads[symbol] = fresh.to_dict()
                    source_as_of[symbol] = fresh.current_price.as_of
                    continue
                reason = (
                    history.error
                    if history and history.error
                    else "provider returned no usable adjusted-close history"
                )
                failures[symbol] = reason
                old = records.get(symbol)
                if old:
                    stale_fallbacks += 1
                    output[symbol] = _stale_price_metrics(
                        PriceMetrics.from_dict(old.payload), reason
                    )
                elif fresh:
                    output[symbol] = fresh
                else:
                    output[symbol] = _missing_price_metrics(symbol, reason)
        self.cache.put_many(
            PRICE_NAMESPACE,
            updated_payloads,
            source_as_of=source_as_of,
            schema_version=PRICE_SCHEMA_VERSION,
        )
        return output, {
            "requested": len(symbols),
            "cache_hits": fresh_cache_hits,
            "cache_misses": cache_misses,
            "expired_cache_entries": expired_cache_entries,
            "forced_cache_refreshes": forced_cache_refreshes,
            "stale_cache_reads": stale_cache_reads,
            "fetched": len(to_fetch),
            "updated": len(updated_payloads),
            "stale_fallbacks": stale_fallbacks,
            "failures": failures,
            "ttl_seconds": self.price_ttl_seconds,
        }

    def _load_fundamentals(
        self, symbols: Sequence[str], *, force: bool, allow_fetch: bool
    ) -> tuple[dict[str, FundamentalData], dict[str, Any]]:
        records = self.cache.get_many(
            FUNDAMENTAL_NAMESPACE,
            symbols,
            max_age_seconds=self.fundamentals_ttl_seconds,
        )
        output: dict[str, FundamentalData] = {}
        to_fetch: list[str] = []
        fresh_cache_hits = 0
        stale_cache_reads = 0
        cache_misses = 0
        expired_cache_entries = 0
        forced_cache_refreshes = 0
        stale_fallbacks = 0
        for symbol in symbols:
            record = records.get(symbol)
            schema_current = (
                record is not None
                and record.schema_version >= FUNDAMENTAL_SCHEMA_VERSION
            )
            if record and record.fresh and schema_current and not force:
                output[symbol] = FundamentalData.from_dict(record.payload)
                fresh_cache_hits += 1
            elif not allow_fetch:
                if record:
                    stale_cache_reads += 1
                    stale_fallbacks += 1
                    output[symbol] = _stale_fundamentals(
                        FundamentalData.from_dict(record.payload),
                        "cache TTL expired; run a full refresh",
                    )
                else:
                    cache_misses += 1
                    output[symbol] = _missing_fundamentals(
                        symbol, "not cached; run a full refresh"
                    )
            else:
                if record:
                    if record.fresh:
                        forced_cache_refreshes += 1
                    else:
                        expired_cache_entries += 1
                else:
                    cache_misses += 1
                to_fetch.append(symbol)

        failures: dict[str, str] = {}
        updated_payloads: dict[str, Mapping[str, Any]] = {}
        source_as_of: dict[str, str | None] = {}
        if to_fetch:
            try:
                fetched = self.provider.fetch_fundamentals(to_fetch)
            except Exception as exc:
                fetched = {}
                provider_error = f"{type(exc).__name__}: {exc}"
                failures.update({symbol: provider_error for symbol in to_fetch})
            for symbol in to_fetch:
                fresh = fetched.get(symbol)
                if fresh and _fundamental_has_data(fresh):
                    output[symbol] = fresh
                    updated_payloads[symbol] = fresh.to_dict()
                    source_as_of[symbol] = _fundamental_as_of(fresh)
                    if fresh.errors:
                        failures[symbol] = "; ".join(fresh.errors)
                    continue
                reason = (
                    "; ".join(fresh.errors)
                    if fresh and fresh.errors
                    else "provider returned no usable fundamental fields"
                )
                failures[symbol] = reason
                old = records.get(symbol)
                if old:
                    stale_fallbacks += 1
                    output[symbol] = _stale_fundamentals(
                        FundamentalData.from_dict(old.payload), reason
                    )
                elif fresh:
                    output[symbol] = fresh
                else:
                    output[symbol] = _missing_fundamentals(symbol, reason)
        self.cache.put_many(
            FUNDAMENTAL_NAMESPACE,
            updated_payloads,
            source_as_of=source_as_of,
            schema_version=FUNDAMENTAL_SCHEMA_VERSION,
        )
        return output, {
            "requested": len(symbols),
            "cache_hits": fresh_cache_hits,
            "cache_misses": cache_misses,
            "expired_cache_entries": expired_cache_entries,
            "forced_cache_refreshes": forced_cache_refreshes,
            "stale_cache_reads": stale_cache_reads,
            "fetched": len(to_fetch),
            "updated": len(updated_payloads),
            "stale_fallbacks": stale_fallbacks,
            "failures": failures,
            "ttl_seconds": self.fundamentals_ttl_seconds,
        }

    def _load_market_overlay(
        self, *, force: bool, allow_fetch: bool
    ) -> tuple[MarketOverlay, dict[str, Any]]:
        price, price_stats = self._load_prices(
            ["SPY", "^VIX"], force=force, allow_fetch=allow_fetch
        )
        spy = price.get("SPY") or _missing_price_metrics("SPY", "SPY price unavailable")
        vix_price = price.get("^VIX") or _missing_price_metrics(
            "^VIX", "VIX price unavailable"
        )
        spy_drawdown = spy.drawdown_52_week_pct
        if vix_price.current_price.usable:
            vix = MetricValue(
                vix_price.current_price.value,
                "index",
                status=vix_price.current_price.status,
                as_of=vix_price.current_price.as_of,
                provenance=vix_price.current_price.provenance,
                raw_value=vix_price.current_price.raw_value,
                note="Latest ^VIX adjusted close from Yahoo/yfinance",
            )
        else:
            vix = MetricValue.missing(
                "index",
                vix_price.current_price.note or "VIX price unavailable",
                provenance=vix_price.current_price.provenance,
                as_of=vix_price.current_price.as_of,
            )
        fear_greed, sentiment_stats = self._load_fear_greed(
            force=force, allow_fetch=allow_fetch
        )
        overlay = score_market_overlay(
            {
                "spy_drawdown_52_week_pct": spy_drawdown,
                "vix": vix,
                "fear_greed": fear_greed,
            }
        )
        failures = {
            **dict(price_stats.get("failures", {})),
            **dict(sentiment_stats.get("failures", {})),
        }
        return overlay, {
            "complete": overlay.complete,
            "score": overlay.score,
            "price_cache": price_stats,
            "sentiment_cache": sentiment_stats,
            "failures": failures,
        }

    def _load_fear_greed(
        self, *, force: bool, allow_fetch: bool
    ) -> tuple[MetricValue, dict[str, Any]]:
        record = self.cache.get(
            SENTIMENT_NAMESPACE,
            "fear_greed",
            max_age_seconds=self.sentiment_ttl_seconds,
        )
        if record and record.fresh and not force:
            return MetricValue.from_dict(record.payload), {
                "cache_hit": True,
                "updated": 0,
                "failures": {},
            }
        if not allow_fetch:
            if record:
                stale = MetricValue.from_dict(record.payload).with_status(
                    MetricStatus.STALE,
                    "cache TTL expired; run a price refresh",
                )
                return stale, {
                    "cache_hit": False,
                    "updated": 0,
                    "stale_fallback": True,
                    "failures": {},
                }
            missing = MetricValue.missing(
                "index", "not cached; run a price refresh"
            )
            return missing, {
                "cache_hit": False,
                "updated": 0,
                "failures": {},
            }
        try:
            fresh = self.provider.fetch_fear_greed()
        except Exception as exc:
            fresh = MetricValue.missing(
                "index", f"provider exception: {type(exc).__name__}: {exc}"
            )
        if fresh.usable:
            self.cache.put(
                SENTIMENT_NAMESPACE,
                "fear_greed",
                fresh.to_dict(),
                source_as_of=fresh.as_of,
            )
            return fresh, {"cache_hit": False, "updated": 1, "failures": {}}
        reason = fresh.note or "Fear & Greed is unavailable"
        if record:
            stale = MetricValue.from_dict(record.payload).with_status(
                MetricStatus.STALE,
                f"stale cached value after refresh failure: {reason}",
            )
            return stale, {
                "cache_hit": False,
                "updated": 0,
                "stale_fallback": True,
                "failures": {"fear_greed": reason},
            }
        return fresh, {
            "cache_hit": False,
            "updated": 0,
            "failures": {"fear_greed": reason},
        }


def _seconds(value: timedelta | float) -> float:
    seconds = value.total_seconds() if isinstance(value, timedelta) else float(value)
    if seconds < 0:
        raise ValueError("cache TTL cannot be negative")
    return seconds


def _missing_provenance(note: str) -> Provenance:
    return Provenance(
        source="none",
        retrieved_at=utc_now_iso(),
        basis="No provider datum was available",
        notes=(note,),
    )


def _missing_price_metrics(symbol: str, note: str) -> PriceMetrics:
    provenance = (_missing_provenance(note),)
    return PriceMetrics(
        symbol=symbol,
        current_price=MetricValue.missing("USD", note, provenance=provenance),
        high_52_week=MetricValue.missing("USD", note, provenance=provenance),
        drawdown_52_week_pct=MetricValue.missing("percent", note, provenance=provenance),
        sma_200=MetricValue.missing("USD", note, provenance=provenance),
        distance_200dma_pct=MetricValue.missing("percent", note, provenance=provenance),
        rsi_14=MetricValue.missing("index", note, provenance=provenance),
        return_6_1_pct=MetricValue.missing("percent", note, provenance=provenance),
        return_12_1_pct=MetricValue.missing("percent", note, provenance=provenance),
        volatility_12m_pct=MetricValue.missing("percent", note, provenance=provenance),
    )


def _missing_fundamentals(symbol: str, note: str) -> FundamentalData:
    provenance = (_missing_provenance(note),)
    return FundamentalData(
        symbol=symbol,
        forward_pe=MetricValue.missing("multiple", note, provenance=provenance),
        forward_pe_history=(),
        trailing_pe=MetricValue.missing("multiple", note, provenance=provenance),
        trailing_pe_history=(),
        annual_diluted_eps=(),
        debt_to_equity=MetricValue.missing("ratio", note, provenance=provenance),
        market_cap=MetricValue.missing("USD", note, provenance=provenance),
        annual_diluted_shares=(),
        dividend_yield_history=(),
        annual_net_income=(),
        annual_operating_cash_flow=(),
        annual_free_cash_flow=(),
        errors=(note,),
    )


def _stale_metric(metric: MetricValue, reason: str) -> MetricValue:
    if not metric.usable:
        return metric
    return metric.with_status(
        MetricStatus.STALE,
        f"stale cached value after refresh failure: {reason}",
    )


def _stale_price_metrics(value: PriceMetrics, reason: str) -> PriceMetrics:
    return PriceMetrics(
        symbol=value.symbol,
        current_price=_stale_metric(value.current_price, reason),
        high_52_week=_stale_metric(value.high_52_week, reason),
        drawdown_52_week_pct=_stale_metric(value.drawdown_52_week_pct, reason),
        sma_200=_stale_metric(value.sma_200, reason),
        distance_200dma_pct=_stale_metric(value.distance_200dma_pct, reason),
        rsi_14=_stale_metric(value.rsi_14, reason),
        return_6_1_pct=_stale_metric(value.return_6_1_pct, reason),
        return_12_1_pct=_stale_metric(value.return_12_1_pct, reason),
        volatility_12m_pct=_stale_metric(value.volatility_12m_pct, reason),
    )


def _stale_fundamentals(value: FundamentalData, reason: str) -> FundamentalData:
    return FundamentalData(
        symbol=value.symbol,
        forward_pe=_stale_metric(value.forward_pe, reason),
        forward_pe_history=_stale_dated_values(value.forward_pe_history, reason),
        trailing_pe=_stale_metric(value.trailing_pe, reason),
        trailing_pe_history=_stale_dated_values(
            value.trailing_pe_history, reason
        ),
        annual_diluted_eps=_stale_dated_values(value.annual_diluted_eps, reason),
        debt_to_equity=_stale_metric(value.debt_to_equity, reason),
        market_cap=_stale_metric(value.market_cap, reason),
        annual_diluted_shares=_stale_dated_values(
            value.annual_diluted_shares, reason
        ),
        dividend_yield_history=_stale_dated_values(
            value.dividend_yield_history, reason
        ),
        annual_net_income=_stale_dated_values(value.annual_net_income, reason),
        annual_operating_cash_flow=_stale_dated_values(
            value.annual_operating_cash_flow, reason
        ),
        annual_free_cash_flow=_stale_dated_values(
            value.annual_free_cash_flow, reason
        ),
        currency=value.currency,
        errors=(*value.errors, f"stale cache used: {reason}"),
    )


def _stale_dated_values(
    values: Sequence[DatedValue], reason: str
) -> tuple[DatedValue, ...]:
    """Attach an auditable stale marker to history rows used in calculations."""

    marker = f"stale cache used after refresh failure: {reason}"
    output: list[DatedValue] = []
    for item in values:
        lineage = item.provenance or (
            Provenance(
                source="local SQLite cache",
                retrieved_at=utc_now_iso(),
                as_of=item.period_end,
                basis="cached historical observation",
            ),
        )
        stale_lineage = tuple(
            Provenance(
                source=source.source,
                retrieved_at=source.retrieved_at,
                as_of=source.as_of,
                source_url=source.source_url,
                basis=source.basis,
                official=source.official,
                notes=(
                    source.notes
                    if any("stale cache used" in note.casefold() for note in source.notes)
                    else (*source.notes, marker)
                ),
            )
            for source in lineage
        )
        output.append(
            DatedValue(
                period_end=item.period_end,
                value=item.value,
                period_type=item.period_type,
                provenance=stale_lineage,
            )
        )
    return tuple(output)


def _fundamental_has_data(value: FundamentalData) -> bool:
    return any(
        metric.usable
        for metric in (
            value.forward_pe,
            value.trailing_pe,
            value.debt_to_equity,
            value.market_cap,
        )
    ) or bool(
        value.annual_diluted_eps
        or value.trailing_pe_history
        or value.annual_diluted_shares
        or value.dividend_yield_history
        or value.annual_net_income
        or value.annual_operating_cash_flow
        or value.annual_free_cash_flow
    )


def _fundamental_as_of(value: FundamentalData) -> str | None:
    candidates = [
        metric.as_of
        for metric in (
            value.forward_pe,
            value.trailing_pe,
            value.debt_to_equity,
            value.market_cap,
        )
        if metric.as_of
    ]
    candidates.extend(item.period_end for item in value.annual_diluted_eps)
    candidates.extend(
        source.as_of
        for item in value.trailing_pe_history
        for source in item.provenance
        if source.as_of
    )
    candidates.extend(item.period_end for item in value.annual_diluted_shares)
    candidates.extend(item.period_end for item in value.annual_net_income)
    candidates.extend(item.period_end for item in value.annual_operating_cash_flow)
    candidates.extend(item.period_end for item in value.annual_free_cash_flow)
    candidates.extend(
        source.as_of
        for item in value.dividend_yield_history
        for source in item.provenance
        if source.as_of
    )
    return max(candidates) if candidates else None


def _chart_history_payload(
    history: PriceHistory,
    *,
    cache_status: str,
    valuation_history: Mapping[str, Any] | None = None,
    valuation_cache_status: str = "miss",
) -> dict[str, Any]:
    pairs = [
        (str(day), float(close))
        for day, close in zip(history.dates, history.adjusted_closes)
        if close is not None and float(close) > 0
    ]
    dates = [item[0] for item in pairs]
    closes = [item[1] for item in pairs]
    rsi_values = wilder_rsi_series(closes, period=14) if closes else ()
    price_points = [
        {"date": day, "value": round(close, 6)} for day, close in pairs
    ]
    rsi_points = [
        {"date": day, "value": round(float(rsi), 4)}
        for day, rsi in zip(dates, rsi_values)
        if rsi is not None
    ]
    valuation = dict(valuation_history or {})
    valuation["cache_status"] = valuation_cache_status
    return {
        "symbol": history.symbol,
        "period": "3y",
        "as_of": dates[-1] if dates else None,
        "cache_status": cache_status,
        "series": {
            "adjusted_close": _downsample_points(price_points),
            "rsi_14": _downsample_points(rsi_points),
        },
        "valuation": valuation,
        "provenance": history.provenance.to_dict(),
        "error": history.error,
    }


def _has_weekly_valuation_points(value: Mapping[str, Any]) -> bool:
    """Return whether ``value`` satisfies the non-legacy weekly contract."""

    return _weekly_valuation_payload_error(value) is None


def _is_weekly_valuation_cache_record(record: CacheRecord) -> bool:
    """Reject old/sparse cache rows before they can be shown as weekly data."""

    return (
        int(record.schema_version) >= VALUATION_HISTORY_SCHEMA_VERSION
        and _weekly_valuation_payload_error(
            record.payload,
            require_schema=True,
        )
        is None
    )


def _weekly_valuation_payload_error(
    value: Mapping[str, Any],
    *,
    require_schema: bool = False,
) -> str | None:
    """Validate series shape, weekly cadence, and explicit metadata.

    The validator deliberately accepts either one or both P/E series, because
    a vendor may omit a ratio for a subset of quarterly anchors.  It rejects
    undated/annual legacy snapshots and any payload whose declared metadata no
    longer describes the actual points.
    """

    if not isinstance(value, Mapping):
        return "valuation payload is not an object"
    if value.get("period") != "5y":
        return "valuation period is not 5y"
    if value.get("frequency") != "weekly":
        return "valuation frequency is not weekly"
    raw_schema = value.get("schema_version")
    if raw_schema is not None:
        try:
            if int(raw_schema) < VALUATION_HISTORY_SCHEMA_VERSION:
                return (
                    "valuation payload schema version is older than "
                    f"{VALUATION_HISTORY_SCHEMA_VERSION}"
                )
        except (TypeError, ValueError):
            return "valuation payload schema version is invalid"
    elif require_schema:
        return "valuation payload schema version is missing"

    metadata = value.get("metadata")
    if not isinstance(metadata, Mapping):
        return "weekly valuation metadata is missing"
    if metadata.get("period") != "5y":
        return "weekly valuation metadata period is not 5y"
    if metadata.get("frequency") != "weekly":
        return "weekly valuation metadata frequency is not weekly"
    point_counts = metadata.get("point_counts")
    if not isinstance(point_counts, Mapping):
        return "weekly valuation point counts are missing"
    coverage_range = metadata.get("range")
    if not isinstance(coverage_range, Mapping):
        return "weekly valuation coverage range is missing"

    series = value.get("series")
    if not isinstance(series, Mapping):
        return "weekly valuation series is missing"
    usable_series = 0
    cadence_evidence = False
    observed_dates: list[str] = []
    for key in ("trailing_pe_weekly", "forward_pe_weekly"):
        points = series.get(key)
        if not isinstance(points, Sequence) or isinstance(
            points, (str, bytes, bytearray)
        ):
            return f"weekly valuation series {key} is invalid"
        try:
            declared_count = int(point_counts.get(key, -1))
        except (TypeError, ValueError):
            return f"weekly valuation point count for {key} is invalid"
        if declared_count != len(points):
            return (
                f"weekly valuation point count for {key} does not match "
                "the series"
            )
        if points:
            usable_series += 1
        previous_day: date | None = None
        seen_iso_weeks: set[tuple[int, int]] = set()
        spacings: list[int] = []
        for index, point in enumerate(points):
            if not isinstance(point, Mapping):
                return f"weekly valuation point {key}[{index}] is not an object"
            raw_day = point.get("date", point.get("period_end"))
            if raw_day is None:
                return f"weekly valuation point {key}[{index}] has no date"
            day_text = str(raw_day)[:10]
            try:
                day = date.fromisoformat(day_text)
            except ValueError:
                return f"weekly valuation point {key}[{index}] has invalid date"
            if day.weekday() >= 5:
                return (
                    f"weekly valuation point {key}[{index}] is not a trading day"
                )
            try:
                number = float(point.get("value"))
            except (TypeError, ValueError):
                return f"weekly valuation point {key}[{index}] has invalid value"
            if not isfinite(number) or number <= 0:
                return f"weekly valuation point {key}[{index}] has invalid value"
            if previous_day is not None:
                spacing = (day - previous_day).days
                if day <= previous_day:
                    return (
                        f"weekly valuation series {key} dates are not strictly "
                        "ascending"
                    )
                if spacing < _WEEKLY_VALUATION_MIN_SPACING_DAYS:
                    return (
                        f"weekly valuation series {key} has non-weekly date "
                        f"spacing ({spacing} days)"
                    )
                spacings.append(spacing)
            iso = day.isocalendar()
            iso_key = (int(iso.year), int(iso.week))
            if iso_key in seen_iso_weeks:
                return f"weekly valuation series {key} repeats ISO week {iso_key}"
            seen_iso_weeks.add(iso_key)
            previous_day = day
            observed_dates.append(day.isoformat())

        regular_intervals = sum(
            _WEEKLY_VALUATION_MIN_SPACING_DAYS
            <= spacing
            <= _WEEKLY_VALUATION_MAX_REGULAR_SPACING_DAYS
            for spacing in spacings
        )
        if points and len(points) >= _WEEKLY_VALUATION_MIN_CADENCE_POINTS:
            series_has_cadence = (
                regular_intervals / len(spacings)
                >= _WEEKLY_VALUATION_MIN_REGULAR_SPACING_RATIO
            )
            if not series_has_cadence:
                return (
                    f"weekly valuation series {key} lacks cadence evidence: "
                    f"at least {_WEEKLY_VALUATION_MIN_REGULAR_SPACING_RATIO:.0%} "
                    "of adjacent spacings must be in the "
                    f"{_WEEKLY_VALUATION_MIN_SPACING_DAYS}- to "
                    f"{_WEEKLY_VALUATION_MAX_REGULAR_SPACING_DAYS}-day band"
                )
            cadence_evidence = True
        elif len(points) > 1 and regular_intervals == 0:
            return (
                f"weekly valuation series {key} lacks cadence evidence: "
                "a short partial series must contain a weekly interval"
            )

    if usable_series == 0:
        return "weekly valuation series has no points"
    if not cadence_evidence:
        return (
            "weekly valuation payload lacks cadence evidence: at least "
            f"{_WEEKLY_VALUATION_MIN_CADENCE_POINTS} points in one nonempty "
            "series must have at least "
            f"{_WEEKLY_VALUATION_MIN_REGULAR_SPACING_RATIO:.0%} of adjacent "
            f"spacings in the {_WEEKLY_VALUATION_MIN_SPACING_DAYS}- to "
            f"{_WEEKLY_VALUATION_MAX_REGULAR_SPACING_DAYS}-day weekly band"
        )
    actual_start = min(observed_dates)
    actual_end = max(observed_dates)
    if str(coverage_range.get("start")) != actual_start:
        return "weekly valuation coverage start does not match the series"
    if str(coverage_range.get("end")) != actual_end:
        return "weekly valuation coverage end does not match the series"
    for field, expected in (
        ("coverage_start", actual_start),
        ("coverage_end", actual_end),
    ):
        if metadata.get(field) is not None and str(metadata.get(field)) != expected:
            return f"weekly valuation metadata {field} does not match the series"
    if value.get("coverage_start") is not None and str(
        value.get("coverage_start")
    ) != actual_start:
        return "valuation coverage_start does not match the series"
    if value.get("coverage_end") is not None and str(
        value.get("coverage_end")
    ) != actual_end:
        return "valuation coverage_end does not match the series"
    if value.get("as_of") is not None and str(value.get("as_of")) != actual_end:
        return "valuation as_of does not match the series"
    top_counts = value.get("point_counts")
    if top_counts is not None and top_counts != point_counts:
        return "valuation point_counts do not match metadata"
    try:
        declared_total = int(metadata.get("point_count", -1))
    except (TypeError, ValueError):
        return "weekly valuation point count is invalid"
    if declared_total != max(
        int(point_counts.get("trailing_pe_weekly", 0)),
        int(point_counts.get("forward_pe_weekly", 0)),
    ):
        return "weekly valuation point count does not match the series"
    if value.get("point_count") is not None:
        try:
            top_point_count = int(value.get("point_count"))
        except (TypeError, ValueError):
            return "valuation point_count is invalid"
        if top_point_count != declared_total:
            return "valuation point_count does not match metadata"
    return None


def _empty_weekly_valuation_payload(
    canonical: str,
    *,
    error: str | None,
) -> dict[str, Any]:
    """Return a typed empty response that cannot be confused with a snapshot."""

    metadata = {
        "period": "5y",
        "frequency": "weekly",
        "point_count": 0,
        "point_counts": {
            "trailing_pe_weekly": 0,
            "forward_pe_weekly": 0,
        },
        "coverage_start": None,
        "coverage_end": None,
        "range": {"start": None, "end": None},
    }
    return {
        "symbol": canonical,
        "schema_version": VALUATION_HISTORY_SCHEMA_VERSION,
        "period": "5y",
        "frequency": "weekly",
        "as_of": None,
        "coverage_start": None,
        "coverage_end": None,
        "point_count": metadata["point_count"],
        "point_counts": metadata["point_counts"],
        "metadata": metadata,
        "anchor_count": 0,
        "series": {
            "trailing_pe_weekly": [],
            "forward_pe_weekly": [],
        },
        "methodology": None,
        "limitations": (
            "Weekly valuation history is unavailable; legacy snapshots "
            "remain available in the stock detail payload."
        ),
        "backtest_safe": False,
        "provenance": [],
        "error": error,
    }


def _downsample_points(
    points: Sequence[Mapping[str, Any]], *, maximum: int = 160
) -> list[dict[str, Any]]:
    if len(points) <= maximum:
        return [dict(item) for item in points]
    if maximum < 2:
        return [dict(points[-1])]
    last_index = len(points) - 1
    indices = {
        round(position * last_index / (maximum - 1)) for position in range(maximum)
    }
    return [dict(points[index]) for index in sorted(indices)]
