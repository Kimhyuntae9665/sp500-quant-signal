from __future__ import annotations

import json
import unittest

from portfolio_service import (
    CatalogSnapshot,
    PortfolioProviderError,
    PortfolioService,
    PortfolioValidationError,
    YahooFinanceProvider,
    extract_naver_gold_quote,
    filter_kospi_rows,
)


class _QuoteProvider:
    def __init__(self, quotes):
        self.quotes = quotes
        self.calls: list[str] = []

    def quote(self, symbol: str):
        self.calls.append(symbol)
        value = self.quotes.get(symbol)
        if isinstance(value, Exception):
            raise value
        return value


class _FxProvider:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    def get_usdkrw(self):
        self.calls += 1
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class _CatalogProvider:
    def __init__(self, rows):
        self.rows = rows

    def get_catalog(self):
        return CatalogSnapshot(tuple(self.rows), source="test KRX mirror")


class PortfolioSearchTests(unittest.TestCase):
    def test_us_filter_accepts_equity_and_etf_but_rejects_non_us_quotes(self):
        rows = [
            {"symbol": "AAPL", "quoteType": "EQUITY", "exchange": "NMS"},
            {"symbol": "SPY", "quoteType": "ETF", "region": "US"},
            {"symbol": "005930.KS", "quoteType": "EQUITY", "region": "KR"},
            {"symbol": "BTC-USD", "quoteType": "CRYPTOCURRENCY", "region": "US"},
            {"symbol": "7203.T", "quoteType": "EQUITY", "region": "JP"},
        ]
        accepted = [row["symbol"] for row in rows if YahooFinanceProvider.is_us_listed(row)]
        self.assertEqual(accepted, ["AAPL", "SPY"])

    def test_kospi_filter_requires_current_stk_or_kospi_rows_and_searches_code_or_name(self):
        rows = [
            {"Code": "005930", "Name": "삼성전자", "Market": "KOSPI", "MarketId": "STK"},
            {"Code": "000660", "Name": "SK하이닉스", "Market": "KOSPI", "MarketId": "STK"},
            {"Code": "035420", "Name": "NAVER", "Market": "KOSDAQ", "MarketId": "KSQ"},
            {"Code": "123456", "Name": "잘못된 시장", "Market": "KOSDAQ", "MarketId": "STK"},
        ]
        by_name = filter_kospi_rows(rows, "삼성", 20)
        self.assertEqual([item["symbol"] for item in by_name], ["005930"])
        by_code = filter_kospi_rows(rows, "000660", 20)
        self.assertEqual([item["name"] for item in by_code], ["SK하이닉스"])
        self.assertEqual(filter_kospi_rows(rows, "NAVER", 20), [])


class GoldExtractionTests(unittest.TestCase):
    def test_gold_quote_is_extracted_recursively_from_next_data(self):
        payload = {
            "props": {
                "pageProps": {
                    "dehydratedState": {
                        "queries": [
                            {"state": {"data": {"result": {"name": "other"}}}},
                            {
                                "state": {
                                    "data": {
                                        "result": {
                                            "reutersCode": "M04020000",
                                            "symbolCode": "M04020000",
                                            "closePrice": "195,540",
                                            "localTradedAt": "2026-08-07T15:19:58+09:00",
                                        }
                                    }
                                }
                            },
                        ]
                    }
                }
            }
        }
        document = (
            '<html><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(payload, ensure_ascii=False)
            + "</script></html>"
        )
        quote = extract_naver_gold_quote(document)
        self.assertEqual(quote.price, 195540.0)
        self.assertEqual(quote.currency, "KRW")
        self.assertEqual(quote.as_of, "2026-08-07T15:19:58+09:00")
        self.assertIn("공식 KRX API가 아닙니다", quote.source.note)


class PortfolioComposeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.quotes = _QuoteProvider(
            {
                "AAPL": {"price": 100.0, "currency": "USD", "as_of": "2026-08-07T20:00:00Z"},
                "005930.KS": {"price": 70000.0, "currency": "KRW", "as_of": "2026-08-07T06:00:00Z"},
            }
        )
        self.fx = _FxProvider(
            {"price": 1400.0, "currency": "KRW", "as_of": "2026-08-07T20:00:00Z"}
        )
        self.gold = _QuoteProvider(
            {"M04020000": {"price": 200000.0, "currency": "KRW", "as_of": "2026-08-07T06:00:00Z"}}
        )
        self.service = PortfolioService(
            quote_provider=self.quotes,
            fx_provider=self.fx,
            gold_provider=self.gold,
        )

    def test_valuation_converts_usd_to_krw_and_weights_all_groups(self):
        result = self.service.compose(
            {
                "holdings": [
                    {"market": "us", "symbol": "AAPL", "name": "Apple", "quantity": 2},
                    {"market": "kospi", "symbol": "005930", "name": "삼성전자", "quantity": 1},
                    {"market": "gold", "symbol": "M04020000", "name": "KRX 금 99.99_1kg", "quantity": 3},
                ]
            }
        )
        self.assertEqual(result["base_currency"], "KRW")
        self.assertEqual(result["unit"], "만원")
        self.assertEqual(result["total_value_krw"], 950000.0)
        self.assertEqual(result["fx"]["usdkrw"], 1400.0)
        self.assertEqual([item["symbol"] for item in result["priced"]], ["AAPL", "005930", "M04020000"])
        self.assertAlmostEqual(sum(item["weight_pct"] for item in result["priced"]), 100.0, places=4)
        self.assertEqual(result["groups"][0]["color"], "#3D73D9")
        self.assertEqual(result["groups"][1]["color"], "#D95567")
        self.assertEqual(result["groups"][2]["color"], "#D99B18")
        self.assertIn("quote", result["priced"][0])
        self.assertEqual(self.quotes.calls, ["AAPL", "005930.KS"])
        self.assertEqual(self.gold.calls, ["M04020000"])

    def test_cash_is_included_as_a_distinct_gray_group(self):
        result = self.service.compose(
            {
                "holdings": [
                    {"market": "us", "symbol": "AAPL", "name": "Apple", "quantity": 1},
                ],
                "cash_krw": 210000,
            }
        )
        self.assertEqual(result["total_value_krw"], 350000.0)
        cash = next(item for item in result["priced"] if item["market"] == "cash")
        self.assertEqual(cash["symbol"], "KRW")
        self.assertEqual(cash["value_manwon"], 21.0)
        self.assertEqual(cash["quantity_unit"], "KRW")
        cash_group = next(group for group in result["groups"] if group["key"] == "cash")
        self.assertEqual(cash_group["color"], "#7D8995")
        self.assertAlmostEqual(cash_group["weight_pct"], 60.0, places=4)

    def test_cash_only_portfolio_requires_no_market_provider(self):
        result = self.service.compose({"holdings": [], "cash_krw": 500000})
        self.assertEqual(result["total_value_krw"], 500000.0)
        self.assertEqual([item["market"] for item in result["priced"]], ["cash"])
        self.assertEqual(self.quotes.calls, [])
        self.assertEqual(self.gold.calls, [])
        self.assertEqual(self.fx.calls, 0)

    def test_cash_validation_is_strict_and_requires_at_least_one_asset(self):
        cases = [
            ({"holdings": [], "cash_krw": -1}, "invalid_cash"),
            ({"holdings": [], "cash_krw": 1.5}, "invalid_cash"),
            ({"holdings": [], "cash_krw": True}, "invalid_cash"),
            ({"holdings": [], "cash_krw": "10000"}, "invalid_cash"),
            ({"holdings": [], "cash_krw": 0}, "invalid_holdings"),
        ]
        for payload, code in cases:
            with self.subTest(code=code, payload=payload):
                with self.assertRaises(PortfolioValidationError) as raised:
                    self.service.compose(payload)
                self.assertEqual(raised.exception.code, code)

    def test_validation_rejects_unknown_fields_duplicates_and_non_integer_units(self):
        cases = [
            ({"holdings": [{"market": "us", "symbol": "AAPL", "name": "A", "quantity": 1, "extra": 1}]}, "unknown_fields"),
            ({"holdings": [{"market": "kospi", "symbol": "005930", "name": "A", "quantity": 1.5}]}, "invalid_quantity"),
            ({"holdings": [{"market": "gold", "symbol": "M04020000", "name": "금", "quantity": 1.0}]}, "invalid_quantity"),
            ({"holdings": [
                {"market": "us", "symbol": "AAPL", "name": "A", "quantity": 1},
                {"market": "us", "symbol": "aapl", "name": "A", "quantity": 2},
            ]}, "duplicate_holding"),
        ]
        for payload, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(PortfolioValidationError) as raised:
                    self.service.compose(payload)
                self.assertEqual(getattr(raised.exception, "code", None), code)

    def test_partial_quote_failure_is_returned_without_losing_priced_holdings(self):
        self.quotes.quotes["005930.KS"] = PortfolioProviderError(
            "quote_unavailable", "KOSPI quote unavailable"
        )
        result = self.service.compose(
            {
                "holdings": [
                    {"market": "us", "symbol": "AAPL", "name": "Apple", "quantity": 1},
                    {"market": "kospi", "symbol": "005930", "name": "삼성전자", "quantity": 1},
                ]
            }
        )
        self.assertEqual([item["symbol"] for item in result["priced"]], ["AAPL"])
        self.assertEqual([item["symbol"] for item in result["unpriced"]], ["005930"])
        self.assertEqual(result["errors"], result["unpriced"])
        self.assertTrue(result["warnings"])

    def test_no_successful_quotes_raises_stable_provider_error(self):
        self.quotes.quotes["AAPL"] = PortfolioProviderError("quote_unavailable", "down")
        self.fx.value = PortfolioProviderError("portfolio_provider_error", "fx down")
        with self.assertRaises(PortfolioProviderError) as raised:
            self.service.compose(
                {"holdings": [{"market": "us", "symbol": "AAPL", "name": "Apple", "quantity": 1}]}
            )
        self.assertEqual(raised.exception.code, "portfolio_no_priced_holdings")


if __name__ == "__main__":
    unittest.main()
