from __future__ import annotations

from pathlib import Path
import re
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = PROJECT_ROOT / "web"


class PortfolioWebContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
        cls.javascript = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
        cls.styles = (WEB_ROOT / "styles.css").read_text(encoding="utf-8")
        cls.service_worker = (WEB_ROOT / "sw.js").read_text(encoding="utf-8")

    def test_cash_controls_are_unique_and_non_submitting(self) -> None:
        self.assertEqual(self.html.count('id="portfolio-cash-amount"'), 1)
        self.assertEqual(self.html.count('id="portfolio-cash-clear"'), 1)
        self.assertRegex(
            self.html,
            r'<input\s+id="portfolio-cash-amount"[^>]*type="number"[^>]*min="0"[^>]*step="1"',
        )
        self.assertRegex(
            self.html,
            r'<button[^>]*id="portfolio-cash-clear"[^>]*type="button"',
        )

    def test_storage_v2_cash_and_auto_restore_contract_is_present(self) -> None:
        self.assertIn("const PORTFOLIO_STORAGE_VERSION = 2", self.javascript)
        self.assertIn("cashManwon", self.javascript)
        self.assertIn("cash_krw", self.javascript)
        self.assertIn("restored.hasSaved", self.javascript)
        self.assertIn("composePortfolio({ auto: true })", self.javascript)

    def test_distinct_holding_palettes_cover_every_group(self) -> None:
        for group in ("us", "kospi", "gold", "cash"):
            self.assertRegex(
                self.javascript,
                rf"{group}: Object\.freeze\(\[\"#[0-9A-Fa-f]{{6}}\"",
            )
        self.assertIn("assignPortfolioHoldingColors(holdings)", self.javascript)
        palette_lines = re.findall(r'(?:us|kospi|gold|cash): Object\.freeze\(\[(.*?)\]\)', self.javascript)
        self.assertEqual(len(palette_lines), 4)
        for palette in palette_lines:
            colors = re.findall(r'#[0-9A-Fa-f]{6}', palette)
            self.assertGreaterEqual(len(colors), 8)
            self.assertEqual(len(colors), len(set(colors)))

    def test_portfolio_result_is_full_width_and_cash_is_styled(self) -> None:
        layout = re.search(r"\.portfolio-layout\s*\{(?P<body>.*?)\}", self.styles, re.DOTALL)
        self.assertIsNotNone(layout)
        self.assertIn("grid-template-columns: minmax(0, 1fr)", layout.group("body"))
        self.assertIn("--portfolio-cash:", self.styles)
        self.assertIn(".portfolio-cash-section", self.styles)

    def test_static_shell_versions_are_consistently_v24(self) -> None:
        self.assertIn('styles.css?v=24', self.html)
        self.assertIn('app.js?v=24', self.html)
        self.assertIn('sp500-quant-shell-v24', self.service_worker)
        self.assertIn('styles.css?v=24', self.service_worker)
        self.assertIn('app.js?v=24', self.service_worker)
        self.assertNotIn("v23", self.html)
        self.assertNotIn("v23", self.service_worker)


if __name__ == "__main__":
    unittest.main()
