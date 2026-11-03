"""Export public, cache-backed API snapshots for a static cloud deployment.

Default operation never fetches provider data. --refresh explicitly refreshes
screening data; --history-limit explicitly permits that many on-demand charts.
The output contains market research only, never user portfolio holdings.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from math import isfinite
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quant_engine.models import PriceHistory, Provenance, json_safe, utc_now_iso
from quant_engine.service import (
    CHART_HISTORY_NAMESPACE,
    PRICE_NAMESPACE,
    PRICE_SCHEMA_VERSION,
    VALUATION_HISTORY_NAMESPACE,
    SignalService,
    _chart_history_payload,
    _empty_weekly_valuation_payload,
    _is_weekly_valuation_cache_record,
)

EXPORT_FORMAT = "sp500-quant-cloud-v1"
RISK_NAMESPACE = "cloud_risk_daily_v1"
EXTRA_ASSETS = {
    "SPY": "SPDR S&P 500 ETF", "TLT": "iShares 20+ Year Treasury Bond ETF",
    "LQD": "iShares Investment Grade Corporate Bond ETF", "VCIT": "Vanguard Intermediate-Term Corporate Bond ETF",
    "GLD": "SPDR Gold Shares", "QQQ": "Invesco QQQ Trust", "IVV": "iShares Core S&P 500 ETF",
    "VOO": "Vanguard S&P 500 ETF", "SGOV": "iShares 0-3 Month Treasury Bond ETF",
    "BND": "Vanguard Total Bond Market ETF", "SCHD": "Schwab US Dividend Equity ETF",
}
RISK_SYMBOLS = tuple(dict.fromkeys([*EXTRA_ASSETS, "AAPL", "MSFT", "GOOG", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "CVX", "PEP", "SPGI", "KMB", "PRU", "WFC", "PG", "VICI"]))
PORTFOLIO_NAMESPACE = "cloud_public_portfolio_v1"
_SYMBOL = re.compile(r"^[A-Z0-9^][A-Z0-9.^=-]{0,19}$")
_LOCAL_PATH = re.compile(
    r"(?<![A-Za-z])(?:[A-Za-z]:[\\/]|\\\\|file://|/(?:Users|home|tmp|var|private)/)[^\s\"<>]*",
    re.IGNORECASE,
)


def public_json(value: Any) -> Any:
    """Keep source URLs and data lineage, but never publish local file paths."""
    value = json_safe(value)
    if isinstance(value, dict):
        return {
            key: public_json(item)
            for key, item in value.items()
            if key.lower() not in {"source_path", "cache_path", "file_path"}
        }
    if isinstance(value, list):
        return [public_json(item) for item in value]
    if isinstance(value, str):
        return _LOCAL_PATH.sub("[local path omitted]", value)
    return value


def cached_history(service: SignalService, symbol: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Use the engine's chart serializer without invoking its fetching method."""
    record = service.cache.get(
        CHART_HISTORY_NAMESPACE, symbol, max_age_seconds=service.price_ttl_seconds
    )
    history = (
        PriceHistory.from_dict(record.payload)
        if record and record.schema_version == 1
        else PriceHistory(
            symbol=symbol, dates=(), adjusted_closes=(),
            provenance=Provenance(source="cloud snapshot cache", retrieved_at=""),
            error="Chart history is not cached; no on-demand fetch was requested.",
        )
    )
    valuation = service.cache.get(
        VALUATION_HISTORY_NAMESPACE, symbol,
        max_age_seconds=service.price_ttl_seconds,
    )
    valid_valuation = valuation is not None and _is_weekly_valuation_cache_record(valuation)
    payload = _chart_history_payload(
        history,
        cache_status=("fresh" if record.fresh else "stale") if record and record.schema_version == 1 else "miss",
        valuation_history=(
            dict(valuation.payload) if valid_valuation
            else _empty_weekly_valuation_payload(symbol, error="No valid weekly valuation cache.")
        ),
        valuation_cache_status=("fresh" if valuation.fresh else "stale") if valid_valuation else "miss",
    )
    daily = [
        {"date": day, "value": close}
        for day, close in zip(history.dates, history.adjusted_closes)
        if close > 0
    ]
    return payload, daily


def _validate_output(output: Path) -> None:
    if output in {ROOT, Path.home().resolve(), Path(output.anchor)}:
        raise ValueError("Choose a dedicated export directory, not a workspace/home/filesystem root.")
    if output.exists() and (not output.is_dir() or output.is_symlink()):
        raise ValueError("Export output must be a regular dedicated directory.")
    if output.exists() and any(output.iterdir()):
        marker = output / "manifest.json"
        try:
            manifest = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ValueError("Refusing to overwrite a nonempty directory without an exporter manifest.") from None
        if not isinstance(manifest, dict) or manifest.get("format") != EXPORT_FORMAT:
            raise ValueError("Existing directory is not a managed cloud snapshot export.")
    for subdirectory in ("stocks", "histories", "risk"):
        target = output / subdirectory
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            raise ValueError(f"Unsafe export subdirectory: {subdirectory}")


def refresh_public_portfolio(service: SignalService) -> dict[str, str]:
    """Persist public FX/gold/catalog data only; never construct user holdings."""
    from portfolio_service import KrxCatalogProvider, NaverGoldProvider, YahooFinanceProvider, filter_kospi_rows

    status = {}
    provider = YahooFinanceProvider()
    for key, fetch in (("usd_krw", lambda: provider.get_usdkrw()), ("gold", lambda: NaverGoldProvider().quote()), ("catalog", lambda: KrxCatalogProvider().get_catalog())):
        try:
            value = fetch()
            if key == "catalog":
                payload = {"catalog": filter_kospi_rows(value.rows, "", len(value.rows)), "source": value.source}
            else:
                payload = {"value" if key == "usd_krw" else "price": value.price, "as_of": value.as_of, "source": value.source.to_dict()}
            service.cache.put(PORTFOLIO_NAMESPACE, key, payload)
            status[key] = "updated"
        except Exception as exc:
            # Preserve any previous successful cache, expose no private exception text.
            status[key] = f"failed:{type(exc).__name__}"
    def fetch_quote(symbol):
        try:
            value = provider.quote(symbol)
            if isinstance(value.price, (int, float)) and isfinite(value.price) and value.price > 0 and isinstance(value.as_of, str):
                service.cache.put(PORTFOLIO_NAMESPACE, f"quote:{symbol}", {"price": value.price, "currency": value.currency, "as_of": value.as_of, "source": value.source.to_dict()})
        except Exception:
            pass  # Preserve previous timestamped public quotes on partial failure.
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(fetch_quote, [*EXTRA_ASSETS, "005930.KS", "000660.KS", "035420.KS", "035720.KS"]))
    return status


def export_snapshot(
    service: SignalService,
    output: str | Path,
    *,
    refresh: str = "none",
    history_limit: int = 0,
    portfolio_refresh: bool = False,
) -> dict[str, Any]:
    if refresh not in {"none", "quick", "full"}:
        raise ValueError("refresh must be none, quick or full")
    if history_limit < 0:
        raise ValueError("history_limit must be nonnegative")
    raw_output = Path(output)
    if raw_output.is_symlink():
        raise ValueError("Export output may not be a symbolic link.")
    destination = raw_output.resolve()
    _validate_output(destination)

    report = None
    if refresh != "none":
        report = service.refresh(prices=True, fundamentals=refresh == "full")
    result = service.screen(allow_fetch=False)
    symbols = sorted(signal.company.symbol for signal in result.signals)
    if any(not _SYMBOL.fullmatch(symbol) for symbol in symbols):
        raise ValueError("An unsafe stock symbol cannot be used as an asset filename.")
    records = service.cache.get_many(CHART_HISTORY_NAMESPACE, symbols, max_age_seconds=service.price_ttl_seconds)
    megacaps = {"AAPL", "MSFT", "GOOG", "GOOGL", "AMZN", "META", "NVDA", "TSLA"}
    # Missing/old charts rotate across scheduled runs instead of always fetching A.. .
    history_order = sorted(symbols, key=lambda symbol: (
        bool(records.get(symbol) and records[symbol].fresh),
        0 if symbol in megacaps else 1,
        records[symbol].fetched_at if symbol in records else "",
        symbol,
    ))
    requested_histories = history_order[:history_limit]
    for symbol in requested_histories:
        service.get_chart_history(symbol)
    benchmark_status = "not_requested"
    if history_limit:
        try:
            benchmark = service.provider.fetch_price_history(["SPY", "KRW=X"], period="3y")
            for symbol in ("SPY", "KRW=X"):
                history = benchmark.get(symbol)
                if history and history.adjusted_closes:
                    service.cache.put(CHART_HISTORY_NAMESPACE, symbol, history.to_dict(), source_as_of=history.dates[-1], schema_version=1)
            benchmark_status = "updated" if benchmark.get("SPY") and benchmark["SPY"].adjusted_closes else "unavailable"
        except Exception as exc:
            benchmark_status = f"failed:{type(exc).__name__}"
    portfolio_refresh_status = refresh_public_portfolio(service) if portfolio_refresh else {}
    if portfolio_refresh:
        try:
            for symbol, history in service.provider.fetch_price_history(RISK_SYMBOLS, period="5y").items():
                if history.adjusted_closes:
                    service.cache.put(RISK_NAMESPACE, symbol, history.to_dict(), source_as_of=history.dates[-1], schema_version=1)
        except Exception as exc:
            portfolio_refresh_status["risk_prices"] = f"failed:{type(exc).__name__}"

    generated_at = utc_now_iso()
    assets: dict[str, Any] = {
        "dashboard.json": result.to_dict(),
        "rules.json": service.get_rules(),
        "catalog.json": {
            "generated_at": generated_at,
            "symbols": [member.to_dict() for member in result.universe.members],
        },
    }
    prices: dict[str, Any] = {}
    quotes: dict[str, Any] = {}
    history_counts = {"fresh": 0, "stale": 0, "miss": 0}
    for signal in result.signals:
        symbol = signal.company.symbol
        assets[f"stocks/{symbol}.json"] = signal.to_dict()
        history, daily = cached_history(service, symbol)
        assets[f"histories/{symbol}.json"] = history
        prices[symbol] = daily
        assets[f"risk/{symbol}.json"] = {"symbol": symbol, "frequency": "daily", "adjusted": True, "prices": daily, "source": "Yahoo adjusted daily chart cache", "as_of": daily[-1]["date"] if daily else None}
        history_counts[history["cache_status"]] += 1
        metric = signal.metrics.get("current_price")
        quotes[symbol] = {
            "price": metric.value if metric else None,
            "currency": "USD",
            "as_of": metric.as_of if metric else None,
            "status": metric.status.value if metric else "missing",
            "source": {"provider": "screening cache", "cache_only": True},
        }

    # SPY is not a roster company, but its cached daily closes are the benchmark.
    for symbol in ("SPY", "USDKRW=X", "KRW=X"):
        _, daily = cached_history(service, symbol)
        prices[symbol] = daily
        record = service.cache.get(PRICE_NAMESPACE, symbol, max_age_seconds=service.price_ttl_seconds)
        metric = record.payload.get("current_price", {}) if record and record.schema_version >= PRICE_SCHEMA_VERSION else {}
        if isinstance(metric, Mapping) and metric.get("value") is not None:
            quotes[symbol] = {
                "price": metric["value"], "currency": "KRW" if symbol != "SPY" else "USD",
                "as_of": metric.get("as_of"), "status": "ok" if record.fresh else "stale",
                "source": {"provider": "screening cache", "cache_only": True},
            }
        elif daily:
            quotes[symbol] = {
                "price": daily[-1]["value"], "currency": "KRW" if symbol != "SPY" else "USD",
                "as_of": daily[-1]["date"], "status": "stale",
                "source": {"provider": "chart cache", "cache_only": True},
            }

    fx_quote = quotes.get("USDKRW=X") or quotes.get("KRW=X") or {}
    for symbol in [*EXTRA_ASSETS, "005930.KS", "000660.KS", "035420.KS", "035720.KS"]:
        quote_record = service.cache.get(PORTFOLIO_NAMESPACE, f"quote:{symbol}", max_age_seconds=service.price_ttl_seconds)
        if quote_record:
            quotes[symbol] = dict(quote_record.payload)
    for symbol in RISK_SYMBOLS:
        record = service.cache.get(RISK_NAMESPACE, symbol, max_age_seconds=service.price_ttl_seconds)
        if not record:
            continue
        history = PriceHistory.from_dict(record.payload)
        daily = [{"date": day, "value": close} for day, close in zip(history.dates, history.adjusted_closes) if close > 0]
        prices[symbol] = daily
        assets[f"risk/{symbol}.json"] = {"symbol": symbol, "frequency": "daily", "adjusted": True, "prices": daily, "source": "Yahoo Finance via yfinance; adjusted daily close", "as_of": daily[-1]["date"] if daily else None}
        if symbol in EXTRA_ASSETS and daily:
            # Adjusted prices are valid for returns, not a raw current quote.
            # The latest adjustment factor normally equals one, but do not assume it.
            metric_record = service.cache.get(PRICE_NAMESPACE, symbol, max_age_seconds=service.price_ttl_seconds)
            raw_metric = metric_record.payload.get("current_price", {}) if metric_record else {}
            if raw_metric.get("value") is not None:
                quotes[symbol] = {"price": raw_metric["value"], "currency": "USD", "as_of": raw_metric.get("as_of"), "source": {"provider": "Yahoo price metrics cache"}}
    fx = {"symbol": "USDKRW=X", "rate": fx_quote.get("price"), "as_of": fx_quote.get("as_of"), "status": fx_quote.get("status", "missing")}
    assets["market-data.json"] = {"generated_at": generated_at, "source": "cached", "quotes": quotes, "prices": prices, "fx": fx}
    assets["portfolio.json"] = {
        "generated_at": generated_at,
        "catalog": [{"symbol": member.symbol, "name": member.name, "market": "us", "currency": "USD", "quantity_unit": "share"} for member in result.universe.members],
        "quotes": quotes,
        "usd_krw": {"value": fx["rate"], "as_of": fx["as_of"], "source": {"provider": "screening cache", "status": fx["status"], "cache_only": True}},
        "gold": {"price": None, "as_of": None, "source": {"status": "missing", "cache_only": True}},
    }
    assets["portfolio.json"]["catalog"].extend({"symbol": symbol, "name": name, "market": "us", "currency": "USD", "quantity_unit": "share", "asset_type": "ETF"} for symbol, name in EXTRA_ASSETS.items() if symbol not in symbols)
    for key in ("usd_krw", "gold", "catalog"):
        record = service.cache.get(PORTFOLIO_NAMESPACE, key, max_age_seconds=86400)
        if record:
            payload = dict(record.payload)
            if key == "catalog":
                assets["portfolio.json"]["catalog"].extend(payload.get("catalog", []))
            else:
                source = dict(payload.get("source", {}))
                source.update({"status": "ok" if record.fresh else "stale", "cache_only": True})
                payload["source"] = source
                assets["portfolio.json"][key] = payload
    stored_fx = assets["portfolio.json"]["usd_krw"]
    if stored_fx.get("value") is not None:
        fx.update({"rate": stored_fx["value"], "as_of": stored_fx.get("as_of"), "status": stored_fx["source"]["status"]})
    manifest = {
        "format": EXPORT_FORMAT, "generated_at": generated_at,
        "refresh": {"mode": refresh, "history_limit": history_limit, "history_requested": requested_histories, "benchmark": benchmark_status, "portfolio_requested": portfolio_refresh, "portfolio": portfolio_refresh_status, "report": json_safe(report)},
        "counts": {"stocks": len(symbols), "histories": len(symbols), "history_cache": history_counts},
        "symbols": symbols, "files": sorted(assets),
        "contains_portfolio_holdings": False,
        "notice": "Research snapshot; missing values remain null. No live cloud-provider fetches.",
    }
    assets["manifest.json"] = manifest
    destination.mkdir(parents=True, exist_ok=True)
    # Stage/validate every JSON before publishing. Never remove unrelated files.
    with tempfile.TemporaryDirectory(prefix=".cloud-export-", dir=destination.parent) as staging:
        staged = Path(staging)
        for name, payload in assets.items():
            target = staged / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(public_json(payload), ensure_ascii=False, allow_nan=False, separators=(",", ":")), encoding="utf-8")
        for name in assets:  # manifest is last, so it marks completed publication.
            target = destination / name
            if target.is_symlink():
                raise ValueError(f"Refusing to overwrite a symbolic-link asset: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            (staged / name).replace(target)
    return public_json(manifest)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "cloud-data")
    parser.add_argument("--refresh", choices=("none", "quick", "full"), default="none")
    parser.add_argument("--history-limit", type=int, default=0)
    parser.add_argument("--portfolio-refresh", action="store_true", help="Explicitly fetch and persist public USD/KRW, gold and KOSPI catalog data")
    args = parser.parse_args(argv)
    if args.history_limit < 0:
        parser.error("--history-limit must be nonnegative")
    manifest = export_snapshot(SignalService(), args.output, refresh=args.refresh, history_limit=args.history_limit, portfolio_refresh=args.portfolio_refresh)
    print(json.dumps({key: manifest[key] for key in ("format", "generated_at", "counts", "refresh")}, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
