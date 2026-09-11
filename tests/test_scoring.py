"""Fixture-based unittest suite for deterministic scoring and cache behavior."""

from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from quant_engine.cache import SQLiteCache
from quant_engine.models import (
    DatedValue,
    FundamentalData,
    MetricStatus,
    MetricValue,
    PriceHistory,
    Provenance,
    Recommendation,
    StockMetrics,
    UniverseMember,
    UniverseSnapshot,
)
from quant_engine.providers.yfinance_provider import normalize_yahoo_debt_to_equity
from quant_engine.providers.legacy_forward_pe import load_legacy_forward_pe_cache
from quant_engine.providers.yfinance_provider import YFinanceProvider
from quant_engine.scoring import (
    calculate_cash_flow_quality,
    calculate_eps_growth,
    calculate_forward_pe_gap,
    calculate_price_metrics,
    calculate_shareholder_return_metrics,
    get_rule_definitions,
    recommendation_for_score,
    score_market_overlay,
    score_stock,
    validate_annual_share_history,
)
from quant_engine.service import SignalService, _stale_fundamentals
from quant_engine.universe import SP500Universe


FIXTURE_PROVENANCE = Provenance(
    source="unit-test fixture",
    source_url="https://example.invalid/fixture",
    retrieved_at="2026-07-19T12:00:00Z",
    as_of="2026-07-18",
    basis="deterministic values",
)

EXPECTED_COMPANY_RULE_KEYS = (
    "drawdown_52w",
    "pe_gap_3y",
    "distance_200dma",
    "eps_growth_yoy",
    "rsi_14",
    "shareholder_return_3y",
    "shares_dilution_3y",
    "debt_to_equity",
    "cash_flow_quality",
)


def metric(value: float | None, unit: str = "percent") -> MetricValue:
    if value is None:
        return MetricValue.missing(
            unit, "fixture intentionally missing", provenance=(FIXTURE_PROVENANCE,)
        )
    return MetricValue(
        value,
        unit,
        as_of="2026-07-18",
        provenance=(FIXTURE_PROVENANCE,),
    )


def fixture_stock(
    *,
    sector: str = "Information Technology",
    sub_industry: str = "Systems Software",
    **overrides: float | None,
) -> StockMetrics:
    values: dict[str, float | None] = {
        "drawdown_52_week_pct": 0.0,
        "pe_gap_3y_pct": 0.0,
        "distance_200dma_pct": 0.0,
        "eps_growth_yoy_pct": 0.0,
        "rsi_14": 50.0,
        "cash_flow_quality_3y": 0.8,
        "debt_to_equity": 1.0,
        "shareholder_return_3y_avg_pct": None,
        "shares_change_3y_pct": None,
    }
    values.update(overrides)
    units = {
        "drawdown_52_week_pct": "percent",
        "pe_gap_3y_pct": "percent",
        "distance_200dma_pct": "percent",
        "eps_growth_yoy_pct": "percent",
        "rsi_14": "index",
        "cash_flow_quality_3y": "ratio",
        "debt_to_equity": "ratio",
        "shareholder_return_3y_avg_pct": "percent",
        "shares_change_3y_pct": "percent",
    }
    return StockMetrics(
        member=UniverseMember("TEST", "Fixture Corp", sector, sub_industry),
        metrics={key: metric(value, units[key]) for key, value in values.items()},
    )


def component_points(stock: StockMetrics, key: str) -> int | None:
    signal = score_stock(stock)
    return next(item.points for item in signal.components if item.key == key)


class CompanyRuleTests(unittest.TestCase):
    def test_52_week_drawdown_boundaries(self) -> None:
        cases = [
            (-40, 3),
            (-30, 3),
            (-29.99, 2),
            (-15, 2),
            (-14.99, 1),
            (-5, 1),
            (-4.99, 0),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(
                    component_points(
                        fixture_stock(drawdown_52_week_pct=value), "drawdown_52w"
                    ),
                    expected,
                )

    def test_three_year_forward_pe_gap_boundaries(self) -> None:
        for value, expected in [(-25, 2), (-20, 2), (-19.99, 1), (-10, 1), (-9.99, 0)]:
            with self.subTest(value=value):
                self.assertEqual(
                    component_points(fixture_stock(pe_gap_3y_pct=value), "pe_gap_3y"),
                    expected,
                )

    def test_cash_flow_quality_boundaries(self) -> None:
        cases = [
            (1.2, 2),
            (1.19, 1),
            (0.9, 1),
            (0.89, 0),
            (0.76, 0),
            (0.75, -1),
            (0.51, -1),
            (0.5, -2),
            (-0.2, -2),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(
                    component_points(
                        fixture_stock(cash_flow_quality_3y=value),
                        "cash_flow_quality",
                    ),
                    expected,
                )

    def test_200dma_distance_boundaries(self) -> None:
        for value, expected in [(-25, 2), (-20, 2), (-19.99, 1), (-5, 1), (-4.99, 0)]:
            with self.subTest(value=value):
                self.assertEqual(
                    component_points(
                        fixture_stock(distance_200dma_pct=value), "distance_200dma"
                    ),
                    expected,
                )

    def test_eps_growth_boundaries(self) -> None:
        cases = [
            (20, 2),
            (19.99, 1),
            (5, 1),
            (4.99, 0),
            (-4.99, 0),
            (-5, -1),
            (-19.99, -1),
            (-20, -2),
            (-50, -2),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(
                    component_points(
                        fixture_stock(eps_growth_yoy_pct=value), "eps_growth_yoy"
                    ),
                    expected,
                )

    def test_rsi_boundaries(self) -> None:
        for value, expected in [(80, -2), (75, -2), (74.99, -1), (65, -1), (64.99, 0), (25, 1), (25.01, 0)]:
            with self.subTest(value=value):
                self.assertEqual(
                    component_points(fixture_stock(rsi_14=value), "rsi_14"), expected
                )

    def test_debt_to_equity_boundaries_and_yahoo_normalization(self) -> None:
        for value, expected in [(0.2, 1), (0.25, 1), (0.251, 0), (2, -1), (3.99, -1), (4, -2)]:
            with self.subTest(value=value):
                self.assertEqual(
                    component_points(
                        fixture_stock(debt_to_equity=value), "debt_to_equity"
                    ),
                    expected,
                )
        self.assertAlmostEqual(normalize_yahoo_debt_to_equity(79.548), 0.79548)
        self.assertIsNone(normalize_yahoo_debt_to_equity(None))
        self.assertIsNone(normalize_yahoo_debt_to_equity(-10))

    def test_shareholder_return_boundaries(self) -> None:
        cases = [(1.99, 0), (2, 1), (3.99, 1), (4, 2), (5.99, 2), (6, 3), (9, 3)]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(
                    component_points(
                        fixture_stock(shareholder_return_3y_avg_pct=value),
                        "shareholder_return_3y",
                    ),
                    expected,
                )

    def test_three_year_share_dilution_penalties(self) -> None:
        cases = [(-10, 0), (0.99, 0), (1, -1), (4.99, -1), (5, -2), (9.99, -2), (10, -3)]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(
                    component_points(
                        fixture_stock(shares_change_3y_pct=value),
                        "shares_dilution_3y",
                    ),
                    expected,
                )

    def test_financial_and_reit_leverage_are_not_scored(self) -> None:
        financial = score_stock(
            fixture_stock(sector="Financials", debt_to_equity=8.0)
        )
        reit = score_stock(
            fixture_stock(
                sector="Real Estate",
                sub_industry="Retail REITs",
                debt_to_equity=8.0,
            )
        )
        for signal in (financial, reit):
            item = next(x for x in signal.components if x.key == "debt_to_equity")
            self.assertFalse(item.applicable)
            self.assertIsNone(item.points)
            self.assertNotEqual(signal.company_score, -2)
        self.assertEqual(financial.coverage_total, 7)
        self.assertEqual(financial.coverage_count, 5)
        self.assertEqual(financial.coverage_pct, 71.4)
        self.assertEqual(reit.coverage_total, 7)
        self.assertEqual(reit.coverage_count, 5)
        self.assertEqual(reit.coverage_pct, 71.4)

    def test_missing_is_null_not_silent_zero(self) -> None:
        stock = fixture_stock(
            drawdown_52_week_pct=None,
            pe_gap_3y_pct=None,
            distance_200dma_pct=None,
            cash_flow_quality_3y=None,
        )
        signal = score_stock(stock)
        self.assertEqual(signal.coverage_count, 3)
        self.assertFalse(signal.eligible_for_ranking)
        self.assertEqual(signal.recommendation, Recommendation.INSUFFICIENT_DATA)
        missing = [item for item in signal.components if item.points is None]
        self.assertEqual(len(missing), 6)
        payload = signal.to_dict()
        self.assertIsNone(payload["metrics"]["pe_gap_3y_pct"]["value"])
        json.dumps(payload, allow_nan=False)

    def test_dashboard_row_is_compact_but_keeps_metric_scores(self) -> None:
        signal = score_stock(fixture_stock(drawdown_52_week_pct=-30.0))
        payload = signal.to_dashboard_dict()
        self.assertNotIn("components", payload)
        self.assertNotIn("history", payload)
        drawdown = payload["metrics"]["drawdown_52_week_pct"]
        self.assertEqual(drawdown["points"], 3)
        self.assertTrue(drawdown["applicable"])
        self.assertLessEqual(len(drawdown["provenance"]), 1)
        if drawdown["provenance"]:
            self.assertNotIn("basis", drawdown["provenance"][0])
        json.dumps(payload, allow_nan=False)


class DerivedMetricTests(unittest.TestCase):
    def test_three_year_cash_flow_quality_uses_comparable_periods(self) -> None:
        cash = tuple(
            DatedValue(
                period,
                value,
                "annual_operating_cash_flow",
                (FIXTURE_PROVENANCE,),
            )
            for period, value in (
                ("2025-12-31", 120.0),
                ("2024-12-31", 90.0),
                ("2023-12-31", 80.0),
            )
        )
        income = tuple(
            DatedValue(
                period,
                value,
                "annual_net_income",
                (FIXTURE_PROVENANCE,),
            )
            for period, value in (
                ("2025-12-31", 100.0),
                ("2024-12-31", 100.0),
                ("2023-12-31", 100.0),
            )
        )
        quality, history = calculate_cash_flow_quality(cash, income)
        self.assertAlmostEqual(quality.value or 0, 290 / 300)
        self.assertEqual(len(history), 3)
        self.assertEqual(history[-1].period_end, "2025-12-31")

    def test_forward_pe_gap_requires_comparable_dated_forward_history(self) -> None:
        current = MetricValue(
            8.0,
            "multiple",
            as_of="2026-07-18",
            provenance=(FIXTURE_PROVENANCE,),
        )
        history = [
            DatedValue("2025-07-18", 10.0, "forward_pe", (FIXTURE_PROVENANCE,)),
            DatedValue("2024-07-18", 10.0, "forward_pe", (FIXTURE_PROVENANCE,)),
            DatedValue("2023-07-18", 10.0, "forward_pe", (FIXTURE_PROVENANCE,)),
        ]
        median_metric, gap = calculate_forward_pe_gap(current, history)
        self.assertEqual(median_metric.value, 10.0)
        self.assertEqual(gap.value, -20.0)
        self.assertEqual(component_points(fixture_stock(pe_gap_3y_pct=gap.value), "pe_gap_3y"), 2)

        _, invalid_gap = calculate_forward_pe_gap(
            current, [DatedValue("2025-07-18", 10.0, "trailing_pe")]
        )
        self.assertIsNone(invalid_gap.value)
        self.assertEqual(invalid_gap.status, MetricStatus.MISSING)
        self.assertIn("not substituted", invalid_gap.note or "")

        _, one_point_gap = calculate_forward_pe_gap(
            current,
            [DatedValue("2025-07-18", 10.0, "forward_pe", (FIXTURE_PROVENANCE,))],
        )
        self.assertIsNone(one_point_gap.value)
        self.assertIn("at least 2", one_point_gap.note or "")

        two_point_median, two_point_gap = calculate_forward_pe_gap(
            current,
            [
                DatedValue("2025-07-18", 10.0, "forward_pe", (FIXTURE_PROVENANCE,)),
                DatedValue("2024-07-18", 10.0, "forward_pe", (FIXTURE_PROVENANCE,)),
            ],
        )
        self.assertEqual(two_point_median.value, 10.0)
        self.assertEqual(two_point_gap.value, -20.0)
        self.assertIn("2 of", two_point_gap.note or "")

    def test_legacy_forward_pe_loader_validates_current_and_keeps_dates_undated(self) -> None:
        fixture = [
            {
                "Symbol": "AAA",
                "url": "https://stockanalysis.com/stocks/aaa/financials/ratios/",
                "Forward PE": 15.0,
                "Forward PE_hist": [15.0, 12.0, 10.0, None, 8.0, 7.0],
                "PE Ratio": 18.0,
                "PE Ratio_hist": [18.0, 16.0, 14.0, 12.0, 10.0, 8.0],
                "Dividend Yield": 2.5,
                "Dividend Yield_hist": [2.5, 2.7, 2.9, 3.0],
            },
            {
                "Symbol": "BAD",
                "url": "https://stockanalysis.com/stocks/bad/financials/ratios/",
                "Forward PE": 20.0,
                "Forward PE_hist": [19.0, 10.0, 9.0],
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.json"
            path.write_text(json.dumps(fixture), encoding="utf-8")
            loaded = load_legacy_forward_pe_cache(path)
            self.assertEqual(loaded.records_seen, 2)
            self.assertEqual(loaded.records_loaded, 1)
            self.assertNotIn("BAD", loaded.history_by_symbol)
            observations = loaded.history_by_symbol["AAA"]
            self.assertEqual([item.value for item in observations], [12.0, 10.0, 8.0, 7.0])
            self.assertEqual(
                [item.period_end for item in observations],
                [
                    "legacy_fy_minus_1",
                    "legacy_fy_minus_2",
                    "legacy_fy_minus_4",
                    "legacy_fy_minus_5",
                ],
            )
            self.assertTrue(all(item.provenance for item in observations))
            self.assertIn("File modification time", observations[0].provenance[0].notes[1])
            trailing = loaded.trailing_pe_by_symbol["AAA"]
            self.assertEqual(
                [item.value for item in trailing],
                [16.0, 14.0, 12.0, 10.0, 8.0],
            )
            self.assertTrue(all(item.period_type == "trailing_pe" for item in trailing))
            self.assertEqual(
                [item.value for item in loaded.dividend_yield_by_symbol["AAA"]],
                [2.5, 2.7, 2.9],
            )

            provider = YFinanceProvider(
                legacy_forward_pe_path=path,
                auto_detect_legacy=False,
            )
            self.assertEqual(len(provider.historical_forward_pe["AAA"]), 4)
            self.assertEqual(len(provider.historical_trailing_pe["AAA"]), 5)
            self.assertEqual(len(provider.historical_dividend_yield["AAA"]), 3)
            current = MetricValue(
                9.0,
                "multiple",
                as_of="2026-07-18",
                provenance=(FIXTURE_PROVENANCE,),
            )
            median_metric, gap = calculate_forward_pe_gap(
                current, provider.historical_forward_pe["AAA"]
            )
            self.assertEqual(median_metric.value, 10.0)
            self.assertEqual(gap.value, -10.0)
            self.assertEqual(gap.status, MetricStatus.STALE)
            self.assertIn("legacy", (gap.note or "").casefold())

    def test_eps_growth_uses_latest_comparable_annual_periods(self) -> None:
        values = [
            DatedValue("2025-12-31", 1.20, "annual", (FIXTURE_PROVENANCE,)),
            DatedValue("2024-12-31", 1.00, "annual", (FIXTURE_PROVENANCE,)),
        ]
        latest, prior, growth = calculate_eps_growth(values)
        self.assertEqual(latest.value, 1.20)
        self.assertEqual(prior.value, 1.00)
        self.assertEqual(growth.value, 20.0)

        _, _, invalid = calculate_eps_growth(
            [
                DatedValue("2025-12-31", 1.0),
                DatedValue("2024-12-31", 0.0),
            ]
        )
        self.assertIsNone(invalid.value)
        self.assertEqual(invalid.status, MetricStatus.MISSING)

    def test_shareholder_return_proxy_uses_three_dividends_and_four_share_years(self) -> None:
        dividends = (
            DatedValue("legacy_current", 2.5, "dividend_yield", (FIXTURE_PROVENANCE,)),
            DatedValue("legacy_fy_minus_1", 2.7, "dividend_yield", (FIXTURE_PROVENANCE,)),
            DatedValue("legacy_fy_minus_2", 2.9, "dividend_yield", (FIXTURE_PROVENANCE,)),
        )
        shares = (
            DatedValue("2025-12-31", 100.0, "annual_diluted_shares", (FIXTURE_PROVENANCE,)),
            DatedValue("2024-12-31", 105.0, "annual_diluted_shares", (FIXTURE_PROVENANCE,)),
            DatedValue("2023-12-31", 110.0, "annual_diluted_shares", (FIXTURE_PROVENANCE,)),
            DatedValue("2022-12-31", 115.0, "annual_diluted_shares", (FIXTURE_PROVENANCE,)),
        )
        dividend_avg, buyback_avg, shareholder, share_change, history = (
            calculate_shareholder_return_metrics(dividends, shares)
        )
        self.assertAlmostEqual(dividend_avg.value or 0, 2.7)
        self.assertGreater(buyback_avg.value or 0, 4.0)
        self.assertGreater(shareholder.value or 0, 6.0)
        self.assertAlmostEqual(share_change.value or 0, -13.0434782609)
        self.assertEqual(len(history), 3)
        self.assertEqual(shareholder.status, MetricStatus.STALE)

    def test_share_continuity_outlier_is_withheld_from_score_and_chart(self) -> None:
        dividends = (
            DatedValue("legacy_current", 2.0, "dividend_yield", (FIXTURE_PROVENANCE,)),
            DatedValue("legacy_fy_minus_1", 2.0, "dividend_yield", (FIXTURE_PROVENANCE,)),
            DatedValue("legacy_fy_minus_2", 2.0, "dividend_yield", (FIXTURE_PROVENANCE,)),
        )
        shares = (
            DatedValue("2025-12-31", 228_000_000, "annual_diluted_shares", (FIXTURE_PROVENANCE,)),
            DatedValue("2024-12-31", 23_110_000_000, "annual_diluted_shares", (FIXTURE_PROVENANCE,)),
            DatedValue("2023-12-31", 232_000_000, "annual_diluted_shares", (FIXTURE_PROVENANCE,)),
            DatedValue("2022-12-31", 236_000_000, "annual_diluted_shares", (FIXTURE_PROVENANCE,)),
        )
        validated, warning = validate_annual_share_history(shares, limit=4)
        self.assertEqual(validated, ())
        self.assertIn("outlier", warning or "")

        _, buyback, shareholder, share_change, history = (
            calculate_shareholder_return_metrics(dividends, shares)
        )
        self.assertEqual(buyback.status, MetricStatus.MISSING)
        self.assertEqual(shareholder.status, MetricStatus.MISSING)
        self.assertEqual(share_change.status, MetricStatus.MISSING)
        self.assertIn("outlier", buyback.note or "")
        self.assertEqual(history, ())

    def test_stale_history_marker_propagates_to_derived_metrics(self) -> None:
        base = FundamentalData(
            symbol="TEST",
            forward_pe=metric(8.0, "multiple"),
            forward_pe_history=(
                DatedValue("2025-07-18", 10.0, "forward_pe", (FIXTURE_PROVENANCE,)),
                DatedValue("2024-07-18", 11.0, "forward_pe", (FIXTURE_PROVENANCE,)),
            ),
            trailing_pe=metric(9.0, "multiple"),
            annual_diluted_eps=(
                DatedValue("2025-12-31", 1.2, "annual", (FIXTURE_PROVENANCE,)),
                DatedValue("2024-12-31", 1.0, "annual", (FIXTURE_PROVENANCE,)),
            ),
            debt_to_equity=metric(0.3, "ratio"),
            # Provider/model input only; market cap is not a score/display factor.
            market_cap=metric(1_000_000_000, "USD"),
            annual_diluted_shares=(
                DatedValue("2025-12-31", 100.0, "annual_diluted_shares", (FIXTURE_PROVENANCE,)),
                DatedValue("2024-12-31", 102.0, "annual_diluted_shares", (FIXTURE_PROVENANCE,)),
                DatedValue("2023-12-31", 104.0, "annual_diluted_shares", (FIXTURE_PROVENANCE,)),
                DatedValue("2022-12-31", 106.0, "annual_diluted_shares", (FIXTURE_PROVENANCE,)),
            ),
            dividend_yield_history=(
                DatedValue("legacy_current", 2.0, "dividend_yield", (FIXTURE_PROVENANCE,)),
                DatedValue("legacy_fy_minus_1", 2.1, "dividend_yield", (FIXTURE_PROVENANCE,)),
                DatedValue("legacy_fy_minus_2", 2.2, "dividend_yield", (FIXTURE_PROVENANCE,)),
            ),
        )
        stale = _stale_fundamentals(base, "fixture provider outage")
        self.assertTrue(
            any(
                "stale cache used" in note
                for source in stale.annual_diluted_eps[0].provenance
                for note in source.notes
            )
        )

        _, _, eps_growth = calculate_eps_growth(stale.annual_diluted_eps)
        _, pe_gap = calculate_forward_pe_gap(
            stale.forward_pe, stale.forward_pe_history
        )
        _, buyback, _, share_change, _ = calculate_shareholder_return_metrics(
            stale.dividend_yield_history, stale.annual_diluted_shares
        )
        self.assertEqual(eps_growth.status, MetricStatus.STALE)
        self.assertEqual(pe_gap.status, MetricStatus.STALE)
        self.assertEqual(buyback.status, MetricStatus.STALE)
        self.assertEqual(share_change.status, MetricStatus.STALE)

    def test_price_history_cache_parses_date_close_pairs_together(self) -> None:
        payload = {
            "symbol": "TEST",
            "dates": ["2026-01-01", "2026-01-02", "2026-01-03"],
            "adjusted_closes": [100.0, None, 300.0],
            "provenance": FIXTURE_PROVENANCE.to_dict(),
        }
        history = PriceHistory.from_dict(payload)
        self.assertEqual(history.dates, ("2026-01-01", "2026-01-03"))
        self.assertEqual(history.adjusted_closes, (100.0, 300.0))

    def test_price_metrics_use_adjusted_close_and_wilder_rsi(self) -> None:
        start = date(2025, 9, 1)
        dates = tuple((start + timedelta(days=i)).isoformat() for i in range(220))
        closes = tuple(100.0 + i for i in range(220))
        history = PriceHistory(
            symbol="TEST",
            dates=dates,
            adjusted_closes=closes,
            provenance=FIXTURE_PROVENANCE,
        )
        result = calculate_price_metrics(history)
        self.assertEqual(result.current_price.value, 319.0)
        self.assertEqual(result.high_52_week.value, 319.0)
        self.assertEqual(result.drawdown_52_week_pct.value, 0.0)
        self.assertAlmostEqual(result.sma_200.value or 0, 219.5)
        self.assertEqual(result.rsi_14.value, 100.0)

    def test_price_metrics_include_skipped_month_return_inputs(self) -> None:
        start = date(2025, 1, 1)
        dates = tuple((start + timedelta(days=i)).isoformat() for i in range(320))
        closes = tuple(100.0 + i * 0.25 for i in range(320))
        result = calculate_price_metrics(
            PriceHistory(
                symbol="TEST",
                dates=dates,
                adjusted_closes=closes,
                provenance=FIXTURE_PROVENANCE,
            )
        )
        self.assertTrue(result.return_6_1_pct.usable)
        self.assertTrue(result.return_12_1_pct.usable)
        self.assertTrue(result.volatility_12m_pct.usable)


class RecommendationAndOverlayTests(unittest.TestCase):
    def test_recommendation_ranges_are_exact(self) -> None:
        cases = [
            (10, Recommendation.STRONG_BUY),
            (9, Recommendation.WATCH_BUY),
            (5, Recommendation.WATCH_BUY),
            (4, Recommendation.NEUTRAL),
            (-2, Recommendation.NEUTRAL),
            (-3, Recommendation.WATCH_SELL),
            (-7, Recommendation.WATCH_SELL),
            (-8, Recommendation.SELL),
        ]
        for score, expected in cases:
            with self.subTest(score=score):
                self.assertEqual(recommendation_for_score(score), expected)
        self.assertEqual(
            recommendation_for_score(99, coverage_count=3),
            Recommendation.INSUFFICIENT_DATA,
        )

    def test_market_overlay_is_separate_and_marks_missing(self) -> None:
        overlay = score_market_overlay(
            {
                "spy_drawdown_52_week_pct": metric(-1.1),
                "vix": metric(16.5, "index"),
                "fear_greed": metric(43, "index"),
            }
        )
        self.assertEqual(overlay.score, -1)
        self.assertTrue(overlay.complete)
        company = score_stock(fixture_stock(), overlay)
        self.assertEqual(company.company_score, 0)
        self.assertEqual(company.market_score, -1)
        self.assertEqual(company.total_score, -1)

        partial = score_market_overlay(
            {
                "spy_drawdown_52_week_pct": metric(-25),
                "vix": metric(None, "index"),
                "fear_greed": metric(None, "index"),
            }
        )
        self.assertEqual(partial.score, 1)
        self.assertFalse(partial.complete)
        self.assertIsNone(partial.components[1].points)

    def test_rules_and_signal_components_exclude_retired_factors(self) -> None:
        rules = get_rule_definitions()
        rule_keys = tuple(rule["key"] for rule in rules["company_rules"])
        self.assertEqual(rule_keys, EXPECTED_COMPANY_RULE_KEYS)
        self.assertEqual(len(rule_keys), 9)
        self.assertNotIn("sector_relative_value", rule_keys)
        self.assertNotIn("relative_momentum", rule_keys)
        self.assertNotIn("market_cap", rule_keys)

        signal = score_stock(
            fixture_stock(
                shareholder_return_3y_avg_pct=2.0,
                shares_change_3y_pct=0.0,
            )
        )
        component_keys = tuple(component.key for component in signal.components)
        self.assertEqual(component_keys, EXPECTED_COMPANY_RULE_KEYS)
        self.assertEqual(signal.coverage_total, 9)
        self.assertEqual(signal.coverage_count, 9)
        self.assertNotIn("sector_relative_value", component_keys)
        self.assertNotIn("relative_momentum", component_keys)
        self.assertNotIn("market_cap", component_keys)
        self.assertEqual(rules["minimum_company_metrics"], 5)
        self.assertEqual(
            rules["company_rules"][0]["thresholds"][0],
            {"operator": "<=", "value": -30.0, "points": 3},
        )


class UniverseAndCacheServiceTests(unittest.TestCase):
    def test_checked_in_universe_is_complete_and_rejects_header_tokens(self) -> None:
        snapshot = SP500Universe().get_snapshot()
        self.assertEqual(len(snapshot.members), 503)
        self.assertNotIn("SYMBOL", snapshot.symbols)
        self.assertNotIn("TICKER", snapshot.symbols)
        self.assertIn("BRK.B", snapshot.symbols)
        self.assertEqual(
            next(item for item in snapshot.members if item.symbol == "BRK.B").provider_symbol,
            "BRK-B",
        )
        self.assertIsNotNone(snapshot.provenance.as_of)

    def test_dashboard_is_cache_only_and_incremental_fetches_only_missing(self) -> None:
        provider = FixtureProvider()
        universe = FixtureUniverse()
        with tempfile.TemporaryDirectory() as directory:
            service = SignalService(
                provider=provider,
                universe=universe,  # type: ignore[arg-type]
                cache=SQLiteCache(Path(directory) / "fixture.sqlite3"),
            )
            initial = service.screen(["AAA"])
            self.assertEqual(provider.price_batches, [])
            self.assertEqual(provider.fundamental_batches, [])
            self.assertEqual(initial.signals[0].recommendation, Recommendation.INSUFFICIENT_DATA)

            first = service.screen(["AAA"], allow_fetch=True)
            self.assertIn(("AAA",), provider.price_batches)
            self.assertIn(("AAA",), provider.fundamental_batches)
            self.assertTrue(first.signals[0].eligible_for_ranking)
            calls_after_first = (len(provider.price_batches), len(provider.fundamental_batches))

            service.screen(["AAA"])
            self.assertEqual(
                (len(provider.price_batches), len(provider.fundamental_batches)),
                calls_after_first,
            )

            service.screen(["AAA", "BBB"], allow_fetch=True)
            self.assertIn(("BBB",), provider.price_batches)
            self.assertIn(("BBB",), provider.fundamental_batches)
            self.assertNotIn(("AAA", "BBB"), provider.fundamental_batches)


class FixtureUniverse:
    def __init__(self) -> None:
        self._members = (
            UniverseMember("AAA", "Alpha", "Industrials", "Machinery"),
            UniverseMember("BBB", "Beta", "Health Care", "Biotechnology"),
        )
        self._snapshot = UniverseSnapshot(
            members=self._members,
            provenance=FIXTURE_PROVENANCE,
            fallback_used=False,
        )

    def get_snapshot(self, force_refresh: bool = False) -> UniverseSnapshot:
        return self._snapshot

    def get_symbols(self) -> list[str]:
        return [item.symbol for item in self._members]


class FixtureProvider:
    name = "fixture provider"

    def __init__(self) -> None:
        self.price_batches: list[tuple[str, ...]] = []
        self.fundamental_batches: list[tuple[str, ...]] = []
        self.fear_greed_calls = 0

    def fetch_price_history(
        self, symbols: list[str] | tuple[str, ...], *, period: str = "1y"
    ) -> dict[str, PriceHistory]:
        self.price_batches.append(tuple(symbols))
        start = date(2025, 9, 1)
        dates = tuple((start + timedelta(days=i)).isoformat() for i in range(220))
        output: dict[str, PriceHistory] = {}
        for symbol in symbols:
            base = 20.0 if symbol == "^VIX" else 100.0
            closes = tuple(base + (i % 5) for i in range(220))
            output[symbol] = PriceHistory(
                symbol=symbol,
                dates=dates,
                adjusted_closes=closes,
                provenance=FIXTURE_PROVENANCE,
            )
        return output

    def fetch_fundamentals(
        self, symbols: list[str] | tuple[str, ...]
    ) -> dict[str, FundamentalData]:
        self.fundamental_batches.append(tuple(symbols))
        return {
            symbol: FundamentalData(
                symbol=symbol,
                forward_pe=MetricValue(
                    8.0,
                    "multiple",
                    as_of="2026-07-18",
                    provenance=(FIXTURE_PROVENANCE,),
                ),
                forward_pe_history=(
                    DatedValue(
                        "2025-07-18", 10.0, "forward_pe", (FIXTURE_PROVENANCE,)
                    ),
                ),
                trailing_pe=metric(9.0, "multiple"),
                annual_diluted_eps=(
                    DatedValue("2025-12-31", 1.2, "annual", (FIXTURE_PROVENANCE,)),
                    DatedValue("2024-12-31", 1.0, "annual", (FIXTURE_PROVENANCE,)),
                ),
                debt_to_equity=metric(0.2, "ratio"),
                # Provider/model input only; market cap is not a score/display factor.
                market_cap=metric(1_000_000_000, "USD"),
                annual_diluted_shares=(
                    DatedValue("2025-12-31", 100.0, "annual_diluted_shares", (FIXTURE_PROVENANCE,)),
                    DatedValue("2024-12-31", 102.0, "annual_diluted_shares", (FIXTURE_PROVENANCE,)),
                    DatedValue("2023-12-31", 104.0, "annual_diluted_shares", (FIXTURE_PROVENANCE,)),
                    DatedValue("2022-12-31", 106.0, "annual_diluted_shares", (FIXTURE_PROVENANCE,)),
                ),
                dividend_yield_history=(
                    DatedValue("legacy_current", 2.0, "dividend_yield", (FIXTURE_PROVENANCE,)),
                    DatedValue("legacy_fy_minus_1", 2.1, "dividend_yield", (FIXTURE_PROVENANCE,)),
                    DatedValue("legacy_fy_minus_2", 2.2, "dividend_yield", (FIXTURE_PROVENANCE,)),
                ),
                currency="USD",
            )
            for symbol in symbols
        }

    def fetch_fear_greed(self) -> MetricValue:
        self.fear_greed_calls += 1
        return metric(50.0, "index")


if __name__ == "__main__":
    unittest.main()
