from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import tempfile
import unittest

from quant_engine.cache import SQLiteCache
from quant_engine.service import (
    VALUATION_HISTORY_NAMESPACE,
    SignalService,
    _has_weekly_valuation_points,
    _weekly_valuation_payload_error,
)
from quant_engine.providers.stockanalysis_valuation import (
    ValuationAnchor,
    align_anchor_closes,
    calculate_weekly_valuation_series,
    parse_quarterly_valuation_html,
    weekly_valuation_metadata,
    weekly_last_closes,
)


class QuarterlyValuationParserTests(unittest.TestCase):
    def test_parser_keeps_exact_period_dates_and_ratio_rows(self) -> None:
        html = """
        <table>
          <thead>
            <tr><th>Fiscal Quarter</th><th>Current</th><th>Q4 2025</th></tr>
            <tr>
              <th>Period Ending</th>
              <th>Aug '26 Aug 5, 2026</th>
              <th>Sep '25 Sep 27, 2025</th>
            </tr>
          </thead>
          <tbody>
            <tr><td>Last Close Price</td><td>311.00</td><td>255.46</td></tr>
            <tr><td>PE Ratio</td><td>35.49</td><td>34.24</td></tr>
            <tr><td>Forward PE</td><td>33.82</td><td>32.97</td></tr>
          </tbody>
        </table>
        """

        anchors = parse_quarterly_valuation_html(html)

        self.assertEqual(len(anchors), 2)
        self.assertEqual(anchors[0].period_end, "2025-09-27")
        self.assertEqual(anchors[1].period_end, "2026-08-05")
        self.assertAlmostEqual(anchors[0].close, 255.46)
        self.assertAlmostEqual(anchors[1].forward_pe or 0, 33.82)


class WeeklyValuationCalculationTests(unittest.TestCase):
    def test_missing_vendor_anchor_close_uses_nearest_prior_yahoo_close(self) -> None:
        anchors = (
            ValuationAnchor("2026-03-28", None, 20.0, 18.0),
            ValuationAnchor("2026-06-27", 120.0, 22.0, 19.0),
        )

        aligned = align_anchor_closes(
            anchors,
            (
                ("2026-03-27", 101.0),
                ("2026-03-30", 102.0),
                ("2026-06-26", 119.0),
            ),
        )

        self.assertEqual(aligned[0].close, 101.0)
        self.assertEqual(aligned[1].close, 120.0)

    def test_last_trading_close_is_kept_for_each_iso_week(self) -> None:
        weekly = weekly_last_closes(
            (
                ("2026-07-27", 100.0),
                ("2026-07-31", 104.0),
                ("2026-08-01", 999.0),
                ("2026-08-03", 105.0),
                ("2026-08-05", 107.0),
            )
        )

        self.assertEqual(
            weekly,
            (("2026-07-31", 104.0), ("2026-08-05", 107.0)),
        )

    def test_weekly_ratios_use_latest_quarterly_implied_eps(self) -> None:
        anchors = (
            ValuationAnchor("2024-01-01", 100.0, 10.0, 8.0),
            ValuationAnchor("2024-03-31", 120.0, 12.0, 10.0),
        )
        weekly = (
            ("2023-12-29", 90.0),
            ("2024-01-05", 110.0),
            ("2024-03-29", 130.0),
            ("2024-04-05", 120.0),
        )

        trailing, forward = calculate_weekly_valuation_series(weekly, anchors)

        self.assertEqual([item["date"] for item in trailing], [
            "2024-01-05",
            "2024-03-29",
            "2024-04-05",
        ])
        self.assertEqual([item["value"] for item in trailing], [11.0, 13.0, 12.0])
        self.assertEqual([item["value"] for item in forward], [8.8, 10.4, 10.0])
        self.assertEqual(trailing[-1]["anchor_date"], "2024-03-31")

    def test_undefined_trailing_pe_omits_weeks_without_carrying_prior_eps(self) -> None:
        anchors = (
            ValuationAnchor("2024-01-01", 100.0, 10.0, 8.0),
            ValuationAnchor("2024-01-31", 120.0, None, 9.0),
        )
        weekly = (
            ("2024-01-05", 110.0),
            ("2024-02-02", 130.0),
        )

        trailing, forward = calculate_weekly_valuation_series(weekly, anchors)

        self.assertEqual([item["date"] for item in trailing], ["2024-01-05"])
        self.assertEqual(
            [item["date"] for item in forward], ["2024-01-05", "2024-02-02"]
        )

    def test_weekly_payload_metadata_reports_count_range_and_frequency(self) -> None:
        points = [
            {"date": f"2024-01-{day:02d}", "value": 20.0}
            for day in (5, 12, 19, 26)
        ]
        metadata = weekly_valuation_metadata(points, points)

        self.assertEqual(metadata["frequency"], "weekly")
        self.assertEqual(metadata["point_count"], 4)
        self.assertEqual(
            metadata["point_counts"],
            {"trailing_pe_weekly": 4, "forward_pe_weekly": 4},
        )
        self.assertEqual(
            metadata["range"],
            {"start": "2024-01-05", "end": "2024-01-26"},
        )


class WeeklyValuationCacheContractTests(unittest.TestCase):
    @staticmethod
    def _points(
        count: int = 8,
        start: date = date(2025, 1, 3),
    ) -> list[dict[str, object]]:
        return [
            {
                "date": (start + timedelta(days=index * 7)).isoformat(),
                "value": 20.0 + index,
            }
            for index in range(count)
        ]

    @staticmethod
    def _payload_for_series(
        trailing_points: list[dict[str, object]],
        forward_points: list[dict[str, object]],
    ) -> dict[str, object]:
        metadata = weekly_valuation_metadata(trailing_points, forward_points)
        return {
            "symbol": "AAA",
            "schema_version": 2,
            "period": "5y",
            "frequency": "weekly",
            "as_of": metadata["coverage_end"],
            "coverage_start": metadata["coverage_start"],
            "coverage_end": metadata["coverage_end"],
            "point_count": metadata["point_count"],
            "point_counts": metadata["point_counts"],
            "metadata": metadata,
            "series": {
                "trailing_pe_weekly": trailing_points,
                "forward_pe_weekly": forward_points,
            },
            "provenance": [],
        }

    def _payload(self, count: int = 8) -> dict[str, object]:
        points = self._points(count)
        return self._payload_for_series(points, points)

    def test_validator_accepts_year_gap_when_other_series_is_continuous(self) -> None:
        continuous = self._points()
        gapped = []
        for index, point in enumerate(continuous):
            day = date.fromisoformat(str(point["date"]))
            if index >= 4:
                day += timedelta(days=364)
            gapped.append({**point, "date": day.isoformat()})

        self.assertEqual(
            (
                date.fromisoformat(str(gapped[4]["date"]))
                - date.fromisoformat(str(gapped[3]["date"]))
            ).days,
            371,
        )
        payload = self._payload_for_series(gapped, continuous)

        self.assertIsNone(_weekly_valuation_payload_error(payload))
        self.assertTrue(_has_weekly_valuation_points(payload))

    def test_validator_rejects_dated_annual_slots_labeled_weekly(self) -> None:
        annual_dates = (
            "2017-01-06",
            "2018-01-05",
            "2019-01-04",
            "2020-01-03",
            "2021-01-08",
            "2022-01-07",
            "2023-01-06",
            "2024-01-05",
        )
        annual = [
            {"date": day, "value": 20.0 + index}
            for index, day in enumerate(annual_dates)
        ]
        payload = self._payload_for_series(annual, annual)

        error = _weekly_valuation_payload_error(payload)

        self.assertIsNotNone(error)
        self.assertIn("cadence", error or "")

    def test_validator_rejects_annual_secondary_series_even_with_continuous_peer(
        self,
    ) -> None:
        annual = [
            {
                "date": (date(2017, 1, 6) + timedelta(days=index * 364)).isoformat(),
                "value": 20.0 + index,
            }
            for index in range(8)
        ]
        payload = self._payload_for_series(annual, self._points())

        error = _weekly_valuation_payload_error(payload)

        self.assertIsNotNone(error)
        self.assertIn("trailing_pe_weekly", error or "")
        self.assertIn("cadence", error or "")

    def test_validator_rejects_descending_dates(self) -> None:
        descending = list(reversed(self._points()))
        payload = self._payload_for_series(descending, self._points())

        error = _weekly_valuation_payload_error(payload)

        self.assertIsNotNone(error)
        self.assertIn("ascending", error or "")

    def test_validator_rejects_sub_three_day_and_duplicate_week_anomalies(self) -> None:
        continuous = self._points(start=date(2025, 1, 6))
        sub_three = [dict(point) for point in continuous]
        sub_three[1]["date"] = "2025-01-08"
        payload = self._payload_for_series(sub_three, continuous)
        spacing_error = _weekly_valuation_payload_error(payload)

        duplicate_week = [dict(point) for point in continuous]
        duplicate_week[1]["date"] = "2025-01-09"
        payload = self._payload_for_series(duplicate_week, continuous)
        duplicate_error = _weekly_valuation_payload_error(payload)

        self.assertIsNotNone(spacing_error)
        self.assertIn("spacing", spacing_error or "")
        self.assertIsNotNone(duplicate_error)
        self.assertIn("ISO week", duplicate_error or "")

    def test_weekly_payload_validator_rejects_undated_legacy_snapshot(self) -> None:
        legacy = {
            "period": "5y",
            "frequency": "weekly",
            "series": {
                "forward_pe_weekly": [
                    {"date": "legacy_fy_minus_1", "value": 20.0}
                ]
            },
        }

        self.assertFalse(_has_weekly_valuation_points(legacy))
        self.assertIn("metadata", _weekly_valuation_payload_error(legacy) or "")

    def test_v2_cache_namespace_is_not_used_as_weekly_fallback(self) -> None:
        class FailingProvider:
            def fetch_weekly_valuation_history(self, symbol: str) -> dict[str, object]:
                raise RuntimeError("offline")

        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteCache(Path(directory) / "valuation.sqlite3")
            cache.put(
                "chart_weekly_valuation_v2",
                "AAA",
                self._payload(3),
                schema_version=1,
            )
            service = SignalService(
                provider=FailingProvider(),
                cache=cache,
                price_ttl=timedelta(hours=6),
            )

            payload, status = service._get_weekly_valuation_history("AAA")

        self.assertEqual(status, "miss")
        self.assertEqual(payload["series"]["forward_pe_weekly"], [])
        self.assertIn("offline", payload["error"] or "")

    def test_invalid_current_cache_is_replaced_by_valid_provider_payload(self) -> None:
        class Provider:
            def __init__(self, payload: dict[str, object]) -> None:
                self.payload = payload
                self.calls = 0

            def fetch_weekly_valuation_history(self, symbol: str) -> dict[str, object]:
                self.calls += 1
                return self.payload

        provider = Provider(self._payload())
        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteCache(Path(directory) / "valuation.sqlite3")
            cache.put(
                VALUATION_HISTORY_NAMESPACE,
                "AAA",
                {"period": "5y", "frequency": "weekly", "series": {}},
                schema_version=1,
            )
            service = SignalService(provider=provider, cache=cache)
            payload, status = service._get_weekly_valuation_history("AAA")
            stored = cache.get(VALUATION_HISTORY_NAMESPACE, "AAA")

        self.assertEqual(status, "updated")
        self.assertEqual(provider.calls, 1)
        self.assertEqual(payload["metadata"]["point_count"], 8)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.schema_version, 2)


if __name__ == "__main__":
    unittest.main()
