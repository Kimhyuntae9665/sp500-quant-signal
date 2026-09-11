"""Build auditable weekly valuation charts from free convenience sources.

StockAnalysis exposes quarterly ``Last Close Price``, ``PE Ratio``, and
``Forward PE`` observations.  Those observations imply the earnings
denominators used by the vendor at each quarterly anchor:

``implied EPS = anchor close / anchor P/E``.

For charting only, this module holds each valid quarterly denominator until
the next anchor and divides each week's last Yahoo close by that denominator.
When a quarterly P/E is undefined or non-positive (for example because
earnings are negative), that series omits the affected weeks until a later
valid anchor; it never fills or carries a stale P/E/EPS through the undefined
quarter.  The result is a weekly price-sensitive valuation proxy, not a
historical weekly analyst-consensus feed and not a point-in-time-safe backtest
dataset.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime
from io import StringIO
from math import isfinite
import re
from typing import Any, Mapping, Sequence

from ..models import Provenance, utc_now_iso


STOCKANALYSIS_QUARTERLY_URL = (
    "https://stockanalysis.com/stocks/{symbol}/financials/ratios/?p=quarterly"
)
YAHOO_HISTORY_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_FULL_DATE_RE = re.compile(r"\b([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})\b")
_MONTH_YEAR_RE = re.compile(r"\b([A-Z][a-z]{2})\s*'(\d{2})\b")
_TARGET_ROWS = ("Last Close Price", "PE Ratio", "Forward PE")

# The cache namespace is versioned in ``service.py`` as well.  Keep an
# explicit payload version here so a cached sparse/legacy snapshot cannot be
# mistaken for this provider's weekly contract.
WEEKLY_VALUATION_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class ValuationAnchor:
    """One quarterly vendor observation used to infer earnings denominators."""

    period_end: str
    close: float | None
    trailing_pe: float | None
    forward_pe: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.period_end,
            "close": self.close,
            "trailing_pe": self.trailing_pe,
            "forward_pe": self.forward_pe,
        }


def fetch_weekly_valuation_history(symbol: str) -> dict[str, Any]:
    """Return approximately five years of weekly P/E chart observations.

    The returned mapping is JSON-safe and intentionally keeps data-source and
    methodology metadata beside the series so the UI cannot present the proxy
    as a vendor-provided weekly forward-consensus history.
    """

    canonical = str(symbol).strip().upper()
    if not canonical:
        raise ValueError("symbol is required")
    vendor_symbol = canonical.replace(".", "-")
    # StockAnalysis keeps exchange-style class separators in its current URL
    # slugs (for example BRK.B and BF.B), while Yahoo uses hyphens.
    stockanalysis_symbol = canonical.lower()
    source_url = STOCKANALYSIS_QUARTERLY_URL.format(
        symbol=stockanalysis_symbol
    )
    retrieved_at = utc_now_iso()

    html = _download_stockanalysis_html(source_url)
    parsed_anchors = parse_quarterly_valuation_html(html)
    daily_closes = _download_yahoo_raw_closes(vendor_symbol)
    anchors = align_anchor_closes(parsed_anchors, daily_closes)
    if len(anchors) < 4:
        raise ValueError(
            f"StockAnalysis returned only {len(anchors)} usable quarterly anchors"
        )
    weekly_closes = weekly_last_closes(daily_closes)
    trailing_points, forward_points = calculate_weekly_valuation_series(
        weekly_closes, anchors
    )
    if not trailing_points and not forward_points:
        raise ValueError(
            "no weekly P/E values could be aligned to the quarterly anchors"
        )

    all_dates = [
        str(item["date"]) for item in (*trailing_points, *forward_points)
    ]
    as_of = max(all_dates) if all_dates else None
    coverage_start = min(all_dates) if all_dates else None
    metadata = weekly_valuation_metadata(trailing_points, forward_points)
    stockanalysis_provenance = Provenance(
        source="StockAnalysis quarterly ratios",
        source_url=source_url,
        retrieved_at=retrieved_at,
        as_of=anchors[-1].period_end,
        basis=(
            "quarterly Period Ending, PE Ratio, Forward PE, and Last Close "
            "Price when available"
        ),
        official=False,
        notes=(
            "StockAnalysis identifies the underlying data provider on each company page.",
            "Quarterly anchors are vendor-calculated ratios, not SEC-reported ratios.",
            "A missing vendor anchor price is filled with the nearest prior Yahoo Close.",
            "Undefined or non-positive P/E anchors omit affected weeks; no P/E/EPS interpolation or stale carry is used.",
            "Period-ending dates may precede public release dates; not point-in-time backtest safe.",
        ),
    )
    yahoo_provenance = Provenance(
        source="Yahoo Finance via yfinance",
        source_url=YAHOO_HISTORY_URL.format(symbol=vendor_symbol),
        retrieved_at=retrieved_at,
        as_of=as_of,
        basis=(
            "daily Close, period=5y, interval=1d, auto_adjust=False; "
            "last available trading close in each ISO week"
        ),
        official=False,
        notes=(
            "Unofficial convenience source; fields and corporate-action handling may change.",
        ),
    )
    return {
        "symbol": canonical,
        "schema_version": WEEKLY_VALUATION_SCHEMA_VERSION,
        "period": "5y",
        "frequency": "weekly",
        "as_of": as_of,
        "coverage_start": coverage_start,
        "coverage_end": as_of,
        "point_count": metadata["point_count"],
        "point_counts": metadata["point_counts"],
        "metadata": metadata,
        "anchor_frequency": "quarterly",
        "anchor_count": len(anchors),
        "anchors": [item.to_dict() for item in anchors],
        "series": {
            "trailing_pe_weekly": trailing_points,
            "forward_pe_weekly": forward_points,
        },
        "methodology": (
            "Each weekly value equals that week's last Yahoo Close divided by "
            "the latest quarterly implied EPS. Implied EPS equals the "
            "StockAnalysis quarterly Last Close Price divided by its P/E. "
            "If that quarterly P/E is undefined or non-positive, the affected "
            "series' weeks are omitted until a valid P/E anchor returns; no "
            "interpolation or stale denominator carry is applied."
        ),
        "limitations": (
            "Forward P/E is a weekly price-sensitive proxy using quarterly "
            "forward-EPS anchors; it is not a historical weekly consensus feed. "
            "Undefined P/E weeks are omitted rather than filled, so trailing "
            "and forward series can have different counts and long gaps."
        ),
        "backtest_safe": False,
        "provenance": [
            stockanalysis_provenance.to_dict(),
            yahoo_provenance.to_dict(),
        ],
        "error": None,
    }


def parse_quarterly_valuation_html(html: str) -> tuple[ValuationAnchor, ...]:
    """Parse StockAnalysis quarterly ratio tables without assuming one table."""

    import pandas as pd

    if not str(html).strip():
        raise ValueError("StockAnalysis response was empty")
    tables = pd.read_html(StringIO(str(html)), displayed_only=False)
    values_by_date: dict[str, dict[str, float | None]] = {}

    for table in tables:
        if table.empty or len(table.columns) < 2:
            continue
        row_labels = [str(value).strip() for value in table.iloc[:, 0].tolist()]
        for column_index, column in enumerate(table.columns[1:], start=1):
            period_end = _period_end_from_column(column)
            if period_end is None:
                continue
            bucket = values_by_date.setdefault(period_end, {})
            for row_index, label in enumerate(row_labels):
                if label not in _TARGET_ROWS:
                    continue
                bucket[label] = _positive_float(
                    table.iloc[row_index, column_index]
                )

    anchors: list[ValuationAnchor] = []
    for period_end, values in values_by_date.items():
        close = values.get("Last Close Price")
        trailing_pe = values.get("PE Ratio")
        forward_pe = values.get("Forward PE")
        if trailing_pe is None and forward_pe is None:
            continue
        anchors.append(
            ValuationAnchor(
                period_end=period_end,
                close=close,
                trailing_pe=trailing_pe,
                forward_pe=forward_pe,
            )
        )
    anchors.sort(key=lambda item: item.period_end)
    return tuple(anchors)


def align_anchor_closes(
    anchors: Sequence[ValuationAnchor],
    daily_closes: Sequence[tuple[str, float]],
) -> tuple[ValuationAnchor, ...]:
    """Fill a missing vendor anchor price with the nearest prior Yahoo Close."""

    ordered_closes = [
        (str(day)[:10], close)
        for day, raw_close in sorted(daily_closes, key=lambda item: item[0])
        if (close := _positive_float(raw_close)) is not None
    ]
    output: list[ValuationAnchor] = []
    close_index = -1
    for anchor in sorted(anchors, key=lambda item: item.period_end):
        while (
            close_index + 1 < len(ordered_closes)
            and ordered_closes[close_index + 1][0] <= anchor.period_end
        ):
            close_index += 1
        close = _positive_float(anchor.close)
        if close is None and close_index >= 0:
            close = ordered_closes[close_index][1]
        if close is None:
            continue
        output.append(
            ValuationAnchor(
                period_end=anchor.period_end,
                close=close,
                trailing_pe=anchor.trailing_pe,
                forward_pe=anchor.forward_pe,
            )
        )
    return tuple(output)


def weekly_last_closes(
    daily_closes: Sequence[tuple[str, float]],
) -> tuple[tuple[str, float], ...]:
    """Keep the final available trading close in every ISO week."""

    weekly: dict[tuple[int, int], tuple[str, float]] = {}
    for raw_day, raw_close in sorted(daily_closes, key=lambda item: item[0]):
        try:
            day = date.fromisoformat(str(raw_day)[:10])
            close = float(raw_close)
        except (TypeError, ValueError):
            continue
        if day.weekday() >= 5:
            # Yahoo's daily feed should not contain weekends, but a malformed
            # fixture/cache row must not become a fake weekly observation.
            continue
        if not isfinite(close) or close <= 0:
            continue
        iso = day.isocalendar()
        weekly[(iso.year, iso.week)] = (day.isoformat(), close)
    return tuple(weekly[key] for key in sorted(weekly))


def calculate_weekly_valuation_series(
    weekly_closes: Sequence[tuple[str, float]],
    anchors: Sequence[ValuationAnchor],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply the latest available quarterly denominator to each weekly close."""

    ordered_anchors = sorted(anchors, key=lambda item: item.period_end)
    trailing: list[dict[str, Any]] = []
    forward: list[dict[str, Any]] = []
    if not ordered_anchors:
        return trailing, forward

    anchor_index = -1
    for raw_day, raw_close in sorted(weekly_closes, key=lambda item: item[0]):
        day = str(raw_day)[:10]
        close = _positive_float(raw_close)
        if close is None:
            continue
        while (
            anchor_index + 1 < len(ordered_anchors)
            and ordered_anchors[anchor_index + 1].period_end <= day
        ):
            anchor_index += 1
        if anchor_index < 0:
            continue
        anchor = ordered_anchors[anchor_index]
        trailing_eps = (
            anchor.close / anchor.trailing_pe
            if anchor.close is not None
            and anchor.trailing_pe is not None
            and anchor.trailing_pe > 0
            else None
        )
        forward_eps = (
            anchor.close / anchor.forward_pe
            if anchor.close is not None
            and anchor.forward_pe is not None
            and anchor.forward_pe > 0
            else None
        )
        if trailing_eps is not None and trailing_eps > 0:
            trailing.append(
                {
                    "date": day,
                    "value": round(close / trailing_eps, 6),
                    "anchor_date": anchor.period_end,
                }
            )
        if forward_eps is not None and forward_eps > 0:
            forward.append(
                {
                    "date": day,
                    "value": round(close / forward_eps, 6),
                    "anchor_date": anchor.period_end,
                }
            )
    return trailing, forward


def weekly_valuation_metadata(
    trailing_points: Sequence[Mapping[str, Any]],
    forward_points: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Describe weekly series counts and coverage without hiding sparse data."""

    point_counts = {
        "trailing_pe_weekly": len(trailing_points),
        "forward_pe_weekly": len(forward_points),
    }
    all_dates = [
        str(item.get("date", item.get("period_end", "")))[:10]
        for item in (*trailing_points, *forward_points)
        if item.get("date", item.get("period_end"))
    ]
    coverage_start = min(all_dates) if all_dates else None
    coverage_end = max(all_dates) if all_dates else None
    return {
        "period": "5y",
        "frequency": "weekly",
        "point_count": max(point_counts.values(), default=0),
        "point_counts": point_counts,
        "coverage_start": coverage_start,
        "coverage_end": coverage_end,
        "range": {"start": coverage_start, "end": coverage_end},
    }


def _download_stockanalysis_html(url: str) -> str:
    import requests

    response = requests.get(url, headers=_BROWSER_HEADERS, timeout=25)
    response.raise_for_status()
    return response.text


def _download_yahoo_raw_closes(
    vendor_symbol: str,
) -> tuple[tuple[str, float], ...]:
    import yfinance as yf

    frame = yf.download(
        vendor_symbol,
        period="5y",
        interval="1d",
        auto_adjust=False,
        actions=False,
        repair=True,
        progress=False,
        threads=False,
        group_by="column",
        timeout=25,
    )
    if frame is None or getattr(frame, "empty", True):
        raise ValueError("Yahoo returned no five-year daily Close history")
    series = _close_series(frame, vendor_symbol)
    output: list[tuple[str, float]] = []
    for index, raw_value in series.dropna().items():
        close = _positive_float(raw_value)
        if close is None:
            continue
        if hasattr(index, "date"):
            day = index.date().isoformat()
        else:
            day = str(index)[:10]
        output.append((day, close))
    if not output:
        raise ValueError("Yahoo five-year history contained no usable Close values")
    return tuple(output)


def _close_series(frame: Any, vendor_symbol: str) -> Any:
    columns = frame.columns
    if getattr(columns, "nlevels", 1) > 1:
        for candidate in (
            ("Close", vendor_symbol),
            (vendor_symbol, "Close"),
        ):
            if candidate in columns:
                return frame[candidate]
        first_level = list(columns.get_level_values(0))
        second_level = list(columns.get_level_values(1))
        if "Close" in first_level:
            result = frame["Close"]
            return result.iloc[:, 0] if hasattr(result, "iloc") and result.ndim > 1 else result
        if "Close" in second_level:
            result = frame.xs("Close", axis=1, level=1)
            return result.iloc[:, 0] if result.ndim > 1 else result
    if "Close" not in columns:
        raise ValueError("Yahoo history did not contain a Close column")
    result = frame["Close"]
    return result.iloc[:, 0] if hasattr(result, "iloc") and result.ndim > 1 else result


def _period_end_from_column(column: Any) -> str | None:
    parts = column if isinstance(column, tuple) else (column,)
    text = " ".join(str(part) for part in parts if str(part) != "nan")
    full_dates = _FULL_DATE_RE.findall(text)
    if full_dates:
        try:
            return datetime.strptime(full_dates[-1], "%b %d, %Y").date().isoformat()
        except ValueError:
            pass
    month_year = _MONTH_YEAR_RE.search(text)
    if not month_year:
        return None
    try:
        month = datetime.strptime(month_year.group(1), "%b").month
        year = 2000 + int(month_year.group(2))
    except ValueError:
        return None
    return date(year, month, monthrange(year, month)[1]).isoformat()


def _positive_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "").replace("x", "")
        if cleaned in {"", "-", "—", "N/A", "nan"}:
            return None
    else:
        cleaned = value
    try:
        number = float(cleaned)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) and number > 0 else None
