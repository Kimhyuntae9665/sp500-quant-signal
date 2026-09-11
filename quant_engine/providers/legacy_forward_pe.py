"""Transparent adapter for the user's optional legacy valuation cache.

The source file contains undated ``Forward PE_hist`` and ``PE Ratio_hist``
arrays. This loader never fabricates fiscal dates. It validates that element
zero matches each record's contemporaneous value, drops that current element,
and labels up to five subsequent positive observations as undated legacy FY
slots. The scoring layer still limits its 3Y forward-P/E calculation to the
three most recent comparable observations.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from math import isclose, isfinite
from pathlib import Path
from typing import Any, Mapping

from ..models import DatedValue, Provenance


@dataclass(frozen=True)
class LegacyForwardPECache:
    history_by_symbol: Mapping[str, tuple[DatedValue, ...]]
    trailing_pe_by_symbol: Mapping[str, tuple[DatedValue, ...]]
    dividend_yield_by_symbol: Mapping[str, tuple[DatedValue, ...]]
    source_path: str
    file_mtime: str
    records_seen: int
    records_loaded: int
    warnings: tuple[str, ...]


def load_legacy_forward_pe_cache(path: str | Path) -> LegacyForwardPECache:
    """Parse a local StockAnalysis ratio snapshot without trusting its dates."""

    source_path = Path(path).expanduser().resolve()
    stat = source_path.stat()
    file_mtime = datetime.fromtimestamp(
        stat.st_mtime, timezone.utc
    ).isoformat().replace("+00:00", "Z")
    with source_path.open("r", encoding="utf-8-sig") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise ValueError("legacy forward-P/E cache root must be a JSON array")

    history: dict[str, tuple[DatedValue, ...]] = {}
    trailing_history: dict[str, tuple[DatedValue, ...]] = {}
    dividend_history: dict[str, tuple[DatedValue, ...]] = {}
    rejected = 0
    for record in payload:
        if not isinstance(record, Mapping):
            rejected += 1
            continue
        symbol = str(record.get("Symbol", "")).strip().upper()
        if symbol:
            dividends = _load_dividend_yield_history(
                record,
                symbol=symbol,
                source_path=source_path,
                file_mtime=file_mtime,
            )
            if dividends:
                dividend_history[symbol] = dividends
        if not symbol:
            rejected += 1
            continue
        forward_values = _load_pe_history(
            record,
            current_key="Forward PE",
            history_key="Forward PE_hist",
            period_type="forward_pe",
            source_path=source_path,
            file_mtime=file_mtime,
        )
        trailing_values = _load_pe_history(
            record,
            current_key="PE Ratio",
            history_key="PE Ratio_hist",
            period_type="trailing_pe",
            source_path=source_path,
            file_mtime=file_mtime,
        )
        if forward_values:
            history[symbol] = forward_values
        else:
            rejected += 1
        if trailing_values:
            trailing_history[symbol] = trailing_values

    warnings = (
        "Loaded up to five undated legacy forward/trailing P-E observations; no exact fiscal dates were invented.",
        "The 3Y forward-P-E score still uses only the three most recent comparable observations.",
        "Loaded validated dividend-yield slots for the 3Y shareholder-return proxy where available.",
        "Legacy inputs are marked stale and retain their local-file mtime and source URL.",
        f"Rejected {rejected} forward-P-E records whose schema/current value/history was not verifiable.",
    )
    return LegacyForwardPECache(
        history_by_symbol=history,
        trailing_pe_by_symbol=trailing_history,
        dividend_yield_by_symbol=dividend_history,
        source_path=str(source_path),
        file_mtime=file_mtime,
        records_seen=len(payload),
        records_loaded=len(history),
        warnings=warnings,
    )


def _load_pe_history(
    record: Mapping[str, Any],
    *,
    current_key: str,
    history_key: str,
    period_type: str,
    source_path: Path,
    file_mtime: str,
) -> tuple[DatedValue, ...]:
    """Load five validated historical P/E slots while preserving missing dates."""

    current = _positive_float(record.get(current_key))
    raw_history = record.get(history_key)
    if current is None or not isinstance(raw_history, list) or not raw_history:
        return ()
    first = _positive_float(raw_history[0])
    if first is None or not isclose(first, current, rel_tol=1e-6, abs_tol=1e-6):
        # A mismatch means array ordering/basis cannot be safely inferred.
        return ()

    source_url = str(record.get("url", "")).strip() or None
    values: list[DatedValue] = []
    for original_offset, raw in enumerate(raw_history[1:], start=1):
        number = _positive_float(raw)
        if number is None:
            continue
        slot = f"legacy_fy_minus_{original_offset}"
        provenance = Provenance(
            source="User-provided StockAnalysis ratio cache (legacy snapshot)",
            source_url=source_url,
            retrieved_at=file_mtime,
            as_of=file_mtime[:10],
            basis=(
                f"{history_key}[{original_offset}] after validating and excluding "
                "the matching current snapshot element; exact fiscal date absent"
            ),
            official=False,
            notes=(
                f"Legacy local file: {source_path}",
                f"File modification time: {file_mtime}",
                "Undated legacy FY observation; exact period end was not inferred.",
                "May be stale; the current P/E metric still comes from the live/cache Yahoo source.",
            ),
        )
        values.append(
            DatedValue(
                period_end=slot,
                value=number,
                period_type=period_type,
                provenance=(provenance,),
            )
        )
        if len(values) == 5:
            break
    return tuple(values)


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) and number > 0 else None


def _nonnegative_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) and number >= 0 else None


def _load_dividend_yield_history(
    record: Mapping[str, Any],
    *,
    symbol: str,
    source_path: Path,
    file_mtime: str,
) -> tuple[DatedValue, ...]:
    """Load the latest three validated dividend-yield slots without dates.

    The legacy ratio snapshot orders its array as current/TTM followed by
    older fiscal slots but does not include exact period labels. We preserve
    that limitation explicitly instead of manufacturing calendar dates.
    """

    current = _nonnegative_float(record.get("Dividend Yield"))
    raw_history = record.get("Dividend Yield_hist")
    if current is None or not isinstance(raw_history, list) or not raw_history:
        return ()
    first = _nonnegative_float(raw_history[0])
    if first is None or not isclose(first, current, rel_tol=1e-6, abs_tol=1e-6):
        return ()

    source_url = str(record.get("url", "")).strip() or None
    values: list[DatedValue] = []
    for original_offset, raw in enumerate(raw_history):
        number = _nonnegative_float(raw)
        if number is None:
            continue
        slot = "legacy_current" if original_offset == 0 else f"legacy_fy_minus_{original_offset}"
        provenance = Provenance(
            source="User-provided StockAnalysis ratio cache (legacy snapshot)",
            source_url=source_url,
            retrieved_at=file_mtime,
            as_of=file_mtime[:10],
            basis=(
                f"Dividend Yield_hist[{original_offset}] after validating the current "
                "snapshot element; exact fiscal date absent"
            ),
            official=False,
            notes=(
                f"Legacy local file: {source_path}",
                f"File modification time: {file_mtime}",
                "Undated legacy yield observation; exact period end was not inferred.",
                "Used only as a stale component of the calculated shareholder-return proxy.",
            ),
        )
        values.append(
            DatedValue(
                period_end=slot,
                value=number,
                period_type="dividend_yield",
                provenance=(provenance,),
            )
        )
        if len(values) == 3:
            break
    return tuple(values) if len(values) == 3 else ()
