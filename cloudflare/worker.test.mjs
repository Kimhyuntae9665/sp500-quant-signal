import test, { afterEach } from "node:test";
import assert from "node:assert/strict";
import worker from "./worker.mjs";

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });
const stamp = "2026-10-30T20:00:00Z";
const portfolio = {
  generated_at: stamp,
  catalog: [{ symbol: "AAPL", name: "Apple", market: "us" }, { symbol: "005930", name: "삼성전자", market: "kospi" }],
  quotes: { AAPL: { price: 100, currency: "USD", as_of: stamp }, "005930": { price: 50000, currency: "KRW", as_of: stamp } },
  usd_krw: { value: 1400, as_of: stamp }, gold: { price: 100000, as_of: stamp },
};
function env(fixtures = {}) {
  const data = {
    "/data/dashboard.json": { generated_at: stamp, stocks: [{ symbol: "AAPL" }] },
    "/data/rules.json": { rules: [] }, "/data/stocks/AAPL.json": { symbol: "AAPL" },
    "/data/histories/AAPL.json": { symbol: "AAPL", series: [] }, "/data/portfolio.json": portfolio, ...fixtures,
  };
  return { ASSETS: { fetch: async request => {
    const path = new URL(request.url).pathname;
    return data[path] === undefined ? new Response("not found", { status: 404 }) : new Response(JSON.stringify(data[path]), { headers: { "Content-Type": "application/json" } });
  } } };
}
async function call(path, payload, environment = env(), method) {
  const response = await worker.fetch(new Request(`https://app.example${path}`, payload === undefined ? { method: method || "GET" } : {
    method: method || "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  }), environment);
  return { response, data: response.headers.get("Content-Type")?.includes("json") ? await response.json() : await response.text() };
}
const holding = (symbol = "AAPL", selectedMarket = "us", quantity = 2) => ({ symbol, market: selectedMarket, name: symbol, quantity });

test("dashboard preserves timestamp and truthfully exposes scheduled refresh", async () => {
  const { response, data } = await call("/api/dashboard");
  assert.equal(response.status, 200);
  assert.equal(data.generated_at, stamp);
  assert.equal(data.refresh.running, false);
  assert.equal(data.refresh.mode, "cloud_scheduled");
  assert.equal(data.cloud_snapshot.source, "bundled_assets");
});
test("health, rules, stock and history preserve API routes", async () => {
  for (const route of ["health", "rules", "stocks/AAPL", "stocks/AAPL/history"]) assert.equal((await call(`/api/${route}`)).response.status, 200);
  assert.equal((await call("/api/health")).data.gs_quant_lab.mode, "cloud_javascript_public_data");
  assert.equal((await call("/api/health", undefined, env({ "/data/dashboard.json": undefined }))).response.status, 503);
});
test("refresh never claims to enqueue a task", async () => {
  const { response, data } = await call("/api/refresh", { mode: "full" });
  assert.equal(response.status, 409);
  assert.equal(data.error.code, "cloud_scheduled_refresh");
  assert.equal(data.accepted, undefined);
});
test("static assets and HEAD are supported", async () => {
  const environment = env({ "/index.html": { html: true } });
  assert.equal((await call("/index.html", undefined, environment)).response.status, 200);
  const response = await worker.fetch(new Request("https://app.example/api/dashboard", { method: "HEAD" }), environment);
  assert.equal(response.status, 200);
  assert.equal(await response.text(), "");
});
test("snapshot remote URL is fixed and fallback is explicitly labelled", async () => {
  const environment = { ...env(), DATA_BASE_URL: "https://kimhyuntae9665.github.io/sp500-quant-signal/data/" };
  globalThis.fetch = async (url, options) => {
    assert.equal(url.href, `${environment.DATA_BASE_URL}dashboard.json`);
    assert.equal(options.redirect, "error");
    assert.equal(options.cf.cacheTtl, 300);
    return new Response(JSON.stringify({ generated_at: "2026-11-01T00:00:00Z" }));
  };
  let result = await call("/api/dashboard", undefined, environment);
  assert.equal(result.data.cloud_snapshot.source, "github_pages");
  globalThis.fetch = async () => { throw new Error("network"); };
  result = await call("/api/dashboard", undefined, environment);
  assert.equal(result.data.generated_at, stamp);
  assert.equal(result.data.cloud_snapshot.bundled_fallback, true);
  assert.match(result.data.warnings[0], /배포 시/);
  assert.equal((await call("/api/dashboard", undefined, { ...env(), DATA_BASE_URL: "https://evil.example/" })).response.status, 503);
});
test("portfolio valuation matches Python response contracts and units", async () => {
  globalThis.fetch = async () => { throw new Error("No outbound call expected"); };
  const { response, data } = await call("/api/portfolio/compose", {
    holdings: [holding(), holding("005930", "kospi", 3), holding("M04020000", "gold", 2)], cash_krw: 10000,
  });
  assert.equal(response.status, 200);
  assert.equal(data.total_value_krw, 640000);
  assert.equal(data.total_value_manwon, 64);
  assert.deepEqual(data.holdings, data.priced_holdings);
  assert.deepEqual(data.errors, []);
  assert.equal(data.holdings[2].quantity_unit, "g");
  assert.equal(data.holdings[0].quote_as_of, stamp);
  assert.equal(data.groups.length, 4);
  assert.ok(Math.abs(data.holdings.reduce((sum, item) => sum + item.weight_pct, 0) - 100) < 0.00001);
});
test("cash-only portfolio needs no quote and does not claim stock pricing", async () => {
  const { data } = await call("/api/portfolio/compose", { cash_krw: 100000 });
  assert.equal(data.total_value_krw, 100000);
  assert.equal(data.fx.rate, null);
  assert.equal(data.holdings[0].source.provider, "User input");
});
test("unpriced positions are visible and never silently assigned zero", async () => {
  globalThis.fetch = async () => new Response("unavailable", { status: 503 });
  const { data } = await call("/api/portfolio/compose", { holdings: [holding("QQQ")], cash_krw: 1000 });
  assert.equal(data.total_value_krw, 1000);
  assert.equal(data.unpriced.length, 1);
  assert.equal(data.unpriced[0].error.code, "quote_unavailable");
  assert.equal(data.unpriced[0].value_krw, undefined);
  assert.equal((await call("/api/portfolio/compose", { holdings: [holding("QQQ")] })).response.status, 502);
});
test("missing timestamps and incorrect currencies are not valid snapshot quotes", async () => {
  globalThis.fetch = async () => { throw new Error("unavailable"); };
  for (const quote of [{ price: 100 }, { price: 100, currency: "KRW", as_of: stamp }]) {
    const { data } = await call("/api/portfolio/compose", { holdings: [holding()], cash_krw: 1 }, env({ "/data/portfolio.json": { ...portfolio, quotes: { AAPL: quote } } }));
    assert.equal(data.unpriced.length, 1);
  }
});
test("non-SP500 US search uses only fixed Yahoo endpoint and filters foreign instruments", async () => {
  globalThis.fetch = async (url, options) => {
    assert.equal(url.origin, "https://query1.finance.yahoo.com");
    assert.equal(url.pathname, "/v1/finance/search");
    assert.equal(url.searchParams.get("q"), "test");
    assert.equal(options.redirect, "error");
    return new Response(JSON.stringify({ quotes: [
      { symbol: "SMALL", longname: "Small US Corp", quoteType: "EQUITY", exchange: "NMS" },
      { symbol: "005930.KS", quoteType: "EQUITY", exchange: "KSC" },
      { symbol: "BAD/URL", quoteType: "EQUITY", exchange: "NMS" },
    ] }));
  };
  const { data } = await call("/api/portfolio/search?market=us&q=test");
  assert.deepEqual(data.results.map(item => item.symbol), ["SMALL"]);
});
test("KOSPI search returns six-digit codes and excludes KOSDAQ", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ quotes: [
    { symbol: "005930.KS", shortname: "Samsung", quoteType: "EQUITY" },
    { symbol: "123456.KQ", quoteType: "EQUITY" },
  ] }));
  const { data } = await call("/api/portfolio/search?market=kospi&q=Samsung");
  assert.deepEqual(data.results.map(item => item.symbol), ["005930"]);
});
test("provider failure returns labelled snapshot-only search", async () => {
  globalThis.fetch = async () => { throw new Error("unavailable"); };
  const { data } = await call("/api/portfolio/search?market=us&q=Apple");
  assert.equal(data.results[0].symbol, "AAPL");
  assert.match(data.warnings[0], /스냅샷/);
});
test("live valuation fetches arbitrary valid US ticker and FX without user-controlled origin", async () => {
  const seen = [];
  globalThis.fetch = async (url, options) => {
    seen.push(url.href);
    assert.equal(url.hostname, "query1.finance.yahoo.com");
    assert.equal(options.redirect, "error");
    return new Response(JSON.stringify({ chart: { result: [{ meta: {
      currency: url.pathname.endsWith("KRW%3DX") ? "KRW" : "USD", regularMarketPrice: url.pathname.endsWith("KRW%3DX") ? 1400 : 10,
      regularMarketTime: 1793386800,
    } }] } }));
  };
  const { response, data } = await call("/api/portfolio/compose", { holdings: [holding("SMALL")] }, env({ "/data/portfolio.json": { catalog: [] } }));
  assert.equal(response.status, 200);
  assert.equal(data.total_value_krw, 28000);
  assert.equal(seen.length, 2);
});
test("public quote subrequests are bounded for 50 missing holdings", async () => {
  let count = 0;
  globalThis.fetch = async () => { count++; return new Response("unavailable", { status: 503 }); };
  const { data } = await call("/api/portfolio/compose", { holdings: Array.from({ length: 50 }, (_, index) => holding(`X${index}`)), cash_krw: 1 });
  assert.equal(count, 16);
  assert.equal(data.unpriced.length, 50);
});
test("domestic gold is read only from fixed Naver instrument with its source date", async () => {
  globalThis.fetch = async (url, options) => {
    assert.equal(url, "https://m.stock.naver.com/marketindex/metals/M04020000");
    assert.equal(options.redirect, "error");
    return new Response('<html><script id="__NEXT_DATA__" type="application/json">{"props":{"gold":{"reutersCode":"M04020000","closePrice":"123,000","localTradedAt":"2026-10-30 15:30:00"}}}</script></html>');
  };
  const { data } = await call("/api/portfolio/compose", { holdings: [holding("M04020000", "gold", 3)] }, env({ "/data/portfolio.json": {} }));
  assert.equal(data.total_value_krw, 369000);
  assert.equal(data.holdings[0].quote_as_of, "2026-10-30T15:30:00+09:00");
  assert.equal(data.holdings[0].source.official, false);
});
test("Risk Lab endpoint runs JavaScript statistics from fixed adjusted-price source", async () => {
  let count = 0;
  const dates = Array.from({ length: 100 }, (_, index) => Math.floor(Date.now() / 1000) - (110 - index) * 86400);
  globalThis.fetch = async (url, options) => {
    count++;
    assert.equal(url.origin, "https://query1.finance.yahoo.com");
    assert.match(url.pathname, /^\/v8\/finance\/chart\/(AAA|BBB|SPY)$/);
    assert.equal(url.searchParams.get("range"), "5y");
    assert.equal(options.redirect, "error");
    return new Response(JSON.stringify({ chart: { result: [{ meta: { currency: "USD" }, timestamp: dates,
      indicators: { adjclose: [{ adjclose: dates.map((_, index) => 100 + index + index % 2) }] } }] } }));
  };
  const { response, data } = await call("/api/gs-quant/analyze", { positions: [{ symbol: "AAA", weight_pct: 60 }, { symbol: "BBB", weight_pct: 40 }], benchmark: "SPY", lookback: "3y", risk_free_rate_pct: 4 });
  assert.equal(response.status, 200);
  assert.equal(count, 3);
  assert.equal(data.engine.gs_quant_runtime, false);
  assert.equal(data.metrics.portfolio.observations, 99);
});
test("input validation rejects unsupported fields, quantities, markets and duplicates", async () => {
  for (const payload of [
    { holdings: Array(51).fill(holding()) }, { holdings: [holding(), holding()] },
    { holdings: [holding("https://evil.example")] }, { holdings: [holding("AAPL", "other")] },
    { holdings: [holding("005930", "kospi", 0.5)] }, { holdings: [holding("M04020000", "gold", 0.5)] },
    { holdings: [holding("AAPL", "us", -1)] }, { holdings: [holding("AAPL", "us", true)] },
    { holdings: [holding()], url: "https://evil.example" }, { cash_krw: 0.5 }, { cash_krw: true }, { cash_krw: 10000000000001 },
    { holdings: "AAPL" }, { holdings: [{ ...holding(), name: "\u0000" }] }, [],
  ]) assert.equal((await call("/api/portfolio/compose", payload)).response.status, 400, JSON.stringify(payload));
});
test("query validation rejects duplicates, unknown fields and malformed limits", async () => {
  for (const path of [
    "/api/portfolio/search?market=us&q=x&q=y", "/api/portfolio/search?market=us&q=x&url=https://evil.example",
    "/api/portfolio/search?market=us&q=x&limit=1.5", "/api/portfolio/search?market=us&q=x&limit=21",
    "/api/portfolio/search?market=us", "/api/portfolio/search?market=us&q=" + "a".repeat(81),
    "/api/dashboard?url=https://evil.example", "/api/stocks/AAPL?url=https://evil.example",
  ]) assert.equal((await call(path)).response.status, 400, path);
});
test("invalid ticker paths cannot access arbitrary asset URLs", async () => {
  for (const symbol of ["https%3A%2F%2Fevil.example", "%2Fetc%2Fpasswd", "A%00", "A..B", "%25", "AAPL%3Furl%3Dhttps%3Aevil"]) {
    assert.equal((await call(`/api/stocks/${symbol}`)).response.status, 400, symbol);
  }
});
test("methods, JSON content type, body size and cross-origin POST are rejected", async () => {
  assert.equal((await call("/api/dashboard", undefined, env(), "DELETE")).response.status, 405);
  assert.equal((await call("/api/refresh")).response.status, 405);
  for (const [headers, content, status] of [
    [{ "Content-Type": "text/plain" }, "{}", 415],
    [{ "Content-Type": "application/json" }, "{", 400],
    [{ "Content-Type": "application/json" }, " ".repeat(65537), 413],
    [{ "Content-Type": "application/json", Origin: "https://evil.example" }, "{}", 403],
  ]) {
    const response = await worker.fetch(new Request("https://app.example/api/portfolio/compose", { method: "POST", headers, body: content }), env());
    assert.equal(response.status, status);
    assert.equal(response.headers.get("Cache-Control"), "no-store");
  }
});
