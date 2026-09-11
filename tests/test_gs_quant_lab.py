"""Deterministic tests for the local GS Quant risk-lab contract."""

from __future__ import annotations

import datetime as dt
import json
import math
import unittest
from unittest.mock import patch

import pandas as pd

from gs_quant_lab import GsQuantLabError, GsQuantLabService, YahooHistoryProvider


class FixtureProvider:
    name = "Deterministic fixture provider"
    source_kind = "test_fixture"
    adjustment = "fixture_adjusted_close"

    def __init__(self, history: dict[str, pd.Series]) -> None:
        self.history = history
        self.calls: list[tuple[list[str], str]] = []

    def fetch_history(self, symbols: list[str], lookback: str):
        self.calls.append((list(symbols), lookback))
        return {symbol: self.history[symbol] for symbol in symbols if symbol in self.history}


def fixture_history() -> dict[str, pd.Series]:
    index = pd.date_range("2025-01-02", periods=90, freq="B")
    steps = range(len(index))
    return {
        "AAPL": pd.Series(
            [100 + 0.35 * step + ((step % 7) - 3) * 0.8 for step in steps],
            index=index,
        ),
        "MSFT": pd.Series(
            [95 + 0.25 * step + ((step % 5) - 2) * 0.5 for step in steps],
            index=index,
        ),
        "SPY": pd.Series(
            [100 + 0.2 * step + ((step % 6) - 2.5) * 0.3 for step in steps],
            index=index,
        ),
    }


def assert_finite_or_none(test: unittest.TestCase, value) -> None:
    if isinstance(value, float):
        test.assertTrue(math.isfinite(value), value)
    elif isinstance(value, list):
        for child in value:
            assert_finite_or_none(test, child)
    elif isinstance(value, dict):
        for child in value.values():
            assert_finite_or_none(test, child)


class GsQuantLabServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = FixtureProvider(fixture_history())
        self.now = lambda: dt.datetime(2026, 8, 29, 0, 0, tzinfo=dt.timezone.utc)
        self.payload = {
            "positions": [
                {"symbol": "AAPL", "weight_pct": 60},
                {"symbol": "MSFT", "weight_pct": 40},
            ],
            "benchmark": "SPY",
            "lookback": "3y",
            "risk_free_rate_pct": 4.0,
        }

    def test_construction_is_side_effect_free(self) -> None:
        provider = FixtureProvider(fixture_history())
        GsQuantLabService(provider=provider)
        self.assertEqual(provider.calls, [])

    def test_response_contract_is_finite_and_uses_fixture_provider(self) -> None:
        result = GsQuantLabService(
            history_provider=self.provider, now_fn=self.now
        ).analyze(self.payload)

        self.assertEqual(result["schema_version"], "gs-risk-lab-v1")
        self.assertEqual(result["generated_at"], "2026-08-29T00:00:00Z")
        self.assertEqual(result["as_of"], "2025-05-07")
        self.assertEqual(result["request"], self.payload)
        self.assertEqual(result["normalized_request"], self.payload)
        self.assertEqual(result["engine"]["version"], "2.1.6")
        self.assertFalse(result["engine"]["marquee_connected"])
        self.assertFalse(result["engine"]["marquee_credentials_required"])
        self.assertEqual(result["data"]["lookback"], "3y")
        self.assertEqual(result["data"]["aligned_observations"], 90)
        self.assertEqual(result["data"]["session_policy"], "completed_us_market_sessions_only")
        self.assertEqual(len(result["chart"]["dates"]), 90)
        self.assertEqual(len(result["chart"]["portfolio"]), 90)
        self.assertNotIn("points", result["chart"])
        self.assertNotIn("aligned", result["chart"])
        self.assertNotIn("chart_series", result)
        self.assertNotIn("aligned_chart_series", result)
        self.assertEqual(result["correlation_matrix"]["symbols"], ["AAPL", "MSFT"])
        self.assertEqual(len(result["per_position_metrics"]), 2)
        self.assertIn("daily_constant_weight", result["formula"]["portfolio_method"])
        self.assertEqual(self.provider.calls, [(["AAPL", "MSFT", "SPY"], "3y")])
        self.assertAlmostEqual(
            sum(item["risk_contribution_pct"] for item in result["per_position_metrics"]),
            100.0,
            places=8,
        )

        json.dumps(result, allow_nan=False)
        assert_finite_or_none(self, result)

    def test_tolerated_weights_are_normalized(self) -> None:
        payload = dict(self.payload)
        payload["positions"] = [
            {"symbol": "aapl", "weight_pct": 59.8},
            {"symbol": "msft", "weight_pct": 40.0},
        ]
        result = GsQuantLabService(self.provider, now_fn=self.now).analyze(payload)
        self.assertEqual(
            [item["symbol"] for item in result["normalized_request"]["positions"]],
            ["AAPL", "MSFT"],
        )
        self.assertAlmostEqual(
            sum(item["weight_pct"] for item in result["normalized_request"]["positions"]),
            100.0,
            places=10,
        )

    def test_validation_errors_are_stable(self) -> None:
        invalid_payloads = [
            ({**self.payload, "positions": [{"symbol": "AAPL", "weight_pct": 100}]}, "validation_error"),
            ({**self.payload, "positions": [{"symbol": "AAPL", "weight_pct": 50}, {"symbol": "aapl", "weight_pct": 50}]}, "validation_error"),
            ({**self.payload, "positions": [{"symbol": "AAPL", "weight_pct": 1}, {"symbol": "MSFT", "weight_pct": 1}]}, "validation_error"),
            ({**self.payload, "benchmark": "005930.KS"}, "validation_error"),
            ({**self.payload, "lookback": "2y"}, "validation_error"),
            ({**self.payload, "risk_free_rate_pct": 20.1}, "validation_error"),
            ({**self.payload, "start_date": "2025-01-01"}, "validation_error"),
        ]
        for payload, code in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(GsQuantLabError) as context:
                    GsQuantLabService(self.provider).analyze(payload)
                self.assertEqual(context.exception.status, 400)
                self.assertEqual(context.exception.code, code)
        self.assertEqual(self.provider.calls, [])

    def test_missing_partial_provider_and_overlap_errors_are_distinct(self) -> None:
        with self.assertRaises(GsQuantLabError) as missing:
            GsQuantLabService({}).analyze(self.payload)
        self.assertEqual(missing.exception.code, "missing_data")
        self.assertEqual(missing.exception.status, 422)

        partial_history = fixture_history()
        del partial_history["MSFT"]
        with self.assertRaises(GsQuantLabError) as partial:
            GsQuantLabService(partial_history).analyze(self.payload)
        self.assertEqual(partial.exception.code, "partial_data")
        self.assertEqual(partial.exception.status, 422)

        def failing_provider(symbols, lookback):
            raise RuntimeError("provider secret should not escape")

        with self.assertRaises(GsQuantLabError) as provider_failure:
            GsQuantLabService(failing_provider).analyze(self.payload)
        self.assertEqual(provider_failure.exception.code, "provider_failure")
        self.assertNotIn("secret", provider_failure.exception.message)
        self.assertEqual(provider_failure.exception.status, 502)

        short_index = pd.date_range("2025-01-01", periods=2, freq="B")
        short = {
            symbol: pd.Series([100.0, 101.0], index=short_index)
            for symbol in ("AAPL", "MSFT", "SPY")
        }
        with self.assertRaises(GsQuantLabError) as overlap:
            GsQuantLabService(short).analyze(self.payload)
        self.assertEqual(overlap.exception.code, "insufficient_overlap")
        self.assertEqual(overlap.exception.status, 422)

    def test_partial_rows_are_warned_and_never_serialized_as_nan(self) -> None:
        history = fixture_history()
        history["AAPL"] = history["AAPL"].copy()
        history["AAPL"].iloc[2] = float("nan")
        result = GsQuantLabService(history, now_fn=self.now).analyze(self.payload)
        self.assertTrue(result["data"]["partial"])
        self.assertTrue(any("Partial adjusted history for AAPL" in warning for warning in result["warnings"]))
        json.dumps(result, allow_nan=False)

    def test_in_progress_us_daily_bar_is_excluded_before_session_settlement(self) -> None:
        history = fixture_history()
        current_index = pd.date_range(end="2026-08-28", periods=90, freq="B")
        for series in history.values():
            series.index = current_index
        now = lambda: dt.datetime(2026, 8, 28, 18, 0, tzinfo=dt.timezone.utc)

        result = GsQuantLabService(history, now_fn=now).analyze(self.payload)

        self.assertEqual(result["as_of"], "2026-08-27")
        self.assertEqual(result["data"]["aligned_observations"], 89)
        self.assertTrue(
            any("뉴욕 정규장 미완료 일봉" in warning for warning in result["warnings"])
        )


class YahooHistoryProviderTests(unittest.TestCase):
    @patch("gs_quant_lab.yf.download")
    def test_default_provider_requests_adjusted_daily_history(self, download) -> None:
        download.return_value = pd.DataFrame()
        YahooHistoryProvider().fetch_history(["AAPL", "SPY"], "5y")
        kwargs = download.call_args.kwargs
        self.assertTrue(kwargs["auto_adjust"])
        self.assertEqual(kwargs["period"], "5y")
        self.assertEqual(kwargs["interval"], "1d")
        self.assertFalse(kwargs["actions"])
        self.assertEqual(kwargs["timeout"], 10)


if __name__ == "__main__":
    unittest.main()
