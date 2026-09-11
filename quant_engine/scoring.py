"""Deterministic metric calculations and score rules.

All thresholds live in :data:`RULE_DEFINITIONS`; both the evaluator and the
HTTP rules endpoint consume this same structure.  Values are percentages (not
fractions) unless the unit explicitly says otherwise.
"""

from __future__ import annotations

from datetime import date
from math import isfinite
from statistics import median, stdev
from typing import Any, Callable, Mapping, Sequence

from .models import (
    DatedValue,
    MarketOverlay,
    MetricStatus,
    MetricValue,
    PriceHistory,
    PriceMetrics,
    Provenance,
    Recommendation,
    ScoreComponent,
    StockMetrics,
    StockSignal,
    UniverseMember,
    utc_now_iso,
)


RULE_DEFINITIONS: dict[str, Any] = {
    "version": "2.0.0",
    "units": {
        "percent": "percentage points (for example, -30 means -30%)",
        "ratio": "decimal ratio (for example, 0.25 means 25%)",
    },
    "company_rules": [
        {
            "key": "drawdown_52w",
            "label": "52주 고점 대비 하락",
            "metric_key": "drawdown_52_week_pct",
            "unit": "percent",
            "thresholds": [
                {"operator": "<=", "value": -30.0, "points": 3},
                {"operator": "<=", "value": -15.0, "points": 2},
                {"operator": "<=", "value": -5.0, "points": 1},
            ],
            "default_points": 0,
        },
        {
            "key": "pe_gap_3y",
            "label": "3년 Forward PER 괴리",
            "metric_key": "pe_gap_3y_pct",
            "unit": "percent",
            "thresholds": [
                {"operator": "<=", "value": -20.0, "points": 2},
                {"operator": "<=", "value": -10.0, "points": 1},
            ],
            "default_points": 0,
            "note": (
                "현재 forward P/E와 비교 가능한 과거 forward P/E 최대 3개를 비교. "
                "검증된 legacy FY 배열은 정확한 날짜를 만들지 않고 stale로 표시"
            ),
        },
        {
            "key": "distance_200dma",
            "label": "200일 이동평균 이격",
            "metric_key": "distance_200dma_pct",
            "unit": "percent",
            "thresholds": [
                {"operator": "<=", "value": -20.0, "points": 2},
                {"operator": "<=", "value": -5.0, "points": 1},
            ],
            "default_points": 0,
        },
        {
            "key": "eps_growth_yoy",
            "label": "최근 연간 희석 EPS 성장률",
            "metric_key": "eps_growth_yoy_pct",
            "unit": "percent",
            "thresholds": [
                {"operator": ">=", "value": 20.0, "points": 2},
                {"operator": ">=", "value": 5.0, "points": 1},
                {"operator": "<=", "value": -20.0, "points": -2},
                {"operator": "<=", "value": -5.0, "points": -1},
            ],
            "default_points": 0,
            "note": "서로 비교 가능한 최신 두 연간 기간; 직전 EPS가 0 이하이면 N/A",
        },
        {
            "key": "rsi_14",
            "label": "RSI(14)",
            "metric_key": "rsi_14",
            "unit": "index",
            "thresholds": [
                {"operator": ">=", "value": 75.0, "points": -2},
                {"operator": ">=", "value": 65.0, "points": -1},
                {"operator": "<=", "value": 25.0, "points": 1},
            ],
            "default_points": 0,
            "note": "Wilder RSI 방식",
        },
        {
            "key": "shareholder_return_3y",
            "label": "3년 평균 주주환원율 프록시",
            "metric_key": "shareholder_return_3y_avg_pct",
            "unit": "percent",
            "thresholds": [
                {"operator": ">=", "value": 6.0, "points": 3},
                {"operator": ">=", "value": 4.0, "points": 2},
                {"operator": ">=", "value": 2.0, "points": 1},
            ],
            "default_points": 0,
            "note": (
                "최근 3개 배당수익률 평균 + 양(+)의 3년 평균 순주식수 감소율. "
                "현금 자사주매입 수익률이 아닌 계산 프록시이며, 희석은 별도 감점"
            ),
        },
        {
            "key": "shares_dilution_3y",
            "label": "최근 3년 희석주식수 증감",
            "metric_key": "shares_change_3y_pct",
            "unit": "percent",
            "thresholds": [
                {"operator": ">=", "value": 10.0, "points": -3},
                {"operator": ">=", "value": 5.0, "points": -2},
                {"operator": ">=", "value": 1.0, "points": -1},
            ],
            "default_points": 0,
            "note": "희석가중평균주식수의 최신 연도와 3년 전 연도를 비교; 감소는 여기서 중복 가점하지 않음",
        },
        {
            "key": "debt_to_equity",
            "label": "부채/자기자본",
            "metric_key": "debt_to_equity",
            "unit": "ratio",
            "thresholds": [
                {"operator": ">=", "value": 4.0, "points": -2},
                {"operator": ">=", "value": 2.0, "points": -1},
                {"operator": "<=", "value": 0.25, "points": 1},
            ],
            "default_points": 0,
            "sector_exclusions": ["Financials", "REIT"],
            "note": "금융사와 REIT는 업종 구조상 이 단순 기준에서 제외",
        },
        {
            "key": "cash_flow_quality",
            "label": "3년 현금흐름 품질",
            "metric_key": "cash_flow_quality_3y",
            "unit": "ratio",
            "thresholds": [
                {"operator": ">=", "value": 1.2, "points": 2},
                {"operator": ">=", "value": 0.9, "points": 1},
                {"operator": "<=", "value": 0.5, "points": -2},
                {"operator": "<=", "value": 0.75, "points": -1},
            ],
            "default_points": 0,
            "sector_exclusions": ["Financials", "REIT", "Real Estate"],
            "note": (
                "최근 3개 공통 회계연도의 누적 영업현금흐름 ÷ 누적 순이익. "
                "누적 순이익이 0 이하이면 N/A"
            ),
        },
    ],
    "market_rules": [
        {
            "key": "spy_drawdown",
            "label": "SPY 52주 고점 대비 하락",
            "metric_key": "spy_drawdown_52_week_pct",
            "unit": "percent",
            "thresholds": [
                {"operator": "<=", "value": -20.0, "points": 1},
                {"operator": ">", "value": -5.0, "points": -1},
            ],
            "default_points": 0,
        },
        {
            "key": "vix",
            "label": "VIX",
            "metric_key": "vix",
            "unit": "index",
            "thresholds": [
                {"operator": ">=", "value": 30.0, "points": 1},
                {"operator": "<=", "value": 12.0, "points": -1},
            ],
            "default_points": 0,
        },
        {
            "key": "fear_greed",
            "label": "CNN Fear & Greed",
            "metric_key": "fear_greed",
            "unit": "index",
            "thresholds": [
                {"operator": "<=", "value": 25.0, "points": 1},
                {"operator": ">=", "value": 75.0, "points": -1},
            ],
            "default_points": 0,
            "note": "선택적 비공식 편의 데이터; 실패 시 N/A",
        },
    ],
    "recommendations": [
        {"code": "strong_buy", "label": "강력 매수", "min": 10, "max": None},
        {"code": "watch_buy", "label": "매수 관심", "min": 5, "max": 9},
        {"code": "neutral", "label": "중립", "min": -2, "max": 4},
        {"code": "watch_sell", "label": "매도 관심", "min": -7, "max": -3},
        {"code": "sell", "label": "매도", "min": None, "max": -8},
    ],
    "minimum_company_metrics": 5,
    "missing_data_policy": {
        "metric_value": None,
        "score_contribution": None,
        "recommendation_below_minimum": "insufficient_data",
        "eligible_for_ranking_below_minimum": False,
    },
}


def get_rule_definitions() -> dict[str, Any]:
    """Return the JSON-serializable rule schema used by the evaluator."""

    # The structure contains only JSON primitives.  Copy nested containers so
    # callers cannot mutate the live evaluator configuration accidentally.
    import copy

    return copy.deepcopy(RULE_DEFINITIONS)


get_rules = get_rule_definitions


def calculate_price_metrics(history: PriceHistory) -> PriceMetrics:
    """Calculate price, trend, RSI, and skipped-month momentum inputs."""

    pairs = [
        (day, float(close))
        for day, close in zip(history.dates, history.adjusted_closes)
        if _finite_positive(close)
    ]
    provenance = history.provenance
    if not pairs:
        note = history.error or "adjusted close history is unavailable"
        return _missing_price_metrics(history.symbol, provenance, note)

    as_of = pairs[-1][0]
    closes = [item[1] for item in pairs]
    current = closes[-1]
    source_lineage = (provenance,)
    current_metric = MetricValue(
        current,
        "USD",
        as_of=as_of,
        provenance=source_lineage,
        note="Provider-adjusted close; see provenance for the exact adjustment basis",
    )

    # A calendar year avoids assuming every ticker has exactly 252 observations.
    cutoff = _subtract_one_year(as_of)
    year_closes = [close for day, close in pairs if day >= cutoff]
    if not year_closes:
        high_metric = MetricValue.missing("USD", "no observations in 52-week window")
        drawdown_metric = MetricValue.missing(
            "percent", "52-week high cannot be calculated"
        )
    else:
        high = max(year_closes)
        calculated = _calculation_provenance(
            as_of,
            "max adjusted close over the trailing calendar year; current/high - 1",
            provenance,
        )
        high_metric = MetricValue(
            high, "USD", as_of=as_of, provenance=(calculated, provenance)
        )
        drawdown_metric = MetricValue(
            _percent_change(current, high),
            "percent",
            as_of=as_of,
            provenance=(calculated, provenance),
        )

    if len(closes) < 200:
        sma_metric = MetricValue.missing(
            "USD",
            f"requires 200 closes; received {len(closes)}",
            provenance=source_lineage,
            as_of=as_of,
        )
        distance_metric = MetricValue.missing(
            "percent",
            "200DMA distance cannot be calculated",
            provenance=source_lineage,
            as_of=as_of,
        )
    else:
        sma = sum(closes[-200:]) / 200.0
        calculated = _calculation_provenance(
            as_of, "mean of latest 200 adjusted closes; current/SMA200 - 1", provenance
        )
        sma_metric = MetricValue(
            sma, "USD", as_of=as_of, provenance=(calculated, provenance)
        )
        distance_metric = MetricValue(
            _percent_change(current, sma),
            "percent",
            as_of=as_of,
            provenance=(calculated, provenance),
        )

    rsi_value = wilder_rsi(closes, period=14)
    if rsi_value is None:
        rsi_metric = MetricValue.missing(
            "index",
            f"requires at least 15 valid closes; received {len(closes)}",
            provenance=source_lineage,
            as_of=as_of,
        )
    else:
        calculated = _calculation_provenance(as_of, "Wilder RSI, period=14", provenance)
        rsi_metric = MetricValue(
            rsi_value,
            "index",
            as_of=as_of,
            provenance=(calculated, provenance),
        )

    return_6_1, return_12_1, volatility_12m = calculate_skipped_month_momentum(
        pairs,
        provenance,
    )
    return PriceMetrics(
        symbol=history.symbol,
        current_price=current_metric,
        high_52_week=high_metric,
        drawdown_52_week_pct=drawdown_metric,
        sma_200=sma_metric,
        distance_200dma_pct=distance_metric,
        rsi_14=rsi_metric,
        return_6_1_pct=return_6_1,
        return_12_1_pct=return_12_1,
        volatility_12m_pct=volatility_12m,
    )


def calculate_skipped_month_momentum(
    pairs: Sequence[tuple[str, float]],
    provenance: Provenance,
    *,
    skip_sessions: int = 21,
    six_month_sessions: int = 126,
    twelve_month_sessions: int = 252,
) -> tuple[MetricValue, MetricValue, MetricValue]:
    """Return 6-1, 12-1 returns and pre-skip annualized volatility.

    The latest 21 trading sessions are excluded to reduce short-term reversal
    noise.  The 6-month and 12-month windows therefore compare session T-21
    with approximately T-147 and T-273, respectively.
    """

    as_of = pairs[-1][0] if pairs else None
    closes = [float(close) for _, close in pairs]
    lineage = (provenance,)
    end_index = len(closes) - 1 - skip_sessions
    six_start = end_index - six_month_sessions
    twelve_start = end_index - twelve_month_sessions

    if end_index < 0 or six_start < 0:
        six_metric = MetricValue.missing(
            "percent",
            (
                f"requires at least {skip_sessions + six_month_sessions + 1} "
                f"daily closes; received {len(closes)}"
            ),
            provenance=lineage,
            as_of=as_of,
        )
    else:
        six_value = _percent_change(closes[end_index], closes[six_start])
        calculation = _calculation_provenance(
            as_of or "",
            (
                f"adjusted-close return from session T-{skip_sessions + six_month_sessions} "
                f"to T-{skip_sessions}; latest {skip_sessions} sessions excluded"
            ),
            provenance,
        )
        six_metric = MetricValue(
            six_value,
            "percent",
            as_of=as_of,
            provenance=(calculation, provenance),
            note="6-month momentum excluding the latest 21 trading sessions",
        )

    if end_index < 0 or twelve_start < 0:
        note = (
            f"requires at least {skip_sessions + twelve_month_sessions + 1} "
            f"daily closes; received {len(closes)}"
        )
        twelve_metric = MetricValue.missing(
            "percent", note, provenance=lineage, as_of=as_of
        )
        volatility_metric = MetricValue.missing(
            "percent", note, provenance=lineage, as_of=as_of
        )
    else:
        twelve_value = _percent_change(closes[end_index], closes[twelve_start])
        window = closes[twelve_start : end_index + 1]
        daily_returns = [
            current / prior - 1.0
            for prior, current in zip(window, window[1:])
            if prior > 0
        ]
        volatility = (
            stdev(daily_returns) * (252.0 ** 0.5) * 100.0
            if len(daily_returns) >= 2
            else None
        )
        calculation = _calculation_provenance(
            as_of or "",
            (
                f"adjusted-close return and annualized daily volatility from "
                f"session T-{skip_sessions + twelve_month_sessions} to "
                f"T-{skip_sessions}; latest {skip_sessions} sessions excluded"
            ),
            provenance,
        )
        twelve_metric = MetricValue(
            twelve_value,
            "percent",
            as_of=as_of,
            provenance=(calculation, provenance),
            note="12-month momentum excluding the latest 21 trading sessions",
        )
        volatility_metric = (
            MetricValue(
                volatility,
                "percent",
                as_of=as_of,
                provenance=(calculation, provenance),
                note="Annualized realized volatility over the 12-1 month window",
            )
            if volatility is not None and volatility > 0
            else MetricValue.missing(
                "percent",
                "12-1 month realized volatility is zero or unavailable",
                provenance=(calculation, provenance),
                as_of=as_of,
            )
        )

    return six_metric, twelve_metric, volatility_metric


def wilder_rsi(closes: Sequence[float], period: int = 14) -> float | None:
    """Return Wilder's smoothed RSI or ``None`` for insufficient/invalid data."""

    if period <= 0 or len(closes) < period + 1:
        return None
    values = [float(item) for item in closes]
    if any(not isfinite(item) for item in values):
        return None
    changes = [new - old for old, new in zip(values, values[1:])]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = ((period - 1) * avg_gain + gain) / period
        avg_loss = ((period - 1) * avg_loss + loss) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    relative_strength = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + relative_strength))


def wilder_rsi_series(
    closes: Sequence[float], period: int = 14
) -> tuple[float | None, ...]:
    """Return a Wilder RSI value aligned to every input close."""

    if period <= 0:
        raise ValueError("period must be positive")
    values = [float(item) for item in closes]
    output: list[float | None] = [None] * len(values)
    if len(values) < period + 1 or any(not isfinite(item) for item in values):
        return tuple(output)

    changes = [new - old for old, new in zip(values, values[1:])]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def current_rsi() -> float:
        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0
        relative_strength = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + relative_strength))

    output[period] = current_rsi()
    for index, (gain, loss) in enumerate(
        zip(gains[period:], losses[period:]), start=period + 1
    ):
        avg_gain = ((period - 1) * avg_gain + gain) / period
        avg_loss = ((period - 1) * avg_loss + loss) / period
        output[index] = current_rsi()
    return tuple(output)


def calculate_forward_pe_gap(
    current_forward_pe: MetricValue,
    history: Sequence[DatedValue],
    *,
    years: int = 3,
) -> tuple[MetricValue, MetricValue]:
    """Return ``(3Y median forward P/E, gap %)`` using comparable observations.

    Historical trailing P/E is intentionally not mixed with current forward
    P/E.  Free yfinance does not expose a historical forward-P/E series, so a
    normal live pull yields N/A unless a separately dated and attributed
    forward-P/E history provider is configured.
    """

    if not current_forward_pe.usable or not _finite_positive(current_forward_pe.value):
        missing = MetricValue.missing(
            "multiple",
            "current forward P/E is unavailable or non-positive",
            provenance=current_forward_pe.provenance,
            as_of=current_forward_pe.as_of,
        )
        return missing, MetricValue.missing(
            "percent",
            "3Y forward P/E gap cannot be calculated",
            provenance=current_forward_pe.provenance,
            as_of=current_forward_pe.as_of,
        )

    as_of_date = _parse_date(current_forward_pe.as_of)
    valid: list[DatedValue] = []
    for item in history:
        if (
            not _finite_positive(item.value)
            or item.period_type != "forward_pe"
            or not item.provenance
        ):
            continue
        item_date = _parse_date(item.period_end)
        if as_of_date and item_date:
            age_days = (as_of_date - item_date).days
            if age_days < 0 or age_days > years * 366:
                continue
        valid.append(item)

    if len(valid) < 2:
        note = (
            f"requires at least 2 comparable historical forward-P/E observations; "
            f"received {len(valid)}; trailing P/E was not substituted"
        )
        return (
            MetricValue.missing(
                "multiple",
                note,
                provenance=current_forward_pe.provenance,
                as_of=current_forward_pe.as_of,
            ),
            MetricValue.missing(
                "percent",
                note,
                provenance=current_forward_pe.provenance,
                as_of=current_forward_pe.as_of,
            ),
        )

    # Providers return history newest-first, but normalize known date/legacy
    # formats defensively before limiting the 3Y calculation. The UI can show
    # five slots without silently changing the existing three-observation
    # scoring rule.
    dated = [(item_date, item) for item in valid if (item_date := _parse_date(item.period_end))]
    if len(dated) == len(valid):
        valid = [item for _, item in sorted(dated, key=lambda pair: pair[0], reverse=True)]
    else:
        legacy: list[tuple[int, DatedValue]] = []
        for item in valid:
            prefix = "legacy_fy_minus_"
            if not item.period_end.startswith(prefix):
                legacy = []
                break
            try:
                legacy.append((int(item.period_end.removeprefix(prefix)), item))
            except ValueError:
                legacy = []
                break
        if len(legacy) == len(valid):
            valid = [item for _, item in sorted(legacy, key=lambda pair: pair[0])]
    valid = valid[: max(1, int(years))]

    median_value = float(median(item.value for item in valid))
    periods = ", ".join(sorted(item.period_end for item in valid))
    history_provenance = tuple(
        source
        for item in valid
        for source in item.provenance
    )
    legacy_input = any(
        "legacy" in source.source.casefold()
        or any("legacy" in note.casefold() for note in source.notes)
        for source in history_provenance
    )
    stale_cache_input = (
        current_forward_pe.status == MetricStatus.STALE
        or _dated_values_are_stale(valid)
    )
    history_as_of = max(
        (source.as_of for source in history_provenance if source.as_of),
        default=current_forward_pe.as_of,
    )
    legacy_note = (
        "Uses stale, undated legacy FY observations; exact fiscal dates were not inferred."
        if legacy_input
        else None
    )
    stale_cache_note = (
        "Uses cached inputs whose refresh failed."
        if stale_cache_input
        else None
    )
    sample_note = (
        f"Uses {len(valid)} of the target {years} comparable historical observations."
        if len(valid) < years
        else None
    )
    metric_note = " ".join(
        item for item in (legacy_note, stale_cache_note, sample_note) if item
    ) or None
    provenance = Provenance(
        source="calculation",
        retrieved_at=utc_now_iso(),
        as_of=current_forward_pe.as_of,
        basis=f"median of {len(valid)} comparable forward-P/E observations: {periods}",
        notes=("Inputs retain their provider provenance in forward_pe metric.",),
    )
    median_metric = MetricValue(
        median_value,
        "multiple",
        status=(
            MetricStatus.STALE
            if legacy_input or stale_cache_input
            else MetricStatus.OK
        ),
        as_of=history_as_of,
        provenance=(provenance, *current_forward_pe.provenance, *history_provenance),
        note=metric_note,
    )
    gap_metric = MetricValue(
        _percent_change(float(current_forward_pe.value), median_value),
        "percent",
        status=(
            MetricStatus.STALE
            if legacy_input or stale_cache_input
            else MetricStatus.OK
        ),
        as_of=current_forward_pe.as_of,
        provenance=(provenance, *current_forward_pe.provenance, *history_provenance),
        note=metric_note,
    )
    return median_metric, gap_metric


def calculate_eps_growth(
    annual_eps: Sequence[DatedValue],
    *,
    provenance: Sequence[Provenance] = (),
) -> tuple[MetricValue, MetricValue, MetricValue]:
    """Return latest EPS, prior EPS, and comparable annual YoY growth percent."""

    valid = sorted(
        (
            item
            for item in annual_eps
            if isfinite(float(item.value)) and item.period_type == "annual"
        ),
        key=lambda item: item.period_end,
        reverse=True,
    )
    if len(valid) < 2:
        missing = MetricValue.missing(
            "USD/share", "requires two annual diluted-EPS observations", provenance=provenance
        )
        return missing, missing, MetricValue.missing(
            "percent", "annual EPS YoY growth cannot be calculated", provenance=provenance
        )

    latest, prior = valid[0], valid[1]
    latest_lineage = latest.provenance or tuple(provenance)
    prior_lineage = prior.provenance or tuple(provenance)
    input_status = (
        MetricStatus.STALE
        if _dated_values_are_stale((latest, prior))
        else MetricStatus.OK
    )
    stale_note = (
        "Calculated from stale cached annual EPS history"
        if input_status == MetricStatus.STALE
        else None
    )
    latest_metric = MetricValue(
        latest.value,
        "USD/share",
        status=input_status,
        as_of=latest.period_end,
        provenance=latest_lineage,
        note=stale_note,
    )
    prior_metric = MetricValue(
        prior.value,
        "USD/share",
        status=input_status,
        as_of=prior.period_end,
        provenance=prior_lineage,
        note=stale_note,
    )
    latest_date, prior_date = _parse_date(latest.period_end), _parse_date(prior.period_end)
    if latest_date is None or prior_date is None:
        return latest_metric, prior_metric, MetricValue.missing(
            "percent", "EPS period end is not a valid date", provenance=provenance
        )
    spacing = (latest_date - prior_date).days
    if not 300 <= spacing <= 430:
        return latest_metric, prior_metric, MetricValue.missing(
            "percent",
            f"annual EPS periods are not comparable ({spacing} days apart)",
            provenance=provenance,
            as_of=latest.period_end,
        )
    if prior.value <= 0 or latest.value < 0:
        return latest_metric, prior_metric, MetricValue.missing(
            "percent",
            "growth percentage is not meaningful when prior EPS <= 0 or latest EPS < 0",
            provenance=provenance,
            as_of=latest.period_end,
        )
    input_provenance = tuple(
        source for item in (latest, prior) for source in item.provenance
    ) or tuple(provenance)
    calculation = Provenance(
        source="calculation",
        retrieved_at=utc_now_iso(),
        as_of=latest.period_end,
        basis=f"({latest.period_end} diluted EPS / {prior.period_end} diluted EPS - 1) * 100",
    )
    growth = _percent_change(latest.value, prior.value)
    return latest_metric, prior_metric, MetricValue(
        growth,
        "percent",
        status=input_status,
        as_of=latest.period_end,
        provenance=(calculation, *input_provenance),
        note=stale_note,
    )


def validate_annual_share_history(
    values: Sequence[DatedValue],
    *,
    limit: int | None = None,
    maximum_adjacent_factor: float = 10.0,
) -> tuple[tuple[DatedValue, ...], str | None]:
    """Return usable annual share observations or reject a continuity outlier.

    Diluted weighted-average shares should already be split-adjusted across
    comparative statements.  A ten-fold consecutive-year discontinuity is
    therefore treated as a likely vendor/unit error and the whole comparison
    window is withheld instead of letting one row distort the score or chart.
    """

    if maximum_adjacent_factor <= 1:
        raise ValueError("maximum_adjacent_factor must be greater than one")
    ordered = sorted(
        (
            item
            for item in values
            if item.period_type == "annual_diluted_shares"
            and isfinite(float(item.value))
            and float(item.value) > 0
            and _parse_date(item.period_end) is not None
        ),
        key=lambda item: item.period_end,
        reverse=True,
    )
    if limit is not None:
        ordered = ordered[: max(int(limit), 0)]
    for current, prior in zip(ordered, ordered[1:]):
        current_date = _parse_date(current.period_end)
        prior_date = _parse_date(prior.period_end)
        if current_date is None or prior_date is None:
            continue
        spacing = (current_date - prior_date).days
        if not 300 <= spacing <= 430:
            continue
        factor = max(float(current.value), float(prior.value)) / min(
            float(current.value), float(prior.value)
        )
        if factor >= maximum_adjacent_factor:
            return (), (
                "annual diluted-share continuity outlier: "
                f"{current.period_end} vs {prior.period_end} differs by {factor:.1f}x; "
                "share-derived metrics were withheld"
            )
    return tuple(ordered), None


def calculate_shareholder_return_metrics(
    dividend_yields: Sequence[DatedValue],
    annual_diluted_shares: Sequence[DatedValue],
    *,
    years: int = 3,
) -> tuple[
    MetricValue,
    MetricValue,
    MetricValue,
    MetricValue,
    tuple[DatedValue, ...],
]:
    """Calculate a transparent 3Y shareholder-return proxy and dilution.

    The proxy is the average of three validated dividend-yield slots plus the
    positive part of the average net share-count reduction rate. A negative
    net reduction (dilution) is not double-counted here; it is evaluated by the
    separate cumulative three-year share-change rule.
    """

    if years <= 0:
        raise ValueError("years must be positive")

    valid_dividends = [
        item
        for item in dividend_yields
        if item.period_type == "dividend_yield"
        and isfinite(float(item.value))
        and float(item.value) >= 0
    ][:years]
    dividend_lineage = tuple(
        source for item in valid_dividends for source in item.provenance
    )
    dividend_as_of = next(
        (
            source.as_of
            for item in valid_dividends
            for source in item.provenance
            if source.as_of
        ),
        None,
    )
    dividend_legacy_input = any(
        item.period_end.casefold().startswith("legacy")
        or any(
            "legacy" in source.source.casefold()
            or any("legacy" in note.casefold() for note in source.notes)
            for source in item.provenance
        )
        for item in valid_dividends
    )
    dividend_stale_input = _dated_values_are_stale(valid_dividends)
    dividend_status = (
        MetricStatus.STALE
        if dividend_legacy_input or dividend_stale_input
        else MetricStatus.OK
    )
    if len(valid_dividends) == years:
        dividend_average_value = sum(item.value for item in valid_dividends) / years
        dividend_calculation = Provenance(
            source="calculation",
            retrieved_at=utc_now_iso(),
            as_of=dividend_as_of,
            basis=f"mean of latest {years} validated dividend-yield slots",
            notes=(
                (
                    "Legacy slots have no exact fiscal dates and are marked stale."
                    if dividend_legacy_input
                    else "Inputs retain their provider provenance."
                ),
            ),
        )
        dividend_average = MetricValue(
            dividend_average_value,
            "percent",
            status=dividend_status,
            as_of=dividend_as_of,
            provenance=(dividend_calculation, *dividend_lineage),
            note=(
                "3Y average dividend yield from validated but undated legacy slots"
                if dividend_legacy_input
                else (
                    "Calculated from stale cached dividend-yield history"
                    if dividend_stale_input
                    else "3Y average dividend yield"
                )
            ),
        )
    else:
        dividend_average = MetricValue.missing(
            "percent",
            f"requires {years} validated dividend-yield observations; received {len(valid_dividends)}",
            provenance=dividend_lineage,
            as_of=dividend_as_of,
        )

    raw_shares = sorted(
        (
            item
            for item in annual_diluted_shares
            if item.period_type == "annual_diluted_shares"
            and isfinite(float(item.value))
            and float(item.value) > 0
            and _parse_date(item.period_end) is not None
        ),
        key=lambda item: item.period_end,
        reverse=True,
    )[: years + 1]
    shares, share_continuity_error = validate_annual_share_history(
        raw_shares, limit=years + 1
    )
    buyback_history: list[DatedValue] = []
    invalid_spacing = False
    for current, prior in zip(shares, shares[1:]):
        current_date = _parse_date(current.period_end)
        prior_date = _parse_date(prior.period_end)
        if current_date is None or prior_date is None:
            invalid_spacing = True
            continue
        spacing = (current_date - prior_date).days
        if not 300 <= spacing <= 430:
            invalid_spacing = True
            continue
        # Equivalent to (prior shares - current shares) / prior shares * 100:
        # positive means the diluted share count fell and holders gained.
        rate = round((prior.value - current.value) / prior.value * 100.0, 10)
        input_lineage = (*current.provenance, *prior.provenance)
        calculation = Provenance(
            source="calculation",
            retrieved_at=utc_now_iso(),
            as_of=current.period_end,
            basis=(
                f"({prior.period_end} diluted average shares - {current.period_end} "
                f"diluted average shares) / {prior.period_end} shares * 100"
            ),
        )
        buyback_history.append(
            DatedValue(
                period_end=current.period_end,
                value=rate,
                period_type="net_buyback_yield_proxy",
                provenance=(calculation, *input_lineage),
            )
        )

    shares_lineage = tuple(source for item in raw_shares for source in item.provenance)
    shares_as_of = raw_shares[0].period_end if raw_shares else None
    shares_status = (
        MetricStatus.STALE
        if _dated_values_are_stale(shares)
        else MetricStatus.OK
    )
    if len(buyback_history) == years and not invalid_spacing:
        buyback_average_value = sum(item.value for item in buyback_history) / years
        buyback_calculation = Provenance(
            source="calculation",
            retrieved_at=utc_now_iso(),
            as_of=shares_as_of,
            basis=f"mean of {years} annual net diluted-share reduction rates",
        )
        buyback_average = MetricValue(
            buyback_average_value,
            "percent",
            status=shares_status,
            as_of=shares_as_of,
            provenance=(buyback_calculation, *shares_lineage),
            note=(
                "Calculated from stale cached annual shares; positive means net "
                "share-count reduction; negative means dilution"
                if shares_status == MetricStatus.STALE
                else "Positive means net share-count reduction; negative means dilution"
            ),
        )
    else:
        buyback_average = MetricValue.missing(
            "percent",
            share_continuity_error
            or f"requires {years + 1} comparable annual diluted-share observations",
            provenance=shares_lineage,
            as_of=shares_as_of,
        )

    if len(shares) == years + 1 and not invalid_spacing:
        latest, oldest = shares[0], shares[-1]
        shares_change = MetricValue(
            _percent_change(latest.value, oldest.value),
            "percent",
            status=shares_status,
            as_of=latest.period_end,
            provenance=(
                Provenance(
                    source="calculation",
                    retrieved_at=utc_now_iso(),
                    as_of=latest.period_end,
                    basis=(
                        f"({latest.period_end} diluted average shares / {oldest.period_end} "
                        "diluted average shares - 1) * 100"
                    ),
                ),
                *shares_lineage,
            ),
            note=(
                "Calculated from stale cached annual shares; positive means cumulative "
                "dilution; negative means share-count reduction"
                if shares_status == MetricStatus.STALE
                else "Positive means cumulative dilution; negative means share-count reduction"
            ),
        )
    else:
        shares_change = MetricValue.missing(
            "percent",
            share_continuity_error
            or f"requires {years + 1} comparable annual diluted-share observations",
            provenance=shares_lineage,
            as_of=shares_as_of,
        )

    if dividend_average.usable and buyback_average.usable:
        shareholder_value = float(dividend_average.value) + max(
            float(buyback_average.value), 0.0
        )
        shareholder_return = MetricValue(
            shareholder_value,
            "percent",
            status=(
                MetricStatus.STALE
                if MetricStatus.STALE
                in {dividend_average.status, buyback_average.status}
                else MetricStatus.OK
            ),
            as_of=max(
                (item for item in (dividend_average.as_of, buyback_average.as_of) if item),
                default=None,
            ),
            provenance=(
                Provenance(
                    source="calculation",
                    retrieved_at=utc_now_iso(),
                    basis=(
                        "3Y average dividend yield + max(3Y average net diluted-share "
                        "reduction rate, 0); dilution is scored separately"
                    ),
                ),
                *dividend_average.provenance,
                *buyback_average.provenance,
            ),
            note="Calculated proxy, not vendor-reported cash shareholder yield",
        )
    else:
        shareholder_return = MetricValue.missing(
            "percent",
            "requires both 3Y dividend-yield and diluted-share histories",
            provenance=(*dividend_average.provenance, *buyback_average.provenance),
        )

    return (
        dividend_average,
        buyback_average,
        shareholder_return,
        shares_change,
        tuple(buyback_history),
    )


def calculate_cash_flow_quality(
    annual_operating_cash_flow: Sequence[DatedValue],
    annual_net_income: Sequence[DatedValue],
    *,
    years: int = 3,
) -> tuple[MetricValue, tuple[DatedValue, ...]]:
    """Return cumulative CFO/net-income conversion and annual chart ratios."""

    if years <= 0:
        raise ValueError("years must be positive")
    cash_by_period = {
        item.period_end: item
        for item in annual_operating_cash_flow
        if item.period_type == "annual_operating_cash_flow"
        and isfinite(float(item.value))
        and _parse_date(item.period_end) is not None
    }
    income_by_period = {
        item.period_end: item
        for item in annual_net_income
        if item.period_type == "annual_net_income"
        and isfinite(float(item.value))
        and _parse_date(item.period_end) is not None
    }
    periods = sorted(set(cash_by_period) & set(income_by_period), reverse=True)[:years]
    lineage = tuple(
        source
        for period in periods
        for item in (cash_by_period[period], income_by_period[period])
        for source in item.provenance
    )
    as_of = periods[0] if periods else None
    if len(periods) < years:
        return (
            MetricValue.missing(
                "ratio",
                (
                    f"requires {years} comparable annual operating-cash-flow "
                    f"and net-income periods; received {len(periods)}"
                ),
                provenance=lineage,
                as_of=as_of,
            ),
            (),
        )

    annual_ratios: list[DatedValue] = []
    for period in reversed(periods):
        income = float(income_by_period[period].value)
        cash = float(cash_by_period[period].value)
        if income <= 0:
            continue
        calculation = Provenance(
            source="calculation",
            retrieved_at=utc_now_iso(),
            as_of=period,
            basis=f"{period} operating cash flow / {period} net income",
        )
        annual_ratios.append(
            DatedValue(
                period_end=period,
                value=cash / income,
                period_type="cash_flow_quality_ratio",
                provenance=(
                    calculation,
                    *cash_by_period[period].provenance,
                    *income_by_period[period].provenance,
                ),
            )
        )

    cumulative_income = sum(float(income_by_period[period].value) for period in periods)
    cumulative_cash = sum(float(cash_by_period[period].value) for period in periods)
    if cumulative_income <= 0:
        return (
            MetricValue.missing(
                "ratio",
                "three-year cumulative net income is zero or negative",
                provenance=lineage,
                as_of=as_of,
            ),
            tuple(annual_ratios),
        )

    stale = _dated_values_are_stale(
        tuple(cash_by_period[period] for period in periods)
        + tuple(income_by_period[period] for period in periods)
    )
    calculation = Provenance(
        source="calculation",
        retrieved_at=utc_now_iso(),
        as_of=as_of,
        basis=(
            f"sum operating cash flow / sum net income for fiscal periods "
            f"{', '.join(reversed(periods))}"
        ),
    )
    return (
        MetricValue(
            cumulative_cash / cumulative_income,
            "ratio",
            status=MetricStatus.STALE if stale else MetricStatus.OK,
            as_of=as_of,
            provenance=(calculation, *lineage),
            note=(
                "Calculated from stale cached annual statements"
                if stale
                else "Three-year cumulative operating-cash-flow conversion"
            ),
        ),
        tuple(annual_ratios),
    )


def calculate_fcf_yield(
    annual_free_cash_flow: Sequence[DatedValue],
    market_cap: MetricValue,
) -> MetricValue:
    """Return latest annual free cash flow divided by current market cap."""

    values = sorted(
        (
            item
            for item in annual_free_cash_flow
            if item.period_type == "annual_free_cash_flow"
            and isfinite(float(item.value))
            and _parse_date(item.period_end) is not None
        ),
        key=lambda item: item.period_end,
        reverse=True,
    )
    if not values or not market_cap.usable or not _finite_positive(market_cap.value):
        return MetricValue.missing(
            "percent",
            "requires latest annual free cash flow and positive current market cap",
            provenance=(
                *(values[0].provenance if values else ()),
                *market_cap.provenance,
            ),
            as_of=market_cap.as_of or (values[0].period_end if values else None),
        )
    latest = values[0]
    status = (
        MetricStatus.STALE
        if market_cap.status == MetricStatus.STALE
        or _dated_values_are_stale((latest,))
        else MetricStatus.OK
    )
    calculation = Provenance(
        source="calculation",
        retrieved_at=utc_now_iso(),
        as_of=market_cap.as_of,
        basis=f"{latest.period_end} annual free cash flow / current market cap * 100",
    )
    return MetricValue(
        float(latest.value) / float(market_cap.value) * 100.0,
        "percent",
        status=status,
        as_of=market_cap.as_of,
        provenance=(calculation, *latest.provenance, *market_cap.provenance),
        note="Annual FCF yield calculated for supplemental display; not a company score factor",
    )


def compose_stock_metrics(
    member: UniverseMember,
    price: PriceMetrics,
    fundamentals: Any,
) -> StockMetrics:
    """Join cached price and fundamental snapshots and derive score metrics."""

    pe_median, pe_gap = calculate_forward_pe_gap(
        fundamentals.forward_pe, fundamentals.forward_pe_history
    )
    eps_latest, eps_prior, eps_growth = calculate_eps_growth(
        fundamentals.annual_diluted_eps
    )
    (
        dividend_yield_average,
        net_buyback_average,
        shareholder_return_average,
        shares_change,
        net_buyback_history,
    ) = calculate_shareholder_return_metrics(
        fundamentals.dividend_yield_history,
        fundamentals.annual_diluted_shares,
    )
    cash_flow_quality, cash_conversion_history = calculate_cash_flow_quality(
        fundamentals.annual_operating_cash_flow,
        fundamentals.annual_net_income,
    )
    fcf_yield = calculate_fcf_yield(
        fundamentals.annual_free_cash_flow,
        fundamentals.market_cap,
    )
    chart_shares, _ = validate_annual_share_history(
        fundamentals.annual_diluted_shares, limit=4
    )
    metrics: dict[str, MetricValue] = {
        **price.metrics(),
        "forward_pe": fundamentals.forward_pe,
        "pe_3y_median": pe_median,
        "pe_gap_3y_pct": pe_gap,
        "trailing_pe": fundamentals.trailing_pe,
        "fcf_yield_pct": fcf_yield,
        "eps_latest": eps_latest,
        "eps_prior": eps_prior,
        "eps_growth_yoy_pct": eps_growth,
        "cash_flow_quality_3y": cash_flow_quality,
        "dividend_yield_3y_avg_pct": dividend_yield_average,
        "net_buyback_yield_3y_avg_pct": net_buyback_average,
        "shareholder_return_3y_avg_pct": shareholder_return_average,
        "shares_change_3y_pct": shares_change,
        "debt_to_equity": fundamentals.debt_to_equity,
    }
    return StockMetrics(
        member=member,
        metrics=metrics,
        history={
            "forward_pe": tuple(fundamentals.forward_pe_history),
            "trailing_pe": tuple(fundamentals.trailing_pe_history),
            "annual_diluted_eps": tuple(fundamentals.annual_diluted_eps),
            "annual_diluted_shares": chart_shares,
            "annual_net_income": tuple(fundamentals.annual_net_income),
            "annual_operating_cash_flow": tuple(
                fundamentals.annual_operating_cash_flow
            ),
            "annual_free_cash_flow": tuple(fundamentals.annual_free_cash_flow),
            "cash_flow_quality_ratio": cash_conversion_history,
            "dividend_yield": tuple(fundamentals.dividend_yield_history),
            "net_buyback_yield_proxy": net_buyback_history,
        },
    )


def score_stock(
    stock: StockMetrics,
    market_overlay: MarketOverlay | None = None,
) -> StockSignal:
    """Score one stock, retaining ``None`` contributions for missing factors."""

    components: list[ScoreComponent] = []
    for rule in RULE_DEFINITIONS["company_rules"]:
        metric = stock.metrics.get(str(rule["metric_key"]))
        excluded_reason = _sector_exclusion(stock.member, rule)
        if excluded_reason:
            components.append(
                ScoreComponent(
                    key=str(rule["key"]),
                    label=str(rule["label"]),
                    metric_key=str(rule["metric_key"]),
                    value=metric.value if metric else None,
                    unit=str(rule["unit"]),
                    points=None,
                    applicable=False,
                    reason=excluded_reason,
                    metric_status=metric.status if metric else MetricStatus.MISSING,
                )
            )
            continue
        components.append(_evaluate_rule(rule, metric))

    applicable = [item for item in components if item.applicable]
    valid = [item for item in applicable if item.points is not None]
    company_score = sum(int(item.points) for item in valid)
    coverage_count = len(valid)
    # Sector exclusions are genuinely non-applicable, not missing data.  Keeping
    # them out of the denominator prevents Financials and REITs from being
    # permanently capped at 83.3% coverage.
    coverage_total = len(applicable)
    coverage_pct = round(coverage_count / coverage_total * 100.0, 1) if coverage_total else 0.0
    minimum = int(RULE_DEFINITIONS["minimum_company_metrics"])
    eligible = coverage_count >= minimum
    market_score = market_overlay.score if market_overlay else 0
    total_score = company_score + market_score
    recommendation = recommendation_for_score(
        total_score, coverage_count=coverage_count, minimum_coverage=minimum
    )

    warnings: list[str] = []
    if not eligible:
        warnings.append(
            f"Only {coverage_count}/{coverage_total} company factors are usable; "
            f"at least {minimum} are required for a recommendation."
        )
    missing = [item.label for item in components if item.points is None and item.applicable]
    if missing:
        warnings.append("Missing/invalid factors: " + ", ".join(missing))
    stale = [item.label for item in components if item.metric_status == MetricStatus.STALE]
    if stale:
        warnings.append("Stale cached factors: " + ", ".join(stale))
    if market_overlay and not market_overlay.complete:
        warnings.append("Market overlay is incomplete; only available overlay factors were applied.")

    return StockSignal(
        company=stock.member,
        metrics=stock.metrics,
        components=tuple(components),
        company_score=company_score,
        market_score=market_score,
        total_score=total_score,
        coverage_count=coverage_count,
        coverage_total=coverage_total,
        coverage_pct=coverage_pct,
        eligible_for_ranking=eligible,
        recommendation=recommendation,
        market_overlay=market_overlay,
        history=stock.history,
        warnings=tuple(warnings),
    )


def score_market_overlay(metrics: Mapping[str, MetricValue]) -> MarketOverlay:
    """Score market context independently from company-specific factors."""

    components = tuple(
        _evaluate_rule(rule, metrics.get(str(rule["metric_key"])))
        for rule in RULE_DEFINITIONS["market_rules"]
    )
    return MarketOverlay(
        metrics=dict(metrics),
        components=components,
        score=sum(item.points for item in components if item.points is not None),
        complete=all(item.points is not None for item in components),
    )


def recommendation_for_score(
    score: int,
    *,
    coverage_count: int = 6,
    minimum_coverage: int = 5,
) -> Recommendation:
    if coverage_count < minimum_coverage:
        return Recommendation.INSUFFICIENT_DATA
    for band in RULE_DEFINITIONS["recommendations"]:
        minimum = band.get("min")
        maximum = band.get("max")
        if minimum is not None and score < int(minimum):
            continue
        if maximum is not None and score > int(maximum):
            continue
        return Recommendation(str(band["code"]))
    return Recommendation.NEUTRAL


_OPERATORS: Mapping[str, Callable[[float, float], bool]] = {
    "<=": lambda actual, boundary: actual <= boundary,
    "<": lambda actual, boundary: actual < boundary,
    ">=": lambda actual, boundary: actual >= boundary,
    ">": lambda actual, boundary: actual > boundary,
    "==": lambda actual, boundary: actual == boundary,
}


def _evaluate_rule(
    rule: Mapping[str, Any], metric: MetricValue | None
) -> ScoreComponent:
    if metric is None or not metric.usable:
        status = metric.status if metric else MetricStatus.MISSING
        note = metric.note if metric else "metric is absent"
        return ScoreComponent(
            key=str(rule["key"]),
            label=str(rule["label"]),
            metric_key=str(rule["metric_key"]),
            value=metric.value if metric else None,
            unit=str(rule["unit"]),
            points=None,
            applicable=True,
            reason=f"N/A — {note or status.value}",
            metric_status=status,
        )
    actual = float(metric.value)
    for threshold in rule.get("thresholds", ()):
        operator = str(threshold["operator"])
        boundary = float(threshold["value"])
        if _OPERATORS[operator](actual, boundary):
            points = int(threshold["points"])
            return ScoreComponent(
                key=str(rule["key"]),
                label=str(rule["label"]),
                metric_key=str(rule["metric_key"]),
                value=actual,
                unit=str(rule["unit"]),
                points=points,
                applicable=True,
                reason=f"{actual:.2f} {operator} {boundary:g} → {points:+d}",
                metric_status=metric.status,
            )
    points = int(rule.get("default_points", 0))
    return ScoreComponent(
        key=str(rule["key"]),
        label=str(rule["label"]),
        metric_key=str(rule["metric_key"]),
        value=actual,
        unit=str(rule["unit"]),
        points=points,
        applicable=True,
        reason=f"No threshold matched → {points:+d}",
        metric_status=metric.status,
    )


def _sector_exclusion(member: UniverseMember, rule: Mapping[str, Any]) -> str | None:
    exclusions = set(str(item) for item in rule.get("sector_exclusions", ()) or ())
    if not exclusions:
        return None
    rule_key = str(rule.get("key", ""))
    if "Financials" in exclusions and member.sector.casefold() == "financials":
        if rule_key == "cash_flow_quality":
            return "Not scored: bank and insurer operating cash flow is not comparable to industrial companies."
        return "Not scored: leverage has sector-specific meaning for financial companies."
    if any(
        member.sector.casefold() == exclusion.casefold()
        for exclusion in exclusions
        if exclusion not in {"Financials", "REIT"}
    ):
        return (
            f"Not scored: {member.sector} requires sector-specific valuation "
            "and cash-flow measures."
        )
    combined = f"{member.sector} {member.sub_industry}".casefold()
    if "REIT" in exclusions and (
        "reit" in combined or "real estate investment trust" in combined
    ):
        return "Not scored: REIT analysis requires FFO/AFFO, NAV, and maturity data."
    return None


def _missing_price_metrics(
    symbol: str, provenance: Provenance, note: str
) -> PriceMetrics:
    lineage = (provenance,)
    return PriceMetrics(
        symbol=symbol,
        current_price=MetricValue.missing("USD", note, provenance=lineage),
        high_52_week=MetricValue.missing("USD", note, provenance=lineage),
        drawdown_52_week_pct=MetricValue.missing("percent", note, provenance=lineage),
        sma_200=MetricValue.missing("USD", note, provenance=lineage),
        distance_200dma_pct=MetricValue.missing("percent", note, provenance=lineage),
        rsi_14=MetricValue.missing("index", note, provenance=lineage),
        return_6_1_pct=MetricValue.missing("percent", note, provenance=lineage),
        return_12_1_pct=MetricValue.missing("percent", note, provenance=lineage),
        volatility_12m_pct=MetricValue.missing("percent", note, provenance=lineage),
    )


def _calculation_provenance(
    as_of: str, basis: str, source: Provenance
) -> Provenance:
    return Provenance(
        source="calculation",
        retrieved_at=utc_now_iso(),
        as_of=as_of,
        basis=basis,
        notes=(f"Input source: {source.source}",),
    )


def _finite_positive(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return isfinite(number) and number > 0


def _dated_values_are_stale(values: Sequence[DatedValue]) -> bool:
    """Detect the explicit cache-staleness marker carried by history rows."""

    return any(
        "stale cache used" in note.casefold()
        for item in values
        for source in item.provenance
        for note in source.notes
    )


def _percent_change(current: float, reference: float) -> float:
    """Stable percentage change; rounding prevents boundary float artifacts."""

    return round((float(current) / float(reference) - 1.0) * 100.0, 10)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _subtract_one_year(value: str) -> str:
    parsed = _parse_date(value)
    if parsed is None:
        return "0000-00-00"
    try:
        return parsed.replace(year=parsed.year - 1).isoformat()
    except ValueError:  # February 29
        return parsed.replace(year=parsed.year - 1, day=28).isoformat()
