"""S&P 500 deterministic quant-signal engine.

This package is research tooling, not individualized investment advice.
"""

from .cache import SQLiteCache
from .models import (
    MarketOverlay,
    MetricStatus,
    MetricValue,
    Recommendation,
    ScreeningResult,
    StockSignal,
)
from .scoring import RULE_DEFINITIONS, get_rule_definitions, get_rules
from .service import SignalService
from .universe import SP500Universe

__all__ = [
    "MarketOverlay",
    "MetricStatus",
    "MetricValue",
    "Recommendation",
    "RULE_DEFINITIONS",
    "SP500Universe",
    "SQLiteCache",
    "ScreeningResult",
    "SignalService",
    "StockSignal",
    "get_rule_definitions",
    "get_rules",
]
