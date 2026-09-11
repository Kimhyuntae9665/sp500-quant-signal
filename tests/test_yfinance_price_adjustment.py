import unittest
from unittest.mock import patch

import pandas as pd

from quant_engine.providers.yfinance_provider import YFinanceProvider


class YFinancePriceAdjustmentTests(unittest.TestCase):
    dates = pd.to_datetime(
        [
            "2026-08-05",
            "2026-08-06",
            "2026-08-07",
            "2026-08-11",
            "2026-08-12",
            "2026-08-13",
        ]
    )

    @staticmethod
    def _frame(
        prices_by_symbol,
        splits_by_symbol,
        *,
        multi=True,
        include_close=True,
    ):
        symbols = list(prices_by_symbol)
        if multi:
            data = {}
            for symbol in symbols:
                prices = prices_by_symbol[symbol]
                data[("Adj Close", symbol)] = prices
                data[("Stock Splits", symbol)] = splits_by_symbol[symbol]
                if include_close:
                    data[("Close", symbol)] = [value * 1.01 for value in prices]
            frame = pd.DataFrame(data, index=YFinancePriceAdjustmentTests.dates)
            frame.columns = pd.MultiIndex.from_tuples(frame.columns)
            return frame
        data = {
            "Adj Close": prices_by_symbol[symbols[0]],
            "Stock Splits": splits_by_symbol[symbols[0]],
        }
        if include_close:
            data["Close"] = [value * 1.01 for value in data["Adj Close"]]
        return pd.DataFrame(data, index=YFinancePriceAdjustmentTests.dates)

    @staticmethod
    def _history(frame, symbols):
        with patch("yfinance.download", return_value=frame) as download:
            history = YFinanceProvider(auto_detect_legacy=False).fetch_price_history(
                symbols, period="1y"
            )
        download.assert_called_once()
        call = download.call_args.kwargs
        return history, call

    def test_unadjusted_forward_split_repairs_only_history_before_event(self):
        frame = self._frame(
            {"TESTF": [88.0, 89.0, 90.0, 45.0, 45.5, 46.0]},
            {"TESTF": [0.0, 0.0, 0.0, 2.0, 0.0, 0.0]},
            multi=False,
        )

        history, call = self._history(frame, ["TESTF"])

        self.assertEqual(call["auto_adjust"], False)
        self.assertEqual(call["actions"], True)
        self.assertEqual(
            history["TESTF"].adjusted_closes,
            (44.0, 44.5, 45.0, 45.0, 45.5, 46.0),
        )
        self.assertIn("Adj Close", history["TESTF"].provenance.basis)
        self.assertIn("auto_adjust=False", history["TESTF"].provenance.basis)
        self.assertTrue(
            any("Applied conditional split continuity repair" in note
                for note in history["TESTF"].provenance.notes)
        )

    def test_already_adjusted_forward_split_is_not_double_adjusted(self):
        frame = self._frame(
            {"TESTA": [44.0, 44.5, 45.0, 45.0, 45.5, 46.0]},
            {"TESTA": [0.0, 0.0, 0.0, 2.0, 0.0, 0.0]},
            multi=False,
        )

        history, _ = self._history(frame, ["TESTA"])

        self.assertEqual(
            history["TESTA"].adjusted_closes,
            (44.0, 44.5, 45.0, 45.0, 45.5, 46.0),
        )
        self.assertTrue(
            any("was not materially closer" in note
                for note in history["TESTA"].provenance.notes)
        )

    def test_reverse_split_repairs_history_with_inverse_factor(self):
        frame = self._frame(
            {"TESTR": [49.0, 50.0, 51.0, 100.0, 101.0, 102.0]},
            {"TESTR": [0.0, 0.0, 0.0, 0.5, 0.0, 0.0]},
            multi=False,
        )

        history, _ = self._history(frame, ["TESTR"])

        self.assertEqual(
            history["TESTR"].adjusted_closes,
            (98.0, 100.0, 102.0, 100.0, 101.0, 102.0),
        )
        self.assertTrue(
            any("multiplied by 2" in note
                for note in history["TESTR"].provenance.notes)
        )

    def test_no_split_action_leaves_adj_close_unchanged(self):
        frame = self._frame(
            {"TESTN": [98.0, 99.0, 100.0, 101.0, 100.5, 102.0]},
            {"TESTN": [0.0] * len(self.dates)},
            multi=False,
        )

        history, _ = self._history(frame, ["TESTN"])

        self.assertEqual(
            history["TESTN"].adjusted_closes,
            (98.0, 99.0, 100.0, 101.0, 100.5, 102.0),
        )
        self.assertTrue(
            any("no split continuity repair" in note
                for note in history["TESTN"].provenance.notes)
        )

    def test_multi_symbol_multiindex_extracts_each_adj_close_and_action(self):
        frame = self._frame(
            {
                "TESTF": [88.0, 89.0, 90.0, 45.0, 45.5, 46.0],
                "TESTN": [98.0, 99.0, 100.0, 101.0, 100.5, 102.0],
            },
            {
                "TESTF": [0.0, 0.0, 0.0, 2.0, 0.0, 0.0],
                # Batch frames can contain NaN actions on dates where another
                # ticker traded; they must never be interpreted as a split.
                "TESTN": [float("nan"), 0.0, 0.0, 0.0, 0.0, 0.0],
            },
        )

        history, call = self._history(frame, ["TESTF", "TESTN"])

        self.assertEqual(set(history), {"TESTF", "TESTN"})
        self.assertEqual(
            history["TESTF"].adjusted_closes,
            (44.0, 44.5, 45.0, 45.0, 45.5, 46.0),
        )
        self.assertEqual(
            history["TESTN"].adjusted_closes,
            (98.0, 99.0, 100.0, 101.0, 100.5, 102.0),
        )
        self.assertEqual(call["tickers"], ["TESTF", "TESTN"])


if __name__ == "__main__":
    unittest.main()
