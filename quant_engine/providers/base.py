"""Provider protocol used by :mod:`quant_engine.service` and test fixtures."""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from ..models import FundamentalData, MetricValue, PriceHistory


@runtime_checkable
class MarketDataProvider(Protocol):
    name: str

    def fetch_price_history(
        self, symbols: Sequence[str], *, period: str = "1y"
    ) -> Mapping[str, PriceHistory]: ...

    def fetch_fundamentals(
        self, symbols: Sequence[str]
    ) -> Mapping[str, FundamentalData]: ...

    def fetch_weekly_valuation_history(
        self, symbol: str
    ) -> Mapping[str, Any]:
        """Return the versioned five-year weekly valuation payload.

        Implementations must emit ISO dates representing each week's last
        available trading close, explicit ``frequency``/point-count/range
        metadata, and provenance.  Historical Forward P/E remains a
        quarterly-anchor proxy; this interface does not imply a true weekly
        analyst-consensus history.
        """
        ...

    def fetch_fear_greed(self) -> MetricValue: ...
