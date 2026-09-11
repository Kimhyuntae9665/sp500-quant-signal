"""Typed, JSON-safe domain models for the quant signal engine.

The models deliberately keep a metric's value, status, as-of date, and source
together.  A missing vendor field therefore remains ``None`` all the way to
the API instead of silently turning into zero.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime, timezone
from enum import Enum
from math import isfinite
from typing import Any, Mapping, Sequence


def utc_now_iso() -> str:
    """Return an ISO-8601 UTC timestamp suitable for provenance records."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def json_safe(value: Any) -> Any:
    """Recursively convert model values into objects accepted by json.dumps."""

    if isinstance(value, Enum):
        return json_safe(value.value)
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if isfinite(value) else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return json_safe(value.to_dict())
    if is_dataclass(value) and not isinstance(value, type):
        return json_safe(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(item) for item in value]
    return str(value)


class MetricStatus(str, Enum):
    OK = "ok"
    MISSING = "missing"
    INVALID = "invalid"
    STALE = "stale"


@dataclass(frozen=True)
class Provenance:
    """Where a reported or calculated datum came from."""

    source: str
    retrieved_at: str
    as_of: str | None = None
    source_url: str | None = None
    basis: str | None = None
    official: bool = False
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return json_safe(asdict(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Provenance":
        return cls(
            source=str(value.get("source", "unknown")),
            retrieved_at=str(value.get("retrieved_at", "")),
            as_of=_optional_str(value.get("as_of")),
            source_url=_optional_str(value.get("source_url")),
            basis=_optional_str(value.get("basis")),
            official=bool(value.get("official", False)),
            notes=tuple(str(item) for item in value.get("notes", ()) or ()),
        )


@dataclass(frozen=True)
class MetricValue:
    """A nullable numeric value with units and audit metadata."""

    value: float | None
    unit: str
    status: MetricStatus = MetricStatus.OK
    as_of: str | None = None
    provenance: tuple[Provenance, ...] = ()
    raw_value: float | str | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        if self.value is None and self.status == MetricStatus.OK:
            object.__setattr__(self, "status", MetricStatus.MISSING)
        if self.value is not None and not isfinite(float(self.value)):
            object.__setattr__(self, "value", None)
            object.__setattr__(self, "status", MetricStatus.INVALID)

    @property
    def usable(self) -> bool:
        return self.value is not None and self.status in {
            MetricStatus.OK,
            MetricStatus.STALE,
        }

    def with_status(self, status: MetricStatus, note: str | None = None) -> "MetricValue":
        return MetricValue(
            value=self.value,
            unit=self.unit,
            status=status,
            as_of=self.as_of,
            provenance=self.provenance,
            raw_value=self.raw_value,
            note=note if note is not None else self.note,
        )

    def to_dict(self) -> dict[str, Any]:
        return json_safe(asdict(self))

    @classmethod
    def missing(
        cls,
        unit: str,
        note: str,
        *,
        provenance: Sequence[Provenance] = (),
        as_of: str | None = None,
    ) -> "MetricValue":
        return cls(
            None,
            unit,
            MetricStatus.MISSING,
            as_of=as_of,
            provenance=tuple(provenance),
            note=note,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MetricValue":
        raw_status = str(value.get("status", MetricStatus.MISSING.value))
        try:
            status = MetricStatus(raw_status)
        except ValueError:
            status = MetricStatus.INVALID
        number = _optional_float(value.get("value"))
        raw_value = value.get("raw_value")
        return cls(
            value=number,
            unit=str(value.get("unit", "number")),
            status=status,
            as_of=_optional_str(value.get("as_of")),
            provenance=tuple(
                Provenance.from_dict(item)
                for item in value.get("provenance", ()) or ()
                if isinstance(item, Mapping)
            ),
            raw_value=raw_value if isinstance(raw_value, (str, int, float)) else None,
            note=_optional_str(value.get("note")),
        )


@dataclass(frozen=True)
class DatedValue:
    period_end: str
    value: float
    period_type: str = "annual"
    provenance: tuple[Provenance, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return json_safe(asdict(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DatedValue":
        return cls(
            period_end=str(value["period_end"]),
            value=float(value["value"]),
            period_type=str(value.get("period_type", "annual")),
            provenance=tuple(
                Provenance.from_dict(item)
                for item in value.get("provenance", ()) or ()
                if isinstance(item, Mapping)
            ),
        )


@dataclass(frozen=True)
class UniverseMember:
    symbol: str
    name: str
    sector: str
    sub_industry: str
    cik: str | None = None

    @property
    def provider_symbol(self) -> str:
        """Yahoo represents share-class dots with hyphens (BRK.B -> BRK-B)."""

        return self.symbol.replace(".", "-")

    def to_dict(self) -> dict[str, Any]:
        return json_safe(asdict(self))


@dataclass(frozen=True)
class UniverseSnapshot:
    members: tuple[UniverseMember, ...]
    provenance: Provenance
    fallback_used: bool
    warnings: tuple[str, ...] = ()

    @property
    def symbols(self) -> list[str]:
        return [member.symbol for member in self.members]

    def to_dict(self) -> dict[str, Any]:
        return {
            "members": [item.to_dict() for item in self.members],
            "symbols": self.symbols,
            "count": len(self.members),
            "provenance": self.provenance.to_dict(),
            "fallback_used": self.fallback_used,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class PriceHistory:
    symbol: str
    dates: tuple[str, ...]
    adjusted_closes: tuple[float, ...]
    provenance: Provenance
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return json_safe(asdict(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PriceHistory":
        # Validate each date/close pair together.  Filtering only the close
        # array would shift every later close onto the wrong trading date when
        # a cached payload contains a null or otherwise invalid observation.
        pairs: list[tuple[str, float]] = []
        raw_dates = value.get("dates", ()) or ()
        raw_closes = value.get("adjusted_closes", ()) or ()
        for raw_date, raw_close in zip(raw_dates, raw_closes):
            number = _optional_float(raw_close)
            if number is not None:
                pairs.append((str(raw_date), number))
        raw_provenance = value.get("provenance")
        provenance = (
            Provenance.from_dict(raw_provenance)
            if isinstance(raw_provenance, Mapping)
            else Provenance(source="unknown", retrieved_at="")
        )
        return cls(
            symbol=str(value.get("symbol", "")),
            dates=tuple(day for day, _ in pairs),
            adjusted_closes=tuple(close for _, close in pairs),
            provenance=provenance,
            error=_optional_str(value.get("error")),
        )


@dataclass(frozen=True)
class PriceMetrics:
    symbol: str
    current_price: MetricValue
    high_52_week: MetricValue
    drawdown_52_week_pct: MetricValue
    sma_200: MetricValue
    distance_200dma_pct: MetricValue
    rsi_14: MetricValue
    return_6_1_pct: MetricValue = field(
        default_factory=lambda: MetricValue.missing(
            "percent", "6-1 month momentum is unavailable"
        )
    )
    return_12_1_pct: MetricValue = field(
        default_factory=lambda: MetricValue.missing(
            "percent", "12-1 month momentum is unavailable"
        )
    )
    volatility_12m_pct: MetricValue = field(
        default_factory=lambda: MetricValue.missing(
            "percent", "12-month realized volatility is unavailable"
        )
    )

    def metrics(self) -> dict[str, MetricValue]:
        return {
            "current_price": self.current_price,
            "high_52_week": self.high_52_week,
            "drawdown_52_week_pct": self.drawdown_52_week_pct,
            "sma_200": self.sma_200,
            "distance_200dma_pct": self.distance_200dma_pct,
            "rsi_14": self.rsi_14,
            "return_6_1_pct": self.return_6_1_pct,
            "return_12_1_pct": self.return_12_1_pct,
            "volatility_12m_pct": self.volatility_12m_pct,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            **{key: item.to_dict() for key, item in self.metrics().items()},
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PriceMetrics":
        def metric(name: str, unit: str) -> MetricValue:
            raw = value.get(name)
            if isinstance(raw, Mapping):
                return MetricValue.from_dict(raw)
            return MetricValue.missing(unit, f"cached field {name} is absent")

        return cls(
            symbol=str(value.get("symbol", "")),
            current_price=metric("current_price", "USD"),
            high_52_week=metric("high_52_week", "USD"),
            drawdown_52_week_pct=metric("drawdown_52_week_pct", "percent"),
            sma_200=metric("sma_200", "USD"),
            distance_200dma_pct=metric("distance_200dma_pct", "percent"),
            rsi_14=metric("rsi_14", "index"),
            return_6_1_pct=metric("return_6_1_pct", "percent"),
            return_12_1_pct=metric("return_12_1_pct", "percent"),
            volatility_12m_pct=metric("volatility_12m_pct", "percent"),
        )


@dataclass(frozen=True)
class FundamentalData:
    symbol: str
    forward_pe: MetricValue
    forward_pe_history: tuple[DatedValue, ...]
    trailing_pe: MetricValue
    annual_diluted_eps: tuple[DatedValue, ...]
    debt_to_equity: MetricValue
    market_cap: MetricValue
    trailing_pe_history: tuple[DatedValue, ...] = ()
    annual_diluted_shares: tuple[DatedValue, ...] = ()
    dividend_yield_history: tuple[DatedValue, ...] = ()
    annual_net_income: tuple[DatedValue, ...] = ()
    annual_operating_cash_flow: tuple[DatedValue, ...] = ()
    annual_free_cash_flow: tuple[DatedValue, ...] = ()
    currency: str | None = None
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "forward_pe": self.forward_pe.to_dict(),
            "forward_pe_history": [item.to_dict() for item in self.forward_pe_history],
            "trailing_pe": self.trailing_pe.to_dict(),
            "trailing_pe_history": [
                item.to_dict() for item in self.trailing_pe_history
            ],
            "annual_diluted_eps": [item.to_dict() for item in self.annual_diluted_eps],
            "debt_to_equity": self.debt_to_equity.to_dict(),
            "market_cap": self.market_cap.to_dict(),
            "annual_diluted_shares": [
                item.to_dict() for item in self.annual_diluted_shares
            ],
            "dividend_yield_history": [
                item.to_dict() for item in self.dividend_yield_history
            ],
            "annual_net_income": [
                item.to_dict() for item in self.annual_net_income
            ],
            "annual_operating_cash_flow": [
                item.to_dict() for item in self.annual_operating_cash_flow
            ],
            "annual_free_cash_flow": [
                item.to_dict() for item in self.annual_free_cash_flow
            ],
            "currency": self.currency,
            "errors": list(self.errors),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FundamentalData":
        def metric(name: str, unit: str) -> MetricValue:
            raw = value.get(name)
            if isinstance(raw, Mapping):
                return MetricValue.from_dict(raw)
            return MetricValue.missing(unit, f"cached field {name} is absent")

        return cls(
            symbol=str(value.get("symbol", "")),
            forward_pe=metric("forward_pe", "multiple"),
            forward_pe_history=tuple(
                DatedValue.from_dict(item)
                for item in value.get("forward_pe_history", ()) or ()
                if isinstance(item, Mapping)
            ),
            trailing_pe=metric("trailing_pe", "multiple"),
            trailing_pe_history=tuple(
                DatedValue.from_dict(item)
                for item in value.get("trailing_pe_history", ()) or ()
                if isinstance(item, Mapping)
            ),
            annual_diluted_eps=tuple(
                DatedValue.from_dict(item)
                for item in value.get("annual_diluted_eps", ()) or ()
                if isinstance(item, Mapping)
            ),
            debt_to_equity=metric("debt_to_equity", "ratio"),
            market_cap=metric("market_cap", "USD"),
            annual_diluted_shares=tuple(
                DatedValue.from_dict(item)
                for item in value.get("annual_diluted_shares", ()) or ()
                if isinstance(item, Mapping)
            ),
            dividend_yield_history=tuple(
                DatedValue.from_dict(item)
                for item in value.get("dividend_yield_history", ()) or ()
                if isinstance(item, Mapping)
            ),
            annual_net_income=tuple(
                DatedValue.from_dict(item)
                for item in value.get("annual_net_income", ()) or ()
                if isinstance(item, Mapping)
            ),
            annual_operating_cash_flow=tuple(
                DatedValue.from_dict(item)
                for item in value.get("annual_operating_cash_flow", ()) or ()
                if isinstance(item, Mapping)
            ),
            annual_free_cash_flow=tuple(
                DatedValue.from_dict(item)
                for item in value.get("annual_free_cash_flow", ()) or ()
                if isinstance(item, Mapping)
            ),
            currency=_optional_str(value.get("currency")),
            errors=tuple(str(item) for item in value.get("errors", ()) or ()),
        )


@dataclass(frozen=True)
class StockMetrics:
    member: UniverseMember
    metrics: Mapping[str, MetricValue]
    history: Mapping[str, tuple[DatedValue, ...]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "company": self.member.to_dict(),
            "metrics": {key: item.to_dict() for key, item in self.metrics.items()},
            "history": {
                key: [item.to_dict() for item in values]
                for key, values in self.history.items()
            },
        }


@dataclass(frozen=True)
class ScoreComponent:
    key: str
    label: str
    metric_key: str
    value: float | None
    unit: str
    points: int | None
    applicable: bool
    reason: str
    metric_status: MetricStatus

    def to_dict(self) -> dict[str, Any]:
        return json_safe(asdict(self))


class Recommendation(str, Enum):
    STRONG_BUY = "strong_buy"
    WATCH_BUY = "watch_buy"
    NEUTRAL = "neutral"
    WATCH_SELL = "watch_sell"
    SELL = "sell"
    INSUFFICIENT_DATA = "insufficient_data"


RECOMMENDATION_LABELS: Mapping[Recommendation, str] = {
    Recommendation.STRONG_BUY: "강력 매수",
    Recommendation.WATCH_BUY: "매수 관심",
    Recommendation.NEUTRAL: "중립",
    Recommendation.WATCH_SELL: "매도 관심",
    Recommendation.SELL: "매도",
    Recommendation.INSUFFICIENT_DATA: "데이터 부족",
}


@dataclass(frozen=True)
class MarketOverlay:
    metrics: Mapping[str, MetricValue]
    components: tuple[ScoreComponent, ...] = ()
    score: int = 0
    complete: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "metrics": {key: item.to_dict() for key, item in self.metrics.items()},
            "components": [item.to_dict() for item in self.components],
            "score": self.score,
            "complete": self.complete,
        }


@dataclass(frozen=True)
class StockSignal:
    company: UniverseMember
    metrics: Mapping[str, MetricValue]
    components: tuple[ScoreComponent, ...]
    company_score: int
    market_score: int
    total_score: int
    coverage_count: int
    coverage_total: int
    coverage_pct: float
    eligible_for_ranking: bool
    recommendation: Recommendation
    market_overlay: MarketOverlay | None = None
    history: Mapping[str, tuple[DatedValue, ...]] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.company.symbol,
            "company": self.company.to_dict(),
            "metrics": {key: item.to_dict() for key, item in self.metrics.items()},
            "components": [item.to_dict() for item in self.components],
            "company_score": self.company_score,
            "market_score": self.market_score,
            "total_score": self.total_score,
            "coverage_count": self.coverage_count,
            "coverage_total": self.coverage_total,
            "coverage_pct": self.coverage_pct,
            "eligible_for_ranking": self.eligible_for_ranking,
            "recommendation": self.recommendation.value,
            "recommendation_label": RECOMMENDATION_LABELS[self.recommendation],
            "market_overlay": self.market_overlay.to_dict() if self.market_overlay else None,
            "history": {
                key: [item.to_dict() for item in values]
                for key, values in self.history.items()
            },
            "warnings": list(self.warnings),
        }

    def to_dashboard_dict(self) -> dict[str, Any]:
        """Return the compact row shape used by the 500-company dashboard.

        Full audit lineage and historical arrays remain available from the
        single-stock endpoint. Repeating them for every table row makes the
        mobile payload unnecessarily large.
        """

        component_by_metric = {
            component.metric_key: component for component in self.components
        }
        return {
            "symbol": self.company.symbol,
            "company": self.company.to_dict(),
            "metrics": {
                key: _compact_metric_dict(item, component_by_metric.get(key))
                for key, item in self.metrics.items()
            },
            "company_score": self.company_score,
            "market_score": self.market_score,
            "total_score": self.total_score,
            "coverage_count": self.coverage_count,
            "coverage_total": self.coverage_total,
            "coverage_pct": self.coverage_pct,
            "eligible_for_ranking": self.eligible_for_ranking,
            "recommendation": self.recommendation.value,
            "recommendation_label": RECOMMENDATION_LABELS[self.recommendation],
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class ScreeningResult:
    generated_at: str
    as_of: str | None
    universe: UniverseSnapshot
    signals: tuple[StockSignal, ...]
    market_overlay: MarketOverlay | None
    data_quality: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        eligible = [item for item in self.signals if item.eligible_for_ranking]
        signal_payloads = [item.to_dashboard_dict() for item in self.signals]
        return {
            "generated_at": self.generated_at,
            "as_of": self.as_of,
            "universe": self.universe.to_dict(),
            "count": len(self.signals),
            "eligible_count": len(eligible),
            "market_overlay": self.market_overlay.to_dict() if self.market_overlay else None,
            "signals": signal_payloads,
            "data_quality": json_safe(self.data_quality),
        }


def _compact_metric_dict(
    metric: MetricValue, component: ScoreComponent | None = None
) -> dict[str, Any]:
    """Keep table-critical metadata without repeating full audit chains."""

    payload: dict[str, Any] = {
        "value": json_safe(metric.value),
        "unit": metric.unit,
        "status": metric.status.value,
        "as_of": metric.as_of,
        "note": metric.note,
    }
    preferred = next(
        (source for source in metric.provenance if source.source != "calculation"),
        metric.provenance[0] if metric.provenance else None,
    )
    if preferred is not None:
        payload["provenance"] = [
            {
                "source": preferred.source,
                "retrieved_at": preferred.retrieved_at,
                "as_of": preferred.as_of,
                "official": preferred.official,
            }
        ]
    else:
        payload["provenance"] = []
    if component is not None:
        payload.update(
            {
                "points": component.points,
                "reason": component.reason,
                "applicable": component.applicable,
            }
        )
    return payload


@dataclass(frozen=True)
class RefreshReport:
    started_at: str
    finished_at: str
    requested: int
    prices_requested: bool
    fundamentals_requested: bool
    price_updated: int
    fundamentals_updated: int
    failures: Mapping[str, tuple[str, ...]]

    def to_dict(self) -> dict[str, Any]:
        return json_safe(asdict(self))


def _optional_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
