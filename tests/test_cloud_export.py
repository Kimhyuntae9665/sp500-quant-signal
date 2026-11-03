from __future__ import annotations

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from quant_engine.cache import SQLiteCache
from quant_engine.models import PriceHistory, Provenance, UniverseMember, UniverseSnapshot
from quant_engine.scoring import calculate_price_metrics
from quant_engine.service import CHART_HISTORY_NAMESPACE, PRICE_NAMESPACE, PRICE_SCHEMA_VERSION, SignalService
from scripts.export_cloud import EXPORT_FORMAT, export_snapshot, public_json, completed_daily


class FixtureUniverse:
    def get_snapshot(self):
        return UniverseSnapshot(
            members=(UniverseMember("AAA", "Alpha", "Industrials", "Machinery"), UniverseMember("BRK.B", "Berkshire", "Financials", "Insurance")),
            provenance=Provenance(source="fixture", retrieved_at="2026-01-01T00:00:00Z"),
            fallback_used=False,
        )


class CloudExportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.provider = Mock(name="network must not be called")
        self.provider.name = "fixture"
        self.provider.data_warnings = []
        self.provider.legacy_forward_pe_cache = None
        self.provider.fetch_price_history.return_value = {}
        self.service = SignalService(
            provider=self.provider, cache=SQLiteCache(self.root / "cache.sqlite3"),
            universe=FixtureUniverse(),
        )
        self.output = self.root / "public"

    def cache_history(self, symbol="AAA", *, stale=False):
        history = PriceHistory(
            symbol=symbol, dates=("2026-01-01", "2026-01-02", "2026-01-03"),
            adjusted_closes=(100.0, 102.0, 101.0),
            provenance=Provenance(source="fixture", retrieved_at="2026-01-03T00:00:00Z", notes=(r"Loaded C:\Users\private\cache.csv",)),
        )
        self.service.cache.put(
            CHART_HISTORY_NAMESPACE, symbol, history.to_dict(),
            fetched_at="2001-01-01T00:00:00Z" if stale else None,
        )
        self.service.cache.put(
            PRICE_NAMESPACE, symbol, calculate_price_metrics(history).to_dict(),
            schema_version=PRICE_SCHEMA_VERSION,
        )

    def read(self, name):
        return json.loads((self.output / name).read_text(encoding="utf-8"))

    def test_default_export_is_cache_only_and_matches_engine_shapes(self):
        self.cache_history(stale=True)
        self.cache_history("SPY")
        manifest = export_snapshot(self.service, self.output)
        self.provider.assert_not_called()
        self.assertEqual(self.provider.mock_calls, [])
        self.assertEqual(manifest["format"], EXPORT_FORMAT)
        self.assertEqual(manifest["counts"]["stocks"], 2)
        self.assertEqual(manifest["counts"]["history_cache"], {"fresh": 0, "stale": 1, "miss": 1})
        self.assertEqual(manifest["refresh"]["mode"], "none")
        dashboard = self.read("dashboard.json")
        self.assertEqual(dashboard["count"], 2)
        self.assertEqual({row["symbol"] for row in dashboard["signals"]}, {"AAA", "BRK.B"})
        self.assertEqual(self.read("stocks/AAA.json")["symbol"], "AAA")
        self.assertIn("components", self.read("stocks/AAA.json"))
        self.assertIn("company_rules", self.read("rules.json"))
        self.assertEqual(self.read("histories/AAA.json")["cache_status"], "stale")
        self.assertEqual(self.read("histories/BRK.B.json")["series"]["adjusted_close"], [])
        self.assertEqual(self.read("histories/BRK.B.json")["valuation"]["cache_status"], "miss")
        market = self.read("market-data.json")
        self.assertEqual(market["prices"]["SPY"][-1], {"date": "2026-01-02", "value": 102.0})
        self.assertIsNone(market["fx"]["rate"])
        portfolio = self.read("portfolio.json")
        self.assertEqual(portfolio["catalog"][0]["market"], "us")
        self.assertIsNone(portfolio["usd_krw"]["value"])
        self.assertIsNone(portfolio["gold"]["price"])
        self.assertFalse(manifest["contains_portfolio_holdings"])
        for path in self.output.rglob("*"):
            if path.is_file():
                self.assertEqual(path.suffix, ".json")
                raw = path.read_text(encoding="utf-8")
                self.assertNotIn("C:\\\\Users", raw)
                self.assertNotIn("NaN", raw)
                self.assertNotIn("Infinity", raw)

    def test_explicit_refresh_and_chart_fetch_are_bounded(self):
        with patch.object(self.service, "refresh", return_value={"requested": 2}) as refresh, patch.object(self.service, "get_chart_history", return_value={}) as history:
            manifest = export_snapshot(self.service, self.output, refresh="quick", history_limit=1)
            refresh.assert_called_once_with(prices=True, fundamentals=False)
            history.assert_called_once_with("AAA")
            self.assertEqual(manifest["refresh"]["history_requested"], ["AAA"])
            self.provider.fetch_price_history.assert_called_once_with(["SPY", "KRW=X"], period="3y")
        with patch.object(self.service, "refresh", return_value={}) as refresh:
            export_snapshot(self.service, self.output, refresh="full")
            refresh.assert_called_once_with(prices=True, fundamentals=True)

    def test_history_selection_rotates_to_missing_after_cached_symbol(self):
        self.cache_history("AAA")
        with patch.object(self.service, "get_chart_history", return_value={}) as history:
            export_snapshot(self.service, self.output, history_limit=1)
            history.assert_called_once_with("BRK.B")

    def test_snapshot_partial_bar_never_becomes_completed_as_cache_ages(self):
        history = PriceHistory(symbol="SPY", dates=("2026-07-09", "2026-07-10"), adjusted_closes=(100, 101), provenance=Provenance(source="fixture", retrieved_at="2026-07-10T18:00:00Z"))
        self.assertEqual(completed_daily(history), [{"date": "2026-07-09", "value": 100}])
        settled = PriceHistory(symbol="SPY", dates=history.dates, adjusted_closes=history.adjusted_closes, provenance=Provenance(source="fixture", retrieved_at="2026-07-10T20:15:00Z"))
        self.assertEqual(len(completed_daily(settled)), 2)

    def test_portfolio_refresh_persists_public_data_for_offline_exports(self):
        source = Mock()
        source.to_dict.return_value = {"provider": "fixture", "official": False}
        fx = SimpleNamespace(price=1400.0, as_of="2026-01-03", source=source)
        gold = SimpleNamespace(price=150000.0, as_of="2026-01-03", source=source)
        catalog = SimpleNamespace(rows=({"Code": "005930", "Name": "Samsung", "Market": "KOSPI"},), source="fixture")
        with patch("portfolio_service.YahooFinanceProvider") as yahoo, patch("portfolio_service.NaverGoldProvider") as naver, patch("portfolio_service.KrxCatalogProvider") as krx:
            yahoo.return_value.get_usdkrw.return_value = fx
            naver.return_value.quote.return_value = gold
            krx.return_value.get_catalog.return_value = catalog
            manifest = export_snapshot(self.service, self.output, portfolio_refresh=True)
            self.assertEqual(manifest["refresh"]["portfolio"], {"usd_krw": "updated", "gold": "updated", "catalog": "updated"})
            export_snapshot(self.service, self.output)
            yahoo.assert_called_once()
            naver.assert_called_once()
            krx.assert_called_once()
        portfolio = self.read("portfolio.json")
        self.assertEqual(portfolio["usd_krw"]["value"], 1400.0)
        self.assertEqual(portfolio["gold"]["price"], 150000.0)
        self.assertIn("005930", [item["symbol"] for item in portfolio["catalog"]])
        self.assertEqual(self.read("market-data.json")["fx"]["rate"], 1400.0)

    def test_reexport_preserves_unrelated_files_and_rejects_unmanaged_directory(self):
        self.output.mkdir()
        unrelated = self.output / "important.txt"
        unrelated.write_text("preserve", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "nonempty"):
            export_snapshot(self.service, self.output)
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "preserve")
        fresh = self.root / "managed"
        export_snapshot(self.service, fresh)
        (fresh / "unrelated.txt").write_text("keep", encoding="utf-8")
        export_snapshot(self.service, fresh)
        self.assertEqual((fresh / "unrelated.txt").read_text(encoding="utf-8"), "keep")

    def test_public_json_redacts_paths_and_nonfinite_numbers(self):
        cleaned = public_json({"source_path": r"C:\private.csv", "note": "Error: /home/private/data.csv", "url": "https://example.com/data", "value": float("nan"), "values": [float("inf"), 2.0]})
        self.assertNotIn("source_path", cleaned)
        self.assertNotIn("/home/private", cleaned["note"])
        self.assertEqual(cleaned["url"], "https://example.com/data")
        self.assertIsNone(cleaned["value"])
        self.assertEqual(cleaned["values"], [None, 2.0])

    def test_invalid_options_fail_before_fetching(self):
        with self.assertRaises(ValueError):
            export_snapshot(self.service, self.output, history_limit=-1)
        with self.assertRaises(ValueError):
            export_snapshot(self.service, self.output, refresh="anything")
        self.assertEqual(self.provider.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
