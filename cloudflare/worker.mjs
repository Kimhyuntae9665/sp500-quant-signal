import { analyzeRisk } from "./risk.mjs";

// Public market data only. Portfolio inputs are computed in-memory, never stored.
const SYMBOL = /^[A-Z0-9][A-Z0-9.-]{0,14}$/;
const GOLD = "M04020000";
const GROUPS = {
  us: { label: "미국 포트", color: "#3D73D9" },
  kospi: { label: "한국 포트", color: "#D95567" },
  gold: { label: "금", color: "#D99B18" },
  cash: { label: "현금", color: "#7D8995" },
};
const CLOUD_NOTE = "클라우드 신호는 GitHub Actions 예약 작업으로 갱신됩니다. 표시된 데이터 기준일을 확인하세요.";
const SNAPSHOT_NOTE = "정기 수집 스냅샷 기준이며 실시간 시세가 아닙니다. 누락 시세는 합계에서 제외합니다.";
const MAX_LIVE_QUOTES = 16;
const now = () => new Date().toISOString();
const positive = value => typeof value === "number" && Number.isFinite(value) && value > 0;
const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
const refresh = () => ({ running: false, status: "idle", mode: "cloud_scheduled", message: CLOUD_NOTE, started_at: null, finished_at: null, error: null });

class ApiError extends Error {
  constructor(status, code, message, details) { super(message); this.status = status; this.code = code; this.details = details; }
}
function fail(code, message) { throw new ApiError(400, code, message); }
function json(payload, status = 200) {
  return new Response(JSON.stringify(payload), { status, headers: {
    "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",
  } });
}
function fields(value, allowed) {
  if (!object(value)) fail("invalid_body", "JSON 본문은 객체여야 합니다.");
  if (Object.keys(value).some(key => !allowed.includes(key))) fail("unknown_fields", "허용되지 않은 JSON 항목입니다.");
}
function query(url, allowed) {
  for (const key of url.searchParams.keys()) {
    if (!allowed.includes(key) || url.searchParams.getAll(key).length !== 1) fail("invalid_query", "허용되지 않거나 중복된 검색 항목입니다.");
  }
}
function market(value) {
  if (typeof value !== "string" || !["us", "kospi", "gold"].includes(value)) fail("invalid_market", "market은 us, kospi, gold 중 하나여야 합니다.");
  return value;
}
function ticker(value) {
  let symbol;
  try { symbol = decodeURIComponent(value).trim().toUpperCase(); } catch { fail("invalid_ticker", "잘못된 종목 코드입니다."); }
  if (!SYMBOL.test(symbol) || symbol.includes("..")) fail("invalid_ticker", "종목 코드는 영숫자, 점, 하이픈 1~15자입니다.");
  return symbol;
}
async function asset(env, request, path, optional = false) {
  const trustedBase = "https://kimhyuntae9665.github.io/sp500-quant-signal/data/";
  let fallbackWarning = null;
  if (env.DATA_BASE_URL) {
    if (env.DATA_BASE_URL !== trustedBase) throw new ApiError(503, "snapshot_configuration_invalid", "허용된 공개 스냅샷 경로 설정을 확인하세요.");
    try {
      const remote = await fetch(new URL(path.slice("/data/".length), trustedBase), {
        redirect: "error", signal: AbortSignal.timeout(5000), cf: { cacheTtl: 300, cacheEverything: true }, headers: { Accept: "application/json" },
      });
      if (!remote.ok) throw new Error("Snapshot unavailable");
      const result = await remote.json();
      if (!object(result)) throw new Error("Invalid snapshot");
      return { ...result, cloud_snapshot: { source: "github_pages", bundled_fallback: false, cache_ttl_seconds: 300 } };
    } catch { fallbackWarning = "최신 공개 스냅샷에 연결하지 못해 배포 시 포함된 데이터를 사용합니다. 기준일이 오래되었을 수 있습니다."; }
  }
  const response = await env.ASSETS.fetch(new Request(new URL(path, request.url), { method: "GET" }));
  if (!response.ok) {
    if (optional && response.status === 404) return { cloud_snapshot: { source: "unavailable", bundled_fallback: Boolean(fallbackWarning), warning: fallbackWarning }, warnings: fallbackWarning ? [fallbackWarning] : [] };
    throw new ApiError(response.status === 404 ? 404 : 503, "snapshot_unavailable", "수집된 데이터가 아직 없습니다. 예약 수집 결과를 확인하세요.");
  }
  try {
    const result = await response.json();
    if (!object(result)) throw new Error("Invalid snapshot");
    return { ...result, cloud_snapshot: { source: "bundled_assets", bundled_fallback: Boolean(fallbackWarning), warning: fallbackWarning },
      ...(fallbackWarning ? { warnings: [...(Array.isArray(result.warnings) ? result.warnings : []), fallbackWarning] } : {}) };
  } catch { throw new ApiError(503, "snapshot_invalid", "수집 데이터 형식을 확인할 수 없습니다."); }
}
async function body(request, allowed) {
  if (!(request.headers.get("Content-Type") || "").toLowerCase().startsWith("application/json")) throw new ApiError(415, "unsupported_media_type", "application/json 본문이 필요합니다.");
  const origin = request.headers.get("Origin");
  if (origin && origin !== new URL(request.url).origin) throw new ApiError(403, "origin_not_allowed", "다른 사이트에서 보낸 요청은 허용하지 않습니다.");
  const reader = request.body?.getReader();
  let size = 0;
  const chunks = [];
  if (reader) {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > 65536) { await reader.cancel(); throw new ApiError(413, "body_too_large", "요청 본문은 64KiB 이하여야 합니다."); }
      chunks.push(value);
    }
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
  let result;
  try { result = JSON.parse(new TextDecoder().decode(bytes)); } catch { fail("invalid_json", "올바른 JSON 본문이 필요합니다."); }
  fields(result, allowed);
  return result;
}
function source(raw, fallback = "Scheduled public market snapshot") {
  return object(raw) ? raw : { provider: "Public market snapshot", source: typeof raw === "string" ? raw : fallback, official: false };
}
function quote(raw, currency) {
  if (!object(raw) || !positive(raw.price ?? raw.value) || raw.currency && raw.currency !== currency || typeof raw.as_of !== "string" || !Number.isFinite(Date.parse(raw.as_of))) return null;
  return { price: raw.price ?? raw.value, currency, as_of: raw.as_of, source: source(raw.source) };
}
async function yahoo(path, params, budget = { remaining: 2 }) {
  // Neither host nor endpoint is controlled by callers. Never follow redirects.
  const attempts = [];
  for (const host of ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]) {
    if (budget.remaining <= 0) { attempts.push({ host, status: "request_budget_exhausted" }); break; }
    budget.remaining--;
    const url = new URL(path, `https://${host}`);
    for (const [key, value] of Object.entries(params)) url.searchParams.set(key, value);
    try {
      const response = await fetch(url, { redirect: "error", signal: AbortSignal.timeout(6000), headers: {
        Accept: "application/json", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
      } });
      attempts.push({ host, status: response.status });
      if (!response.ok) continue;
      const result = await response.json();
      if (!object(result)) continue;
      Object.defineProperty(result, "cloudUpstream", { value: { host, attempts } });
      return result;
    } catch (error) { attempts.push({ host, status: error.name === "TimeoutError" || error.name === "AbortError" ? "timeout" : "network_or_invalid_json" }); }
  }
  throw new ApiError(502, "market_provider_unavailable", "외부 편의 시세 제공처에 연결할 수 없습니다.", { attempts });
}
async function liveQuote(symbol, currency) {
  const data = await yahoo(`/v8/finance/chart/${encodeURIComponent(symbol)}`, { range: "5d", interval: "1d", includePrePost: "false" });
  const row = data?.chart?.result?.[0];
  const meta = row?.meta;
  if (!meta || meta.currency !== currency) return null;
  let price = meta.regularMarketPrice;
  let epoch = meta.regularMarketTime;
  if (!positive(price)) {
    const closes = row.indicators?.quote?.[0]?.close || [];
    for (let index = closes.length - 1; index >= 0; index--) if (positive(closes[index])) { price = closes[index]; epoch = row.timestamp?.[index]; break; }
  }
  if (!positive(price) || !positive(epoch) || !Number.isFinite(new Date(epoch * 1000).getTime())) return null;
  return { price, currency, as_of: new Date(epoch * 1000).toISOString(), source: {
    provider: "Yahoo Finance", source: "Yahoo Finance convenience API (unofficial)", official: false,
    url: `https://${data.cloudUpstream.host}/v8/finance/chart/${encodeURIComponent(symbol)}`,
    note: "요청 시 조회한 지연 가능 편의 시세입니다. 공식 거래소 API가 아닙니다.",
  } };
}
async function liveGold() {
  const url = "https://m.stock.naver.com/marketindex/metals/M04020000";
  const response = await fetch(url, { redirect: "error", signal: AbortSignal.timeout(8000), headers: { Accept: "text/html", "Accept-Language": "ko-KR" } });
  if (!response.ok) return null;
  const document = await response.text();
  const match = document.match(/<script\b[^>]*\bid=["']__NEXT_DATA__["'][^>]*>(.*?)<\/script>/s);
  if (!match) return null;
  const stack = [JSON.parse(match[1])];
  let visited = 0;
  while (stack.length && visited++ < 10000) {
    const item = stack.pop();
    if (!item || typeof item !== "object") continue;
    if (item.reutersCode === GOLD || item.symbolCode === GOLD) {
      for (const key of ["closePrice", "currentPrice", "nowPrice", "tradePrice", "price", "value"]) {
        const price = typeof item[key] === "string" ? Number(item[key].replaceAll(",", "").replace(/\s*(?:원|KRW)\s*$/u, "")) : item[key];
        const rawDate = item.localTradedAt || item.tradedAt || item.asOf;
        const as_of = typeof rawDate === "string" && /^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?$/.test(rawDate)
          ? `${rawDate.replace(" ", "T")}${rawDate.length === 10 ? "T00:00:00" : ""}+09:00` : rawDate;
        const result = quote({ price, as_of, source: { provider: "Naver", source: "Naver mobile market-index __NEXT_DATA__", url, official: false, note: "KRX 국내 금의 네이버 편의 시세(원/g)이며 공식 KRX API가 아닙니다." } }, "KRW");
        if (result) return result;
      }
    }
    for (const value of Object.values(item)) if (value && typeof value === "object") stack.push(value);
  }
  return null;
}
async function riskHistory(symbol, lookback, request, env, diagnostics, sources, budget) {
  // Validation remains here as well as in risk.mjs: no arbitrary URL/interval.
  if (!SYMBOL.test(symbol) || symbol.includes("..") || !["1y", "3y", "5y"].includes(lookback)) throw new ApiError(400, "invalid_symbol", "허용되지 않은 위험 분석 종목 또는 기간입니다.");
  if (request) {
    const snapshot = await asset(env, request, `/data/risk/${symbol}.json`, true);
    const prices = snapshot.prices;
    if (snapshot.frequency === "daily" && snapshot.adjusted === true && Array.isArray(prices) && prices.length >= 60) {
      sources[symbol] = { kind: "scheduled_daily_snapshot", as_of: snapshot.as_of ?? prices.at(-1)?.date ?? null, source: snapshot.source ?? null, cloud_snapshot: snapshot.cloud_snapshot };
      if (snapshot.cloud_snapshot?.bundled_fallback) diagnostics.push(`${symbol}: ${snapshot.cloud_snapshot.warning}`);
      return prices;
    }
  }
  let data;
  try { data = await yahoo(`/v8/finance/chart/${encodeURIComponent(symbol)}`, { range: lookback === "3y" ? "5y" : lookback, interval: "1d", includePrePost: "false" }, budget); }
  catch (error) { throw new ApiError(502, "history_unavailable", `${symbol}의 일별 수정 종가를 확인할 수 없습니다.`, { symbol, ...(error.details || {}) }); }
  const row = data?.chart?.result?.[0];
  const adjusted = row?.indicators?.adjclose?.[0]?.adjclose;
  if (!row || !Array.isArray(adjusted) || row.meta?.currency !== "USD") throw new ApiError(502, "history_unavailable", `${symbol}의 수정 종가를 확인할 수 없습니다.`);
  if (sources) sources[symbol] = { kind: "yahoo_direct", upstream: data.cloudUpstream };
  return (row.timestamp || []).flatMap((timestamp, index) => positive(timestamp) && positive(adjusted[index]) ? [{ date: new Date(timestamp * 1000).toISOString().slice(0, 10), value: adjusted[index] }] : []);
}
function instrument(item, selectedMarket = item.market) {
  return { id: `${selectedMarket}:${item.symbol}`, market: selectedMarket, symbol: item.symbol,
    name: item.name || item.symbol, exchange: item.exchange || (selectedMarket === "us" ? "US" : "KRX"),
    asset_type: item.asset_type || (selectedMarket === "gold" ? "GOLD" : "EQUITY"),
    currency: selectedMarket === "us" ? "USD" : "KRW", quantity_unit: selectedMarket === "gold" ? "g" : "shares" };
}
async function search(request, env, url) {
  query(url, ["market", "q", "limit"]);
  const selectedMarket = market(url.searchParams.get("market"));
  const q = (url.searchParams.get("q") || "").trim();
  if (q.length > 80 || /[\u0000-\u001f]/u.test(q) || selectedMarket !== "gold" && !q) fail("invalid_query", "검색어는 1~80자여야 합니다.");
  const limitText = url.searchParams.get("limit") ?? "10";
  if (!/^(?:[1-9]|1\d|20)$/.test(limitText)) fail("invalid_limit", "검색 결과 수는 1~20 사이 정수여야 합니다.");
  const limit = Number(limitText);
  const snapshot = await asset(env, request, "/data/portfolio.json", true);
  const warnings = Array.isArray(snapshot.warnings) ? [...snapshot.warnings] : [];
  let results = (Array.isArray(snapshot.catalog) ? snapshot.catalog : [])
    .filter(item => item.market === selectedMarket && `${item.symbol} ${item.name}`.toLocaleLowerCase().includes(q.toLocaleLowerCase()))
    .slice(0, limit).map(item => instrument(item));
  if (selectedMarket === "gold") results = !q || [GOLD, "KRX 금 99.99_1kg", "gold"].some(text => text.toLowerCase().includes(q.toLowerCase()))
    ? [instrument({ symbol: GOLD, name: "KRX 금 99.99_1kg", market: "gold" })] : [];
  // Exact catalog matches need no vendor call, including ETFs outside S&P 500.
  if (results.some(item => item.symbol.toLowerCase() === q.toLowerCase())) {
    results.sort((a, b) => Number(b.symbol.toLowerCase() === q.toLowerCase()) - Number(a.symbol.toLowerCase() === q.toLowerCase()));
    return { query: q, market: selectedMarket, results, source: source(snapshot.source).source, cloud_snapshot: snapshot.cloud_snapshot ?? null,
      warnings: [...warnings, "수집된 종목 목록에서 검색했습니다. 수집 범위 밖 종목이나 목록 기준일 이후의 신규 상장은 누락될 수 있습니다."] };
  }
  if (["us", "kospi"].includes(selectedMarket)) {
    try {
      const data = await yahoo("/v1/finance/search", { q, quotesCount: Math.max(limit * 3, 20), newsCount: 0 });
      if (!Array.isArray(data.quotes)) throw new Error("Invalid search");
      const exchanges = new Set(["NMS", "NGM", "NCM", "NYQ", "ASE", "PCX", "BTS", "BATS", "NASDAQ", "NYSE", "NYSEARCA", "AMEX"]);
      const dynamic = data.quotes.filter(item => selectedMarket === "kospi"
        ? /^\d{6}\.KS$/.test(item.symbol || "") && item.quoteType === "EQUITY"
        : SYMBOL.test(item.symbol || "") && !item.symbol.includes("..") && ["EQUITY", "ETF"].includes(item.quoteType) && exchanges.has(item.exchange));
      const merged = [...dynamic.map(item => instrument({ symbol: selectedMarket === "kospi" ? item.symbol.slice(0, 6) : item.symbol, name: item.longname || item.shortname || item.symbol, exchange: item.exchange, asset_type: item.quoteType }, selectedMarket)), ...results];
      results = merged.filter((item, index) => merged.findIndex(candidate => candidate.id === item.id) === index).slice(0, limit);
      return { query: q, market: selectedMarket, results, cloud_snapshot: snapshot.cloud_snapshot ?? null, upstream: data.cloudUpstream, source: "Yahoo Finance convenience API (unofficial) + scheduled catalog", warnings: [...warnings, selectedMarket === "us" ? "검색 범위는 S&P 500에 한정되지 않은 미국 상장 주식·ETF입니다. 편의 API 장애·제공 범위에 따라 누락될 수 있습니다." : "KOSPI 스냅샷과 Yahoo의 .KS 검색을 사용합니다. 전체 상장 종목의 검색 완전성을 보장하지 않습니다."] };
    } catch (error) { warnings.push("검색 제공처에 연결하지 못해 수집 스냅샷 내 종목만 검색했습니다."); snapshot.upstream_diagnostics = error.details?.attempts || []; }
  }
  if (selectedMarket === "kospi") warnings.push("KOSPI 검색은 마지막으로 수집된 종목 목록 기준입니다.");
  return { query: q, market: selectedMarket, results, source: source(snapshot.source).source, cloud_snapshot: snapshot.cloud_snapshot ?? null, upstream_diagnostics: snapshot.upstream_diagnostics ?? [], warnings };
}
function validatePortfolio(payload) {
  const holdings = payload.holdings ?? [];
  if (!Array.isArray(holdings)) fail("invalid_holdings", "holdings는 배열이어야 합니다.");
  if (holdings.length > 50) fail("too_many_holdings", "최대 50개 종목까지 입력할 수 있습니다.");
  const cash = payload.cash_krw ?? 0;
  if (!Number.isSafeInteger(cash) || cash < 0 || cash > 10000000000000) fail("invalid_cash", "현금은 0~10조원 범위의 정수 원화 금액이어야 합니다.");
  const seen = new Set();
  const normalized = holdings.map(item => {
    fields(item, ["market", "symbol", "name", "quantity"]);
    const selectedMarket = market(item.market);
    if (typeof item.symbol !== "string") fail("invalid_symbol", "종목 코드는 문자열이어야 합니다.");
    const symbol = item.symbol.trim().toUpperCase();
    if (selectedMarket === "us" ? !SYMBOL.test(symbol) || symbol.includes("..") : selectedMarket === "kospi" ? !/^\d{6}$/.test(symbol) : symbol !== GOLD) fail("invalid_symbol", "시장에 맞는 종목 코드를 입력하세요.");
    if (typeof item.name !== "string" || !item.name.trim() || item.name.trim().length > 200 || /[\u0000-\u001f]/u.test(item.name)) fail("invalid_name", "종목 이름은 1~200자여야 합니다.");
    if (!positive(item.quantity) || selectedMarket !== "us" && !Number.isSafeInteger(item.quantity)) fail("invalid_quantity", "미국 주식 수량은 양수, 한국 주식·금 수량은 양의 정수여야 합니다.");
    const id = `${selectedMarket}:${symbol}`;
    if (seen.has(id)) fail("duplicate_holding", "같은 종목의 수량을 합쳐 입력하세요.");
    seen.add(id);
    return { id, market: selectedMarket, symbol, name: item.name.trim(), quantity: item.quantity, unit: selectedMarket === "gold" ? "g" : "shares", quantity_unit: selectedMarket === "gold" ? "g" : "shares" };
  });
  if (!normalized.length && !cash) fail("invalid_holdings", "보유 종목 또는 현금을 입력하세요.");
  return { holdings: normalized, cash };
}
async function compose(request, env, payload) {
  const { holdings, cash } = validatePortfolio(payload);
  const snapshot = holdings.length ? await asset(env, request, "/data/portfolio.json", true) : {};
  const warnings = [SNAPSHOT_NOTE, ...(Array.isArray(snapshot.warnings) ? snapshot.warnings : [])];
  const needsFx = holdings.some(item => item.market === "us");
  let fx = needsFx ? quote(snapshot.usd_krw, "KRW") : null;
  let fxDiagnostics = [];
  const fxPromise = !fx && needsFx
    ? liveQuote("KRW=X", "KRW").then(value => { fx = value; }).catch(error => { fxDiagnostics = error.details?.attempts || []; })
    : Promise.resolve();
  let liveCount = 0;
  const priced = [], unpriced = [];
  await Promise.all(holdings.map(async holding => {
    const currency = holding.market === "us" ? "USD" : "KRW";
    const quoteMap = object(snapshot.quotes) ? snapshot.quotes : {};
    let local = quote(holding.market === "gold" ? snapshot.gold : quoteMap[holding.id] ?? quoteMap[holding.symbol], currency);
    if (!local && holding.market === "gold") { try { local = await liveGold(); } catch { /* Missing gold stays unpriced. */ } }
    if (!local && holding.market !== "gold" && liveCount < MAX_LIVE_QUOTES) {
      liveCount++;
      try { local = await liveQuote(holding.market === "kospi" ? `${holding.symbol}.KS` : holding.symbol, currency); } catch (error) { holding.upstream_diagnostics = error.details?.attempts || []; }
    }
    if (holding.market === "us") await fxPromise;
    const rate = holding.market === "us" ? fx?.price : 1;
    const value = holding.quantity * local?.price * rate;
    if (!local || !positive(rate) || !positive(value)) {
      unpriced.push({ ...holding, error: { code: "quote_unavailable", message: !positive(rate) ? "USD/KRW 환율을 확인할 수 없습니다." : "기준일이 있는 유효 시세가 없거나 요청별 조회 한도에 도달해 합계에서 제외했습니다." } });
      return;
    }
    priced.push({ ...holding, local_price: local.price, price: local.price, currency, price_currency: currency, fx_rate: rate,
      value_krw: value, value_manwon: value / 10000, group_key: holding.market, group_label: GROUPS[holding.market].label,
      color: GROUPS[holding.market].color, quote: { as_of: local.as_of, source: local.source }, quote_as_of: local.as_of, as_of: local.as_of, source: local.source });
  }));
  const inputOrder = new Map(holdings.map((item, index) => [item.id, index]));
  priced.sort((a, b) => inputOrder.get(a.id) - inputOrder.get(b.id));
  unpriced.sort((a, b) => inputOrder.get(a.id) - inputOrder.get(b.id));
  if (cash) {
    const timestamp = now(), meta = { provider: "User input", source: "사용자 입력 현금", official: false };
    priced.push({ id: "cash:KRW", market: "cash", symbol: "KRW", name: "현금", quantity: cash, unit: "KRW", quantity_unit: "KRW",
      local_price: 1, price: 1, currency: "KRW", price_currency: "KRW", fx_rate: 1, value_krw: cash, value_manwon: cash / 10000,
      group_key: "cash", group_label: GROUPS.cash.label, color: GROUPS.cash.color, quote: { as_of: timestamp, source: meta },
      quote_as_of: timestamp, as_of: timestamp, source: meta });
  }
  if (!priced.length) throw new ApiError(502, "portfolio_no_priced_holdings", "확인 가능한 시세가 없어 포트폴리오를 계산할 수 없습니다.", {
    quotes: unpriced.map(item => ({ symbol: item.symbol, error: item.error.code, upstream_diagnostics: item.upstream_diagnostics ?? [] })), fx_upstream_diagnostics: fxDiagnostics,
  });
  const total = priced.reduce((sum, item) => sum + item.value_krw, 0);
  if (!positive(total)) throw new ApiError(400, "invalid_quantity", "계산 가능한 범위를 초과한 수량입니다.");
  for (const item of priced) item.weight_pct = Math.round(item.value_krw / total * 1e8) / 1e6;
  const groups = Object.entries(GROUPS).map(([key, definition]) => {
    const value = priced.filter(item => item.group_key === key).reduce((sum, item) => sum + item.value_krw, 0);
    return { key, group_key: key, ...definition, value_krw: value, value_manwon: value / 10000, weight_pct: Math.round(value / total * 1e8) / 1e6 };
  });
  if (unpriced.length) warnings.push(`${unpriced.length}개 종목을 합계에서 제외했습니다. 제외 종목을 확인하세요.`);
  if (liveCount) warnings.push(`스냅샷에 없는 종목은 요청당 최대 ${MAX_LIVE_QUOTES}개까지 외부 편의 시세로 조회했습니다.`);
  return { generated_at: now(), snapshot_generated_at: snapshot.generated_at ?? null, cloud_snapshot: snapshot.cloud_snapshot ?? null, base_currency: "KRW", unit: "만원",
    total_value_krw: total, total_value_manwon: total / 10000,
    fx: { pair: "USD/KRW", rate: fx?.price ?? null, usdkrw: fx?.price ?? null, as_of: fx?.as_of ?? null, source: fx?.source ?? null, upstream_diagnostics: fxDiagnostics },
    groups, holdings: priced, priced, priced_holdings: priced, unpriced, errors: unpriced, warnings };
}
async function route(request, env) {
  const url = new URL(request.url), path = url.pathname;
  if (!path.startsWith("/api/")) {
    if (!["GET", "HEAD"].includes(request.method)) throw new ApiError(405, "method_not_allowed", "GET 또는 HEAD 요청만 허용됩니다.");
    return env.ASSETS.fetch(request);
  }
  if (!["GET", "POST", "HEAD"].includes(request.method)) throw new ApiError(405, "method_not_allowed", "지원하지 않는 HTTP 메서드입니다.");
  if (request.method === "POST") {
    query(url, []);
    if (path === "/api/portfolio/compose") return json(await compose(request, env, await body(request, ["holdings", "cash_krw"])));
    if (path === "/api/refresh") {
      const data = await body(request, ["mode", "limit"]);
      if (data.mode !== undefined && !["quick", "full"].includes(data.mode)) fail("invalid_mode", "mode는 quick 또는 full이어야 합니다.");
      if (data.limit != null && (!Number.isInteger(data.limit) || data.limit < 1 || data.limit > 1000)) fail("invalid_limit", "limit은 1~1000 사이 정수여야 합니다.");
      throw new ApiError(409, "cloud_scheduled_refresh", `${CLOUD_NOTE} 이 버튼은 수집 작업을 시작하지 않습니다.`);
    }
    if (path === "/api/gs-quant/analyze") {
      const payload = await body(request, ["positions", "benchmark", "lookback", "risk_free_rate_pct"]);
      const diagnostics = [], sources = {};
      // Worst case: 13 remote snapshot + 13 bundled reads + 22 Yahoo attempts = 48.
      const budget = { remaining: 22 };
      const upstreamDiagnostics = [];
      try {
        const result = await analyzeRisk(payload, { prices: {} }, async (symbol, lookback) => {
          try { return await riskHistory(symbol, lookback, request, env, diagnostics, sources, budget); }
          catch (error) { upstreamDiagnostics.push({ symbol, ...(error.details || {}) }); throw error; }
        });
        const published = Object.values(sources).some(item => item.kind === "scheduled_daily_snapshot");
        const provider = published ? "Scheduled daily adjusted-price snapshot / Yahoo direct fallback" : "Yahoo Finance chart adjusted close";
        Object.assign(result.data, { provider, source: provider, source_name: provider, source_by_symbol: sources,
          source_disclosure: { ...result.data.source_disclosure, source_name: provider } });
        result.warnings.push(...diagnostics);
        result.data.warnings_count = result.warnings.length;
        return json(result);
      }
      catch (error) { if (error instanceof ApiError) throw error; throw new ApiError(Number.isInteger(error.status) && error.status >= 400 && error.status <= 599 ? error.status : 502, error.code || "risk_analysis_unavailable", error.message || "위험 분석 데이터를 조회할 수 없습니다.", { ...(error.details || {}), ...(upstreamDiagnostics.length ? { upstream_diagnostics: upstreamDiagnostics } : {}) }); }
    }
    throw new ApiError(405, "method_not_allowed", "이 경로는 POST 요청을 지원하지 않습니다.");
  }
  if (path === "/api/portfolio/search") return json(await search(request, env, url));
  query(url, []);
  if (["/api/portfolio/compose", "/api/refresh", "/api/gs-quant/analyze"].includes(path)) throw new ApiError(405, "method_not_allowed", "POST 요청이 필요합니다.");
  if (path === "/api/health") {
    let dashboard;
    try { dashboard = await asset(env, request, "/data/dashboard.json"); } catch { /* Health must remain diagnosable before the first snapshot. */ }
    return json({ ok: Boolean(dashboard), service: "sp500-quant-signal", time: now(), deployment: "cloudflare_scheduled_snapshot",
      engine: { ready: Boolean(dashboard), type: "Scheduled Python snapshot" }, refresh: refresh(),
      data_generated_at: dashboard?.generated_at ?? dashboard?.as_of ?? null,
      cloud_snapshot: dashboard?.cloud_snapshot ?? null,
      gs_quant_lab: { ready: true, mode: "cloud_javascript_public_data", marquee_connected: false, engine: "JavaScript statistics (not Python gs-quant)" } }, dashboard ? 200 : 503);
  }
  if (path === "/api/dashboard") return json({ ...await asset(env, request, "/data/dashboard.json"), refresh: refresh(), cloud: { scheduled: true, message: CLOUD_NOTE } });
  if (path === "/api/rules") return json(await asset(env, request, "/data/rules.json"));
  const match = path.match(/^\/api\/stocks\/([^/]+)(\/history)?$/);
  if (match) {
    const symbol = ticker(match[1]);
    return json(await asset(env, request, `/data/${match[2] ? "histories" : "stocks"}/${symbol}.json`));
  }
  throw new ApiError(404, "not_found", "API 경로를 찾을 수 없습니다.");
}
export default {
  async fetch(request, env) {
    try {
      const response = await route(request, env);
      return request.method === "HEAD" ? new Response(null, { status: response.status, headers: response.headers }) : response;
    } catch (error) {
      const response = json({ error: { code: error instanceof ApiError ? error.code : "internal_error", message: error instanceof ApiError ? error.message : "요청 처리 중 오류가 발생했습니다.", ...(error instanceof ApiError && error.details ? { details: error.details } : {}) }, time: now() }, error instanceof ApiError ? error.status : 500);
      return request.method === "HEAD" ? new Response(null, { status: response.status, headers: response.headers }) : response;
    }
  },
};
