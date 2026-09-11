from __future__ import annotations

import contextlib
from http.client import HTTPConnection
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest

from server import QuantHTTPServer, RequestHandler


ACCESS_TOKEN = "0123456789abcdef0123456789abcdef"


class _Adapter:
    def dashboard(self):
        return {"signals": []}

    def rules(self):
        return {"version": "test"}

    def stock(self, ticker: str):
        return {"ticker": ticker}

    def stock_history(self, ticker: str):
        return {"symbol": ticker, "prices": [{"date": "2026-01-02", "value": 100.0}]}


class _PortfolioService:
    def search(self, **kwargs):
        return {
            "query": kwargs.get("query", ""),
            "market": kwargs["market"],
            "results": [],
            "source": "test",
            "warnings": [],
        }

    def compose(self, payload):
        return {
            "generated_at": "2026-08-07T00:00:00Z",
            "base_currency": "KRW",
            "unit": "만원",
            "total_value_krw": 10000,
            "total_value_manwon": 1,
            "fx": {},
            "groups": [],
            "priced": payload.get("holdings", []),
            "unpriced": [],
            "warnings": [],
        }


class _GsQuantLabService:
    def analyze(self, payload):
        return {
            "schema_version": "gs-risk-lab-v1",
            "request": payload,
            "engine": {
                "name": "gs-quant",
                "mode": "open_source_local",
                "marquee_connected": False,
            },
            "metrics": {"beta": 0.72},
        }


class ServerAuthenticationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        web_root = Path(self.temp_dir.name)
        (web_root / "index.html").write_text(
            "<!doctype html><title>Quant test</title>", encoding="utf-8"
        )
        self.server = QuantHTTPServer(
            ("127.0.0.1", 0),
            RequestHandler,
            adapter=_Adapter(),
            portfolio_service=_PortfolioService(),
            gs_quant_lab_service=_GsQuantLabService(),
            web_root=web_root,
            access_token=ACCESS_TOKEN,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = int(self.server.server_address[1])

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp_dir.cleanup()

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: bytes | str | None = None,
    ):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        result = (response.status, dict(response.getheaders()), body)
        connection.close()
        return result

    def test_token_bootstrap_sets_cookie_and_redirects_to_clean_url(self) -> None:
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            status, headers, body = self.request(
                f"/?view=board&token={ACCESS_TOKEN}"
            )
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/?view=board")
        self.assertEqual(body, b"")
        self.assertIn("quant_session=", headers["Set-Cookie"])
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertNotIn(ACCESS_TOKEN, captured.getvalue())
        self.assertIn("token=<redacted>", captured.getvalue())

        cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, _, body = self.request("/", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn(b"Quant test", body)

    def test_unauthorized_requests_are_rejected(self) -> None:
        status, headers, body = self.request("/")
        self.assertEqual(status, 401)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("인증".encode(), body)

        status, headers, body = self.request("/api/dashboard")
        self.assertEqual(status, 401)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertIn(b"authentication_required", body)

        status, _, _ = self.request(f"/?token=wrong-{ACCESS_TOKEN}")
        self.assertEqual(status, 401)

    def test_api_header_authentication_is_supported(self) -> None:
        status, _, body = self.request(
            "/api/dashboard", headers={"X-Quant-Token": ACCESS_TOKEN}
        )
        self.assertEqual(status, 200)
        self.assertIn(b"signals", body)

        status, _, body = self.request(
            "/api/rules", headers={"Authorization": f"Bearer {ACCESS_TOKEN}"}
        )
        self.assertEqual(status, 200)
        self.assertIn(b"version", body)

        status, _, body = self.request(
            "/api/stocks/AAPL/history",
            headers={"X-Quant-Token": ACCESS_TOKEN},
        )
        self.assertEqual(status, 200)
        self.assertIn(b'"symbol":"AAPL"', body)
        self.assertIn(b'"prices"', body)

    def test_portfolio_routes_require_authentication(self) -> None:
        status, _, body = self.request("/api/portfolio/search?market=us&q=AAPL")
        self.assertEqual(status, 401)
        self.assertIn(b"authentication_required", body)

        status, _, body = self.request(
            "/api/gs-quant/analyze",
            method="POST",
            headers={"Content-Type": "application/json"},
            body=json.dumps(
                {
                    "positions": [
                        {"symbol": "CVX", "weight_pct": 50},
                        {"symbol": "TLT", "weight_pct": 50},
                    ]
                }
            ),
        )
        self.assertEqual(status, 401)
        self.assertIn(b"authentication_required", body)

        status, _, body = self.request(
            "/api/portfolio/compose",
            method="POST",
            headers={"Content-Type": "application/json"},
            body=json.dumps({"holdings": []}),
        )
        self.assertEqual(status, 401)
        self.assertIn(b"authentication_required", body)

    def test_authorized_portfolio_search_and_compose_routes(self) -> None:
        status, _, body = self.request(
            "/api/portfolio/search?market=us&q=AAPL&limit=5",
            headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["market"], "us")

        status, _, body = self.request(
            "/api/portfolio/compose",
            method="POST",
            headers={
                "Authorization": f"Bearer {ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
            body=json.dumps(
                {
                    "holdings": [
                        {"market": "gold", "symbol": "M04020000", "name": "금", "quantity": 1}
                    ]
                }
            ),
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["base_currency"], "KRW")

        status, _, body = self.request(
            "/api/portfolio/compose",
            method="POST",
            headers={
                "Authorization": f"Bearer {ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
            body=json.dumps({"holdings": [], "cash_krw": 100000}),
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["base_currency"], "KRW")

    def test_authorized_gs_quant_analysis_route(self) -> None:
        request_payload = {
            "positions": [
                {"symbol": "CVX", "weight_pct": 50},
                {"symbol": "TLT", "weight_pct": 50},
            ],
            "benchmark": "SPY",
            "lookback": "3y",
            "risk_free_rate_pct": 4,
        }
        status, _, body = self.request(
            "/api/gs-quant/analyze",
            method="POST",
            headers={
                "Authorization": f"Bearer {ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
            body=json.dumps(request_payload),
        )
        self.assertEqual(status, 200)
        response = json.loads(body)
        self.assertEqual(response["schema_version"], "gs-risk-lab-v1")
        self.assertEqual(response["request"], request_payload)
        self.assertFalse(response["engine"]["marquee_connected"])

    def test_refresh_and_compose_have_separate_body_allow_lists(self) -> None:
        status, _, body = self.request(
            "/api/refresh",
            method="POST",
            headers={
                "Authorization": f"Bearer {ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
            body=json.dumps({"holdings": []}),
        )
        self.assertEqual(status, 400)
        self.assertIn(b"unknown_fields", body)

        status, _, body = self.request(
            "/api/gs-quant/analyze",
            method="POST",
            headers={
                "Authorization": f"Bearer {ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
            body=json.dumps({"holdings": []}),
        )
        self.assertEqual(status, 400)
        self.assertIn(b"unknown_fields", body)

        status, _, body = self.request(
            "/api/portfolio/compose",
            method="POST",
            headers={
                "Authorization": f"Bearer {ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
            body=json.dumps({"mode": "quick"}),
        )
        self.assertEqual(status, 400)
        self.assertIn(b"unknown_fields", body)

    def test_portfolio_search_rejects_unknown_and_duplicate_query_fields(self) -> None:
        status, _, body = self.request(
            "/api/portfolio/search?market=us&q=AAPL&unexpected=1",
            headers={"X-Quant-Token": ACCESS_TOKEN},
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_query")

        status, _, body = self.request(
            "/api/portfolio/search?market=us&market=kospi&q=AAPL",
            headers={"X-Quant-Token": ACCESS_TOKEN},
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_query")


class ServerWithoutAuthenticationTests(unittest.TestCase):
    def test_access_token_remains_optional_for_localhost_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "index.html").write_text("local", encoding="utf-8")
            server = QuantHTTPServer(
                ("127.0.0.1", 0),
                RequestHandler,
                adapter=_Adapter(),
                web_root=root,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = HTTPConnection(
                    "127.0.0.1", int(server.server_address[1]), timeout=5
                )
                connection.request("GET", "/")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


class StaticCachePolicyTests(unittest.TestCase):
    def test_executable_shell_assets_are_always_revalidated(self) -> None:
        self.assertEqual(
            RequestHandler._cache_control("index.html"),
            "no-cache, no-store, must-revalidate",
        )
        self.assertEqual(
            RequestHandler._cache_control("gs-lab.html"),
            "no-cache, no-store, must-revalidate",
        )
        self.assertEqual(
            RequestHandler._cache_control("sw.js"),
            "no-cache, no-store, must-revalidate",
        )
        self.assertEqual(
            RequestHandler._cache_control("app.js"),
            "no-cache, must-revalidate",
        )
        self.assertEqual(
            RequestHandler._cache_control("styles.css"),
            "no-cache, must-revalidate",
        )
        self.assertEqual(
            RequestHandler._cache_control("gs-lab.js"),
            "no-cache, must-revalidate",
        )
        self.assertEqual(
            RequestHandler._cache_control("manifest.webmanifest"),
            "no-cache, must-revalidate",
        )
        self.assertEqual(
            RequestHandler._cache_control("app-icon.png"),
            "public, max-age=3600",
        )


class GsQuantLabStaticContractTests(unittest.TestCase):
    def test_standalone_lab_is_discoverable_and_source_aware(self) -> None:
        root = Path(__file__).resolve().parents[1]
        html = (root / "web" / "gs-lab.html").read_text(encoding="utf-8")
        css = (root / "web" / "gs-lab.css").read_text(encoding="utf-8")
        script = (root / "web" / "gs-lab.js").read_text(encoding="utf-8")
        index = (root / "web" / "index.html").read_text(encoding="utf-8")
        service_worker = (root / "web" / "sw.js").read_text(encoding="utf-8")

        self.assertIn('href="./gs-lab.html"', index)
        self.assertIn('"/api/gs-quant/analyze"', script)
        self.assertIn('"/api/portfolio/compose"', script)
        self.assertIn("sp500-quant-signal.portfolio.v1", script)
        self.assertIn("gs-risk-lab-v1", html)
        self.assertIn("Goldman Sachs의 소속·제휴·승인·보증을 의미하지 않으며", html)
        self.assertIn("Marquee 기관용 API 자격증명은 사용하지 않습니다", html)
        self.assertIn('rel="noopener noreferrer"', html)
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)
        self.assertIn("./gs-lab.html", service_worker)
        self.assertIn("./gs-lab.css", service_worker)
        self.assertIn("./gs-lab.js", service_worker)


if __name__ == "__main__":
    unittest.main()
