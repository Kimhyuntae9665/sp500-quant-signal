/*
 * Expected API JSON (canonical engine shape; aliases are normalized below)
 *
 * GET /api/dashboard
 * {
 *   generated_at: "2026-07-19T12:00:00Z",
 *   as_of: "2026-07-18",
 *   universe: { count: 503, provenance: { source, as_of, retrieved_at }, warnings: [] },
 *   count: 503,
 *   eligible_count: 470,
 *   market_overlay: {
 *     score: -1, complete: true,
 *     metrics: { spy_drawdown_52_week_pct: { value, unit, status, as_of, provenance: [] } },
 *     components: [{ key, label, metric_key, value, unit, points, applicable, reason, metric_status }]
 *   },
 *   signals: [{
 *     symbol: "MSFT", company: { name, sector, sub_industry },
 *     metrics: { current_price: { value, unit, status, as_of, provenance: [], note }, ... },
 *     components: [{ key, label, metric_key, value, unit, points, applicable, reason, metric_status }],
 *     company_score: 8, market_score: -1, total_score: 7,
 *     coverage_count: 7, coverage_total: 9, coverage_pct: 77.8,
 *     eligible_for_ranking: true,
 *     recommendation: "watch_buy", recommendation_label: "매수 관심", warnings: []
 *   }],
 *   data_quality: { ... },
 *   refresh: { status: "idle|queued|running|complete|failed", progress: 0..100, message: "..." }
 * }
 *
 * GET /api/stocks/{ticker} -> one signal object in the same shape as signals[]
 * GET /api/rules -> { version, company_rules: [{ key,label,metric_key,unit,thresholds,default_points,note }],
 *                     market_rules: [...], minimum_company_metrics, recommendation_bands, missing_data_policy }
 * POST /api/refresh body { mode: "quick" } -> 202 { accepted: true, refresh: { status, progress, message } }
 *
 * The normalizer also accepts common wrappers (`data`, `dashboard`, `stocks`, `items`) and
 * camelCase/flat metric fields so the static UI remains useful while the API evolves.
 */

(() => {
  "use strict";

  const API = Object.freeze({
    dashboard: "/api/dashboard",
    stock: (ticker) => `/api/stocks/${encodeURIComponent(ticker)}`,
    history: (ticker) => `/api/stocks/${encodeURIComponent(ticker)}/history`,
    rules: "/api/rules",
    refresh: "/api/refresh",
    portfolioSearch: (market, query, limit = 12) => `/api/portfolio/search?market=${encodeURIComponent(market)}&q=${encodeURIComponent(query)}&limit=${encodeURIComponent(limit)}`,
    portfolioCompose: "/api/portfolio/compose",
  });
  const TABLE_BATCH_DESKTOP = 80;
  const TABLE_BATCH_MOBILE = 30;
  const SEARCH_DEBOUNCE_MS = 160;
  const PORTFOLIO_SEARCH_DEBOUNCE_MS = 300;
  const PORTFOLIO_STORAGE_KEY = "sp500-quant-signal.portfolio.v1";
  const PORTFOLIO_STORAGE_VERSION = 2;
  const PORTFOLIO_MAX_CASH_MANWON = 1000000000;
  const PORTFOLIO_MARKETS = Object.freeze({
    us: { label: "미국", unit: "주", step: 0.0001, min: 0.0001 },
    kospi: { label: "한국", unit: "주", step: 1, min: 1 },
    gold: { label: "금", unit: "g", step: 1, min: 1 },
  });
  const PORTFOLIO_GROUP_ORDER = Object.freeze(["us", "kospi", "gold", "cash"]);
  const PORTFOLIO_GROUP_META = Object.freeze({
    us: { label: "미국 포트", shortLabel: "US", color: "#3D73D9" },
    kospi: { label: "한국 포트", shortLabel: "Korea", color: "#D95567" },
    gold: { label: "금", shortLabel: "Gold", color: "#D99B18" },
    cash: { label: "현금", shortLabel: "Cash", color: "#7D8995" },
  });
  const PORTFOLIO_GROUP_PALETTES = Object.freeze({
    us: Object.freeze(["#3478F6", "#00A6A6", "#6C5CE7", "#138ACF", "#5267E8", "#008F7A", "#8B5CF6", "#1D9BF0", "#4263EB", "#0E7490", "#6366F1", "#16A3B6"]),
    kospi: Object.freeze(["#E85D75", "#F9735B", "#C84C9B", "#D94A4A", "#B45BC7", "#FF8A65", "#AD3F72", "#EF6C8F", "#C2415D", "#E76F51", "#D75A9A", "#F08A5D"]),
    gold: Object.freeze(["#E0A51B", "#F4B740", "#C88A0A", "#F2C94C", "#D97706", "#E8B04A", "#B7791F", "#FFCC4D"]),
    cash: Object.freeze(["#637180", "#7D8995", "#96A1AB", "#AEB7BF", "#566270", "#89959F", "#A2ACB5", "#707D89"]),
  });

  const SIGNALS = Object.freeze({
    "strong-buy": { label: "강한 매수", range: "+10 이상", helper: "기준을 강하게 충족" },
    "buy-watch": { label: "매수 대기", range: "+5 ~ +9", helper: "추가 확인할 후보" },
    neutral: { label: "관망", range: "-2 ~ +4", helper: "방향성 확인 구간" },
    "sell-watch": { label: "매도 대기", range: "-7 ~ -3", helper: "위험 요인 점검" },
    sell: { label: "매도", range: "-8 이하", helper: "감점 요인이 우세" },
    insufficient: { label: "데이터 부족", range: "필수값 미충족", helper: "추천 순위에서 제외" },
  });

  const METRIC_CONFIG = Object.freeze({
    drawdown: {
      label: "52주 고점 대비 하락",
      shortLabel: "고점 대비 하락",
      key: "drawdown_52_week_pct",
      aliases: ["drawdown_52_week_pct", "drawdown52w", "drawdown", "drawdown_from_high", "high_drawdown", "drawdownPct"],
      scoreAliases: ["drawdown_score", "drawdownScore", "high_drawdown_score"],
      unit: "percent",
    },
    peGap: {
      label: "3년 Forward PER 괴리",
      shortLabel: "3Y PER 괴리",
      key: "pe_gap_3y_pct",
      aliases: ["pe_gap_3y_pct", "pe_gap_3y", "peGap3y", "per_gap_3y", "three_year_pe_gap", "peDeviation3y"],
      scoreAliases: ["pe_gap_score", "peGapScore", "per_gap_score"],
      unit: "percent",
    },
    distance200: {
      label: "200일 이동평균 이격",
      shortLabel: "200일선 이격",
      key: "distance_200dma_pct",
      aliases: ["distance_200dma_pct", "distance_200d", "distance200d", "sma200_distance", "distance_from_200d", "ma200Gap"],
      scoreAliases: ["distance_200d_score", "distance200dScore", "sma200_score"],
      unit: "percent",
    },
    epsGrowth: {
      label: "최근 연간 희석 EPS 성장률",
      shortLabel: "EPS 성장",
      key: "eps_growth_yoy_pct",
      aliases: ["eps_growth_yoy_pct", "eps_growth_1y", "epsGrowth", "eps_growth", "annual_eps_growth", "epsYoY"],
      scoreAliases: ["eps_growth_score", "epsGrowthScore", "eps_score"],
      unit: "percent",
    },
    rsi: {
      label: "RSI(14)",
      shortLabel: "RSI",
      key: "rsi_14",
      aliases: ["rsi_14", "rsi14", "rsi"],
      scoreAliases: ["rsi_score", "rsiScore"],
      unit: "index",
    },
    cashFlowQuality: {
      label: "3년 현금흐름 품질",
      shortLabel: "현금흐름 품질",
      key: "cash_flow_quality_3y",
      aliases: ["cash_flow_quality_3y", "cashFlowQuality3y", "cfo_net_income_3y"],
      scoreAliases: ["cash_flow_quality_score", "cashFlowQualityScore"],
      unit: "ratio",
    },
    shareholderReturn: {
      label: "3년 평균 주주환원율 프록시",
      shortLabel: "3Y 주주환원",
      key: "shareholder_return_3y_avg_pct",
      aliases: ["shareholder_return_3y_avg_pct", "shareholderYield3y", "shareholder_return_3y"],
      scoreAliases: ["shareholder_return_score", "shareholderYieldScore"],
      unit: "percent",
    },
    sharesChange: {
      label: "최근 3년 희석주식수 증감",
      shortLabel: "3Y 주식수",
      key: "shares_change_3y_pct",
      aliases: ["shares_change_3y_pct", "sharesChange3y", "share_dilution_3y"],
      scoreAliases: ["shares_dilution_score", "sharesChangeScore"],
      unit: "percent",
    },
    debt: {
      label: "부채/자기자본",
      shortLabel: "부채비율",
      key: "debt_to_equity",
      aliases: ["debt_to_equity", "debt_equity", "debtRatio", "debt_ratio", "debtToEquity"],
      scoreAliases: ["debt_score", "debtScore", "debt_to_equity_score"],
      unit: "ratio",
    },
  });

  const JUDGEMENT_ORDER = Object.freeze({
    "strong-buy": 0,
    "buy-watch": 1,
    neutral: 2,
    "sell-watch": 3,
    sell: 4,
    insufficient: 5,
  });
  const SORT_COLLATOR = new Intl.Collator("ko-KR", { numeric: true, sensitivity: "base" });
  const TABLE_SORT_DEFINITIONS = Object.freeze([
    {
      key: "ticker",
      label: "종목",
      type: "text",
      defaultDirection: "asc",
      modes: { asc: "ticker-asc", desc: "ticker-desc" },
      value: (stock) => stock.ticker,
    },
    {
      key: "sector",
      label: "섹터",
      type: "text",
      defaultDirection: "asc",
      modes: { asc: "sector-asc", desc: "sector-desc" },
      value: (stock) => stock.sector,
    },
    {
      key: "judgement",
      label: "판단",
      type: "category",
      defaultDirection: "asc",
      modes: { asc: "judgement-asc", desc: "judgement-desc" },
      value: (stock) => Object.prototype.hasOwnProperty.call(JUDGEMENT_ORDER, stock.signalType)
        ? JUDGEMENT_ORDER[stock.signalType]
        : null,
    },
    {
      key: "drawdown",
      label: "고점 대비 하락",
      type: "number",
      defaultDirection: "asc",
      modes: { asc: "drawdown-asc", desc: "drawdown-desc" },
      value: (stock) => numericMetricValue(stock.metrics?.drawdown),
    },
    {
      key: "price",
      label: "현재 주가",
      type: "number",
      defaultDirection: "desc",
      modes: { asc: "price-asc", desc: "price-desc" },
      value: (stock) => numericMetricValue(stock.priceMetric, stock.price),
    },
    {
      key: "forwardPE",
      label: "FWD PER",
      type: "number",
      defaultDirection: "asc",
      modes: { asc: "forward-pe-asc", desc: "forward-pe-desc" },
      value: (stock) => numericMetricValue(stock.forwardPeMetric, stock.forwardPE),
    },
    {
      key: "peGap",
      label: "3Y PER 괴리",
      type: "number",
      defaultDirection: "asc",
      modes: { asc: "pe-gap-asc", desc: "pe-gap-desc" },
      value: (stock) => numericMetricValue(stock.metrics?.peGap),
    },
    {
      key: "distance200",
      label: "200일선 이격",
      type: "number",
      defaultDirection: "asc",
      modes: { asc: "distance-200dma-asc", desc: "distance-200dma-desc" },
      value: (stock) => numericMetricValue(stock.metrics?.distance200),
    },
    {
      key: "epsGrowth",
      label: "EPS 성장",
      type: "number",
      defaultDirection: "desc",
      modes: { asc: "eps-asc", desc: "eps-desc" },
      value: (stock) => numericMetricValue(stock.metrics?.epsGrowth),
    },
    {
      key: "rsi",
      label: "RSI",
      type: "number",
      defaultDirection: "desc",
      modes: { asc: "rsi-asc", desc: "rsi-desc" },
      value: (stock) => numericMetricValue(stock.metrics?.rsi),
    },
    {
      key: "cashFlowQuality",
      label: "현금흐름 품질",
      type: "number",
      defaultDirection: "desc",
      modes: { asc: "cash-quality-asc", desc: "cash-quality-desc" },
      value: (stock) => numericMetricValue(stock.metrics?.cashFlowQuality),
    },
    {
      key: "debt",
      label: "부채비율",
      type: "number",
      defaultDirection: "asc",
      modes: { asc: "debt-asc", desc: "debt-desc" },
      value: (stock) => numericMetricValue(stock.metrics?.debt),
    },
    {
      key: "shareholderReturn",
      label: "3Y 주주환원",
      type: "number",
      defaultDirection: "desc",
      modes: { asc: "shareholder-return-asc", desc: "shareholder-return-desc" },
      value: (stock) => numericMetricValue(stock.metrics?.shareholderReturn),
    },
    {
      key: "sharesChange",
      label: "3Y 주식수",
      type: "number",
      defaultDirection: "desc",
      modes: { asc: "shares-change-asc", desc: "shares-change-desc" },
      value: (stock) => numericMetricValue(stock.metrics?.sharesChange),
    },
  ]);
  const TABLE_SORT_MODES = new Map();
  TABLE_SORT_DEFINITIONS.forEach((definition) => {
    ["asc", "desc"].forEach((direction) => {
      TABLE_SORT_MODES.set(definition.modes[direction], { definition, direction });
    });
  });

  const M7_TICKERS = new Set(["AAPL", "MSFT", "NVDA", "AMZN", "META", "TSLA", "GOOG", "GOOGL"]);
  const THEME_FILTERS = Object.freeze({
    "theme:m7": { label: "M7", subtitle: "Magnificent Seven · 7개 기업" },
    "theme:software": { label: "소프트웨어", subtitle: "GICS 하위 산업 Software" },
  });
  const SECTOR_ORDER = Object.freeze([
    "Information Technology", "Health Care", "Financials", "Consumer Discretionary",
    "Communication Services", "Industrials", "Consumer Staples", "Energy",
    "Utilities", "Real Estate", "Materials",
  ]);
  const SECTOR_LABELS = Object.freeze({
    "Information Technology": "정보기술(IT)",
    "Health Care": "헬스케어",
    Financials: "금융",
    "Consumer Discretionary": "임의소비재",
    "Communication Services": "커뮤니케이션 서비스",
    Industrials: "산업재",
    "Consumer Staples": "필수소비재",
    Energy: "에너지",
    Utilities: "유틸리티",
    "Real Estate": "부동산",
    Materials: "소재",
  });

  const DEMO_AS_OF = "2026-07-18";
  const DEMO_SOURCE = "화면 구조 확인용 샘플";
  const DEMO_MARKET = {
    score: -1,
    complete: true,
    metrics: {
      spy_drawdown_52_week_pct: demoMetric(-1.1, "percent", "demo-market"),
      vix_level: demoMetric(16.5, "index", "demo-market"),
      fear_greed_index: demoMetric(43, "index", "demo-market"),
    },
    components: [
      demoComponent("spy_drawdown", "SPY 52주 고점 대비 하락", "spy_drawdown_52_week_pct", -1.1, "percent", -1, "-1.10 > -5 → -1"),
      demoComponent("vix_level", "VIX", "vix_level", 16.5, "index", 0, "기본 구간 → +0"),
      demoComponent("fear_greed", "Fear & Greed", "fear_greed_index", 43, "index", 0, "기본 구간 → +0"),
    ],
  };

  const DEMO_DASHBOARD = {
    generated_at: "2026-07-19T13:30:00Z",
    as_of: DEMO_AS_OF,
    universe: {
      count: 503,
      provenance: { source: DEMO_SOURCE, as_of: DEMO_AS_OF, retrieved_at: "2026-07-19T13:30:00Z" },
      fallback_used: true,
      warnings: ["API 연결 전 인터페이스 확인용 데이터"],
    },
    count: 13,
    eligible_count: 12,
    market_overlay: DEMO_MARKET,
    signals: [
      demoStock("ISRG", "Intuitive Surgical", "헬스케어", 379.5, -37.2, 36.33, -49.7, -22.2, 32.7, 33, 0.0, [3, 2, 2, 2, 0, 1], 9),
      demoStock("ORCL", "Oracle", "소프트웨어", 127.96, -63.0, 15.9, -56.5, -34.3, 38.1, 26, 3.67, [3, 2, 2, 2, 0, 0], 8),
      demoStock("CRM", "Salesforce", "소프트웨어", 167.56, -38.8, 11.84, -75.7, -20.2, 81.5, 51, 1.22, [3, 2, 2, 2, 0, 0], 8),
      demoStock("NFLX", "Netflix", "커뮤니케이션", 73.53, -42.4, 20.64, -48.3, -22.4, 40.8, 39, 0.54, [3, 2, 2, 2, 0, 0], 8),
      demoStock("MSFT", "Microsoft", "소프트웨어", 384.93, -30.7, 22.91, -37.4, -12.9, 23.2, 48, 0.3, [3, 2, 1, 2, 0, 0], 7),
      demoStock("QCOM", "Qualcomm", "반도체", 178.1, -31.5, 16.55, -27.6, 5.4, 114.8, 40, 0.56, [3, 2, 0, 2, 0, 0], 6),
      demoStock("CVX", "Chevron", "에너지", 181.77, -15.3, 12.9, -23.8, 5.1, 112.5, 57, 0.25, [2, 2, 0, 2, 0, 1], 6),
      demoStock("MCD", "McDonald's", "경기방어 소비재", 297.41, -18.2, 23.4, -14.7, -8.8, 28.3, 44, 1.9, [2, 1, 1, 2, 0, 0], 5),
      demoStock("AMZN", "Amazon", "경기민감 소비재", 201.22, -11.7, 29.7, -12.4, -6.2, 31.1, 53, 0.52, [1, 1, 1, 2, 0, 0], 4),
      demoStock("AAPL", "Apple", "하드웨어", 211.18, -4.3, 26.8, -11.2, -5.1, 8.1, 58, 1.51, [0, 1, 1, 1, 0, 0], 2),
      demoStock("TSLA", "Tesla", "자동차", 308.12, -3.1, 61.2, 24.8, 14.3, -27.2, 72, 0.18, [0, -1, 0, -2, -1, 1], -4),
      demoStock("BA", "Boeing", "산업재", 194.7, -2.1, 0, 28.3, 18.4, -44.1, 76, 5.2, [0, -1, -1, -2, -1, -1], -7),
      demoInsufficientStock(),
    ],
    data_quality: {
      eligible_count: 12,
      total_count: 13,
      price_source: DEMO_SOURCE,
      fundamental_source: DEMO_SOURCE,
      note: "실제 API 응답이 아닙니다.",
    },
    refresh: { status: "idle", progress: 0 },
  };

  const DEMO_RULES = {
    version: "demo-2.0",
    minimum_company_metrics: 5,
    company_rules: [
      { key: "drawdown_52w", label: "52주 고점 대비 하락", metric_key: "drawdown_52_week_pct", unit: "percent", thresholds: [{ operator: "<=", value: -30, points: 3 }, { operator: "<=", value: -15, points: 2 }, { operator: "<=", value: -5, points: 1 }], default_points: 0, note: "낙폭은 가격 매력 신호일 뿐, 하락 원인의 건전성을 보장하지 않습니다." },
      { key: "pe_gap_3y", label: "3년 Forward PER 괴리", metric_key: "pe_gap_3y_pct", unit: "percent", thresholds: [{ operator: "<=", value: -20, points: 2 }, { operator: "<=", value: -10, points: 1 }], default_points: 0, note: "동일한 Forward PER 기준의 3년 중앙값과 비교합니다." },
      { key: "distance_200dma", label: "200일 이동평균 이격", metric_key: "distance_200dma_pct", unit: "percent", thresholds: [{ operator: "<=", value: -20, points: 2 }, { operator: "<=", value: -5, points: 1 }], default_points: 0 },
      { key: "eps_growth_yoy", label: "최근 연간 희석 EPS 성장률", metric_key: "eps_growth_yoy_pct", unit: "percent", thresholds: [{ operator: ">=", value: 20, points: 2 }, { operator: ">=", value: 5, points: 1 }, { operator: "<=", value: -20, points: -2 }, { operator: "<=", value: -5, points: -1 }], default_points: 0, note: "비교 가능한 최신 두 연간 기간을 사용합니다." },
      { key: "rsi_14", label: "RSI(14)", metric_key: "rsi_14", unit: "index", thresholds: [{ operator: ">=", value: 75, points: -2 }, { operator: ">=", value: 65, points: -1 }, { operator: "<=", value: 25, points: 1 }], default_points: 0 },
      { key: "shareholder_return_3y", label: "3년 평균 주주환원율 프록시", metric_key: "shareholder_return_3y_avg_pct", unit: "percent", thresholds: [{ operator: ">=", value: 6, points: 3 }, { operator: ">=", value: 4, points: 2 }, { operator: ">=", value: 2, points: 1 }], default_points: 0, note: "3년 평균 배당수익률과 양(+)의 평균 순주식수 감소율을 더합니다." },
      { key: "shares_dilution_3y", label: "최근 3년 희석주식수 증감", metric_key: "shares_change_3y_pct", unit: "percent", thresholds: [{ operator: ">=", value: 10, points: -3 }, { operator: ">=", value: 5, points: -2 }, { operator: ">=", value: 1, points: -1 }], default_points: 0, note: "주식수가 늘어난 기업만 별도 감점합니다." },
      { key: "debt_to_equity", label: "부채/자기자본", metric_key: "debt_to_equity", unit: "ratio", thresholds: [{ operator: ">=", value: 4, points: -2 }, { operator: ">=", value: 2, points: -1 }, { operator: "<=", value: 0.25, points: 1 }], default_points: 0, sector_exclusions: ["Financials", "REIT"], note: "금융사와 REIT는 업종 구조상 단순 기준에서 제외합니다." },
      { key: "cash_flow_quality", label: "3년 현금흐름 품질", metric_key: "cash_flow_quality_3y", unit: "ratio", thresholds: [{ operator: ">=", value: 1.2, points: 2 }, { operator: ">=", value: 0.9, points: 1 }, { operator: "<=", value: 0.5, points: -2 }, { operator: "<=", value: 0.75, points: -1 }], default_points: 0, sector_exclusions: ["Financials", "REIT", "Real Estate"], note: "3년 누적 영업현금흐름을 3년 누적 순이익으로 나눕니다." },
    ],
    market_rules: [
      { key: "spy_drawdown", label: "SPY 52주 고점 대비 하락", metric_key: "spy_drawdown_52_week_pct", unit: "percent", thresholds: [{ operator: "<=", value: -20, points: 1 }, { operator: ">", value: -5, points: -1 }], default_points: 0 },
      { key: "vix_level", label: "VIX", metric_key: "vix_level", unit: "index", thresholds: [{ operator: ">=", value: 35, points: 1 }, { operator: "<=", value: 12, points: -1 }], default_points: 0 },
      { key: "fear_greed", label: "Fear & Greed", metric_key: "fear_greed_index", unit: "index", thresholds: [{ operator: "<=", value: 25, points: 1 }, { operator: ">=", value: 75, points: -1 }], default_points: 0 },
    ],
  };

  const state = {
    dashboard: null,
    stocks: [],
    filteredStocks: [],
    rules: [],
    rulesMeta: null,
    rulesLoaded: false,
    rulesError: null,
    loading: true,
    isDemo: false,
    apiError: null,
    activeView: "board",
    selectedTicker: "",
    detailTab: "judgement",
    filters: { search: "", sector: "all", signal: "all", sort: "score-desc" },
    lastFocused: null,
    refreshing: false,
    refreshTimer: null,
    refreshPollTimer: null,
    refreshProgress: 0,
    deferredInstallPrompt: null,
    tableVisibleCount: 0,
    searchTimer: null,
      portfolio: {
        loaded: false,
        activeMarket: "us",
        holdings: [],
        cashManwon: 0,
        searchResults: [],
        searchSource: "",
        searchWarnings: [],
      searchError: null,
      searchSequence: 0,
      searchTimer: null,
      searching: false,
        composing: false,
        composeSequence: 0,
        autoComposeStarted: false,
        autoComposePending: false,
        autoComposeTimer: null,
        autoComposing: false,
        restoredFromStorage: false,
        lastComposeWasAuto: false,
        result: null,
        error: null,
        storageError: null,
      statusMessage: "",
    },
  };

  const el = {};

  document.addEventListener("DOMContentLoaded", init);

  async function init() {
    cacheElements();
    wireEvents();
    initializeRefreshMode();
    syncNativeShell();
    registerInstallExperience();
    const initialView = getViewFromHash();
    showView(initialView, { updateHash: false, focus: false, scroll: false });
    await loadDashboard();
    await loadRules();
  }

  function cacheElements() {
    const ids = [
      "as-of-header", "as-of-hero", "universe-label", "environment-banner", "retry-api-button",
      "global-alert", "global-alert-title", "global-alert-message", "dismiss-alert", "refresh-button",
      "refresh-track", "refresh-progress", "refresh-mode", "refresh-duration", "mobile-refresh-mode", "mobile-refresh-button",
      "install-app-button", "summary-grid", "recommended-tickers", "show-strong-buy",
      "market-verdict", "market-metrics", "market-total-score", "coverage-value", "price-source-value",
      "fundamental-source-value", "coverage-as-of", "filter-form", "stock-search", "sector-filter", "signal-filter",
      "sort-select", "reset-filters", "active-filter-row", "filtered-count", "table-shell",
      "table-loading", "stock-table-body", "table-pagination", "table-pagination-status", "table-load-more",
      "empty-state", "empty-title", "empty-message", "empty-reset",
      "theme-card-grid", "sector-card-grid",
      "detail-stock-select", "detail-page-content", "rules-grid", "rules-updated", "rules-error",
      "retry-rules", "stock-drawer", "drawer-backdrop", "drawer-close", "drawer-title", "drawer-body",
      "help-button", "help-dialog", "toast-region", "main-content",
      "portfolio-builder-form", "portfolio-market-tabs", "portfolio-search", "portfolio-search-results",
      "portfolio-holdings", "portfolio-cash-amount", "portfolio-cash-clear", "portfolio-compose", "portfolio-reset", "portfolio-status", "portfolio-total",
      "portfolio-group-summary", "portfolio-donut", "portfolio-treemap", "portfolio-unpriced", "portfolio-source-note",
    ];
    ids.forEach((id) => { el[toCamel(id)] = document.getElementById(id); });
    el.navItems = Array.from(document.querySelectorAll("[data-view]"));
    el.viewPanels = Array.from(document.querySelectorAll("[data-view-panel]"));
    el.viewJumps = Array.from(document.querySelectorAll("[data-view-jump]"));
    el.brandLinks = Array.from(document.querySelectorAll("[data-nav]"));
    el.columnSorts = Array.from(document.querySelectorAll("[data-sort]"));
    el.nativeSettingsButtons = Array.from(document.querySelectorAll("[data-native-settings]"));
  }

  function wireEvents() {
    el.navItems.forEach((button) => button.addEventListener("click", () => showView(button.dataset.view)));
    el.viewJumps.forEach((button) => button.addEventListener("click", () => showView(button.dataset.viewJump)));
    el.brandLinks.forEach((link) => link.addEventListener("click", (event) => {
      event.preventDefault();
      showView(link.dataset.nav || "board");
    }));
    window.addEventListener("hashchange", () => showView(getViewFromHash(), { updateHash: false, focus: false }));

    el.stockSearch.addEventListener("input", (event) => {
      state.filters.search = event.target.value.trim();
      window.clearTimeout(state.searchTimer);
      state.searchTimer = window.setTimeout(applyFilters, SEARCH_DEBOUNCE_MS);
    });
    el.filterForm.addEventListener("submit", (event) => {
      event.preventDefault();
      window.clearTimeout(state.searchTimer);
      state.filters.search = el.stockSearch.value.trim();
      applyFilters();
    });
    el.sectorFilter.addEventListener("change", (event) => {
      state.filters.sector = event.target.value;
      applyFilters();
    });
    el.signalFilter.addEventListener("change", (event) => {
      state.filters.signal = event.target.value;
      applyFilters();
    });
    el.sortSelect.addEventListener("change", (event) => {
      state.filters.sort = event.target.value;
      applyFilters();
    });
    el.columnSorts.forEach((button) => button.addEventListener("click", () => {
      const definition = TABLE_SORT_DEFINITIONS.find((item) => item.key === button.dataset.sortKey);
      if (!definition) return;
      const activeMode = TABLE_SORT_MODES.get(state.filters.sort);
      const direction = activeMode?.definition.key === definition.key
        ? (activeMode.direction === "asc" ? "desc" : "asc")
        : (button.dataset.defaultDirection || definition.defaultDirection);
      state.filters.sort = definition.modes[direction];
      applyFilters();
    }));
    el.resetFilters.addEventListener("click", resetFilters);
    el.emptyReset.addEventListener("click", resetFilters);
    el.showStrongBuy.addEventListener("click", () => {
      state.filters.signal = "strong-buy";
      el.signalFilter.value = "strong-buy";
      applyFilters();
      document.getElementById("screener")?.scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "start" });
    });

    el.stockTableBody.addEventListener("click", handleTableClick);
    el.stockTableBody.addEventListener("keydown", handleTableKeydown);
    el.tableLoadMore.addEventListener("click", () => {
      const firstNewIndex = state.tableVisibleCount;
      const restoreFocus = document.activeElement === el.tableLoadMore;
      state.tableVisibleCount += tableBatchSize();
      renderTable();
      if (restoreFocus && el.tablePagination.hidden) {
        el.stockTableBody.querySelectorAll("tr")[firstNewIndex]?.focus();
      }
    });
    el.recommendedTickers.addEventListener("click", (event) => {
      const button = event.target.closest("[data-open-stock]");
      if (button) openStockDrawer(button.dataset.openStock, button);
    });
    el.detailStockSelect.addEventListener("change", () => {
      const ticker = el.detailStockSelect.value;
      if (ticker) {
        state.detailTab = "judgement";
        loadDetailPage(ticker);
      }
    });
    el.detailPageContent.addEventListener("click", handleDetailPageClick);
    el.detailPageContent.addEventListener("keydown", handleDetailPageKeydown);
    [el.themeCardGrid, el.sectorCardGrid].forEach((grid) => grid?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-sector-group]");
      if (button) openSectorGroup(button.dataset.sectorGroup);
    }));

    el.drawerClose.addEventListener("click", closeStockDrawer);
    el.drawerBackdrop.addEventListener("click", closeStockDrawer);
    el.drawerBody.addEventListener("click", handleDrawerBodyClick);
    document.addEventListener("keydown", handleGlobalKeydown);

    el.helpButton.addEventListener("click", () => {
      if (typeof el.helpDialog.showModal === "function") el.helpDialog.showModal();
      else el.helpDialog.setAttribute("open", "");
    });
    el.refreshMode.addEventListener("change", () => setRefreshMode(el.refreshMode.value));
    el.mobileRefreshMode.addEventListener("change", () => setRefreshMode(el.mobileRefreshMode.value));
    el.refreshButton.addEventListener("click", refreshData);
    el.mobileRefreshButton.addEventListener("click", refreshData);
    el.nativeSettingsButtons.forEach((button) => button.addEventListener("click", openNativeSettings));
    window.addEventListener("androidquantready", syncNativeShell);
    window.addEventListener("focus", syncNativeShell);
    el.retryApiButton.addEventListener("click", async () => {
      await loadDashboard();
      await loadRules({ force: true });
    });
    el.dismissAlert.addEventListener("click", () => { el.globalAlert.hidden = true; });
    el.retryRules.addEventListener("click", () => loadRules({ force: true }));

    el.activeFilterRow.addEventListener("click", (event) => {
      const button = event.target.closest("[data-clear-filter]");
      if (!button) return;
      clearFilter(button.dataset.clearFilter);
    });
    wirePortfolioEvents();
  }

  async function loadDashboard({ silent = false, preserveExisting = false } = {}) {
    if (!silent) setDashboardLoading(true);
    try {
      const payload = await fetchJSON(API.dashboard, {}, 18000);
      const dashboard = normalizeDashboard(payload);
      state.dashboard = dashboard;
      state.stocks = dashboard.stocks;
      state.isDemo = false;
      state.apiError = null;
      el.environmentBanner.hidden = true;
      el.globalAlert.hidden = true;
      renderDashboard();
      syncRefreshFromServer(dashboard.refresh);
      return true;
    } catch (error) {
      if (preserveExisting && state.dashboard) {
        showGlobalAlert("최신 데이터 확인에 실패했습니다.", cleanError(error));
        return false;
      }
      state.dashboard = normalizeDashboard(DEMO_DASHBOARD);
      state.stocks = state.dashboard.stocks;
      state.isDemo = true;
      state.apiError = error;
      el.environmentBanner.hidden = false;
      document.querySelector(".status-dot")?.classList.add("is-demo");
      showGlobalAlert("API에 연결할 수 없어 데모 데이터를 표시합니다.", "아래 값은 화면 구조 확인용이며 실제 시세가 아닙니다.");
      renderDashboard();
      return false;
    } finally {
      state.loading = false;
      setDashboardLoading(false);
    }
  }

  async function loadRules({ force = false } = {}) {
    if (state.rulesLoaded && !force) return;
    state.rulesError = null;
    if (force) renderRulesLoading();
    try {
      if (state.isDemo) throw new Error("demo-mode");
      const payload = await fetchJSON(API.rules, {}, 12000);
      const normalized = normalizeRules(payload);
      state.rules = normalized.rules;
      state.rulesMeta = normalized.meta;
      state.rulesLoaded = true;
      renderRules();
    } catch (error) {
      if (state.isDemo) {
        const normalized = normalizeRules(DEMO_RULES);
        state.rules = normalized.rules;
        state.rulesMeta = { ...normalized.meta, demo: true };
        state.rulesLoaded = true;
        renderRules();
      } else {
        state.rules = [];
        state.rulesError = error;
        state.rulesLoaded = false;
        el.rulesGrid.innerHTML = "";
        el.rulesError.hidden = false;
        el.rulesUpdated.textContent = "규칙 API 연결 실패";
      }
    }
  }

  function wirePortfolioEvents() {
    if (el.portfolioBuilderForm) {
      el.portfolioBuilderForm.addEventListener("submit", (event) => {
        event.preventDefault();
        composePortfolio();
      });
    }
    el.portfolioCompose?.addEventListener("click", (event) => {
      event.preventDefault();
      composePortfolio();
    });
    el.portfolioReset?.addEventListener("click", (event) => {
      event.preventDefault();
      resetPortfolio();
    });
    el.portfolioMarketTabs?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-portfolio-market]");
      if (!button || !el.portfolioMarketTabs.contains(button)) return;
      setPortfolioMarket(button.dataset.portfolioMarket);
    });
    el.portfolioMarketTabs?.addEventListener("keydown", handlePortfolioMarketKeydown);
    el.portfolioSearch?.addEventListener("input", () => schedulePortfolioSearch());
    el.portfolioSearchResults?.addEventListener("click", handlePortfolioSearchResultClick);
    el.portfolioSearchResults?.addEventListener("keydown", handlePortfolioSearchResultKeydown);
    el.portfolioHoldings?.addEventListener("click", handlePortfolioHoldingClick);
    el.portfolioHoldings?.addEventListener("input", handlePortfolioQuantityEvent);
    el.portfolioHoldings?.addEventListener("change", handlePortfolioQuantityEvent);
    el.portfolioCashAmount?.addEventListener("input", handlePortfolioCashEvent);
    el.portfolioCashAmount?.addEventListener("change", handlePortfolioCashEvent);
    el.portfolioCashClear?.addEventListener("click", handlePortfolioCashClear);
    const compactCharts = window.matchMedia("(max-width: 720px)");
    const rerenderCharts = () => {
      if (!state.portfolio.result) return;
      renderPortfolioDonut(state.portfolio.result);
      renderPortfolioTreemap(state.portfolio.result);
    };
    if (typeof compactCharts.addEventListener === "function") compactCharts.addEventListener("change", rerenderCharts);
    else if (typeof compactCharts.addListener === "function") compactCharts.addListener(rerenderCharts);
  }

  function handlePortfolioMarketKeydown(event) {
    const button = event.target.closest("[data-portfolio-market]");
    if (!button || !el.portfolioMarketTabs?.contains(button)) return;
    const buttons = Array.from(el.portfolioMarketTabs.querySelectorAll("[data-portfolio-market]"));
    const index = buttons.indexOf(button);
    if (index < 0 || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    let nextIndex = index;
    if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = buttons.length - 1;
    else if (event.key === "ArrowLeft") nextIndex = (index - 1 + buttons.length) % buttons.length;
    else if (event.key === "ArrowRight") nextIndex = (index + 1) % buttons.length;
    buttons[nextIndex]?.focus();
  }

  function handlePortfolioSearchResultClick(event) {
    const button = event.target.closest("[data-portfolio-add-index]");
    if (!button || !el.portfolioSearchResults?.contains(button)) return;
    addPortfolioHolding(Number(button.dataset.portfolioAddIndex));
  }

  function handlePortfolioSearchResultKeydown(event) {
    const button = event.target.closest("[data-portfolio-add-index]");
    if (!button || !el.portfolioSearchResults?.contains(button)) return;
    const buttons = Array.from(el.portfolioSearchResults.querySelectorAll("button[data-portfolio-add-index]:not(:disabled)"));
    const index = buttons.indexOf(button);
    if (index < 0 || !["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    let nextIndex = index;
    if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = buttons.length - 1;
    else if (event.key === "ArrowDown") nextIndex = (index + 1) % buttons.length;
    else if (event.key === "ArrowUp") nextIndex = (index - 1 + buttons.length) % buttons.length;
    buttons[nextIndex]?.focus();
  }

  function handlePortfolioHoldingClick(event) {
    const button = event.target.closest("[data-portfolio-remove-index]");
    if (!button || !el.portfolioHoldings?.contains(button)) return;
    removePortfolioHolding(Number(button.dataset.portfolioRemoveIndex));
  }

  function handlePortfolioQuantityEvent(event) {
    const input = event.target.closest("[data-portfolio-quantity-index]");
    if (!input || !el.portfolioHoldings?.contains(input)) return;
    const index = Number(input.dataset.portfolioQuantityIndex);
    const holding = state.portfolio.holdings[index];
    if (!holding) return;
    const validation = validatePortfolioQuantity(input.value, holding.market);
    setPortfolioQuantityValidity(input, index, validation);
    if (!validation.valid) {
      invalidatePortfolioResult(validation.message);
      return;
    }
    holding.quantity = validation.value;
    if (event.type === "change") input.value = portfolioQuantityInputValue(holding.quantity, holding.market);
    persistPortfolioHoldings();
    invalidatePortfolioResult("보유 수량을 저장했습니다. 다시 계산하세요.");
  }

  function handlePortfolioCashEvent(event) {
    const input = event.target;
    const validation = validatePortfolioCash(input.value);
    setPortfolioCashValidity(input, validation);
    if (!validation.valid) {
      invalidatePortfolioResult(validation.message);
      return;
    }
    state.portfolio.cashManwon = validation.value;
    if (event.type === "change") input.value = portfolioCashInputValue(validation.value, validation.blank);
    persistPortfolioState();
    invalidatePortfolioResult("현금 금액을 저장했습니다. 다시 계산하세요.");
    renderPortfolioCash();
  }

  function handlePortfolioCashClear(event) {
    event.preventDefault();
    state.portfolio.cashManwon = 0;
    if (el.portfolioCashAmount) {
      el.portfolioCashAmount.value = "";
      setPortfolioCashValidity(el.portfolioCashAmount, { valid: true, value: 0, blank: true, message: "" });
    }
    persistPortfolioState();
    invalidatePortfolioResult("현금 금액을 저장하고 지웠습니다. 다시 계산하세요.");
    renderPortfolioCash();
    el.portfolioCashAmount?.focus();
  }

  function loadPortfolioState({ autoCompose = true } = {}) {
    const portfolio = state.portfolio;
    if (portfolio.loaded) {
      renderPortfolioBuilder();
      return;
    }
    portfolio.loaded = true;
    const restored = restorePortfolioState();
    portfolio.holdings = restored.holdings;
    portfolio.cashManwon = restored.cashManwon;
    portfolio.restoredFromStorage = restored.hasSaved;
    portfolio.statusMessage = restored.storageIssue
      ? "저장된 포트폴리오를 읽지 못했습니다. 새 포트폴리오로 시작합니다."
      : restored.hasSaved
        ? "저장된 포트폴리오를 불러왔습니다. 현재 시세로 다시 계산합니다…"
        : "";
    if (restored.migrated) persistPortfolioState();
    renderPortfolioBuilder();
    setPortfolioMarket(portfolio.activeMarket, { search: false });
    if (autoCompose && restored.hasSaved && !portfolio.autoComposeStarted && !portfolio.autoComposePending) {
      portfolio.autoComposePending = false;
      portfolio.autoComposeStarted = true;
      void composePortfolio({ auto: true });
    }
  }

  function setPortfolioMarket(market, { search = true } = {}) {
    const portfolio = state.portfolio;
    if (!Object.prototype.hasOwnProperty.call(PORTFOLIO_MARKETS, market)) return;
    portfolio.activeMarket = market;
    window.clearTimeout(portfolio.searchTimer);
    portfolio.searchSequence += 1;
    portfolio.searchResults = [];
    portfolio.searchSource = "";
    portfolio.searchWarnings = [];
    portfolio.searchError = null;
    portfolio.searching = false;
    if (el.portfolioSearch) el.portfolioSearch.value = "";
    updatePortfolioMarketTabs();
    renderPortfolioSearchResults();
    if (search && market === "gold") searchPortfolio();
  }

  function updatePortfolioMarketTabs() {
    const activeMarket = state.portfolio.activeMarket;
    el.portfolioMarketTabs?.querySelectorAll("[data-portfolio-market]").forEach((button) => {
      const active = button.dataset.portfolioMarket === activeMarket;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
      if (button.getAttribute("role") === "tab") button.setAttribute("aria-selected", String(active));
    });
  }

  function schedulePortfolioSearch({ immediate = false } = {}) {
    const portfolio = state.portfolio;
    window.clearTimeout(portfolio.searchTimer);
    const query = String(el.portfolioSearch?.value || "").trim();
    if (!query && portfolio.activeMarket !== "gold") {
      portfolio.searchSequence += 1;
      portfolio.searchResults = [];
      portfolio.searchSource = "";
      portfolio.searchWarnings = [];
      portfolio.searchError = null;
      portfolio.searching = false;
      renderPortfolioSearchResults();
      return;
    }
    portfolio.searchSequence += 1;
    portfolio.searchResults = [];
    portfolio.searchSource = "";
    portfolio.searchWarnings = [];
    portfolio.searchError = null;
    portfolio.searching = false;
    renderPortfolioSearchResults();
    if (immediate) {
      searchPortfolio();
      return;
    }
    portfolio.searchTimer = window.setTimeout(() => searchPortfolio(), PORTFOLIO_SEARCH_DEBOUNCE_MS);
  }

  async function searchPortfolio() {
    const portfolio = state.portfolio;
    const market = portfolio.activeMarket;
    const query = String(el.portfolioSearch?.value || "").trim();
    if (!query && market !== "gold") {
      schedulePortfolioSearch();
      return;
    }
    const sequence = ++portfolio.searchSequence;
    portfolio.searching = true;
    portfolio.searchError = null;
    renderPortfolioSearchResults();
    try {
      const payload = await fetchJSON(API.portfolioSearch(market, query, 12), {}, 12000);
      if (sequence !== portfolio.searchSequence || market !== portfolio.activeMarket) return;
      const normalized = normalizePortfolioSearch(payload, market);
      portfolio.searchResults = normalized.results;
      portfolio.searchSource = normalized.source;
      portfolio.searchWarnings = normalized.warnings;
      portfolio.searching = false;
      renderPortfolioSearchResults();
    } catch (error) {
      if (sequence !== portfolio.searchSequence || market !== portfolio.activeMarket) return;
      portfolio.searching = false;
      portfolio.searchResults = [];
      portfolio.searchSource = "";
      portfolio.searchWarnings = [];
      portfolio.searchError = error;
      renderPortfolioSearchResults();
    }
  }

  function normalizePortfolioSearch(payload, market) {
    const root = unwrapObject(payload, ["portfolio", "search", "result"]);
    const rawResults = firstArray(root.results, root.items, root.matches, payload?.results);
    const results = [];
    const seen = new Set();
    rawResults.forEach((raw) => {
      const result = normalizePortfolioSearchResult(raw, market);
      const key = portfolioHoldingKey(result);
      if (!result.symbol || seen.has(key)) return;
      seen.add(key);
      results.push(result);
    });
    return {
      results: results.slice(0, 12),
      source: String(firstDefined(root.source, payload?.source, "") || ""),
      warnings: firstArray(root.warnings, payload?.warnings).map((warning) => String(warning)),
    };
  }

  function normalizePortfolioSearchResult(raw, fallbackMarket) {
    const market = normalizePortfolioMarket(firstDefined(raw?.market, fallbackMarket));
    const symbol = String(firstDefined(raw?.symbol, raw?.ticker, raw?.code, raw?.id, "") || "").trim();
    const name = String(firstDefined(raw?.name, raw?.company_name, raw?.companyName, symbol) || symbol).trim();
    return {
      id: String(firstDefined(raw?.id, `${market}:${symbol}`) || `${market}:${symbol}`),
      market,
      symbol,
      name,
      exchange: String(firstDefined(raw?.exchange, raw?.market_name, raw?.marketName, "") || ""),
      assetType: String(firstDefined(raw?.asset_type, raw?.assetType, "") || ""),
      currency: String(firstDefined(raw?.currency, "") || ""),
      quantityUnit: String(firstDefined(raw?.quantity_unit, raw?.quantityUnit, raw?.unit, portfolioMarketMeta(market).unit) || portfolioMarketMeta(market).unit),
    };
  }

  function addPortfolioHolding(resultIndex) {
    const result = state.portfolio.searchResults[resultIndex];
    if (!result || portfolioHasHolding(result)) return;
    const meta = portfolioMarketMeta(result.market);
    state.portfolio.holdings.push({
      id: result.id,
      market: result.market,
      symbol: result.symbol,
      name: result.name,
      quantity: meta.step < 1 ? 1 : meta.min,
      quantityUnit: result.quantityUnit || meta.unit,
    });
    persistPortfolioState();
    invalidatePortfolioResult("보유 자산을 추가하고 저장했습니다. 구성 버튼으로 계산하세요.");
    renderPortfolioHoldings();
    renderPortfolioSearchResults();
    const input = el.portfolioHoldings?.querySelector(`[data-portfolio-quantity-index="${state.portfolio.holdings.length - 1}"]`);
    input?.focus();
    input?.select();
  }

  function removePortfolioHolding(index) {
    if (!Number.isInteger(index) || !state.portfolio.holdings[index]) return;
    state.portfolio.holdings.splice(index, 1);
    persistPortfolioState();
    invalidatePortfolioResult("보유 자산을 삭제하고 저장했습니다. 구성 버튼으로 계산하세요.");
    renderPortfolioHoldings();
    renderPortfolioSearchResults();
  }

  function portfolioHasHolding(candidate) {
    const key = portfolioHoldingKey(candidate);
    return state.portfolio.holdings.some((holding) => portfolioHoldingKey(holding) === key);
  }

  function portfolioHoldingKey(holding) {
    const groupKey = normalizePortfolioGroupKey(firstDefined(holding?.groupKey, holding?.group_key, holding?.market));
    const market = groupKey === "cash" ? "cash" : Object.prototype.hasOwnProperty.call(PORTFOLIO_MARKETS, groupKey)
      ? groupKey
      : normalizePortfolioMarket(holding?.market);
    return `${market}:${String(holding?.symbol || "").trim().toLocaleUpperCase("en-US")}`;
  }

  function normalizePortfolioMarket(market) {
    const value = String(market || "").trim().toLocaleLowerCase("en-US");
    return Object.prototype.hasOwnProperty.call(PORTFOLIO_MARKETS, value) ? value : "us";
  }

  function portfolioMarketMeta(market) {
    return PORTFOLIO_MARKETS[normalizePortfolioMarket(market)];
  }

  function restorePortfolioState() {
    const empty = { holdings: [], cashManwon: 0, hasSaved: false, migrated: false, storageIssue: false };
    try {
      const raw = window.localStorage.getItem(PORTFOLIO_STORAGE_KEY);
      if (!raw) return empty;
      const stored = JSON.parse(raw);
      if (!stored || ![1, PORTFOLIO_STORAGE_VERSION].includes(stored.version) || !Array.isArray(stored.holdings)) {
        return { ...empty, storageIssue: true };
      }
      const seen = new Set();
      const holdings = stored.holdings.map((item) => normalizeStoredPortfolioHolding(item)).filter((holding) => {
        if (!holding) return false;
        const key = portfolioHoldingKey(holding);
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      });
      const cashValidation = stored.version === 1
        ? { valid: true, value: 0, blank: true, message: "" }
        : validateStoredPortfolioCash(stored.cashManwon);
      const cashManwon = cashValidation.valid ? cashValidation.value : 0;
      return {
        holdings,
        cashManwon,
        hasSaved: holdings.length > 0 || cashManwon > 0,
        migrated: true,
        storageIssue: !cashValidation.valid,
      };
    } catch (error) {
      state.portfolio.storageError = error;
      return { ...empty, storageIssue: true };
    }
  }

  function validateStoredPortfolioCash(rawValue) {
    if (rawValue === undefined || rawValue === null || rawValue === "") return { valid: true, value: 0, blank: true, message: "" };
    if (typeof rawValue !== "number") return { valid: false, value: 0, blank: false, message: "저장된 현금 금액이 유효하지 않습니다." };
    return validatePortfolioCash(rawValue);
  }

  function normalizeStoredPortfolioHolding(raw) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
    const rawMarket = String(raw.market || "").trim().toLocaleLowerCase("en-US");
    if (!Object.prototype.hasOwnProperty.call(PORTFOLIO_MARKETS, rawMarket)) return null;
    const market = rawMarket;
    const symbol = String(raw.symbol || "").trim();
    if (!symbol) return null;
    const validation = validatePortfolioQuantity(raw.quantity, market);
    if (!validation.valid) return null;
    return {
      market,
      symbol,
      name: String(raw.name || symbol).trim() || symbol,
      quantity: validation.value,
      quantityUnit: portfolioMarketMeta(market).unit,
      id: `${market}:${symbol}`,
    };
  }

  function persistPortfolioHoldings() {
    persistPortfolioState();
  }

  function persistPortfolioState() {
    const holdings = state.portfolio.holdings.map((holding) => ({
      market: normalizePortfolioMarket(holding.market),
      symbol: String(holding.symbol || "").trim(),
      name: String(holding.name || holding.symbol || "").trim(),
      quantity: holding.quantity,
    })).filter((holding) => holding.symbol && Number.isFinite(holding.quantity) && holding.quantity > 0);
    const cashManwon = Number.isFinite(state.portfolio.cashManwon)
      ? clamp(state.portfolio.cashManwon, 0, PORTFOLIO_MAX_CASH_MANWON)
      : 0;
    try {
      window.localStorage.setItem(PORTFOLIO_STORAGE_KEY, JSON.stringify({ version: PORTFOLIO_STORAGE_VERSION, holdings, cashManwon }));
      state.portfolio.storageError = null;
    } catch (error) {
      state.portfolio.storageError = error;
    }
  }

  function validatePortfolioQuantity(rawValue, market) {
    const meta = portfolioMarketMeta(market);
    const value = numberOrNull(rawValue);
    if (value === null || !Number.isFinite(value) || value < meta.min) {
      return { valid: false, value: null, message: `수량은 ${meta.min} ${meta.unit} 이상이어야 합니다.` };
    }
    const units = value / meta.step;
    if (Math.abs(units - Math.round(units)) > 1e-7) {
      return { valid: false, value: null, message: `${meta.label} 수량 단위(${meta.step})에 맞춰 입력하세요.` };
    }
    const normalized = Number((Math.round(units) * meta.step).toFixed(meta.step < 1 ? 4 : 0));
    return { valid: true, value: normalized, message: "" };
  }

  function validatePortfolioCash(rawValue) {
    const text = String(rawValue ?? "").trim();
    if (!text) return { valid: true, value: 0, blank: true, message: "" };
    const value = Number(text);
    if (!Number.isFinite(value) || value < 0) {
      return { valid: false, value: null, blank: false, message: "현금은 0 이상인 유한한 숫자로 입력하세요." };
    }
    if (value > PORTFOLIO_MAX_CASH_MANWON) {
      return { valid: false, value: null, blank: false, message: `현금은 ${formatNumber(PORTFOLIO_MAX_CASH_MANWON, 0)} 만원 이하로 입력하세요.` };
    }
    return { valid: true, value, blank: false, message: "" };
  }

  function setPortfolioCashValidity(input, validation) {
    input.setCustomValidity(validation.valid ? "" : validation.message);
    input.setAttribute("aria-invalid", String(!validation.valid));
    input.classList.toggle("is-invalid", !validation.valid);
    if (!validation.valid) state.portfolio.statusMessage = validation.message;
  }

  function portfolioCashInputValue(value, blank = false) {
    const number = numberOrNull(value);
    if (number === null) return "";
    if (number === 0) return blank ? "" : "0";
    return String(number);
  }

  function renderPortfolioCash() {
    const input = el.portfolioCashAmount;
    if (!input) return;
    input.min = "0";
    input.max = String(PORTFOLIO_MAX_CASH_MANWON);
    input.step = "0.01";
    if (document.activeElement !== input || input.value === "") {
      input.value = portfolioCashInputValue(state.portfolio.cashManwon, state.portfolio.cashManwon === 0);
    }
    input.setAttribute("aria-invalid", "false");
    input.setCustomValidity("");
    if (el.portfolioCashClear) el.portfolioCashClear.toggleAttribute("disabled", !(state.portfolio.cashManwon > 0));
  }

  function setPortfolioQuantityValidity(input, index, validation) {
    input.setCustomValidity(validation.valid ? "" : validation.message);
    input.setAttribute("aria-invalid", String(!validation.valid));
    input.classList.toggle("is-invalid", !validation.valid);
    const message = el.portfolioHoldings?.querySelector(`[data-portfolio-row-error-index="${index}"]`);
    if (message) {
      message.textContent = validation.message;
      message.hidden = validation.valid;
    }
    if (!validation.valid) {
      state.portfolio.statusMessage = validation.message;
      renderPortfolioStatus();
    }
  }

  function portfolioQuantityInputValue(quantity, market) {
    return portfolioMarketMeta(market).step < 1 ? Number(quantity).toFixed(4) : String(Math.round(quantity));
  }

  function invalidatePortfolioResult(message = "") {
    const portfolio = state.portfolio;
    portfolio.composeSequence += 1;
    portfolio.composing = false;
    portfolio.autoComposing = false;
    portfolio.lastComposeWasAuto = false;
    portfolio.result = null;
    portfolio.error = null;
    portfolio.statusMessage = message;
    renderPortfolioResult();
    renderPortfolioStatus();
  }

  function renderPortfolioBuilder() {
    updatePortfolioMarketTabs();
    renderPortfolioHoldings();
    renderPortfolioCash();
    renderPortfolioSearchResults();
    renderPortfolioResult();
    renderPortfolioStatus();
  }

  function renderPortfolioSearchResults() {
    const container = el.portfolioSearchResults;
    if (!container) return;
    const portfolio = state.portfolio;
    container.setAttribute("aria-busy", String(portfolio.searching));
    container.classList.toggle("is-loading", portfolio.searching);
    container.classList.toggle("is-error", Boolean(portfolio.searchError));
    let markup = "";
    if (portfolio.searching) {
      markup = '<div class="portfolio-search-message" role="status">검색 중입니다…</div>';
    } else if (portfolio.searchError) {
      markup = `<div class="portfolio-search-message is-error" role="alert">검색에 실패했습니다. ${escapeHTML(cleanError(portfolio.searchError))}</div>`;
    } else if (!portfolio.searchResults.length) {
      const query = String(el.portfolioSearch?.value || "").trim();
      markup = query || portfolio.activeMarket === "gold"
        ? '<div class="portfolio-search-message">일치하는 자산이 없습니다.</div>'
        : '<div class="portfolio-search-message">티커·종목명을 입력하면 검색 결과가 표시됩니다.</div>';
    } else {
      markup = portfolio.searchResults.map((result, index) => {
        const alreadyAdded = portfolioHasHolding(result);
        const details = [result.exchange, result.assetType, result.currency].filter(Boolean).join(" · ");
        return `
          <button class="portfolio-search-result" type="button" role="option" aria-label="${escapeAttr(`${result.symbol} ${result.name} ${alreadyAdded ? "이미 추가됨" : "추가"}`)}" aria-selected="${alreadyAdded ? "true" : "false"}" aria-disabled="${alreadyAdded ? "true" : "false"}" data-portfolio-add-index="${index}"${alreadyAdded ? " disabled" : ""}>
            <div class="portfolio-search-result-copy">
              <strong>${escapeHTML(result.symbol)}</strong>
              <span>${escapeHTML(result.name)}</span>
              ${details ? `<small>${escapeHTML(details)}</small>` : ""}
            </div>
            <span class="portfolio-add-button" aria-hidden="true">${alreadyAdded ? "추가됨" : "추가"}</span>
          </button>`;
      }).join("");
    }
    if (portfolio.searchWarnings.length) {
      markup += `<p class="portfolio-search-warning">${portfolio.searchWarnings.map((warning) => escapeHTML(warning)).join(" · ")}</p>`;
    }
    if (portfolio.searchSource) {
      markup += `<p class="portfolio-search-source">검색 출처: ${escapeHTML(portfolio.searchSource)}</p>`;
    }
    container.innerHTML = markup;
  }

  function renderPortfolioHoldings() {
    const container = el.portfolioHoldings;
    if (!container) return;
    const holdings = state.portfolio.holdings;
    if (!holdings.length) {
      container.innerHTML = '<div class="portfolio-empty-state" data-portfolio-empty><span class="portfolio-empty-icon" aria-hidden="true">＋</span><strong>아직 담은 자산이 없습니다</strong><p>위 검색 결과에서 종목을 추가하거나 아래 현금 금액만으로도 구성할 수 있습니다.</p></div>';
      return;
    }
    container.innerHTML = holdings.map((holding, index) => {
      const meta = portfolioMarketMeta(holding.market);
      const unit = holding.quantityUnit || meta.unit;
      return `
        <article class="portfolio-holding-row" data-portfolio-holding="${index}" data-portfolio-holding-index="${index}">
          <div class="portfolio-holding-copy">
            <strong data-portfolio-symbol="${escapeAttr(holding.symbol)}">${escapeHTML(holding.symbol)}</strong>
            <small data-portfolio-name="${escapeAttr(holding.name)}">${escapeHTML(holding.name)} · ${escapeHTML(meta.label)}</small>
            <small class="portfolio-row-error" id="portfolio-row-error-${index}" data-portfolio-row-error-index="${index}" hidden></small>
          </div>
          <label class="portfolio-quantity-field">
            <span>수량 <small>${escapeHTML(unit)}</small></span>
            <input type="number" name="portfolio-quantity-${index}" data-portfolio-quantity="${index}" data-portfolio-quantity-index="${index}" value="${escapeAttr(portfolioQuantityInputValue(holding.quantity, holding.market))}" min="${escapeAttr(String(meta.min))}" step="${escapeAttr(String(meta.step))}" inputmode="decimal" required aria-describedby="portfolio-row-error-${index}" aria-label="${escapeAttr(`${holding.symbol} 수량`)}">
          </label>
          <button class="portfolio-remove-button" type="button" data-portfolio-remove="${index}" data-portfolio-remove-index="${index}" aria-label="${escapeAttr(`${holding.symbol} 삭제`)}">삭제</button>
        </article>`;
    }).join("");
  }

  async function composePortfolio({ auto = false } = {}) {
    loadPortfolioState({ autoCompose: false });
    const portfolio = state.portfolio;
    if (!auto) {
      portfolio.autoComposeStarted = true;
      portfolio.autoComposePending = false;
      window.clearTimeout(portfolio.autoComposeTimer);
      portfolio.autoComposeTimer = null;
    }
    const validation = validatePortfolioHoldings();
    if (!validation.valid) {
      portfolio.statusMessage = validation.message;
      renderPortfolioStatus();
      validation.input?.focus();
      return false;
    }
    const cashInputValidation = validatePortfolioCash(el.portfolioCashAmount ? el.portfolioCashAmount.value : portfolio.cashManwon);
    if (!cashInputValidation.valid) {
      portfolio.statusMessage = cashInputValidation.message;
      if (el.portfolioCashAmount) setPortfolioCashValidity(el.portfolioCashAmount, cashInputValidation);
      renderPortfolioStatus();
      el.portfolioCashAmount?.focus();
      return false;
    }
    portfolio.cashManwon = cashInputValidation.value;
    persistPortfolioState();
    renderPortfolioCash();
    if (!portfolio.holdings.length && !(portfolio.cashManwon > 0)) {
      invalidatePortfolioResult("보유 자산을 추가하거나 0보다 큰 현금을 입력하세요.");
      return false;
    }
    const sequence = ++portfolio.composeSequence;
    portfolio.composing = true;
    portfolio.autoComposing = auto;
    portfolio.lastComposeWasAuto = false;
    portfolio.error = null;
    portfolio.result = null;
    portfolio.statusMessage = auto
      ? "저장된 포트폴리오를 복원했습니다. 현재 시세로 다시 계산하는 중입니다…"
      : "";
    renderPortfolioResult();
    renderPortfolioStatus();
    const holdings = portfolio.holdings.map((holding) => ({
      market: holding.market,
      symbol: holding.symbol,
      name: holding.name,
      quantity: holding.quantity,
    }));
    try {
      const payload = await fetchJSON(API.portfolioCompose, {
        method: "POST",
        body: JSON.stringify({ holdings, cash_krw: Math.round(portfolio.cashManwon * 10000) }),
      }, 20000);
      if (sequence !== portfolio.composeSequence) return false;
      portfolio.result = normalizePortfolioCompose(payload);
      portfolio.error = null;
      portfolio.lastComposeWasAuto = auto;
      portfolio.statusMessage = auto
        ? "저장된 포트폴리오를 불러와 현재 시세로 다시 계산했습니다."
        : "";
      renderPortfolioResult();
      renderPortfolioStatus();
      return true;
    } catch (error) {
      if (sequence !== portfolio.composeSequence) return false;
      portfolio.result = null;
      portfolio.error = error;
      portfolio.lastComposeWasAuto = false;
      renderPortfolioResult();
      renderPortfolioStatus();
      return false;
    } finally {
      if (sequence === portfolio.composeSequence) {
        portfolio.composing = false;
        portfolio.autoComposing = false;
        renderPortfolioStatus();
      }
    }
  }

  function validatePortfolioHoldings() {
    for (let index = 0; index < state.portfolio.holdings.length; index += 1) {
      const holding = state.portfolio.holdings[index];
      const input = el.portfolioHoldings?.querySelector(`[data-portfolio-quantity-index="${index}"]`);
      const validation = validatePortfolioQuantity(input ? input.value : holding.quantity, holding.market);
      if (input) setPortfolioQuantityValidity(input, index, validation);
      if (!validation.valid) return { valid: false, message: validation.message, input };
      holding.quantity = validation.value;
    }
    persistPortfolioState();
    return { valid: true, message: "", input: null };
  }

  function resetPortfolio() {
    const portfolio = state.portfolio;
    window.clearTimeout(portfolio.searchTimer);
    window.clearTimeout(portfolio.autoComposeTimer);
    portfolio.searchSequence += 1;
    portfolio.composeSequence += 1;
    portfolio.searchTimer = null;
    portfolio.autoComposeTimer = null;
    portfolio.autoComposePending = false;
    portfolio.autoComposeStarted = true;
    portfolio.searchResults = [];
    portfolio.searchSource = "";
    portfolio.searchWarnings = [];
    portfolio.searchError = null;
    portfolio.searching = false;
    portfolio.composing = false;
    portfolio.result = null;
    portfolio.error = null;
    portfolio.holdings = [];
    portfolio.cashManwon = 0;
    portfolio.restoredFromStorage = false;
    portfolio.lastComposeWasAuto = false;
    portfolio.statusMessage = "포트폴리오를 초기화했습니다.";
    try {
      window.localStorage.removeItem(PORTFOLIO_STORAGE_KEY);
      portfolio.storageError = null;
    } catch (error) {
      portfolio.storageError = error;
    }
    if (el.portfolioSearch) el.portfolioSearch.value = "";
    renderPortfolioBuilder();
    el.portfolioSearch?.focus();
  }

  function normalizePortfolioCompose(payload) {
    const root = unwrapObject(payload, ["portfolio", "result"]);
    const rawHoldings = firstArray(root.holdings, root.items, payload?.holdings);
    let holdings = rawHoldings.map((raw, index) => normalizePortfolioHolding(raw, index)).filter((holding) => holding.symbol);
    const rawGroups = firstArray(root.groups, payload?.groups);
    const groupRecords = new Map();
    PORTFOLIO_GROUP_ORDER.forEach((key) => {
      const meta = PORTFOLIO_GROUP_META[key];
      groupRecords.set(key, {
        key,
        label: meta.label,
        color: meta.color,
        valueKrw: null,
        valueManwon: null,
        weightPct: null,
        holdings: [],
      });
    });
    rawGroups.forEach((raw, index) => {
      const key = normalizePortfolioGroupKey(firstDefined(raw?.key, raw?.group_key, raw?.groupKey, raw?.market, raw?.label));
      const current = groupRecords.get(key) || {
        key,
        label: PORTFOLIO_GROUP_META[key]?.label || String(firstDefined(raw?.label, key)),
        color: portfolioGroupColor(key, raw?.color, index),
        valueKrw: null,
        valueManwon: null,
        weightPct: null,
        holdings: [],
      };
      current.label = PORTFOLIO_GROUP_META[key]?.label || String(firstDefined(raw?.label, current.label) || current.label);
      current.color = portfolioGroupColor(key, firstDefined(raw?.color, current.color), index);
      current.valueKrw = numberOrNull(firstDefined(raw?.value_krw, raw?.valueKrw, raw?.total_value_krw, raw?.totalValueKrw));
      current.valueManwon = numberOrNull(firstDefined(raw?.value_manwon, raw?.valueManwon, raw?.total_value_manwon, raw?.totalValueManwon));
      current.weightPct = numberOrNull(firstDefined(raw?.weight_pct, raw?.weightPct, raw?.percentage));
      groupRecords.set(key, current);
    });
    const rawCashHoldings = holdings.filter((holding) => holding.groupKey === "cash");
    const rawCashValueKrw = rawCashHoldings.reduce((max, holding) => Math.max(max, holding.valueKrw || 0), 0);
    const rawCashGroupValueKrw = groupRecords.get("cash")?.valueKrw || 0;
    const requestedCashKrw = portfolioCashKrw();
    const cashKrw = requestedCashKrw > 0 ? requestedCashKrw : Math.max(rawCashValueKrw, rawCashGroupValueKrw);
    holdings = holdings.filter((holding) => holding.groupKey !== "cash");
    if (cashKrw > 0) {
      const rawCash = rawCashHoldings[0];
      holdings.push(rawCash
        ? normalizePortfolioCashHolding(rawCash, cashKrw)
        : normalizePortfolioCashHolding(null, cashKrw));
    }
    holdings.forEach((holding) => {
      const group = groupRecords.get(holding.groupKey) || groupRecords.get("us");
      if (holding.valueKrw !== null && holding.valueKrw > 0) group.holdings.push(holding);
    });
    assignPortfolioHoldingColors(holdings);
    const groups = PORTFOLIO_GROUP_ORDER.map((key) => groupRecords.get(key)).concat(
      Array.from(groupRecords.values()).filter((group) => !PORTFOLIO_GROUP_ORDER.includes(group.key)),
    );
    groups.forEach((group) => {
      const derivedKrw = group.holdings.reduce((sum, holding) => sum + (holding.valueKrw || 0), 0);
      if (group.valueKrw === null || derivedKrw > group.valueKrw) group.valueKrw = derivedKrw;
      if (group.valueManwon === null && group.valueKrw !== null) group.valueManwon = group.valueKrw / 10000;
      if (group.key === "cash" && cashKrw > 0) {
        group.valueKrw = cashKrw;
        group.valueManwon = cashKrw / 10000;
      }
    });
    const holdingTotalKrw = holdings.reduce((sum, holding) => sum + (holding.valueKrw && holding.valueKrw > 0 ? holding.valueKrw : 0), 0);
    const groupTotalKrw = groups.reduce((sum, group) => sum + (group.valueKrw && group.valueKrw > 0 ? group.valueKrw : 0), 0);
    const derivedTotalKrw = Math.max(holdingTotalKrw, groupTotalKrw);
    const reportedTotalKrw = numberOrNull(firstDefined(root.total_value_krw, root.totalValueKrw));
    const totalValueKrw = reportedTotalKrw === null ? derivedTotalKrw : Math.max(reportedTotalKrw, derivedTotalKrw);
    const reportedTotalManwon = numberOrNull(firstDefined(root.total_value_manwon, root.totalValueManwon));
    const totalValueManwon = reportedTotalManwon === null ? totalValueKrw / 10000 : Math.max(reportedTotalManwon, totalValueKrw / 10000);
    groups.forEach((group) => {
      group.weightPct = totalValueKrw > 0 ? group.valueKrw / totalValueKrw * 100 : 0;
    });
    holdings.forEach((holding) => {
      holding.weightPct = totalValueKrw > 0 && holding.valueKrw > 0 ? holding.valueKrw / totalValueKrw * 100 : 0;
    });
    const unpriced = firstArray(root.unpriced, payload?.unpriced).map((item) => normalizePortfolioUnpriced(item)).filter(Boolean);
    return {
      generatedAt: firstDefined(root.generated_at, root.generatedAt),
      baseCurrency: String(firstDefined(root.base_currency, root.baseCurrency, "KRW") || "KRW"),
      unit: String(firstDefined(root.unit, "만원") || "만원"),
      totalValueKrw,
      totalValueManwon,
      fx: normalizePortfolioFx(root.fx),
      groups,
      holdings,
      cashKrw,
      cashManwon: cashKrw / 10000,
      unpriced,
      warnings: firstArray(root.warnings, payload?.warnings).map((warning) => String(warning)),
    };
  }

  function normalizePortfolioHolding(raw, index) {
    const groupKey = normalizePortfolioGroupKey(firstDefined(raw?.group_key, raw?.groupKey, raw?.market, raw?.group_label, raw?.groupLabel));
    const market = groupKey === "cash" ? "cash" : Object.prototype.hasOwnProperty.call(PORTFOLIO_MARKETS, groupKey)
      ? groupKey
      : normalizePortfolioMarket(firstDefined(raw?.market, "us"));
    const defaultSymbol = groupKey === "cash" ? "KRW" : "";
    const symbol = String(firstDefined(raw?.symbol, raw?.ticker, raw?.id, defaultSymbol) || "").trim();
    const valueKrw = numberOrNull(firstDefined(raw?.value_krw, raw?.valueKrw));
    const valueManwon = numberOrNull(firstDefined(raw?.value_manwon, raw?.valueManwon)) ?? (valueKrw === null ? null : valueKrw / 10000);
    const unit = groupKey === "cash" ? "KRW" : portfolioMarketMeta(market).unit;
    return {
      id: String(firstDefined(raw?.id, `${market}:${symbol}`) || `${market}:${symbol}`),
      market,
      symbol,
      name: groupKey === "cash" ? "현금" : String(firstDefined(raw?.name, symbol) || symbol),
      quantity: numberOrNull(raw?.quantity),
      quantityUnit: String(firstDefined(raw?.quantity_unit, raw?.quantityUnit, raw?.unit, unit) || unit),
      price: numberOrNull(raw?.price),
      priceCurrency: String(firstDefined(raw?.price_currency, raw?.priceCurrency, raw?.currency, "") || ""),
      fxRate: numberOrNull(firstDefined(raw?.fx_rate, raw?.fxRate)),
      valueKrw,
      valueManwon,
      weightPct: numberOrNull(firstDefined(raw?.weight_pct, raw?.weightPct, raw?.percentage)),
      groupKey,
      groupLabel: PORTFOLIO_GROUP_META[groupKey]?.label || String(firstDefined(raw?.group_label, raw?.groupLabel, groupKey) || groupKey),
      color: "",
      asOf: firstDefined(raw?.as_of, raw?.asOf, raw?.quote_as_of, raw?.quoteAsOf),
      source: portfolioSourceText(raw?.source) || (groupKey === "cash" ? "사용자 입력 현금" : ""),
    };
  }

  function normalizePortfolioCashHolding(raw, valueKrw) {
    const normalized = raw ? normalizePortfolioHolding(raw, 0) : normalizePortfolioHolding({ market: "cash", symbol: "KRW", name: "현금" }, 0);
    return {
      ...normalized,
      id: "cash:KRW",
      market: "cash",
      symbol: "KRW",
      name: "현금",
      quantity: valueKrw,
      quantityUnit: "KRW",
      price: 1,
      priceCurrency: "KRW",
      fxRate: 1,
      valueKrw,
      valueManwon: valueKrw / 10000,
      groupKey: "cash",
      groupLabel: PORTFOLIO_GROUP_META.cash.label,
      color: "",
      source: normalized.source || "사용자 입력 현금",
    };
  }

  function normalizePortfolioGroupKey(key) {
    const value = String(key || "").trim().toLocaleLowerCase("en-US");
    if (value === "cash" || value === "krw" || value.includes("현금")) return "cash";
    if (value === "gold" || value.includes("gold") || value.includes("금")) return "gold";
    if (value === "kospi" || value === "korea" || value === "kr" || value.includes("한국") || value.includes("kospi")) return "kospi";
    return "us";
  }

  function normalizePortfolioUnpriced(raw) {
    if (typeof raw === "string" || typeof raw === "number") return { symbol: String(raw), name: "", reason: "가격 미확인" };
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
    return {
      symbol: String(firstDefined(raw.symbol, raw.ticker, raw.id, "자산") || "자산"),
      name: String(firstDefined(raw.name, "") || ""),
      reason: String(firstDefined(raw.reason, raw.message, raw.error?.message, raw.error?.code, "가격 미확인") || "가격 미확인"),
    };
  }

  function normalizePortfolioFx(raw) {
    if (raw === null || raw === undefined) return null;
    if (typeof raw !== "object" || Array.isArray(raw)) return { rate: numberOrNull(raw), source: "" };
    return {
      rate: numberOrNull(firstDefined(raw.rate, raw.usd_krw, raw.usdKrw, raw.value)),
      source: portfolioSourceText(raw.source || raw.provider),
      asOf: firstDefined(raw.as_of, raw.asOf),
    };
  }

  function portfolioSourceText(raw) {
    if (raw && typeof raw === "object" && !Array.isArray(raw)) {
      return String(firstDefined(raw.source, raw.provider, raw.name, raw.label, "") || "");
    }
    return String(firstDefined(raw, "") || "");
  }

  function portfolioGroupColor(groupKey, rawColor, index = 0) {
    const metaColor = PORTFOLIO_GROUP_META[groupKey]?.color;
    const value = String(rawColor || "").trim();
    if (metaColor) return metaColor;
    if (/^#[0-9a-f]{6}$/i.test(value)) return value;
    return "#74808D";
  }

  function portfolioCashKrw() {
    const cashManwon = numberOrNull(state.portfolio.cashManwon);
    return cashManwon === null || cashManwon <= 0 ? 0 : Math.round(clamp(cashManwon, 0, PORTFOLIO_MAX_CASH_MANWON) * 10000);
  }

  function portfolioHash(value) {
    let hash = 2166136261;
    for (const character of String(value || "")) {
      hash ^= character.charCodeAt(0);
      hash = Math.imul(hash, 16777619);
    }
    return hash >>> 0;
  }

  function portfolioHoldingPalette(groupKey) {
    return PORTFOLIO_GROUP_PALETTES[groupKey] || PORTFOLIO_GROUP_PALETTES.us;
  }

  function portfolioGeneratedFamilyColor(groupKey, key, ordinal = 0) {
    const hash = portfolioHash(`${groupKey}:${key}:${ordinal}`);
    const families = {
      us: { baseHue: 176, span: 104, saturation: 64, saturationSpan: 18, lightness: 44, lightnessSpan: 16 },
      kospi: { baseHue: 326, span: 64, saturation: 68, saturationSpan: 16, lightness: 48, lightnessSpan: 14 },
      gold: { baseHue: 32, span: 24, saturation: 72, saturationSpan: 18, lightness: 44, lightnessSpan: 16 },
      cash: { baseHue: 198, span: 24, saturation: 10, saturationSpan: 8, lightness: 42, lightnessSpan: 24 },
    };
    const family = families[groupKey] || families.us;
    const hue = (family.baseHue + (hash % family.span)) % 360;
    const saturation = family.saturation + ((hash >>> 8) % family.saturationSpan);
    const lightness = family.lightness + ((hash >>> 16) % family.lightnessSpan);
    const chroma = (1 - Math.abs(2 * (lightness / 100) - 1)) * (saturation / 100);
    const x = chroma * (1 - Math.abs(((hue / 60) % 2) - 1));
    const m = lightness / 100 - chroma / 2;
    let red = 0;
    let green = 0;
    let blue = 0;
    if (hue < 60) [red, green, blue] = [chroma, x, 0];
    else if (hue < 120) [red, green, blue] = [x, chroma, 0];
    else if (hue < 180) [red, green, blue] = [0, chroma, x];
    else if (hue < 240) [red, green, blue] = [0, x, chroma];
    else if (hue < 300) [red, green, blue] = [x, 0, chroma];
    else [red, green, blue] = [chroma, 0, x];
    return `#${[red, green, blue].map((channel) => Math.round((channel + m) * 255).toString(16).padStart(2, "0")).join("")}`;
  }

  function assignPortfolioHoldingColors(holdings) {
    const byGroup = new Map();
    holdings.forEach((holding) => {
      if (!byGroup.has(holding.groupKey)) byGroup.set(holding.groupKey, []);
      byGroup.get(holding.groupKey).push(holding);
    });
    byGroup.forEach((groupHoldings, groupKey) => {
      const palette = portfolioHoldingPalette(groupKey);
      const used = new Set();
      const usedColors = new Set();
      let overflowIndex = 0;
      [...groupHoldings].sort((left, right) => portfolioHoldingKey(left).localeCompare(portfolioHoldingKey(right))).forEach((holding) => {
        const stableKey = portfolioHoldingKey(holding);
        const start = portfolioHash(stableKey) % palette.length;
        let paletteIndex = start;
        while (used.has(paletteIndex) && used.size < palette.length) paletteIndex = (paletteIndex + 1) % palette.length;
        if (used.size < palette.length) {
          used.add(paletteIndex);
          holding.color = palette[paletteIndex];
          usedColors.add(holding.color);
        } else {
          do {
            holding.color = portfolioGeneratedFamilyColor(groupKey, stableKey, overflowIndex);
            overflowIndex += 1;
          } while (usedColors.has(holding.color));
          usedColors.add(holding.color);
        }
      });
    });
  }

  function renderPortfolioResult() {
    const result = state.portfolio.result;
    if (el.portfolioTotal) el.portfolioTotal.textContent = result ? `${formatPortfolioKrw(result.totalValueKrw)} · ${formatPortfolioManwon(result.totalValueManwon)} ${result.unit}` : "—";
    if (el.portfolioGroupSummary) {
      const groups = result?.groups?.filter((group) => group.valueKrw > 0 || group.holdings.length) || [];
      if (!groups.length) {
        el.portfolioGroupSummary.innerHTML = '<span class="portfolio-summary-placeholder">시장별 요약은 포트를 구성한 뒤 표시됩니다.</span>';
      } else {
        const list = groups.map((group) => {
          const weight = group.weightPct ?? portfolioGroupWeight(group, result.totalValueKrw);
          return `<div role="listitem" data-portfolio-group="${escapeAttr(group.key)}"><span><i class="portfolio-summary-swatch" style="background:${escapeAttr(portfolioGroupColor(group.key, group.color))}" aria-hidden="true"></i><strong>${escapeHTML(group.label)}</strong></span><small>${escapeHTML(formatPortfolioKrw(group.valueKrw))} · ${escapeHTML(formatPortfolioManwon(group.valueManwon))} 만원 · ${escapeHTML(formatPortfolioPercent(weight))}</small></div>`;
        }).join("");
        el.portfolioGroupSummary.innerHTML = list;
      }
    }
    renderPortfolioUnpriced(result?.unpriced || []);
    renderPortfolioSourceNote(result);
    renderPortfolioDonut(result);
    renderPortfolioTreemap(result);
  }

  function renderPortfolioStatus() {
    const portfolio = state.portfolio;
    const result = portfolio.result;
    let message = "";
    if (portfolio.composing) {
      message = portfolio.autoComposing
        ? "저장된 포트폴리오를 복원해 현재 시세로 다시 계산하는 중입니다…"
        : "포트폴리오 구성과 환산 금액을 계산하는 중입니다…";
    } else if (portfolio.error) {
      message = `구성에 실패했습니다. ${cleanError(portfolio.error)}`;
    } else if (result?.unpriced?.length) {
      const cashGroup = result.groups?.find((group) => group.key === "cash");
      const cashText = cashGroup?.valueKrw > 0 ? ` 현금 ${formatPortfolioManwon(cashGroup.valueManwon)} 만원은 포함되었습니다.` : "";
      message = `구성을 완료했지만 ${result.unpriced.length}개 자산의 가격을 확인하지 못했습니다.${cashText}`;
    } else if (result) {
      const cashGroup = result.groups?.find((group) => group.key === "cash");
      const cashText = cashGroup?.valueKrw > 0 ? ` · 현금 ${formatPortfolioManwon(cashGroup.valueManwon)} 만원 포함` : "";
      message = `${portfolio.lastComposeWasAuto ? "저장된 포트폴리오를 불러와 계산했습니다" : "구성 완료"} · ${formatPortfolioKrw(result.totalValueKrw)} · ${formatPortfolioManwon(result.totalValueManwon)} ${result.unit}${cashText}`;
    } else if (portfolio.statusMessage) {
      message = portfolio.statusMessage;
    } else if (portfolio.holdings.length || portfolio.cashManwon > 0) {
      const parts = [];
      if (portfolio.holdings.length) parts.push(`${portfolio.holdings.length}개 자산`);
      if (portfolio.cashManwon > 0) parts.push(`현금 ${formatPortfolioManwon(portfolio.cashManwon)} 만원`);
      message = `${parts.join(" · ")}을 저장했습니다. 수량을 확인하고 구성하세요.`;
    } else {
      message = "자산을 검색해 추가하거나 현금을 입력하면 포트폴리오를 계산할 수 있습니다.";
    }
    if (result?.warnings?.length) message += ` ${result.warnings.map((warning) => String(warning)).join(" · ")}`;
    if (portfolio.storageError && !portfolio.error) message += " 저장하지 못했습니다. 브라우저 저장 공간을 확인하세요.";
    if (el.portfolioStatus) {
      el.portfolioStatus.textContent = message;
      el.portfolioStatus.setAttribute("role", "status");
      el.portfolioStatus.setAttribute("aria-live", "polite");
      el.portfolioStatus.classList.toggle("is-error", Boolean(portfolio.error));
      el.portfolioStatus.classList.toggle("is-loading", portfolio.composing);
      el.portfolioStatus.classList.toggle("is-partial", Boolean(result?.unpriced?.length));
    }
    el.portfolioCompose?.toggleAttribute("disabled", portfolio.composing);
    el.portfolioBuilderForm?.setAttribute("aria-busy", String(portfolio.composing));
  }

  function renderPortfolioUnpriced(items) {
    const container = el.portfolioUnpriced;
    if (!container) return;
    container.hidden = !items.length;
    if (!items.length) {
      container.textContent = "";
      return;
    }
    const list = items.map((item) => `<li><strong>${escapeHTML(item.symbol)}</strong>${item.name ? ` · ${escapeHTML(item.name)}` : ""}<span> · ${escapeHTML(item.reason)}</span></li>`).join("");
    container.innerHTML = container.tagName.toLowerCase() === "ul" || container.tagName.toLowerCase() === "ol"
      ? list
      : `<ul class="portfolio-unpriced-list">${list}</ul>`;
  }

  function renderPortfolioSourceNote(result) {
    if (!el.portfolioSourceNote) return;
    if (!result) {
      el.portfolioSourceNote.textContent = "가격·환율 출처와 기준일은 구성 후 표시됩니다. 현금은 사용자가 직접 입력한 원화 금액이며 시세 조회를 하지 않습니다. 미국/KOSPI 편의 시세는 지연될 수 있고, KRX 금은 네이버 국내 금 시세(원/g)를 사용합니다. 표시 값은 정보 제공용이며 투자 조언이 아닙니다.";
      return;
    }
    const assetHoldings = result.holdings.filter((holding) => holding.groupKey !== "cash");
    const cashHoldings = result.holdings.filter((holding) => holding.groupKey === "cash");
    const sources = [...new Set(assetHoldings.map((holding) => holding.source).filter(Boolean))];
    const cashGroup = result.groups?.find((group) => group.key === "cash");
    if (cashHoldings.length || cashGroup?.valueKrw > 0) sources.push("사용자 입력 현금");
    const asOf = [...new Set(assetHoldings.map((holding) => holding.asOf).filter(Boolean))];
    const fxParts = result.fx?.rate !== null && result.fx?.rate !== undefined
      ? [`환율 ${formatPortfolioNumber(result.fx.rate, 2)}${result.fx.source ? ` · ${result.fx.source}` : ""}${result.fx.asOf ? ` · ${formatDate(result.fx.asOf, { includeTime: false })}` : ""}`]
      : [];
    const parts = [
      `가격 출처: ${sources.length ? sources.join(", ") : "출처 미표기"}`,
      `기준일: ${asOf.length ? asOf.map((value) => formatDate(value, { includeTime: false })).join(", ") : "미표기"}`,
      cashGroup?.valueKrw > 0 ? `현금 출처: 사용자 입력 · ${formatPortfolioManwon(cashGroup.valueManwon)} 만원` : "",
      ...fxParts,
      result.generatedAt ? `계산 시각: ${formatDate(result.generatedAt, { includeTime: true })}` : "",
      "편의 시세는 지연될 수 있으며 표시 값은 정보 제공용이지 투자 조언이 아닙니다.",
    ].filter(Boolean);
    el.portfolioSourceNote.textContent = parts.join(" · ");
  }

  function portfolioGroupWeight(group, totalKrw) {
    const total = numberOrNull(totalKrw);
    const value = numberOrNull(group?.valueKrw);
    return total && value !== null ? value / total * 100 : 0;
  }

  function formatPortfolioNumber(value, digits = 1) {
    const number = numberOrNull(value);
    return number === null ? "—" : formatNumber(number, digits);
  }

  function formatPortfolioManwon(value) {
    const number = numberOrNull(value);
    if (number === null) return "—";
    const absolute = Math.abs(number);
    const digits = absolute >= 1000 ? 0 : absolute >= 10 ? 1 : 2;
    return formatNumber(number, digits);
  }

  function formatPortfolioKrw(value) {
    const number = numberOrNull(value);
    return number === null ? "—" : `${formatNumber(number, 0)}원`;
  }

  function formatPortfolioPercent(value) {
    const number = numberOrNull(value);
    return number === null ? "—" : `${formatNumber(number, 1)}%`;
  }

  function portfolioUsesCompactCharts() {
    return window.matchMedia("(max-width: 720px)").matches;
  }

  function renderPortfolioDonut(result) {
    const container = el.portfolioDonut;
    if (!container) return;
    const holdings = result?.holdings?.filter((holding) => holding.valueKrw !== null && holding.valueKrw > 0) || [];
    if (!holdings.length) {
      setPortfolioSvg(container, portfolioEmptySvg("포트폴리오를 구성하면 자산 비중이 표시됩니다.", "donut"), "0 0 720 360", "donut");
      return;
    }
    const sorted = holdings.map((holding) => ({ holding }))
      .sort((left, right) => (right.holding.valueKrw - left.holding.valueKrw) || portfolioHoldingKey(left.holding).localeCompare(portfolioHoldingKey(right.holding)));
    const total = sorted.reduce((sum, item) => sum + item.holding.valueKrw, 0);
    const compact = portfolioUsesCompactCharts();
    const width = compact ? 360 : 720;
    const cx = compact ? 180 : 164;
    const cy = compact ? 152 : 154;
    const outerRadius = 126;
    const innerRadius = 75;
    const legendX = compact ? 20 : 382;
    const legendTop = compact ? 322 : 48;
    const legendStep = compact ? 48 : 38;
    const height = compact
      ? legendTop + sorted.length * legendStep + 24
      : Math.max(360, 42 + sorted.length * legendStep + 18);
    let cursor = -Math.PI / 2;
    const segments = [];
    const legend = [];
    sorted.forEach(({ holding }) => {
      const fraction = total > 0 ? holding.valueKrw / total : 0;
      const rawStart = cursor;
      const rawEnd = cursor + fraction * Math.PI * 2;
      cursor = rawEnd;
      const gap = Math.min(0.018, fraction * 0.25);
      const start = rawStart + gap;
      const end = rawEnd - gap;
      const color = holding.color || portfolioGeneratedFamilyColor(holding.groupKey, portfolioHoldingKey(holding));
      const percent = holding.weightPct ?? fraction * 100;
      const title = `${holding.name} (${holding.symbol}) · ${formatPortfolioManwon(holding.valueManwon)} 만원 · ${formatPortfolioPercent(percent)}`;
      if (end > start) {
        segments.push(`<path class="portfolio-donut-segment" d="${portfolioDonutPath(cx, cy, outerRadius, innerRadius, start, end)}" fill="${escapeAttr(color)}"><title>${escapeHTML(title)}</title></path>`);
      }
      const large = fraction >= 0.12 && sorted.length <= 8;
      if (large) {
        const midpoint = (rawStart + rawEnd) / 2;
        const labelRadius = (outerRadius + innerRadius) / 2;
        const x = cx + Math.cos(midpoint) * labelRadius;
        const y = cy + Math.sin(midpoint) * labelRadius;
        const label = holding.symbol || truncate(holding.name, 10);
        segments.push(`<text class="portfolio-donut-label" x="${x.toFixed(2)}" y="${(y - 5).toFixed(2)}" text-anchor="middle"><tspan x="${x.toFixed(2)}" dy="0">${escapeHTML(label)}</tspan><tspan x="${x.toFixed(2)}" dy="16">${escapeHTML(formatPortfolioPercent(percent))}</tspan></text>`);
      }
      legend.push({ holding, color, percent, title, large });
    });
    const legendMarkup = legend.map((item, index) => {
      const y = legendTop + index * legendStep;
      const label = truncate(item.holding.name || item.holding.symbol, compact ? 22 : 28);
      return `<g class="portfolio-donut-legend-item" transform="translate(0 ${y})"><circle cx="${legendX}" cy="0" r="5" fill="${escapeAttr(item.color)}"></circle><text x="${legendX + 13}" y="4"><tspan>${escapeHTML(label)}${item.holding.symbol && item.holding.name !== item.holding.symbol ? ` · ${escapeHTML(item.holding.symbol)}` : ""}</tspan><tspan x="${legendX + 13}" dy="16">${escapeHTML(formatPortfolioManwon(item.holding.valueManwon))} 만원 · ${escapeHTML(formatPortfolioPercent(item.percent))}</tspan></text><title>${escapeHTML(item.title)}</title></g>`;
    }).join("");
    const labelSummary = sorted.map((item) => `${item.holding.name} ${formatPortfolioPercent(item.holding.weightPct ?? item.holding.valueKrw / total * 100)}`).join(", ");
    const inner = `
      <title id="portfolio-donut-svg-title">포트폴리오 자산 비중 도넛 차트</title>
      <desc id="portfolio-donut-svg-desc">${escapeHTML(labelSummary)}</desc>
      <g class="portfolio-donut-chart">${segments.join("")}</g>
      <circle class="portfolio-donut-hole" cx="${cx}" cy="${cy}" r="${innerRadius - 1}" fill="#fcfcf9"></circle>
      <text class="portfolio-donut-total" x="${cx}" y="${cy - 5}" text-anchor="middle"><tspan x="${cx}" dy="0">${escapeHTML(formatPortfolioManwon(result.totalValueManwon))}</tspan><tspan x="${cx}" dy="21">${escapeHTML(result.unit)}</tspan></text>
      <g class="portfolio-donut-legend">${legendMarkup}</g>`;
    const outer = `<svg class="portfolio-chart portfolio-donut-svg" viewBox="0 0 ${width} ${height}" role="img" aria-labelledby="portfolio-donut-svg-title portfolio-donut-svg-desc">${inner}</svg>`;
    setPortfolioSvg(container, outer, `0 0 ${width} ${height}`, "donut", inner);
  }

  function portfolioDonutPath(cx, cy, outerRadius, innerRadius, start, end) {
    const span = end - start;
    if (span >= Math.PI * 2 - 0.001) {
      const mid = start + Math.PI;
      const outerStart = portfolioPolarPoint(cx, cy, outerRadius, start);
      const outerMid = portfolioPolarPoint(cx, cy, outerRadius, mid);
      const innerMid = portfolioPolarPoint(cx, cy, innerRadius, mid);
      const innerStart = portfolioPolarPoint(cx, cy, innerRadius, start);
      return `M ${outerStart.x.toFixed(2)} ${outerStart.y.toFixed(2)} A ${outerRadius} ${outerRadius} 0 1 1 ${outerMid.x.toFixed(2)} ${outerMid.y.toFixed(2)} A ${outerRadius} ${outerRadius} 0 1 1 ${outerStart.x.toFixed(2)} ${outerStart.y.toFixed(2)} M ${innerStart.x.toFixed(2)} ${innerStart.y.toFixed(2)} A ${innerRadius} ${innerRadius} 0 1 0 ${innerMid.x.toFixed(2)} ${innerMid.y.toFixed(2)} A ${innerRadius} ${innerRadius} 0 1 0 ${innerStart.x.toFixed(2)} ${innerStart.y.toFixed(2)} Z`;
    }
    const outerStart = portfolioPolarPoint(cx, cy, outerRadius, start);
    const outerEnd = portfolioPolarPoint(cx, cy, outerRadius, end);
    const innerEnd = portfolioPolarPoint(cx, cy, innerRadius, end);
    const innerStart = portfolioPolarPoint(cx, cy, innerRadius, start);
    const largeArc = span > Math.PI ? 1 : 0;
    return `M ${outerStart.x.toFixed(2)} ${outerStart.y.toFixed(2)} A ${outerRadius} ${outerRadius} 0 ${largeArc} 1 ${outerEnd.x.toFixed(2)} ${outerEnd.y.toFixed(2)} L ${innerEnd.x.toFixed(2)} ${innerEnd.y.toFixed(2)} A ${innerRadius} ${innerRadius} 0 ${largeArc} 0 ${innerStart.x.toFixed(2)} ${innerStart.y.toFixed(2)} Z`;
  }

  function portfolioPolarPoint(cx, cy, radius, angle) {
    return { x: cx + Math.cos(angle) * radius, y: cy + Math.sin(angle) * radius };
  }

  function renderPortfolioTreemap(result) {
    const container = el.portfolioTreemap;
    if (!container) return;
    const groups = result?.groups?.filter((group) => group.valueKrw > 0 && group.holdings.length) || [];
    if (!groups.length) {
      setPortfolioSvg(container, portfolioEmptySvg("구성 결과가 여기에 표시됩니다.", "treemap"), "0 0 1120 450", "treemap");
      return;
    }
    const compact = portfolioUsesCompactCharts();
    const width = compact ? 360 : 1120;
    const chartHeight = compact ? 500 : 390;
    const gap = compact ? 4 : 7;
    const total = groups.reduce((sum, group) => sum + Math.max(0, group.valueKrw || 0), 0);
    const availableWidth = width - gap * (groups.length - 1);
    let x = 0;
    const cells = [];
    const tiny = [];
    groups.forEach((group, groupIndex) => {
      const groupWidth = groupIndex === groups.length - 1
        ? width - x
        : availableWidth * (group.valueKrw / total);
      const labelHeight = compact ? 40 : 46;
      const innerHeight = chartHeight - labelHeight;
      const groupColor = portfolioGroupColor(group.key, group.color, groupIndex);
      const holdings = [...group.holdings].sort((left, right) => (right.valueKrw - left.valueKrw) || portfolioHoldingKey(left).localeCompare(portfolioHoldingKey(right)));
      const holdingsTotal = holdings.reduce((sum, holding) => sum + Math.max(0, holding.valueKrw || 0), 0);
      let y = labelHeight;
      const compactGroupLabels = { us: "미국", kospi: "한국", gold: "금", cash: "현금" };
      const groupLabel = compact
        ? (compactGroupLabels[group.key] || truncate(group.label, 4))
        : (groupWidth >= 118 ? group.label : truncate(group.label, 8));
      const showGroupValue = groupWidth >= (compact ? 135 : 118);
      const groupValueMarkup = showGroupValue ? `<tspan dx="9">${escapeHTML(formatPortfolioManwon(group.valueManwon))} 만원</tspan>` : "";
      const groupMarkup = [`<g class="portfolio-treemap-group" data-portfolio-group="${escapeAttr(group.key)}"><rect class="portfolio-treemap-group-bg" x="${x.toFixed(2)}" y="0" width="${Math.max(0, groupWidth).toFixed(2)}" height="${chartHeight}" rx="8" fill="${escapeAttr(groupColor)}" fill-opacity="0.12"></rect><text class="portfolio-treemap-group-label" x="${(x + (compact ? 7 : 12)).toFixed(2)}" y="28"><tspan>${escapeHTML(groupLabel)}</tspan>${groupValueMarkup}</text>`];
      holdings.forEach((holding, holdingIndex) => {
        const cellHeight = holdingIndex === holdings.length - 1
          ? chartHeight - y
          : innerHeight * (holding.valueKrw / Math.max(holdingsTotal, 1));
        const color = holding.color || portfolioGeneratedFamilyColor(holding.groupKey, portfolioHoldingKey(holding), holdingIndex);
        const percent = holding.weightPct ?? (result.totalValueKrw ? holding.valueKrw / result.totalValueKrw * 100 : 0);
        const isTiny = groupWidth < (compact ? 82 : 100) || cellHeight < 42;
        const isLarge = groupWidth >= (compact ? 160 : 190) && cellHeight >= 84;
        const isMedium = !isLarge && groupWidth >= (compact ? 82 : 100) && cellHeight >= 48;
        const title = `${holding.name} (${holding.symbol}) · ${formatPortfolioManwon(holding.valueManwon)} 만원 · ${formatPortfolioPercent(percent)}`;
        groupMarkup.push(`<rect class="portfolio-treemap-cell" x="${(x + 2).toFixed(2)}" y="${(y + 2).toFixed(2)}" width="${Math.max(0, groupWidth - 4).toFixed(2)}" height="${Math.max(0, cellHeight - 4).toFixed(2)}" rx="5" fill="${escapeAttr(color)}"><title>${escapeHTML(title)}</title></rect>`);
        if (isLarge) {
          groupMarkup.push(`<text class="portfolio-treemap-label" x="${(x + 16).toFixed(2)}" y="${(y + 27).toFixed(2)}"><tspan x="${(x + 16).toFixed(2)}" dy="0">${escapeHTML(truncate(holding.name || holding.symbol, 26))}</tspan><tspan x="${(x + 16).toFixed(2)}" dy="21">${escapeHTML(holding.symbol)} · ${escapeHTML(formatPortfolioManwon(holding.valueManwon))} 만원</tspan><tspan x="${(x + 16).toFixed(2)}" dy="19">${escapeHTML(formatPortfolioPercent(percent))}</tspan></text>`);
        } else if (isMedium) {
          groupMarkup.push(`<text class="portfolio-treemap-label is-compact" x="${(x + 14).toFixed(2)}" y="${(y + 23).toFixed(2)}"><tspan x="${(x + 14).toFixed(2)}" dy="0">${escapeHTML(holding.symbol || truncate(holding.name, 18))}</tspan><tspan x="${(x + 14).toFixed(2)}" dy="19">${escapeHTML(formatPortfolioManwon(holding.valueManwon))} 만원 · ${escapeHTML(formatPortfolioPercent(percent))}</tspan></text>`);
        } else {
          tiny.push({ holding, color, percent, title });
        }
        y += cellHeight;
      });
      groupMarkup.push("</g>");
      cells.push(groupMarkup.join(""));
      x += groupWidth + gap;
    });
    const legendColumns = compact ? 1 : 2;
    const legendRows = Math.ceil(tiny.length / legendColumns);
    const height = chartHeight + (tiny.length ? 35 + legendRows * 25 : 16);
    const tinyLegend = tiny.map((item, index) => {
      const column = index % legendColumns;
      const row = Math.floor(index / legendColumns);
      const itemX = column === 0 ? 8 : width / 2 + 8;
      const itemY = chartHeight + 34 + row * 25;
      return `<g class="portfolio-treemap-legend-item" transform="translate(${itemX} ${itemY})"><circle cx="0" cy="0" r="5" fill="${escapeAttr(item.color)}"></circle><text x="12" y="4">${escapeHTML(truncate(item.holding.name || item.holding.symbol, 25))} · ${escapeHTML(item.holding.symbol)} · ${escapeHTML(formatPortfolioManwon(item.holding.valueManwon))} 만원 · ${escapeHTML(formatPortfolioPercent(item.percent))}</text><title>${escapeHTML(item.title)}</title></g>`;
    }).join("");
    const groupSummary = groups.map((group) => `${group.label} ${formatPortfolioPercent(portfolioGroupWeight(group, result.totalValueKrw))}`).join(", ");
    const inner = `
      <title id="portfolio-treemap-svg-title">시장별 포트폴리오 트리맵</title>
      <desc id="portfolio-treemap-svg-desc">${escapeHTML(groupSummary)}. 각 열은 시장별 금액, 각 칸은 보유 자산 금액에 비례하며 종목별 색상은 같은 시장 안에서도 구분됩니다.</desc>
      <g class="portfolio-treemap-chart">${cells.join("")}</g>
      ${tiny.length ? `<g class="portfolio-treemap-legend"><text class="portfolio-treemap-legend-heading" x="8" y="${chartHeight + 16}">작은 보유 자산</text>${tinyLegend}</g>` : ""}`;
    const outer = `<svg class="portfolio-chart portfolio-treemap-svg" viewBox="0 0 ${width} ${height}" role="img" aria-labelledby="portfolio-treemap-svg-title portfolio-treemap-svg-desc">${inner}</svg>`;
    setPortfolioSvg(container, outer, `0 0 ${width} ${height}`, "treemap", inner);
  }

  function portfolioEmptySvg(message, type) {
    const titleId = `portfolio-${type}-svg-title`;
    const descId = `portfolio-${type}-svg-desc`;
    const viewBox = type === "donut" ? "0 0 720 360" : "0 0 1120 450";
    const x = type === "donut" ? 360 : 560;
    const y = type === "donut" ? 180 : 225;
    const className = type === "donut" ? "portfolio-donut-svg" : "portfolio-treemap-svg";
    return `<svg class="portfolio-chart ${className}" viewBox="${viewBox}" role="img" aria-labelledby="${titleId} ${descId}"><title id="${titleId}">${escapeHTML(type === "donut" ? "포트폴리오 자산 비중 도넛 차트" : "시장별 포트폴리오 트리맵")}</title><desc id="${descId}">${escapeHTML(message)}</desc><text class="portfolio-chart-empty" x="${x}" y="${y}" text-anchor="middle">${escapeHTML(message)}</text></svg>`;
  }

  function setPortfolioSvg(container, outerMarkup, viewBox, type, innerMarkup = null) {
    if (container.tagName.toLowerCase() === "svg") {
      container.setAttribute("viewBox", viewBox);
      container.setAttribute("role", "img");
      container.setAttribute("aria-labelledby", `portfolio-${type}-svg-title portfolio-${type}-svg-desc`);
      container.classList.add("portfolio-chart", `portfolio-${type}-svg`);
      if (innerMarkup !== null) container.innerHTML = innerMarkup;
      else {
        const match = outerMarkup.match(/<svg[^>]*>([\s\S]*)<\/svg>$/i);
        container.innerHTML = match ? match[1] : outerMarkup;
      }
    } else {
      container.innerHTML = outerMarkup;
    }
  }

  function setDashboardLoading(isLoading) {
    state.loading = isLoading;
    el.tableShell.setAttribute("aria-busy", String(isLoading));
    el.tableLoading.hidden = !isLoading;
    if (isLoading && !state.dashboard) {
      el.stockTableBody.innerHTML = "";
      el.emptyState.hidden = true;
    }
  }

  async function fetchJSON(url, options = {}, timeoutMs = 12000) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(url, {
        ...options,
        headers: { Accept: "application/json", ...(options.body ? { "Content-Type": "application/json" } : {}), ...(options.headers || {}) },
        signal: controller.signal,
      });
      const text = await response.text();
      let payload = {};
      if (text) {
        try { payload = JSON.parse(text); }
        catch { throw new Error(`API 응답을 JSON으로 해석할 수 없습니다. (${response.status})`); }
      }
      if (!response.ok) {
        const message = payload?.error?.message || payload?.message || payload?.error || `API 요청 실패 (${response.status})`;
        const error = new Error(String(message));
        error.status = response.status;
        error.payload = payload;
        throw error;
      }
      return payload;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("API 응답 시간이 초과되었습니다.");
      throw error;
    } finally {
      window.clearTimeout(timer);
    }
  }

  function normalizeDashboard(payload) {
    const root = unwrapObject(payload, ["dashboard", "screen", "result"]);
    const rawStocks = firstArray(root.signals, root.stocks, root.items, root.results, root.companies, payload?.signals, payload?.stocks);
    const marketRaw = firstObject(root.market_overlay, root.marketOverlay, root.market, root.market_timing, root.timing);
    const asOf = firstDefined(root.as_of, root.asOf, root.data_date, root.dataDate, root.updated_at, root.updatedAt, root.generated_at, root.generatedAt);
    const generatedAt = firstDefined(root.generated_at, root.generatedAt, root.updated_at, root.updatedAt, asOf);
    const universeRaw = firstObject(root.universe, root.index, {});
    const universeCount = numberOrNull(firstDefined(universeRaw.count, root.universe_count, root.universeCount, root.total_count, root.total, root.count)) ?? rawStocks.length;
    const market = normalizeMarket(marketRaw);
    const sources = normalizeDashboardSources(root, universeRaw, rawStocks);
    const stocks = rawStocks.map((item) => normalizeStock(item, { market, asOf, sources })).filter((item) => item.ticker);
    const eligibleCount = numberOrNull(firstDefined(root.eligible_count, root.eligibleCount, root.data_quality?.eligible_count)) ?? stocks.filter((item) => !item.insufficient).length;
    const coverage = normalizeCoverage(firstDefined(
      root.data_quality?.average_company_factor_coverage_pct,
      root.data_quality?.averageCompanyFactorCoveragePct,
      root.data_quality?.coverage_pct,
      root.data_quality?.coverage,
      root.coverage_pct,
      root.coverage,
    ), eligibleCount, stocks.length || universeCount);
    return {
      raw: root,
      asOf,
      generatedAt,
      universeCount,
      returnedCount: numberOrNull(firstDefined(root.count, rawStocks.length)) ?? rawStocks.length,
      eligibleCount,
      coverage,
      universe: universeRaw,
      sources,
      dataQuality: firstObject(root.data_quality, root.dataQuality, {}),
      refresh: firstObject(root.refresh, root.refresh_status, {}),
      market,
      stocks,
    };
  }

  function normalizeStock(raw, context = {}) {
    const company = firstObject(raw?.company, raw?.profile, {});
    const ticker = String(firstDefined(raw?.symbol, raw?.ticker, raw?.code, company.symbol, "")).trim().toUpperCase();
    const name = String(firstDefined(company.name, raw?.name, raw?.company_name, raw?.companyName, ticker || "이름 없음"));
    const sector = String(firstDefined(company.sector, raw?.sector, raw?.sector_name, raw?.sectorName, "미분류"));
    const subIndustry = String(firstDefined(company.sub_industry, company.subIndustry, raw?.sub_industry, raw?.industry, ""));
    const componentRaw = firstArray(raw?.components, raw?.score_components, raw?.scoreComponents);
    const components = componentRaw.map(normalizeComponent).filter((item) => !isRemovedScoringMetric(item));
    const componentByMetric = new Map(components.map((item) => [normalizeKey(item.metricKey), item]));
    const metrics = {};
    Object.entries(METRIC_CONFIG).forEach(([nameKey, config]) => {
      metrics[nameKey] = normalizeMetric(raw, config, componentByMetric, context.sources);
    });
    const priceMetric = normalizeRawMetric(raw?.metrics?.current_price ?? raw?.current_price ?? raw?.price ?? raw?.currentPrice, "current_price", context.sources);
    const forwardPeMetric = normalizeRawMetric(raw?.metrics?.forward_pe ?? raw?.forward_pe ?? raw?.forwardPE ?? raw?.fwd_per, "forward_pe", context.sources);
    const peMedianMetric = normalizeRawMetric(raw?.metrics?.pe_3y_median, "pe_3y_median", context.sources);
    const dividendYieldAverageMetric = normalizeRawMetric(raw?.metrics?.dividend_yield_3y_avg_pct, "dividend_yield_3y_avg_pct", context.sources);
    const netBuybackAverageMetric = normalizeRawMetric(raw?.metrics?.net_buyback_yield_3y_avg_pct, "net_buyback_yield_3y_avg_pct", context.sources);
    const explicitCompanyScore = numberOrNull(firstDefined(raw?.company_score, raw?.companyScore));
    const explicitMarketScore = numberOrNull(firstDefined(raw?.market_score, raw?.marketScore, raw?.market_overlay?.score, context.market?.score));
    let score = numberOrNull(firstDefined(raw?.total_score, raw?.totalScore, raw?.score, raw?.signal_score, raw?.signalScore));
    if (score === null && components.length) score = components.reduce((sum, item) => sum + (item.points ?? 0), 0) + (explicitMarketScore ?? 0);
    if (score === null) {
      const availableScores = Object.values(metrics).map((metric) => metric.score).filter((value) => value !== null);
      if (availableScores.length) score = availableScores.reduce((sum, value) => sum + value, 0) + (explicitMarketScore ?? 0);
    }
    const explicitRecommendation = firstDefined(raw?.recommendation_label, raw?.recommendationLabel, raw?.recommendation, raw?.signal, raw?.judgement, raw?.rating);
    const coverageCount = numberOrNull(firstDefined(raw?.coverage_count, raw?.coverageCount));
    const coverageTotal = numberOrNull(firstDefined(raw?.coverage_total, raw?.coverageTotal));
    const coverage = normalizeCoverage(firstDefined(raw?.coverage_pct, raw?.coveragePct, raw?.coverage), coverageCount, coverageTotal);
    const explicitEligible = firstDefined(raw?.eligible_for_ranking, raw?.eligibleForRanking, raw?.has_required_data, raw?.hasRequiredData);
    const explicitInsufficient = raw?.insufficient === true || raw?.data_insufficient === true || explicitEligible === false || isInsufficientLabel(explicitRecommendation) || ["missing", "insufficient", "invalid"].includes(String(raw?.data_status || raw?.dataStatus || "").toLowerCase());
    const coreAvailable = Object.values(metrics).filter((metric) => metric.value !== null && !["missing", "invalid"].includes(metric.status) && metric.applicable !== false).length;
    const insufficient = explicitInsufficient || (score === null && coreAvailable < 5);
    const signalType = canonicalSignal(explicitRecommendation, score, insufficient);
    const warnings = firstArray(raw?.warnings, raw?.warning, raw?.errors).map(String);
    const missingMetrics = components.length
      ? components.filter((item) => item.applicable && item.points === null).map((item) => item.label)
      : Object.entries(metrics).filter(([, metric]) => metric.value === null).map(([key]) => METRIC_CONFIG[key].shortLabel);
    const sourceMap = normalizeStockSources(raw, metrics, context.sources);
    const updatedAt = firstDefined(raw?.as_of, raw?.asOf, priceMetric.asOf, ...Object.values(metrics).map((metric) => metric.asOf), context.asOf);
    return {
      raw,
      ticker,
      name,
      sector,
      subIndustry,
      score,
      companyScore: explicitCompanyScore,
      marketScore: explicitMarketScore ?? 0,
      signalType,
      recommendationLabel: signalType === "insufficient" ? SIGNALS.insufficient.label : (String(explicitRecommendation || "").trim() || SIGNALS[signalType].label),
      insufficient,
      coverage,
      coverageCount,
      coverageTotal,
      warnings,
      missingMetrics,
      components,
      metrics,
      price: priceMetric.value,
      priceMetric,
      forwardPE: forwardPeMetric.value,
      forwardPeMetric,
      peMedianMetric,
      dividendYieldAverageMetric,
      netBuybackAverageMetric,
      sources: sourceMap,
      history: normalizeDatedHistory(raw?.history),
      chartHistory: context.chartHistory || null,
      updatedAt,
    };
  }

  function normalizeMetric(raw, config, componentByMetric, fallbackSources) {
    const containers = [raw?.metrics, raw?.fundamentals, raw?.technicals, raw?.valuation, raw];
    let candidate;
    for (const container of containers) {
      candidate = getByAliases(container, config.aliases);
      if (candidate !== undefined) break;
    }
    const metric = normalizeRawMetric(candidate, config.key, fallbackSources);
    let component = componentByMetric.get(normalizeKey(config.key));
    if (!component) component = Array.from(componentByMetric.values()).find((item) => config.aliases.some((alias) => normalizeKey(alias) === normalizeKey(item.metricKey)));
    if (component) {
      metric.score = component.points;
      metric.reason = component.reason;
      metric.applicable = component.applicable;
      metric.status = component.metricStatus || metric.status;
      if (metric.value === null && component.value !== null) metric.value = component.value;
      metric.unit = component.unit || metric.unit;
      metric.label = component.label;
    } else {
      let scoreValue;
      for (const container of containers) {
        scoreValue = getByAliases(container, config.scoreAliases);
        if (scoreValue !== undefined) break;
      }
      if (scoreValue !== undefined) metric.score = numberOrNull(extractValue(scoreValue));
    }
    metric.label ||= config.label;
    metric.unit ||= config.unit;
    return metric;
  }

  function normalizeRawMetric(candidate, metricKey, fallbackSources) {
    const isObject = candidate && typeof candidate === "object" && !Array.isArray(candidate);
    const value = numberOrNull(isObject ? firstDefined(candidate.value, candidate.current, candidate.percent, candidate.pct, candidate.ratio, candidate.raw_value) : candidate);
    const provenance = isObject ? normalizeProvenance(candidate.provenance) : [];
    const fallbackSource = sourceFromFallback(metricKey, fallbackSources);
    return {
      key: metricKey,
      value,
      unit: isObject ? String(firstDefined(candidate.unit, "")) : "",
      status: isObject ? String(firstDefined(candidate.status, value === null ? "missing" : "ok")).toLowerCase() : (value === null ? "missing" : "ok"),
      asOf: isObject ? firstDefined(candidate.as_of, candidate.asOf, provenance[0]?.asOf) : null,
      provenance,
      source: provenance[0]?.source || fallbackSource || "출처 미표기",
      note: isObject ? firstDefined(candidate.note, candidate.notes, null) : null,
      score: isObject ? numberOrNull(firstDefined(candidate.points, candidate.score)) : null,
      reason: isObject ? firstDefined(candidate.reason, "") : "",
      applicable: isObject ? candidate.applicable !== false : true,
      label: "",
    };
  }

  function normalizeDatedHistory(raw) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
    const output = {};
    Object.entries(raw).forEach(([key, values]) => {
      if (!Array.isArray(values)) return;
      output[key] = values.map((item) => ({
        periodEnd: String(firstDefined(item?.period_end, item?.periodEnd, item?.date, item?.label, "")),
        value: numberOrNull(item?.value),
        periodType: String(firstDefined(item?.period_type, item?.periodType, "")),
        provenance: normalizeProvenance(item?.provenance),
      })).filter((item) => item.periodEnd && item.value !== null);
    });
    return output;
  }

  function normalizeChartHistory(payload) {
    const root = unwrapObject(payload, ["history", "chart", "data"]);
    const series = firstObject(root.series, {});
    const valuationRaw = firstObject(root.valuation, {});
    const valuationSeries = firstObject(valuationRaw.series, {});
    const normalizeSeries = (values) => firstArray(values).map((item) => ({
      date: String(firstDefined(item?.date, item?.period_end, item?.periodEnd, "")),
      value: numberOrNull(item?.value),
    })).filter((item) => item.date && item.value !== null);
    const normalizeValuationSeries = (values) => firstArray(values).map((item) => ({
      // Keep only the calendar date in the chart key.  The weekly provider
      // emits YYYY-MM-DD, while older caches may contain a timestamp suffix.
      // Legacy keys such as legacy_fy_minus_1 intentionally remain invalid
      // for the weekly chart and are filtered by weeklyValuationData().
      date: String(firstDefined(item?.date, item?.period_end, item?.periodEnd, "")).trim().slice(0, 10),
      value: numberOrNull(item?.value),
      anchorDate: String(firstDefined(item?.anchor_date, item?.anchorDate, "")),
    })).filter((item) => item.date && item.value !== null);
    return {
      symbol: String(firstDefined(root.symbol, "")),
      period: String(firstDefined(root.period, "3y")),
      asOf: firstDefined(root.as_of, root.asOf, null),
      cacheStatus: String(firstDefined(root.cache_status, root.cacheStatus, "")),
      adjustedClose: normalizeSeries(firstDefined(series.adjusted_close, series.adjustedClose, [])),
      rsi14: normalizeSeries(firstDefined(series.rsi_14, series.rsi14, [])),
      valuation: {
        period: String(firstDefined(valuationRaw.period, "5y")),
        frequency: String(firstDefined(valuationRaw.frequency, "")),
        asOf: firstDefined(valuationRaw.as_of, valuationRaw.asOf, null),
        coverageStart: firstDefined(valuationRaw.coverage_start, valuationRaw.coverageStart, null),
        cacheStatus: String(firstDefined(valuationRaw.cache_status, valuationRaw.cacheStatus, "")),
        anchorCount: numberOrNull(firstDefined(valuationRaw.anchor_count, valuationRaw.anchorCount)) ?? 0,
        trailingPeWeekly: normalizeValuationSeries(firstDefined(valuationSeries.trailing_pe_weekly, valuationSeries.trailingPeWeekly, [])),
        forwardPeWeekly: normalizeValuationSeries(firstDefined(valuationSeries.forward_pe_weekly, valuationSeries.forwardPeWeekly, [])),
        methodology: firstDefined(valuationRaw.methodology, null),
        limitations: firstDefined(valuationRaw.limitations, null),
        backtestSafe: valuationRaw.backtest_safe === true || valuationRaw.backtestSafe === true,
        provenance: normalizeProvenance(valuationRaw.provenance),
        error: firstDefined(valuationRaw.error, null),
      },
      provenance: normalizeProvenance(root.provenance),
      error: firstDefined(root.error, null),
    };
  }

  function normalizeComponent(raw) {
    return {
      key: String(firstDefined(raw?.key, raw?.id, raw?.name, "metric")),
      label: String(firstDefined(raw?.label, raw?.name, raw?.metric_key, "점수 항목")),
      metricKey: String(firstDefined(raw?.metric_key, raw?.metricKey, raw?.key, "")),
      value: numberOrNull(firstDefined(raw?.value, raw?.metric_value, raw?.metricValue)),
      unit: String(firstDefined(raw?.unit, "")),
      points: numberOrNull(firstDefined(raw?.points, raw?.score, raw?.point)),
      applicable: raw?.applicable !== false,
      reason: String(firstDefined(raw?.reason, raw?.description, "")),
      metricStatus: String(firstDefined(raw?.metric_status, raw?.metricStatus, raw?.status, "")).toLowerCase(),
    };
  }

  function isRemovedScoringMetric(raw) {
    const candidates = [raw?.key, raw?.metric_key, raw?.metricKey, raw?.label, raw?.name, raw?.title];
    return candidates.some((candidate) => {
      const key = normalizeKey(candidate);
      return key.includes("sectorrelativevalue")
        || key.includes("sectorvalue")
        || key.includes("relativemomentum")
        || key.includes("marketcap")
        || key.includes("marketcapitalization");
    });
  }

  function normalizeMarket(raw) {
    const source = firstObject(raw, {});
    const componentRaw = firstArray(source.components, source.score_components, source.rules);
    let components = componentRaw.map(normalizeComponent);
    if (!components.length) {
      const configs = [
        { key: "spy_drawdown", label: "SPY 52주 고점 대비 하락", aliases: ["spy_drawdown_52_week_pct", "spy_change", "spyChange", "spy_52w", "spy"] },
        { key: "vix_level", label: "VIX", aliases: ["vix_level", "vix", "vixLevel"] },
        { key: "fear_greed", label: "Fear & Greed", aliases: ["fear_greed_index", "fearGreed", "fear_greed", "fg_index"] },
      ];
      components = configs.map((config) => {
        const candidate = getByAliases(source.metrics || source, config.aliases);
        const metric = normalizeRawMetric(candidate, config.key, {});
        const scoreCandidate = getByAliases(source, [`${config.key}_score`, `${normalizeKey(config.key)}Score`]);
        return { key: config.key, label: config.label, metricKey: config.key, value: metric.value, unit: metric.unit, points: numberOrNull(scoreCandidate), applicable: true, reason: metric.note || "", metricStatus: metric.status };
      }).filter((item) => item.value !== null || item.points !== null);
    }
    let score = numberOrNull(firstDefined(source.score, source.total_score, source.totalScore, source.market_score, source.marketScore));
    if (score === null) score = components.reduce((sum, item) => sum + (item.points ?? 0), 0);
    return {
      raw: source,
      components,
      score: score ?? 0,
      complete: source.complete !== false && components.every((item) => item.points !== null),
      label: String(firstDefined(source.label, source.verdict, source.recommendation, marketVerdict(score ?? 0))),
    };
  }

  function normalizeRules(payload) {
    const root = unwrapObject(payload, ["rules", "rule_definitions", "scoring"]);
    const company = firstArray(root.company_rules, root.companyRules, root.stock_rules, root.stockRules, Array.isArray(root.rules) ? root.rules : null)
      .filter((item) => !isRemovedScoringMetric(item));
    const market = firstArray(root.market_rules, root.marketRules);
    const normalizeOne = (raw, category, index) => {
      const bandsRaw = firstArray(raw.thresholds, raw.bands, raw.ranges, raw.conditions);
      const bands = bandsRaw.map((band) => ({
        condition: String(firstDefined(band.condition, band.label, band.range, `${band.operator ?? ""} ${band.value ?? ""}`)).trim(),
        operator: String(firstDefined(band.operator, "")),
        value: firstDefined(band.value, band.threshold, null),
        score: numberOrNull(firstDefined(band.points, band.score, band.point)) ?? 0,
      }));
      if (raw.default_points !== undefined || raw.defaultScore !== undefined) bands.push({ condition: "그 외 구간", operator: "default", value: null, score: numberOrNull(firstDefined(raw.default_points, raw.defaultScore)) ?? 0 });
      return {
        key: String(firstDefined(raw.key, raw.id, `rule-${category}-${index}`)),
        label: String(firstDefined(raw.label, raw.name, raw.title, raw.metric_key, "점수 규칙")),
        metricKey: String(firstDefined(raw.metric_key, raw.metricKey, raw.key, "")),
        unit: String(firstDefined(raw.unit, "number")),
        description: String(firstDefined(raw.note, raw.description, raw.explanation, "정해진 임계값에 따라 기계적으로 점수를 부여합니다.")),
        source: String(firstDefined(raw.source, raw.provider, "서버 점수 엔진")),
        weight: firstDefined(raw.weight, raw.max_points, raw.maxPoints, null),
        exclusions: firstArray(raw.sector_exclusions, raw.exclusions).map(String),
        category,
        bands,
      };
    };
    const rules = [...company.map((item, index) => normalizeOne(item, "company", index)), ...market.map((item, index) => normalizeOne(item, "market", index))];
    return {
      rules,
      meta: {
        version: String(firstDefined(root.version, root.rule_version, root.ruleVersion, "버전 미표기")),
        minimumMetrics: numberOrNull(firstDefined(root.minimum_company_metrics, root.minimumCompanyMetrics)),
        missingPolicy: firstObject(root.missing_data_policy, root.missingDataPolicy, {}),
        demo: false,
      },
    };
  }

  function renderDashboard() {
    const dashboard = state.dashboard;
    if (!dashboard) return;
    const asOfText = formatDate(dashboard.asOf, { includeTime: false });
    const generatedText = formatDate(dashboard.generatedAt, { includeTime: true });
    el.asOfHeader.textContent = state.isDemo ? "DEMO 데이터" : `${asOfText} 기준`;
    el.asOfHero.textContent = state.isDemo ? `DEMO · ${asOfText} 화면 예시` : `${asOfText} 기준 · ${generatedText} 계산`;
    el.universeLabel.textContent = `S&P 500 · 전체 ${formatInteger(dashboard.universeCount)}개 종목`;
    document.querySelector(".status-dot")?.classList.toggle("is-demo", state.isDemo);
    renderSummary();
    renderCoverage();
    renderRecommendations();
    renderMarket();
    populateSectorFilter();
    renderSectorExplorer();
    populateDetailPicker();
    applyFilters();
  }

  function renderSummary() {
    const counts = Object.keys(SIGNALS).reduce((acc, key) => ({ ...acc, [key]: 0 }), {});
    state.stocks.forEach((stock) => { counts[stock.signalType] = (counts[stock.signalType] || 0) + 1; });
    const order = ["strong-buy", "buy-watch", "neutral", "sell-watch", "sell", "insufficient"];
    el.summaryGrid.innerHTML = order.map((type) => {
      const meta = SIGNALS[type];
      return `
        <article class="summary-card is-${type}">
          <span class="summary-range">${escapeHTML(meta.range)}</span>
          <div class="summary-main">
            <div><strong>${escapeHTML(meta.label)}</strong><span>${escapeHTML(meta.helper)}</span></div>
            <strong class="summary-count">${formatInteger(counts[type] || 0)}<small>종목</small></strong>
          </div>
        </article>`;
    }).join("");
  }

  function renderCoverage() {
    const dashboard = state.dashboard;
    const valid = dashboard.eligibleCount;
    const total = dashboard.stocks.length || dashboard.returnedCount || dashboard.universeCount;
    const coverage = dashboard.coverage ?? (total ? valid / total * 100 : null);
    el.coverageValue.textContent = coverage === null ? `${formatInteger(valid)} / ${formatInteger(total)}` : `${formatPercentNumber(coverage, 1)} · ${formatInteger(valid)}/${formatInteger(total)}`;
    el.priceSourceValue.textContent = dashboard.sources.price || "출처 미표기";
    el.fundamentalSourceValue.textContent = dashboard.sources.fundamentals || "출처 미표기";
    el.coverageAsOf.textContent = formatDate(dashboard.asOf, { includeTime: false });
    [el.priceSourceValue, el.fundamentalSourceValue].forEach((node) => { node.title = node.textContent; });
  }

  function renderRecommendations() {
    const candidates = state.stocks
      .filter((stock) => !stock.insufficient && stock.score !== null && stock.score >= 5)
      .sort((a, b) => compareNullable(a.score, b.score, -1) || compareNullable(a.metrics.drawdown.value, b.metrics.drawdown.value, 1) || a.ticker.localeCompare(b.ticker))
      .slice(0, 9);
    if (!candidates.length) {
      el.recommendedTickers.innerHTML = '<span class="panel-empty">현재 +5점 이상이며 데이터가 충분한 종목이 없습니다.</span>';
      return;
    }
    el.recommendedTickers.innerHTML = candidates.map((stock) => `
      <button class="ticker-pill" type="button" data-open-stock="${escapeAttr(stock.ticker)}" aria-label="${escapeAttr(stock.ticker)} ${escapeAttr(stock.name)} 상세 보기">
        <span>${escapeHTML(stock.ticker)}</span>
        <span class="pill-score">${formatSigned(stock.score)}</span>
        <span class="pill-company">${escapeHTML(stock.name)}</span>
      </button>`).join("");
  }

  function renderMarket() {
    const market = state.dashboard.market;
    const score = market.score ?? 0;
    const tone = score >= 2 ? "positive" : score <= -2 ? "negative" : "neutral";
    el.marketVerdict.className = `market-verdict is-${tone}`;
    el.marketVerdict.textContent = market.label || marketVerdict(score);
    el.marketTotalScore.textContent = formatSigned(score);
    if (!market.components.length) {
      el.marketMetrics.innerHTML = '<div class="metric-line"><span>시장 지표</span><strong>데이터 없음</strong><span class="score-chip is-zero">—</span></div>';
      return;
    }
    el.marketMetrics.innerHTML = market.components.slice(0, 4).map((component) => `
      <div class="metric-line">
        <span>${escapeHTML(component.label)}</span>
        <strong>${formatByUnit(component.value, component.unit)}</strong>
        ${scoreChip(component.points, component.value === null, component.applicable)}
      </div>`).join("");
  }

  function populateSectorFilter() {
    const current = state.filters.sector;
    const sectors = Array.from(new Set(state.stocks.map((stock) => stock.sector).filter(Boolean))).sort((a, b) => a.localeCompare(b, "ko"));
    const themeOptions = Object.entries(THEME_FILTERS).map(([value, meta]) => `<option value="${escapeAttr(value)}">${escapeHTML(meta.label)}</option>`).join("");
    el.sectorFilter.innerHTML = '<option value="all">전체 섹터</option>' + themeOptions + sectors.map((sector) => `<option value="${escapeAttr(sector)}">${escapeHTML(sectorLabel(sector))}</option>`).join("");
    state.filters.sector = sectors.includes(current) || THEME_FILTERS[current] ? current : "all";
    el.sectorFilter.value = state.filters.sector;
  }

  function populateDetailPicker() {
    const current = state.selectedTicker || el.detailStockSelect.value;
    const ordered = [...state.stocks].sort((a, b) => a.ticker.localeCompare(b.ticker));
    el.detailStockSelect.innerHTML = '<option value="">종목을 선택하세요</option>' + ordered.map((stock) => `<option value="${escapeAttr(stock.ticker)}">${escapeHTML(stock.ticker)} · ${escapeHTML(stock.name)}</option>`).join("");
    if (ordered.some((stock) => stock.ticker === current)) el.detailStockSelect.value = current;
  }

  function renderSectorExplorer() {
    if (!el.themeCardGrid || !el.sectorCardGrid) return;
    const themes = Object.entries(THEME_FILTERS).map(([value, meta]) => ({
      value,
      label: meta.label,
      subtitle: meta.subtitle,
      stocks: state.stocks.filter((stock) => matchesSectorFilter(stock, value)),
      theme: true,
    }));
    el.themeCardGrid.innerHTML = themes.map(sectorCardMarkup).join("");

    const available = Array.from(new Set(state.stocks.map((stock) => stock.sector).filter(Boolean)));
    const ordered = [
      ...SECTOR_ORDER.filter((sector) => available.includes(sector)),
      ...available.filter((sector) => !SECTOR_ORDER.includes(sector)).sort((a, b) => a.localeCompare(b, "ko")),
    ];
    el.sectorCardGrid.innerHTML = ordered.map((sector) => sectorCardMarkup({
      value: sector,
      label: sectorLabel(sector),
      subtitle: SECTOR_LABELS[sector] ? sector : "현재 분류",
      stocks: state.stocks.filter((stock) => stock.sector === sector),
      theme: false,
    })).join("");
  }

  function sectorCardMarkup(group) {
    const scored = group.stocks.filter((stock) => !stock.insufficient && stock.score !== null);
    const average = scored.length ? scored.reduce((sum, stock) => sum + stock.score, 0) / scored.length : null;
    const candidates = scored.filter((stock) => stock.score >= 5).length;
    const top = [...scored].sort(sorterFor("score-desc")).slice(0, 3);
    const topMarkup = top.length
      ? top.map((stock) => `<span><b>${escapeHTML(stock.ticker)}</b>${escapeHTML(formatSigned(stock.score))}</span>`).join("")
      : '<span class="sector-no-data">점수 데이터 없음</span>';
    return `
      <button class="sector-card${group.theme ? " is-theme" : ""}" type="button" data-sector-group="${escapeAttr(group.value)}" aria-label="${escapeAttr(group.label)} 종목 보드 열기">
        <span class="sector-card-index" aria-hidden="true">${escapeHTML(sectorMonogram(group.label))}</span>
        <span class="sector-card-heading"><strong>${escapeHTML(group.label)}</strong><small>${escapeHTML(group.subtitle)}</small></span>
        <span class="sector-card-stats">
          <span><small>종목</small><strong>${formatInteger(group.stocks.length)}</strong></span>
          <span><small>평균 점수</small><strong>${average === null ? "—" : formatSigned(average, 1)}</strong></span>
          <span><small>+5 이상</small><strong>${formatInteger(candidates)}</strong></span>
        </span>
        <span class="sector-card-leaders">${topMarkup}</span>
        <span class="sector-card-action">종목 보기 <span aria-hidden="true">→</span></span>
      </button>`;
  }

  function openSectorGroup(value) {
    if (value !== "all" && !THEME_FILTERS[value] && !state.stocks.some((stock) => stock.sector === value)) return;
    // A sector card represents the whole group. Clear board-only narrowing
    // from a previous visit so opening M7, Energy, etc. cannot unexpectedly
    // show just the old ticker search or old recommendation slice.
    state.filters.search = "";
    state.filters.signal = "all";
    state.filters.sector = value;
    el.stockSearch.value = "";
    el.signalFilter.value = "all";
    el.sectorFilter.value = value;
    showView("board");
    applyFilters();
    window.setTimeout(() => document.getElementById("screener")?.scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "start" }), 120);
  }

  function matchesSectorFilter(stock, value) {
    if (!value || value === "all") return true;
    if (value === "theme:m7") return M7_TICKERS.has(stock.ticker);
    if (value === "theme:software") {
      return /software/i.test(`${stock.subIndustry} ${stock.sector}`) || stock.sector === "소프트웨어";
    }
    return stock.sector === value;
  }

  function sectorLabel(value) {
    return SECTOR_LABELS[value] || value || "미분류";
  }

  function filterLabel(value) {
    return THEME_FILTERS[value]?.label || sectorLabel(value);
  }

  function sectorMonogram(label) {
    const text = String(label).replace(/\([^)]*\)/g, "").trim();
    if (/^[A-Za-z0-9]/.test(text)) return text.replace(/[^A-Za-z0-9]/g, "").slice(0, 2).toUpperCase();
    return text.slice(0, 2) || "S";
  }

  function applyFilters() {
    const query = state.filters.search.toLocaleLowerCase("ko");
    let stocks = state.stocks.filter((stock) => {
      const matchesQuery = !query || `${stock.ticker} ${stock.name} ${stock.sector} ${stock.subIndustry}`.toLocaleLowerCase("ko").includes(query);
      const matchesSector = matchesSectorFilter(stock, state.filters.sector);
      const matchesSignal = state.filters.signal === "all" || stock.signalType === state.filters.signal;
      return matchesQuery && matchesSector && matchesSignal;
    });
    stocks = stocks.sort(sorterFor(state.filters.sort));
    state.filteredStocks = stocks;
    state.tableVisibleCount = tableBatchSize();
    syncSortSelect();
    renderActiveFilters();
    renderTable();
    updateSortHeaders();
  }

  function sorterFor(sort) {
    if (sort === "score-asc") return scoreSorter(1);
    if (sort === "score-desc") return scoreSorter(-1);
    const tableMode = TABLE_SORT_MODES.get(sort);
    if (!tableMode) return scoreSorter(-1);
    const direction = tableMode.direction === "asc" ? 1 : -1;
    return (a, b) => compareSortValues(
      tableMode.definition.value(a),
      tableMode.definition.value(b),
      tableMode.definition.type,
      direction,
    ) || compareTicker(a, b);
  }

  function scoreSorter(direction) {
    const tieBreak = (a, b) => compareNullable(numericMetricValue(a.metrics?.drawdown), numericMetricValue(b.metrics?.drawdown), 1)
      || compareNullable(numericMetricValue(a.metrics?.epsGrowth), numericMetricValue(b.metrics?.epsGrowth), -1)
      || compareTicker(a, b);
    const eligibleFirst = (a, b) => Number(a.insufficient) - Number(b.insufficient);
    return (a, b) => eligibleFirst(a, b) || compareNullable(a.score, b.score, direction) || tieBreak(a, b);
  }

  function compareSortValues(left, right, type, direction = 1) {
    if (type === "text") {
      const leftText = left === null || left === undefined || String(left).trim() === "" ? null : String(left);
      const rightText = right === null || right === undefined || String(right).trim() === "" ? null : String(right);
      if (leftText === null && rightText === null) return 0;
      if (leftText === null) return 1;
      if (rightText === null) return -1;
      return SORT_COLLATOR.compare(leftText, rightText) * direction;
    }
    const leftNumber = numberOrNull(left);
    const rightNumber = numberOrNull(right);
    if (leftNumber === null && rightNumber === null) return 0;
    if (leftNumber === null) return 1;
    if (rightNumber === null) return -1;
    return (leftNumber - rightNumber) * direction;
  }

  function compareTicker(a, b) {
    return compareSortValues(a?.ticker, b?.ticker, "text", 1);
  }

  function numericMetricValue(metric, fallback = null) {
    const source = metric && typeof metric === "object" ? metric : { value: metric ?? fallback, status: "ok" };
    const value = numberOrNull(source.value);
    if (value === null || ["missing", "invalid"].includes(String(source.status || "").toLowerCase())) return null;
    return value;
  }

  function syncSortSelect() {
    if (!el.sortSelect) return;
    const hasOption = Array.from(el.sortSelect.options).some((option) => option.value === state.filters.sort);
    if (!hasOption) state.filters.sort = "score-desc";
    el.sortSelect.value = state.filters.sort;
  }

  function renderTable() {
    const stocks = state.filteredStocks;
    el.filteredCount.textContent = formatInteger(stocks.length);
    el.tableShell.setAttribute("aria-busy", "false");
    el.tableLoading.hidden = true;
    if (!stocks.length) {
      el.stockTableBody.innerHTML = "";
      el.tablePagination.hidden = true;
      el.emptyState.hidden = false;
      const hasData = state.stocks.length > 0;
      el.emptyTitle.textContent = hasData ? "조건에 맞는 종목이 없습니다" : "표시할 종목 데이터가 없습니다";
      el.emptyMessage.textContent = hasData ? "검색어나 필터를 조금 넓혀보세요." : "API는 응답했지만 종목 목록이 비어 있습니다. 데이터 적재 상태를 확인하세요.";
      el.emptyReset.hidden = !hasData;
      return;
    }
    el.emptyState.hidden = true;
    const visibleCount = Math.min(Math.max(state.tableVisibleCount, tableBatchSize()), stocks.length);
    const visibleStocks = stocks.slice(0, visibleCount);
    state.tableVisibleCount = visibleCount;
    el.stockTableBody.innerHTML = visibleStocks.map(renderTableRow).join("");
    const remaining = stocks.length - visibleCount;
    el.tablePagination.hidden = remaining <= 0;
    el.tablePaginationStatus.textContent = `${formatInteger(visibleCount)} / ${formatInteger(stocks.length)}개 표시`;
    el.tableLoadMore.textContent = remaining > 0
      ? `다음 ${formatInteger(Math.min(tableBatchSize(), remaining))}개 더 보기`
      : "모두 표시됨";
  }

  function tableBatchSize() {
    return window.matchMedia("(max-width: 720px)").matches ? TABLE_BATCH_MOBILE : TABLE_BATCH_DESKTOP;
  }

  function renderTableRow(stock) {
    const signal = SIGNALS[stock.signalType];
    const accessible = `${stock.ticker} ${stock.name}, ${signal.label}, 총점 ${stock.score === null ? "없음" : formatSigned(stock.score)}`;
    return `
      <tr data-ticker="${escapeAttr(stock.ticker)}" tabindex="0" aria-label="${escapeAttr(accessible)} 상세 보기">
        <td data-label="종목">
          <button class="stock-cell-button" type="button" data-open-stock="${escapeAttr(stock.ticker)}" aria-label="${escapeAttr(stock.ticker)} 상세 보기">
            <span class="ticker-avatar" aria-hidden="true">${escapeHTML(avatarText(stock.ticker))}</span>
            <span class="stock-cell-copy"><strong>${escapeHTML(stock.ticker)}</strong><span title="${escapeAttr(stock.name)}">${escapeHTML(stock.name)}</span></span>
          </button>
        </td>
        <td data-label="섹터"><span class="sector-text" title="${escapeAttr(stock.sector)}">${escapeHTML(stock.sector)}</span></td>
        <td data-label="판단">${signalBadge(stock)}</td>
        <td data-label="고점 대비 하락">${metricCell(stock.metrics.drawdown, formatPercent)}</td>
        <td data-label="현재 주가">${plainMetricCell(stock.priceMetric, formatPrice, stock.price)}</td>
        <td data-label="FWD PER">${plainMetricCell(stock.forwardPeMetric, formatMultiple, stock.forwardPE)}</td>
        <td data-label="3Y PER 괴리">${metricCell(stock.metrics.peGap, formatPercent)}</td>
        <td data-label="200일선 이격">${metricCell(stock.metrics.distance200, formatPercent)}</td>
        <td data-label="EPS 성장">${metricCell(stock.metrics.epsGrowth, formatPercent)}</td>
        <td data-label="RSI">${metricCell(stock.metrics.rsi, formatIndex)}</td>
        <td data-label="현금흐름 품질">${metricCell(stock.metrics.cashFlowQuality, formatRatio)}</td>
        <td data-label="부채비율">${metricCell(stock.metrics.debt, formatRatio)}</td>
        <td data-label="3Y 주주환원">${metricCell(stock.metrics.shareholderReturn, formatPercent)}</td>
        <td data-label="3Y 주식수">${metricCell(stock.metrics.sharesChange, formatPercent)}</td>
        <td data-label="상세"><button class="details-button" type="button" data-open-stock="${escapeAttr(stock.ticker)}" aria-label="${escapeAttr(stock.ticker)} 점수 근거 열기"><span>점수 근거 보기</span><svg viewBox="0 0 20 20" aria-hidden="true"><path d="M7 4.5 12.5 10 7 15.5"></path></svg></button></td>
      </tr>`;
  }

  function renderActiveFilters() {
    const chips = [];
    if (state.filters.search) chips.push({ key: "search", label: `검색: ${state.filters.search}` });
    if (state.filters.sector !== "all") chips.push({ key: "sector", label: filterLabel(state.filters.sector) });
    if (state.filters.signal !== "all") chips.push({ key: "signal", label: SIGNALS[state.filters.signal]?.label || state.filters.signal });
    if (state.filters.sort !== "score-desc") chips.push({ key: "sort", label: el.sortSelect.options[el.sortSelect.selectedIndex]?.text || "정렬" });
    el.activeFilterRow.hidden = chips.length === 0;
    el.activeFilterRow.innerHTML = chips.map((chip) => `
      <span class="filter-chip">${escapeHTML(chip.label)}<button type="button" data-clear-filter="${escapeAttr(chip.key)}" aria-label="${escapeAttr(chip.label)} 필터 해제">×</button></span>`).join("");
    el.resetFilters.disabled = chips.length === 0;
  }

  function updateSortHeaders() {
    const activeMode = TABLE_SORT_MODES.get(state.filters.sort);
    el.columnSorts.forEach((button) => {
      const definition = TABLE_SORT_DEFINITIONS.find((item) => item.key === button.dataset.sortKey);
      if (!definition) return;
      const active = activeMode?.definition.key === definition.key;
      const direction = active ? activeMode.direction : (button.dataset.defaultDirection || definition.defaultDirection);
      const nextDirection = direction === "asc" ? "desc" : "asc";
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
      button.setAttribute("aria-label", active
        ? `${definition.label} 기준 ${sortDirectionLabel(direction, definition)} 정렬 중. 클릭하면 ${sortDirectionLabel(nextDirection, definition)}으로 전환`
        : `${definition.label} 기준 ${sortDirectionLabel(direction, definition)} 정렬`);
      button.title = active
        ? `현재 ${sortDirectionLabel(direction, definition)} · 클릭하여 ${sortDirectionLabel(nextDirection, definition)}으로 전환`
        : `${sortDirectionLabel(direction, definition)} 정렬`;
      const indicator = button.querySelector(".sort-indicator");
      if (indicator) indicator.textContent = active ? (direction === "asc" ? "↑" : "↓") : "↕";
      const th = button.closest("th");
      if (th) th.setAttribute("aria-sort", active ? (direction === "asc" ? "ascending" : "descending") : "none");
    });
  }

  function sortDirectionLabel(direction, definition = null) {
    if (definition?.key === "judgement") return direction === "asc" ? "강한 매수부터" : "데이터 부족부터";
    return direction === "asc" ? "오름차순" : "내림차순";
  }

  function resetFilters() {
    window.clearTimeout(state.searchTimer);
    state.filters = { search: "", sector: "all", signal: "all", sort: "score-desc" };
    el.stockSearch.value = "";
    el.sectorFilter.value = "all";
    el.signalFilter.value = "all";
    el.sortSelect.value = "score-desc";
    applyFilters();
    el.stockSearch.focus();
  }

  function clearFilter(key) {
    window.clearTimeout(state.searchTimer);
    if (key === "search") { state.filters.search = ""; el.stockSearch.value = ""; }
    if (key === "sector") { state.filters.sector = "all"; el.sectorFilter.value = "all"; }
    if (key === "signal") { state.filters.signal = "all"; el.signalFilter.value = "all"; }
    if (key === "sort") { state.filters.sort = "score-desc"; el.sortSelect.value = "score-desc"; }
    applyFilters();
  }

  function renderRulesLoading() {
    el.rulesError.hidden = true;
    el.rulesUpdated.textContent = "규칙 불러오는 중";
    el.rulesGrid.innerHTML = '<article class="rule-card skeleton-block"></article><article class="rule-card skeleton-block"></article><article class="rule-card skeleton-block"></article>';
  }

  function renderRules() {
    el.rulesError.hidden = true;
    const meta = state.rulesMeta || {};
    el.rulesUpdated.textContent = `${meta.demo ? "DEMO 예시 규칙 · " : ""}v${meta.version}${meta.minimumMetrics ? ` · 최소 ${meta.minimumMetrics}개 지표` : ""}`;
    if (!state.rules.length) {
      el.rulesGrid.innerHTML = '<div class="rules-error"><strong>표시할 점수 규칙이 없습니다.</strong></div>';
      return;
    }
    el.rulesGrid.innerHTML = state.rules.map((rule, index) => `
      <article class="rule-card">
        <div class="rule-card-header">
          <span class="rule-number">${String(index + 1).padStart(2, "0")}</span>
          <div class="rule-title-wrap">
            <h3>${escapeHTML(rule.label)}</h3>
            <p>${escapeHTML(rule.category === "market" ? "시장 공통 점수" : rule.metricKey)}</p>
          </div>
          <span class="rule-weight">${rule.category === "market" ? "MARKET" : formatRuleRange(rule)}</span>
        </div>
        <div class="rule-bands">
          ${rule.bands.length ? rule.bands.map((band) => `<div class="rule-band"><span>${escapeHTML(formatBandCondition(band, rule.unit))}</span>${scoreChip(band.score, false, true)}</div>`).join("") : '<div class="rule-band"><span>서버 제공 조건</span><span class="score-chip is-zero">—</span></div>'}
        </div>
        ${rule.exclusions.length ? `<p class="rule-source">적용 제외: ${escapeHTML(rule.exclusions.join(", "))}</p>` : ""}
        <p class="rule-source">${escapeHTML(rule.description)} · 출처: ${escapeHTML(rule.source)}</p>
      </article>`).join("");
  }

  function showView(view, { updateHash = true, focus = true, scroll = true } = {}) {
    const allowed = ["board", "sectors", "detail", "rules", "portfolio"];
    const next = allowed.includes(view) ? view : "board";
    state.activeView = next;
    el.viewPanels.forEach((panel) => {
      const active = panel.dataset.viewPanel === next;
      panel.hidden = !active;
      panel.classList.toggle("is-active", active);
    });
    el.navItems.forEach((button) => {
      const active = button.dataset.view === next;
      button.classList.toggle("is-active", active);
      if (active) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    if (updateHash && window.location.hash !== `#${next}`) history.pushState(null, "", `#${next}`);
    if (next === "rules" && !state.rulesLoaded && !state.rulesError) loadRules();
    if (next === "detail" && state.selectedTicker) loadDetailPage(state.selectedTicker, { fetchRemote: true });
    if (next === "portfolio") loadPortfolioState();
    if (scroll) window.scrollTo({ top: 0, behavior: reducedMotion() ? "auto" : "smooth" });
    if (focus) window.setTimeout(() => el.mainContent.focus({ preventScroll: true }), reducedMotion() ? 0 : 180);
  }

  function getViewFromHash() {
    const value = window.location.hash.replace(/^#/, "");
    return ["board", "sectors", "detail", "rules", "portfolio"].includes(value) ? value : "board";
  }

  function handleTableClick(event) {
    const opener = event.target.closest("[data-open-stock]");
    if (opener) {
      openStockDrawer(opener.dataset.openStock, opener);
      return;
    }
    const row = event.target.closest("tr[data-ticker]");
    if (row) openStockDrawer(row.dataset.ticker, row);
  }

  function handleTableKeydown(event) {
    if (event.target.matches("tr[data-ticker]") && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      openStockDrawer(event.target.dataset.ticker, event.target);
    }
  }

  function handleDetailPageClick(event) {
    const tabButton = event.target.closest("[data-analysis-tab]");
    if (!tabButton) return;
    activateAnalysisTab(tabButton.dataset.analysisTab);
  }

  function handleDetailPageKeydown(event) {
    const tabButton = event.target.closest("[data-analysis-tab]");
    if (!tabButton || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const buttons = Array.from(el.detailPageContent.querySelectorAll("[data-analysis-tab]"));
    const currentIndex = buttons.indexOf(tabButton);
    if (currentIndex < 0 || !buttons.length) return;
    event.preventDefault();
    let nextIndex = currentIndex;
    if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = buttons.length - 1;
    else if (event.key === "ArrowLeft") nextIndex = (currentIndex - 1 + buttons.length) % buttons.length;
    else if (event.key === "ArrowRight") nextIndex = (currentIndex + 1) % buttons.length;
    activateAnalysisTab(buttons[nextIndex].dataset.analysisTab);
  }

  function activateAnalysisTab(tabName, { focus = true } = {}) {
    const allowed = ["judgement", "valuation", "performance", "finance", "outlook"];
    const next = allowed.includes(tabName) ? tabName : "judgement";
    state.detailTab = next;
    const buttons = Array.from(el.detailPageContent.querySelectorAll("[data-analysis-tab]"));
    const panels = Array.from(el.detailPageContent.querySelectorAll("[data-analysis-panel]"));
    buttons.forEach((button) => {
      const active = button.dataset.analysisTab === next;
      button.setAttribute("aria-selected", active ? "true" : "false");
      button.tabIndex = active ? 0 : -1;
      button.classList.toggle("is-active", active);
      if (active && focus) button.focus({ preventScroll: true });
    });
    panels.forEach((panel) => {
      const active = panel.dataset.analysisPanel === next;
      panel.hidden = !active;
      panel.classList.toggle("is-active", active);
    });
  }

  function handleDrawerBodyClick(event) {
    const opener = event.target.closest("[data-open-full-detail]");
    if (!opener) return;
    state.selectedTicker = String(opener.dataset.openFullDetail || "").toUpperCase();
    state.detailTab = "judgement";
    closeStockDrawer();
    showView("detail");
  }

  function handleGlobalKeydown(event) {
    if (event.key === "Escape" && !el.stockDrawer.hidden) {
      closeStockDrawer();
      return;
    }
    if (event.key === "Tab" && !el.stockDrawer.hidden) trapDrawerFocus(event);
    if (event.key === "/" && state.activeView === "board" && !isTypingTarget(event.target)) {
      event.preventDefault();
      el.stockSearch.focus();
    }
  }

  async function openStockDrawer(ticker, trigger = null) {
    const base = state.stocks.find((stock) => stock.ticker === String(ticker).toUpperCase());
    if (!base) return;
    state.selectedTicker = base.ticker;
    state.lastFocused = trigger || document.activeElement;
    el.drawerTitle.textContent = `${base.ticker} 데이터 흐름`;
    el.drawerBody.innerHTML = '<div class="drawer-loading"><div class="loading-spinner" aria-hidden="true"></div><span>5년 주간 밸류에이션과 점수 근거를 확인하고 있어요</span></div>';
    el.drawerBackdrop.hidden = false;
    el.stockDrawer.hidden = false;
    document.body.classList.add("has-drawer");
    window.setTimeout(() => el.drawerClose.focus(), 20);
    const { stock, error, historyError } = await getDetailedStock(base);
    if (el.stockDrawer.hidden || state.selectedTicker !== base.ticker) return;
    el.drawerTitle.textContent = `${stock.ticker} 데이터 흐름`;
    el.drawerBody.innerHTML = renderDrawerContent(stock, error, historyError);
  }

  function closeStockDrawer() {
    if (el.stockDrawer.hidden) return;
    el.stockDrawer.hidden = true;
    el.drawerBackdrop.hidden = true;
    document.body.classList.remove("has-drawer");
    const focusTarget = state.lastFocused;
    state.lastFocused = null;
    if (focusTarget && typeof focusTarget.focus === "function" && document.contains(focusTarget)) focusTarget.focus();
  }

  async function getDetailedStock(base) {
    if (state.isDemo) return { stock: base, error: null, historyError: null };
    const [detailResult, historyResult] = await Promise.allSettled([
      fetchJSON(API.stock(base.ticker), {}, 14000),
      fetchJSON(API.history(base.ticker), {}, 32000),
    ]);
    let raw = base.raw;
    let error = null;
    let historyError = null;
    if (detailResult.status === "fulfilled") {
      raw = mergeStockRaw(base.raw, extractStockPayload(detailResult.value, base.ticker));
    } else {
      error = detailResult.reason;
    }
    const chartHistory = historyResult.status === "fulfilled" ? normalizeChartHistory(historyResult.value) : null;
    if (historyResult.status === "rejected") historyError = historyResult.reason;
    else if (chartHistory?.error) historyError = new Error(String(chartHistory.error));
    else if (chartHistory?.cacheStatus === "stale") historyError = new Error("최신 가격 이력 갱신에 실패해 마지막 성공 캐시를 표시합니다.");
    const stock = normalizeStock(raw, {
      market: state.dashboard.market,
      asOf: state.dashboard.asOf,
      sources: state.dashboard.sources,
      chartHistory,
    });
    return { stock, error, historyError };
  }

  async function loadDetailPage(ticker, { fetchRemote = true } = {}) {
    const base = state.stocks.find((stock) => stock.ticker === String(ticker).toUpperCase());
    if (!base) return;
    state.selectedTicker = base.ticker;
    el.detailStockSelect.value = base.ticker;
    document.getElementById("view-detail")?.classList.add("has-selection");
    el.detailPageContent.setAttribute("aria-busy", "true");
    el.detailPageContent.innerHTML = '<div class="blank-detail"><div class="loading-spinner" aria-hidden="true"></div><strong>종목 분석을 불러오는 중입니다</strong><p>구성 점수와 데이터 원천을 함께 확인합니다.</p></div>';
    const result = fetchRemote ? await getDetailedStock(base) : { stock: base, error: null, historyError: null };
    if (state.selectedTicker !== base.ticker) return;
    el.detailPageContent.setAttribute("aria-busy", "false");
    el.detailPageContent.classList.add("is-populated");
    el.detailPageContent.innerHTML = renderFullDetail(result.stock, result.error, result.historyError);
    activateAnalysisTab(state.detailTab, { focus: false });
  }

  function renderDrawerContent(stock, error, historyError) {
    return `
      ${stockIdentityMarkup(stock)}
      ${totalScoreMarkup(stock)}
      <button class="drawer-full-detail" type="button" data-open-full-detail="${escapeAttr(stock.ticker)}">
        <span><strong>전체 분석 화면</strong><small>판단 · 가치 · 실적 · 재무 · 전망</small></span>
        <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M7 4.5 12.5 10 7 15.5"></path></svg>
      </button>
      ${error ? `<div class="detail-api-warning">상세 API에 연결하지 못해 보드에 이미 수신된 값만 표시합니다. ${escapeHTML(cleanError(error))}</div>` : ""}
      ${historyChartsMarkup(stock, historyError, "drawer")}
      <section class="detail-section" aria-labelledby="drawer-breakdown-heading">
        <div class="detail-section-title"><h4 id="drawer-breakdown-heading">종목 점수 분해</h4><span>값 · 판정 근거 · 점수</span></div>
        <div class="breakdown-list">${breakdownMarkup(stock)}</div>
      </section>
      <section class="detail-section" aria-labelledby="drawer-source-heading">
        <div class="detail-section-title"><h4 id="drawer-source-heading">데이터 커버리지와 출처</h4><span>감사 가능한 원천 정보</span></div>
        ${provenanceMarkup(stock)}
      </section>
      ${warningsMarkup(stock)}
      <p class="detail-note"><strong>기계적 연구 보조 점수이며 투자 조언이 아닙니다.</strong> 큰 낙폭은 떨어지는 칼, 낮은 PER은 가치 함정일 수 있습니다. 기업 고유 위험과 최신 공시를 별도로 확인하세요.</p>`;
  }

  function renderFullDetail(stock, error, historyError) {
    const tabs = [
      { key: "judgement", number: "01", label: "판단" },
      { key: "valuation", number: "02", label: "가치" },
      { key: "performance", number: "03", label: "실적" },
      { key: "finance", number: "04", label: "재무" },
      { key: "outlook", number: "05", label: "전망" },
    ];
    const activeTab = tabs.some((tab) => tab.key === state.detailTab) ? state.detailTab : "judgement";
    return `
      <article class="analysis-workspace">
        ${analysisHeroMarkup(stock)}
        <div class="analysis-tabs" role="tablist" aria-label="${escapeAttr(`${stock.ticker} 분석 항목`)}">
          ${tabs.map((tab) => `
            <button
              class="analysis-tab${tab.key === activeTab ? " is-active" : ""}"
              id="analysis-tab-${escapeAttr(tab.key)}"
              type="button"
              role="tab"
              aria-controls="analysis-panel-${escapeAttr(tab.key)}"
              aria-selected="${tab.key === activeTab ? "true" : "false"}"
              tabindex="${tab.key === activeTab ? "0" : "-1"}"
              data-analysis-tab="${escapeAttr(tab.key)}"
            ><span>${tab.number}</span>${escapeHTML(tab.label)}</button>`).join("")}
        </div>
        ${error ? `<div class="detail-api-warning analysis-api-warning">상세 API 연결 실패 · 보드에서 마지막으로 받은 값을 표시합니다. ${escapeHTML(cleanError(error))}</div>` : ""}
        <div class="analysis-panels">
          <section class="analysis-panel${activeTab === "judgement" ? " is-active" : ""}" id="analysis-panel-judgement" role="tabpanel" aria-labelledby="analysis-tab-judgement" data-analysis-panel="judgement"${activeTab === "judgement" ? "" : " hidden"}>
            ${judgementPanelMarkup(stock, historyError)}
          </section>
          <section class="analysis-panel${activeTab === "valuation" ? " is-active" : ""}" id="analysis-panel-valuation" role="tabpanel" aria-labelledby="analysis-tab-valuation" data-analysis-panel="valuation"${activeTab === "valuation" ? "" : " hidden"}>
            ${valuationPanelMarkup(stock)}
          </section>
          <section class="analysis-panel${activeTab === "performance" ? " is-active" : ""}" id="analysis-panel-performance" role="tabpanel" aria-labelledby="analysis-tab-performance" data-analysis-panel="performance"${activeTab === "performance" ? "" : " hidden"}>
            ${performancePanelMarkup(stock)}
          </section>
          <section class="analysis-panel${activeTab === "finance" ? " is-active" : ""}" id="analysis-panel-finance" role="tabpanel" aria-labelledby="analysis-tab-finance" data-analysis-panel="finance"${activeTab === "finance" ? "" : " hidden"}>
            ${financePanelMarkup(stock)}
          </section>
          <section class="analysis-panel${activeTab === "outlook" ? " is-active" : ""}" id="analysis-panel-outlook" role="tabpanel" aria-labelledby="analysis-tab-outlook" data-analysis-panel="outlook"${activeTab === "outlook" ? "" : " hidden"}>
            ${outlookPanelMarkup(stock)}
          </section>
        </div>
        ${analysisDataFooterMarkup(stock)}
      </article>`;
  }

  function analysisHeroMarkup(stock) {
    const signal = SIGNALS[stock.signalType];
    const high52 = detailMetric(stock, "high_52_week");
    const ytdReturn = calculateYtdReturn(stock.chartHistory?.adjustedClose || []);
    const freshness = stock.priceMetric.status === "stale" ? "기준일 경과" : "최신 캐시";
    return `
      <header class="analysis-stock-hero">
        <div class="analysis-hero-primary">
          <div class="analysis-company">
            <span class="ticker-avatar analysis-avatar" aria-hidden="true">${escapeHTML(avatarText(stock.ticker))}</span>
            <div>
              <div class="analysis-ticker-line">
                <h2>${escapeHTML(stock.ticker)}</h2>
                <span class="analysis-data-status is-${stock.priceMetric.status === "stale" ? "stale" : "current"}">${escapeHTML(freshness)}</span>
              </div>
              <p>${escapeHTML(stock.name)}</p>
              <span>${escapeHTML(stock.sector)}${stock.subIndustry ? ` · ${escapeHTML(stock.subIndustry)}` : ""}</span>
            </div>
          </div>
          <div class="analysis-price">
            <div><strong>${formatPrice(stock.price)}</strong>${ytdReturn === null ? "" : `<span class="${ytdReturn >= 0 ? "is-positive" : "is-negative"}">${formatPercent(ytdReturn)} YTD</span>`}</div>
            <small>조정종가 · ${escapeHTML(formatDate(stock.priceMetric.asOf || stock.updatedAt, { includeTime: false }))}</small>
          </div>
        </div>
        <div class="analysis-hero-stats">
          <div><span>52주 최고 종가</span><strong>${formatPrice(high52.value)}</strong></div>
          <div><span>고점 대비</span><strong class="${(stock.metrics.drawdown.value ?? 0) < 0 ? "is-negative" : ""}">${formatPercent(stock.metrics.drawdown.value)}</strong></div>
          <div class="analysis-signal-stat"><span>기계 판정</span><strong class="signal-badge is-${stock.signalType}">${escapeHTML(signal.label)} <b>${escapeHTML(stock.score === null ? "—" : formatSigned(stock.score))}</b></strong></div>
        </div>
      </header>`;
  }

  function judgementPanelMarkup(stock, historyError) {
    return `
      ${analysisSectionHeading("01 판단", "종합 판단", "점수의 결과보다 어떤 항목이 결과를 만들었는지 먼저 확인하세요.")}
      <div class="judgement-grid">
        <article class="analysis-card decision-card">
          ${decisionCardMarkup(stock)}
        </article>
        <article class="analysis-card contribution-card">
          <div class="analysis-card-heading">
            <div><span>기여도</span><h4>왜 이 점수인가</h4></div>
            <small>중앙선 기준 · 가점은 오른쪽</small>
          </div>
          ${contributionMarkup(stock)}
        </article>
      </div>
      <div class="analysis-secondary-grid">
        ${priceOverviewMarkup(stock, historyError)}
        ${marketEnvironmentMarkup(stock)}
      </div>`;
  }

  function analysisSectionHeading(kicker, title, description) {
    return `
      <div class="analysis-section-heading">
        <div><span>${escapeHTML(kicker)}</span><h3>${escapeHTML(title)}</h3></div>
        <p>${escapeHTML(description)}</p>
      </div>`;
  }

  function decisionCardMarkup(stock) {
    const signal = SIGNALS[stock.signalType];
    const scoreText = stock.score === null ? "—" : formatSigned(stock.score);
    const companyScore = stock.companyScore ?? (stock.score === null ? null : stock.score - stock.marketScore);
    return `
      <div class="decision-summary">
        <strong>${escapeHTML(scoreText)}</strong>
        <div><span>현재 기계 판정</span><h4>${escapeHTML(signal.label)}</h4><p>기업 ${escapeHTML(formatSigned(companyScore))} · 시장 ${escapeHTML(formatSigned(stock.marketScore ?? 0))}</p></div>
      </div>
      ${scoreSpectrumMarkup(stock)}
      ${mechanicalCommentMarkup(stock)}
      <p class="decision-disclaimer">점수는 후보를 좁히는 연구 도구입니다. 사업 훼손, 공시 이벤트, 가격 급변은 별도 확인이 필요합니다.</p>`;
  }

  function scoreSpectrumMarkup(stock) {
    const score = stock.score;
    const position = scoreSpectrumPosition(score);
    return `
      <div class="score-spectrum-wrap">
        <div class="score-spectrum" role="img" aria-label="매도부터 강한 매수까지의 판정 스펙트럼, 현재 ${escapeAttr(SIGNALS[stock.signalType].label)}">
          <span class="is-sell"></span><span class="is-sell-watch"></span><span class="is-neutral"></span><span class="is-buy-watch"></span><span class="is-strong-buy"></span>
          <i class="score-pointer" style="left:${position.toFixed(1)}%"></i>
        </div>
        <div class="score-spectrum-labels"><span>매도</span><span>매도 대기</span><span>관망</span><span>매수 대기</span><span>강한 매수</span></div>
      </div>`;
  }

  function scoreSpectrumPosition(score) {
    const value = numberOrNull(score);
    if (value === null) return 50;
    if (value <= -8) return clamp(18 + (value + 8) * 1.5, 2, 18);
    if (value <= -3) return 20 + (value + 7) / 4 * 20;
    if (value <= 4) return 40 + (value + 2) / 6 * 20;
    if (value <= 9) return 60 + (value - 5) / 4 * 20;
    return clamp(82 + (value - 10) * 2, 82, 98);
  }

  function mechanicalCommentMarkup(stock) {
    const positive = stock.components.filter((item) => (item.points ?? 0) > 0).sort((a, b) => b.points - a.points);
    const negative = stock.components.filter((item) => (item.points ?? 0) < 0).sort((a, b) => a.points - b.points);
    const staleCount = stock.components.filter((item) => item.metricStatus === "stale").length;
    const lead = {
      "strong-buy": "여러 매력 조건이 동시에 충족된 구간입니다.",
      "buy-watch": "가점 요인이 우세하지만 진입 전 위험 점검이 필요한 구간입니다.",
      neutral: "가점과 감점이 상쇄되어 기다림이 우세한 구간입니다.",
      "sell-watch": "부담 요인이 우세해 신규 진입을 서두르지 않는 구간입니다.",
      sell: "기계적 위험 신호가 강하게 겹친 구간입니다.",
      insufficient: "핵심 데이터가 부족해 판정을 보류한 상태입니다.",
    }[stock.signalType] || "현재 조건을 기계적으로 합산한 결과입니다.";
    const parts = [lead];
    if (positive.length) parts.push(`${positive.slice(0, 2).map((item) => item.label).join("·")}에서 가점을 받았습니다.`);
    if (negative.length) parts.push(`${negative.slice(0, 2).map((item) => item.label).join("·")}은 감점 요인입니다.`);
    if (staleCount) parts.push(`${staleCount}개 기업 지표는 기준일이 경과한 캐시이므로 갱신 시 점수가 달라질 수 있습니다.`);
    return `
      <div class="mechanical-comment">
        <span>기계 판정 요약</span>
        <p>${escapeHTML(parts.join(" "))}</p>
      </div>`;
  }

  function contributionMarkup(stock) {
    const companyRows = stock.components.map((item) => ({ ...item, group: "기업" }));
    const marketRows = (state.dashboard?.market?.components || []).map((item) => ({ ...item, group: "시장" }));
    const rows = [...companyRows, ...marketRows];
    const maxPoints = Math.max(3, ...rows.map((item) => Math.abs(item.points ?? 0)));
    return `
      <div class="contribution-list">
        ${rows.map((item) => {
          const points = numberOrNull(item.points);
          const size = points === null ? 0 : Math.min(Math.abs(points) / maxPoints * 50, 50);
          const tone = points > 0 ? "positive" : points < 0 ? "negative" : "zero";
          return `
            <div class="contribution-row">
              <div class="contribution-label"><strong>${escapeHTML(item.label)}</strong><small>${escapeHTML(item.group)} · ${escapeHTML(statusLabel(item.metricStatus, item.applicable))}</small></div>
              <span class="contribution-value">${formatByUnit(item.value, item.unit)}</span>
              <div class="contribution-track" aria-hidden="true"><i></i>${points ? `<b class="is-${tone}" style="--bar-size:${size.toFixed(1)}%"></b>` : ""}</div>
              ${scoreChip(points, item.value === null, item.applicable)}
            </div>`;
        }).join("")}
      </div>`;
  }

  function priceOverviewMarkup(stock, historyError) {
    const allPoints = stock.chartHistory?.adjustedClose || [];
    const points = recentSeries(allPoints, 365);
    const periodReturn = calculateSeriesReturn(points);
    const sma200 = detailMetric(stock, "sma_200");
    return `
      <article class="analysis-card price-overview-card">
        <div class="analysis-card-heading price-heading">
          <div><span>가격 흐름</span><h4>최근 1년 조정종가</h4></div>
          <div><strong>${formatPrice(stock.price)}</strong><small class="${(periodReturn ?? 0) >= 0 ? "is-positive" : "is-negative"}">${periodReturn === null ? "수익률 N/A" : `${formatPercent(periodReturn)} · 1Y`}</small></div>
        </div>
        ${historyError ? `<div class="history-warning compact-warning" role="status">${escapeHTML(cleanError(historyError))}</div>` : ""}
        ${points.length > 1 ? largePriceChartMarkup(points, sma200.value === null ? null : { value: sma200.value, label: "200일선" }) : '<div class="chart-empty large"><span>가격 이력이 없습니다.</span><small>빠른 갱신 후 다시 확인하세요.</small></div>'}
        <div class="price-chart-meta"><span>최초 ${points[0] ? formatPrice(points[0].value) : "—"}</span><span>${points[0] ? escapeHTML(shortChartDate(points[0].date)) : ""} → ${points.length ? escapeHTML(shortChartDate(points[points.length - 1].date)) : ""}</span></div>
      </article>`;
  }

  function largePriceChartMarkup(points, reference = null) {
    const width = 760;
    const height = 248;
    const padding = { top: 18, right: 58, bottom: 28, left: 8 };
    const values = [...points.map((item) => item.value), ...(reference?.value === null || reference?.value === undefined ? [] : [reference.value])].filter(Number.isFinite);
    let minimum = Math.min(...values);
    let maximum = Math.max(...values);
    const spread = Math.max((maximum - minimum) * 0.1, Math.abs(maximum) * 0.01, 1);
    minimum -= spread;
    maximum += spread;
    const plotWidth = width - padding.left - padding.right;
    const plotHeight = height - padding.top - padding.bottom;
    const x = (index) => padding.left + index / (points.length - 1) * plotWidth;
    const y = (value) => padding.top + (maximum - value) / (maximum - minimum) * plotHeight;
    const path = points.map((item, index) => `${index ? "L" : "M"}${x(index).toFixed(2)},${y(item.value).toFixed(2)}`).join(" ");
    const area = `${path} L${x(points.length - 1).toFixed(2)},${(padding.top + plotHeight).toFixed(2)} L${x(0).toFixed(2)},${(padding.top + plotHeight).toFixed(2)} Z`;
    const grid = [0, 1 / 3, 2 / 3, 1].map((ratio) => {
      const value = maximum - (maximum - minimum) * ratio;
      const lineY = padding.top + plotHeight * ratio;
      return `<g><line x1="${padding.left}" y1="${lineY.toFixed(2)}" x2="${width - padding.right}" y2="${lineY.toFixed(2)}"></line><text x="${width - 2}" y="${(lineY + 3).toFixed(2)}" text-anchor="end">${escapeHTML(formatPrice(value))}</text></g>`;
    }).join("");
    const referenceMarkup = reference && reference.value >= minimum && reference.value <= maximum
      ? `<g class="large-chart-reference"><line x1="${padding.left}" y1="${y(reference.value).toFixed(2)}" x2="${width - padding.right}" y2="${y(reference.value).toFixed(2)}"></line><text x="${width - padding.right - 4}" y="${Math.max(12, y(reference.value) - 5).toFixed(2)}" text-anchor="end">${escapeHTML(reference.label)}</text></g>`
      : "";
    const last = points[points.length - 1];
    return `
      <svg class="large-price-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeAttr(`${points[0].date}부터 ${last.date}까지 조정종가 추세`)}">
        <g class="large-chart-grid">${grid}</g>
        <path class="large-chart-area" d="${area}"></path>
        ${referenceMarkup}
        <path class="large-chart-line" d="${path}"></path>
        <circle class="large-chart-last" cx="${x(points.length - 1).toFixed(2)}" cy="${y(last.value).toFixed(2)}" r="4"><title>${escapeHTML(`${last.date}: ${formatPrice(last.value)}`)}</title></circle>
      </svg>`;
  }

  function marketEnvironmentMarkup(stock) {
    const market = state.dashboard?.market;
    if (!market) return `
      <article class="analysis-card market-environment-card">
        <div class="chart-empty large"><span>시장 환경 데이터가 없습니다.</span></div>
      </article>`;
    return `
      <article class="analysis-card market-environment-card">
        <div class="analysis-card-heading">
          <div><span>시장 환경</span><h4>공통 시장 점수</h4></div>
          <strong class="market-score-total">${escapeHTML(formatSigned(market.score))}</strong>
        </div>
        <div class="market-environment-summary"><strong>${escapeHTML(market.label)}</strong><span>모든 종목에 동일 적용</span></div>
        <div class="market-environment-list">
          ${market.components.map((item) => `
            <div>
              <span><strong>${escapeHTML(item.label)}</strong><small>${escapeHTML(statusLabel(item.metricStatus, item.applicable))}</small></span>
              <b>${formatByUnit(item.value, item.unit)}</b>
              <em class="is-${(item.points ?? 0) > 0 ? "positive" : (item.points ?? 0) < 0 ? "negative" : "neutral"}">${(item.points ?? 0) > 0 ? "우호" : (item.points ?? 0) < 0 ? "부담" : "중립"}</em>
              ${scoreChip(item.points, item.value === null, item.applicable)}
            </div>`).join("")}
        </div>
        <p>시장 점수 ${escapeHTML(formatSigned(stock.marketScore ?? market.score))}이 기업 점수에 합산됩니다.</p>
      </article>`;
  }

  function valuationPanelMarkup(stock) {
    const trailingPe = detailMetric(stock, "trailing_pe");
    const fcfYield = detailMetric(stock, "fcf_yield_pct");
    const valuationSeries = valuationPeComparisonSeries(stock);
    const valuationMeta = valuationHistoryMeta(stock);
    const valuationError = stock.chartHistory?.valuation?.error;
    const gap = stock.metrics.peGap.value;
    let insight = "비교 가능한 3년 PER 데이터가 부족해 상대가치 판단을 보류합니다.";
    let tone = "neutral";
    if (gap !== null) {
      if (gap <= -20) { insight = `현재 Forward PER이 3년 기준보다 ${formatPercentNumber(Math.abs(gap))} 낮아 큰 할인 구간으로 분류됩니다.`; tone = "positive"; }
      else if (gap <= -10) { insight = `현재 Forward PER이 3년 기준보다 ${formatPercentNumber(Math.abs(gap))} 낮아 완만한 할인 구간입니다.`; tone = "positive"; }
      else if (gap >= 20) { insight = `현재 Forward PER이 3년 기준보다 ${formatPercentNumber(gap)} 높아 프리미엄 부담이 큰 구간입니다.`; tone = "negative"; }
      else if (gap > 0) { insight = `현재 Forward PER은 3년 기준보다 ${formatPercentNumber(gap)} 높은 소폭 프리미엄 구간입니다.`; }
      else { insight = `현재 Forward PER은 3년 기준과 큰 차이가 없는 구간입니다.`; }
    }
    return `
      ${analysisSectionHeading("02 가치", "가격이 실적 대비 싼가", "자기 과거와 현재 밸류에이션을 함께 비교합니다.")}
      <div class="analysis-metric-grid">
        ${snapshotMetricCard("현재 Forward PER", formatMultiple(stock.forwardPE), stock.forwardPeMetric, "다음 회계연도 예상 EPS 기준")}
        ${snapshotMetricCard("3Y Forward PER 중앙값", formatMultiple(stock.peMedianMetric.value), stock.peMedianMetric, "점수용 과거 스냅샷")}
        ${snapshotMetricCard("3Y PER 괴리", formatPercent(gap), stock.metrics.peGap, "현재 ÷ 3Y 중앙값 - 1", stock.metrics.peGap.score)}
        ${snapshotMetricCard("Trailing PER", formatMultiple(trailingPe.value), trailingPe, "최근 12개월 이익 기준")}
        ${snapshotMetricCard("연간 FCF 수익률", formatPercent(fcfYield.value), fcfYield, "최근 연간 FCF 기준")}
      </div>
      <div class="analysis-tab-grid">
        <article class="analysis-card focus-chart-card">
          <div class="analysis-card-heading"><div><span>밸류에이션 흐름</span><h4>최근 5년 주간 PER 히스토리</h4></div><small>${escapeHTML(valuationMeta.badge)}</small></div>
          ${valuationError ? `<div class="history-warning compact-warning" role="status">주간 PER 갱신 오류: ${escapeHTML(cleanError(valuationError))} 연간 legacy 슬롯은 주간 데이터로 표시하지 않습니다.</div>` : ""}
          ${dualSeriesChartCardMarkup({
            title: "Trailing PER · Forward PER",
            subtitle: valuationMeta.subtitle,
            series: valuationSeries,
            unit: "multiple",
            references: stock.peMedianMetric.value === null ? [] : [{ value: stock.peMedianMetric.value, label: "FWD 3Y 중앙값" }],
            metadata: valuationMeta,
            error: valuationError,
          })}
        </article>
        ${analysisInsightCard("상대가치 해석", insight, tone, "주의: 3Y 비교 슬롯")}
      </div>
      <p class="analysis-method-note">${escapeHTML(valuationMeta.note)} 주간 그래프는 점수 산식을 바꾸지 않으며, 3Y PER 괴리 점수는 기존의 검증 가능한 과거 Forward PER 스냅샷 기준입니다.</p>`;
  }

  function performancePanelMarkup(stock) {
    const latestEps = detailMetric(stock, "eps_latest");
    const priorEps = detailMetric(stock, "eps_prior");
    const growth = stock.metrics.epsGrowth.value;
    const series = annualEpsGrowthSeries(stock);
    let insight = "비교 가능한 확정 연간 희석 EPS가 부족합니다.";
    let tone = "neutral";
    if (growth !== null) {
      if (growth >= 20) { insight = `최근 확정 연간 희석 EPS가 전년 대비 ${formatPercent(growth)} 증가해 강한 성장 가점을 받았습니다.`; tone = "positive"; }
      else if (growth > 0) { insight = `최근 확정 연간 희석 EPS가 전년 대비 ${formatPercent(growth)} 증가했습니다.`; tone = "positive"; }
      else if (growth < 0) { insight = `최근 확정 연간 희석 EPS가 전년 대비 ${formatPercent(growth)} 감소했습니다.`; tone = "negative"; }
      else { insight = "최근 확정 연간 희석 EPS가 전년과 동일합니다."; }
    }
    return `
      ${analysisSectionHeading("03 실적", "확정 이익이 성장했는가", "애널리스트 예상치가 아닌 최근 두 확정 연간 희석 EPS를 비교합니다.")}
      <div class="analysis-metric-grid">
        ${snapshotMetricCard("최근 확정 희석 EPS", latestEps.value === null ? "—" : formatPrice(latestEps.value), latestEps, latestEps.asOf || "회계연도 미표기")}
        ${snapshotMetricCard("직전 확정 희석 EPS", priorEps.value === null ? "—" : formatPrice(priorEps.value), priorEps, priorEps.asOf || "회계연도 미표기")}
        ${snapshotMetricCard("연간 EPS 성장률", formatPercent(growth), stock.metrics.epsGrowth, "확정 연간 GAAP 희석 EPS YoY", stock.metrics.epsGrowth.score)}
      </div>
      <div class="analysis-tab-grid">
        <article class="analysis-card focus-chart-card">
          <div class="analysis-card-heading"><div><span>실적 흐름</span><h4>연간 희석 EPS 성장률</h4></div><small>비교 가능한 연도만</small></div>
          ${chartCardMarkup({ title: "연간 EPS 성장률", subtitle: "확정 희석 EPS YoY", points: series, unit: "percent", references: [{ value: 0, label: "0%" }] })}
        </article>
        ${analysisInsightCard("실적 해석", insight, tone, "확정 연간 수치")}
      </div>
      <p class="analysis-method-note">Finviz의 ‘EPS this Y’ 같은 금년도 예상 성장률과는 다른 지표입니다. 이 화면은 Yahoo 연간 손익계산서의 확정 희석 EPS만 사용합니다.</p>`;
  }

  function financePanelMarkup(stock) {
    const shareSeries = annualShareSeries(stock);
    const financialSector = /financial/i.test(stock.sector);
    const caveat = financialSector
      ? "금융회사는 자본 구조가 일반 기업과 달라 부채/자기자본 점수를 적용하지 않거나 별도로 해석합니다."
      : "부채/자기자본은 음의 자본이나 업종 구조에 따라 왜곡될 수 있으므로 단독으로 안전성을 판단하지 않습니다.";
    const returnValue = stock.metrics.shareholderReturn.value;
    let insight = returnValue === null
      ? "배당과 주식수 이력이 부족해 3년 주주환원 프록시를 계산하지 못했습니다."
      : `3년 주주환원 프록시는 ${formatPercent(returnValue)}입니다. 평균 배당수익률과 양(+)의 순주식수 감소율을 합산한 계산값입니다.`;
    if (stock.metrics.sharesChange.value !== null) {
      insight += stock.metrics.sharesChange.value > 0
        ? ` 같은 기간 희석주식수는 ${formatPercent(stock.metrics.sharesChange.value)} 늘어 별도 희석 위험을 확인해야 합니다.`
        : ` 같은 기간 희석주식수는 ${formatPercentNumber(Math.abs(stock.metrics.sharesChange.value))} 줄었습니다.`;
    }
    if (stock.metrics.cashFlowQuality.value !== null && stock.metrics.cashFlowQuality.applicable !== false) {
      insight += ` 3년 누적 영업현금 전환율은 ${formatRatio(stock.metrics.cashFlowQuality.value)}배입니다.`;
    }
    return `
      ${analysisSectionHeading("04 재무", "현금창출력과 주주환원", "회계이익의 현금 전환, 레버리지, 배당과 희석을 분리해 확인합니다.")}
      <div class="analysis-metric-grid">
        ${snapshotMetricCard("3Y 현금흐름 품질", formatRatio(stock.metrics.cashFlowQuality.value), stock.metrics.cashFlowQuality, "누적 영업현금흐름 ÷ 누적 순이익", stock.metrics.cashFlowQuality.score)}
        ${snapshotMetricCard("부채/자기자본", formatRatio(stock.metrics.debt.value), stock.metrics.debt, financialSector ? "금융업 적용 제외 가능" : "Yahoo 비율 정규화", stock.metrics.debt.score)}
        ${snapshotMetricCard("3Y 주주환원 프록시", formatPercent(returnValue), stock.metrics.shareholderReturn, "평균 배당 + 양의 순매입", stock.metrics.shareholderReturn.score)}
        ${snapshotMetricCard("3Y 평균 배당수익률", formatPercent(stock.dividendYieldAverageMetric.value), stock.dividendYieldAverageMetric, "과거 legacy 슬롯")}
        ${snapshotMetricCard("3Y 평균 순매입률", formatPercent(stock.netBuybackAverageMetric.value), stock.netBuybackAverageMetric, "희석주식수 감소 기반")}
        ${snapshotMetricCard("3Y 희석주식수 증감", formatPercent(stock.metrics.sharesChange.value), stock.metrics.sharesChange, "양수는 희석, 음수는 감소", stock.metrics.sharesChange.score)}
      </div>
      <div class="analysis-tab-grid">
        <article class="analysis-card focus-chart-card">
          <div class="analysis-card-heading"><div><span>자본 흐름</span><h4>희석가중평균주식수</h4></div><small>연간 확정값</small></div>
          ${chartCardMarkup({ title: "희석가중평균주식수", subtitle: "증가하면 별도 희석 감점", points: shareSeries, unit: "shares" })}
        </article>
        ${analysisInsightCard("재무 해석", `${insight} ${caveat}`, returnValue !== null && returnValue >= 4 ? "positive" : "neutral", "계산 프록시")}
      </div>
      <p class="analysis-method-note">현금흐름 품질은 최근 3개 공통 회계연도의 영업현금흐름을 순이익으로 나눈 값입니다. 금융사와 REIT는 현금흐름 구조가 달라 점수에서 제외합니다.</p>`;
  }

  function outlookPanelMarkup(stock) {
    const rsiSeries = (stock.chartHistory?.rsi14 || []).map((item) => ({ label: shortChartDate(item.date), value: item.value, detail: item.date }));
    return `
      ${analysisSectionHeading("05 전망", "무엇이 판정을 바꿀 수 있나", "애널리스트 목표가가 아니라 현재 점수를 움직이는 기계적 조건을 보여줍니다.")}
      <div class="analysis-metric-grid">
        ${snapshotMetricCard("RSI(14)", formatIndex(stock.metrics.rsi.value), stock.metrics.rsi, "단기 과열·침체", stock.metrics.rsi.score)}
        ${snapshotMetricCard("200일선 이격", formatPercent(stock.metrics.distance200.value), stock.metrics.distance200, "현재가 ÷ 200일선 - 1", stock.metrics.distance200.score)}
        ${snapshotMetricCard("52주 최고 종가 대비", formatPercent(stock.metrics.drawdown.value), stock.metrics.drawdown, "최고 일중가가 아닌 최고 종가", stock.metrics.drawdown.score)}
        ${snapshotMetricCard("시장 공통점수", formatSigned(stock.marketScore), { status: state.dashboard?.market?.complete ? "ok" : "missing", asOf: state.dashboard?.asOf }, state.dashboard?.market?.label || "시장 상태")}
      </div>
      <div class="analysis-tab-grid outlook-grid">
        <article class="analysis-card focus-chart-card">
          <div class="analysis-card-heading"><div><span>기술 흐름</span><h4>최근 3년 RSI(14)</h4></div><small>Wilder 방식</small></div>
          ${chartCardMarkup({ title: "RSI(14)", subtitle: "30·70 기준선", points: rsiSeries, unit: "index", fixedDomain: [0, 100], references: [{ value: 30, label: "30" }, { value: 70, label: "70" }] })}
        </article>
        <article class="analysis-card condition-card">
          <div class="analysis-card-heading"><div><span>조건 점검</span><h4>판정 변화 요인</h4></div><small>점수 방향 기준</small></div>
          ${outlookConditionsMarkup(stock)}
        </article>
      </div>
      <div class="outlook-market-wrap">${marketEnvironmentMarkup(stock)}</div>`;
  }

  function snapshotMetricCard(label, value, metric = {}, caption = "", score = null) {
    const status = String(metric?.status || "missing").toLowerCase();
    return `
      <article class="snapshot-metric-card">
        <div><span>${escapeHTML(label)}</span>${score === null || score === undefined ? "" : scoreChip(score, metric?.value === null, metric?.applicable !== false)}</div>
        <strong>${escapeHTML(value)}</strong>
        <p>${escapeHTML(caption)}</p>
        <small class="metric-status is-${escapeAttr(status)}">${escapeHTML(statusLabel(status, metric?.applicable !== false))}${metric?.asOf ? ` · ${escapeHTML(formatDate(metric.asOf, { includeTime: false }))}` : ""}</small>
      </article>`;
  }

  function analysisInsightCard(title, body, tone = "neutral", eyebrow = "기계적 해석") {
    return `
      <article class="analysis-card insight-card is-${escapeAttr(tone)}">
        <span>${escapeHTML(eyebrow)}</span>
        <h4>${escapeHTML(title)}</h4>
        <p>${escapeHTML(body)}</p>
        <div class="insight-rule"><i></i><small>현재 데이터와 공개된 점수 규칙만 사용</small></div>
      </article>`;
  }

  function outlookConditionsMarkup(stock) {
    const rows = [
      { label: "가격 위치", value: formatPercent(stock.metrics.drawdown.value), metric: stock.metrics.drawdown },
      { label: "장기 추세", value: formatPercent(stock.metrics.distance200.value), metric: stock.metrics.distance200 },
      { label: "단기 과열", value: formatIndex(stock.metrics.rsi.value), metric: stock.metrics.rsi },
      { label: "상대 가치", value: formatPercent(stock.metrics.peGap.value), metric: stock.metrics.peGap },
      { label: "현금 전환", value: formatRatio(stock.metrics.cashFlowQuality.value), metric: stock.metrics.cashFlowQuality },
      { label: "확정 이익", value: formatPercent(stock.metrics.epsGrowth.value), metric: stock.metrics.epsGrowth },
    ];
    return `
      <div class="condition-list">
        ${rows.map((item) => {
          const score = item.metric.score;
          const tone = score > 0 ? "positive" : score < 0 ? "negative" : "neutral";
          return `
            <div>
              <i class="is-${tone}"></i>
              <span><strong>${escapeHTML(item.label)}</strong><small>${escapeHTML(statusLabel(item.metric.status, item.metric.applicable))}</small></span>
              <b>${escapeHTML(item.value)}</b>
              <em>${score > 0 ? "가점" : score < 0 ? "감점" : "중립"}</em>
            </div>`;
        }).join("")}
      </div>`;
  }

  function analysisDataFooterMarkup(stock) {
    const priceProvenance = stock.priceMetric.provenance[0];
    const fundamentalProvenance = firstProvenance([stock.forwardPeMetric, stock.metrics.epsGrowth, stock.metrics.debt]);
    return `
      <footer class="analysis-data-footer">
        <div>
          <span>가격 출처</span><strong>${escapeHTML(priceProvenance?.source || stock.sources.price || "출처 미표기")}</strong>
        </div>
        <div>
          <span>재무 출처</span><strong>${escapeHTML(fundamentalProvenance?.source || stock.sources.fundamentals || "출처 미표기")}</strong>
        </div>
        <div>
          <span>데이터 기준일</span><strong>${escapeHTML(formatDate(stock.updatedAt || state.dashboard?.asOf, { includeTime: false }))}</strong>
        </div>
        <div>
          <span>커버리지</span><strong>${escapeHTML(formatCoverage(stock.coverage))}${stock.coverageCount !== null && stock.coverageTotal !== null ? ` · ${stock.coverageCount}/${stock.coverageTotal}` : ""}</strong>
        </div>
        ${warningsMarkup(stock)}
        <p><strong>기계적 연구 보조 점수이며 투자 조언이 아닙니다.</strong> 최신 공시, 기업 고유 위험, 데이터 기준일을 함께 확인하세요.</p>
      </footer>`;
  }

  function detailMetric(stock, key) {
    return normalizeRawMetric(stock.raw?.metrics?.[key], key, stock.sources);
  }

  function valuationPeComparisonSeries(stock) {
    const weekly = weeklyValuationData(stock);
    if (!weekly.available) return emptyValuationSeries();

    const weeklyPoints = (values) => values.map((item) => ({
      key: item.date,
      label: shortChartDate(item.date),
      value: item.value,
      detail: `${item.date}${item.anchorDate ? ` · EPS 앵커 ${item.anchorDate}` : ""}`,
    })).sort((a, b) => a.key.localeCompare(b.key));
    return [
      { key: "forward", label: "Forward PER", points: weeklyPoints(weekly.forward) },
      { key: "trailing", label: "Trailing PER", points: weeklyPoints(weekly.trailing) },
    ];
  }

  function emptyValuationSeries() {
    return [
      { key: "forward", label: "Forward PER", points: [] },
      { key: "trailing", label: "Trailing PER", points: [] },
    ];
  }

  function weeklyValuationData(stock) {
    const valuation = stock.chartHistory?.valuation || null;
    const forward = uniqueWeeklyValuationRows(valuation?.forwardPeWeekly || []);
    const trailing = uniqueWeeklyValuationRows(valuation?.trailingPeWeekly || []);
    const rows = [...forward, ...trailing].sort((left, right) => left.date.localeCompare(right.date));
    const declaredFrequency = String(valuation?.frequency || "").trim().toLowerCase();
    const dateKeys = [...new Set(rows.map((item) => item.date))].sort();
    const looksWeekly = isWeeklyFrequency(declaredFrequency)
      || (!declaredFrequency && looksLikeWeeklyDates(dateKeys));
    const available = rows.length > 0 && looksWeekly;
    const start = dateKeys[0] || null;
    const end = dateKeys[dateKeys.length - 1] || null;
    const frequency = available ? "weekly" : declaredFrequency || "unknown";
    return {
      valuation,
      forward,
      trailing,
      rows,
      dateKeys,
      start,
      end,
      frequency,
      declaredFrequency,
      available,
      legacyCount: legacyValuationRowCount(stock),
    };
  }

  function uniqueWeeklyValuationRows(values) {
    const rows = firstArray(values).map((item) => ({
      date: String(item?.date || "").trim().slice(0, 10),
      value: numberOrNull(item?.value),
      anchorDate: String(item?.anchorDate || "").trim().slice(0, 10),
    })).filter((item) => isIsoCalendarDate(item.date) && item.value !== null);
    const byDate = new Map();
    rows.forEach((item) => byDate.set(item.date, item));
    return [...byDate.values()].sort((left, right) => left.date.localeCompare(right.date));
  }

  function isIsoCalendarDate(value) {
    const text = String(value || "");
    if (!/^\d{4}-\d{2}-\d{2}$/.test(text)) return false;
    const timestamp = Date.parse(`${text}T00:00:00Z`);
    return Number.isFinite(timestamp);
  }

  function isWeeklyFrequency(value) {
    return ["weekly", "week", "1w", "w"].includes(String(value || "").toLowerCase());
  }

  function looksLikeWeeklyDates(dateKeys) {
    if (dateKeys.length < 2) return false;
    const gaps = [];
    for (let index = 1; index < dateKeys.length; index += 1) {
      const previous = Date.parse(`${dateKeys[index - 1]}T00:00:00Z`);
      const current = Date.parse(`${dateKeys[index]}T00:00:00Z`);
      const gap = (current - previous) / 86400000;
      if (Number.isFinite(gap) && gap > 0) gaps.push(gap);
    }
    if (!gaps.length) return false;
    const sorted = gaps.sort((left, right) => left - right);
    const median = sorted[Math.floor(sorted.length / 2)];
    // A holiday week can be 7–14 days apart.  Annual/quarterly legacy
    // snapshots stay far outside this guard and are never shown as weekly.
    return median >= 3 && median <= 14 && sorted.filter((gap) => gap <= 21).length >= Math.max(1, Math.floor(sorted.length * 0.7));
  }

  function legacyValuationRowCount(stock) {
    const rows = [
      ...(stock.history?.forward_pe || []),
      ...(stock.history?.trailing_pe || []),
    ];
    return rows.filter((item) => /^legacy(?:_current|_fy_minus_\d+)$/.test(String(item?.periodEnd || ""))).length;
  }

  function compareValuationPeriodKeys(left, right) {
    const rank = (value) => {
      if (value === "__current__") return Number.POSITIVE_INFINITY;
      const legacy = String(value || "").match(/^legacy_fy_minus_(\d+)$/);
      if (legacy) return -Number(legacy[1]);
      const timestamp = Date.parse(`${String(value || "").slice(0, 10)}T00:00:00Z`);
      return Number.isFinite(timestamp) ? timestamp : null;
    };
    const leftRank = rank(left);
    const rightRank = rank(right);
    if (leftRank !== null && rightRank !== null && leftRank !== rightRank) return leftRank - rightRank;
    return String(left).localeCompare(String(right));
  }

  function valuationHistoryMeta(stock) {
    const weekly = weeklyValuationData(stock);
    if (weekly.available) {
      const source = weekly.valuation?.provenance?.[0]?.source || "StockAnalysis 분기 비율";
      const forwardCount = weekly.forward.length;
      const trailingCount = weekly.trailing.length;
      const weeklyCount = Math.max(forwardCount, trailingCount);
      const start = weekly.start ? formatDate(weekly.start, { includeTime: false }) : null;
      const end = weekly.end ? formatDate(weekly.end, { includeTime: false }) : null;
      return {
        available: true,
        frequency: "weekly",
        badge: `${weeklyCount}주 데이터`,
        subtitle: `${start && end ? `${start} ~ ${end}` : "최근 약 5년"} · 매주 마지막 거래일`,
        start: weekly.start,
        end: weekly.end,
        forwardCount,
        trailingCount,
        note: `주간 PER은 ${forwardCount}개 Forward·${trailingCount}개 Trailing 관측치로, Yahoo 종가를 ${source}의 ${weekly.valuation?.anchorCount || 0}개 분기 앵커에서 역산한 EPS로 나눈 계산값입니다. Forward PER은 분기 예상 EPS를 다음 앵커까지 유지한 가격 민감도 프록시이며, 매주 발표된 컨센서스 원자료나 시점 보존형 백테스트 데이터가 아닙니다. 산출할 수 없는 주간 PER은 임의 보간 없이 생략하고, 14일을 초과하는 장기 공백은 그래프에서 끊어진 선으로 표시합니다.`,
      };
    }
    const legacyCount = weekly.legacyCount;
    return {
      available: false,
      frequency: "unknown",
      badge: "주간 데이터 없음",
      subtitle: "주간 시계열을 불러오지 못함",
      start: null,
      end: null,
      forwardCount: 0,
      trailingCount: 0,
      note: legacyCount
        ? `주간 Forward·Trailing PER을 받지 못해 연간 legacy 슬롯 ${legacyCount}개를 주간 그래프에 섞지 않았습니다. 전체 갱신 후 다시 확인하세요.`
        : "검증 가능한 주간 Forward·Trailing PER이 없어 그래프를 임의 생성하지 않았습니다. 전체 갱신 후 다시 확인하세요.",
    };
  }

  function annualEpsGrowthSeries(stock) {
    const annualEps = [...(stock.history.annual_diluted_eps || [])].sort((a, b) => a.periodEnd.localeCompare(b.periodEnd));
    const output = [];
    for (let index = 1; index < annualEps.length; index += 1) {
      const prior = annualEps[index - 1];
      const current = annualEps[index];
      if (prior.value > 0 && current.value >= 0) {
        output.push({
          label: historyPeriodLabel(current.periodEnd),
          value: (current.value / prior.value - 1) * 100,
          detail: `${prior.value.toFixed(2)} → ${current.value.toFixed(2)} EPS`,
        });
      }
    }
    return output;
  }

  function annualShareSeries(stock) {
    return [...(stock.history.annual_diluted_shares || [])]
      .sort((a, b) => a.periodEnd.localeCompare(b.periodEnd))
      .map((item) => ({ label: historyPeriodLabel(item.periodEnd), value: item.value, detail: item.periodEnd }));
  }

  function recentSeries(points, days) {
    if (!points.length) return [];
    const sorted = [...points].sort((a, b) => a.date.localeCompare(b.date));
    const lastDate = new Date(`${sorted[sorted.length - 1].date}T00:00:00Z`);
    if (Number.isNaN(lastDate.getTime())) return sorted;
    const cutoff = new Date(lastDate);
    cutoff.setUTCDate(cutoff.getUTCDate() - days);
    return sorted.filter((item) => new Date(`${item.date}T00:00:00Z`) >= cutoff);
  }

  function calculateSeriesReturn(points) {
    if (points.length < 2 || !Number.isFinite(points[0].value) || !Number.isFinite(points[points.length - 1].value) || points[0].value === 0) return null;
    return (points[points.length - 1].value / points[0].value - 1) * 100;
  }

  function calculateYtdReturn(points) {
    if (points.length < 2) return null;
    const sorted = [...points].sort((a, b) => a.date.localeCompare(b.date));
    const latest = sorted[sorted.length - 1];
    const year = String(latest.date).slice(0, 4);
    const start = sorted.find((item) => String(item.date).startsWith(year));
    if (!start || !Number.isFinite(start.value) || start.value === 0 || !Number.isFinite(latest.value)) return null;
    return (latest.value / start.value - 1) * 100;
  }

  function stockIdentityMarkup(stock) {
    return `
      <div class="stock-identity">
        <div class="stock-identity-main">
          <span class="ticker-avatar" aria-hidden="true">${escapeHTML(avatarText(stock.ticker))}</span>
          <div><h3>${escapeHTML(stock.ticker)}</h3><p title="${escapeAttr(stock.name)}">${escapeHTML(stock.name)} · ${escapeHTML(stock.sector)}</p></div>
        </div>
        <div class="identity-price"><strong>${formatPrice(stock.price)}</strong><span>현재 주가 · ${formatDate(stock.priceMetric.asOf || stock.updatedAt, { includeTime: false })}</span></div>
      </div>`;
  }

  function totalScoreMarkup(stock) {
    const signal = SIGNALS[stock.signalType];
    const scoreText = stock.score === null ? "—" : formatSigned(stock.score);
    const coverage = stock.coverage;
    const coverageWidth = coverage === null ? 0 : clamp(coverage, 0, 100);
    const composition = stock.insufficient
      ? `${stock.coverageCount ?? "—"}/${stock.coverageTotal ?? "—"}개 회사 지표 사용 가능`
      : `기업 ${formatSigned(stock.companyScore ?? (stock.score - stock.marketScore))} + 시장 ${formatSigned(stock.marketScore ?? 0)}`;
    return `
      <div class="total-score-card is-${stock.signalType}">
        <span class="score-dial">${escapeHTML(scoreText)}</span>
        <div class="total-score-copy">
          <span>최종 시그널</span>
          <strong>${escapeHTML(signal.label)}</strong>
          <p>${escapeHTML(composition)}</p>
          <div class="coverage-meter" aria-label="데이터 커버리지 ${escapeAttr(formatCoverage(coverage))}"><span style="width:${coverageWidth}%"></span></div>
          <small>데이터 커버리지 ${escapeHTML(formatCoverage(coverage))}${stock.insufficient ? " · 추천 제외" : ""}</small>
        </div>
      </div>`;
  }

  function breakdownMarkup(stock) {
    const rows = stock.components.length
      ? stock.components.map((component) => componentDetail(component, stock))
      : Object.entries(METRIC_CONFIG).map(([key, config]) => ({
          label: config.label,
          metricKey: config.key,
          value: stock.metrics[key].value,
          unit: stock.metrics[key].unit || config.unit,
          points: stock.metrics[key].score,
          applicable: stock.metrics[key].applicable,
          reason: stock.metrics[key].reason || stock.metrics[key].note || "서버 제공 점수",
          status: stock.metrics[key].status,
          source: stock.metrics[key].source,
        }));
    const companyRows = rows.map((item) => `
      <div class="breakdown-row">
        <div class="breakdown-label"><strong>${escapeHTML(item.label)}</strong><span>${escapeHTML(statusLabel(item.status, item.applicable))}</span></div>
        <div class="breakdown-value"><strong>${formatByUnit(item.value, item.unit)}</strong><span title="${escapeAttr(item.reason)}">${escapeHTML(truncate(item.reason || item.source || "근거 미표기", 54))}</span></div>
        ${scoreChip(item.points, item.value === null, item.applicable)}
      </div>`).join("");
    const market = state.dashboard?.market;
    const marketRow = market ? `
      <div class="breakdown-row market-breakdown-row">
        <div class="breakdown-label"><strong>시장 환경 공통점수</strong><span>${market.complete ? "3개 시장 지표" : "일부 시장 지표 결측"}</span></div>
        <div class="breakdown-value"><strong>${escapeHTML(market.label)}</strong><span>모든 종목에 동일하게 적용</span></div>
        ${scoreChip(stock.marketScore ?? market.score, false, true)}
      </div>` : "";
    return companyRows + marketRow;
  }

  function componentDetail(component, stock) {
    const rawMetric = stock.raw?.metrics?.[component.metricKey];
    const normalized = normalizeRawMetric(rawMetric, component.metricKey, stock.sources);
    return {
      ...component,
      status: component.metricStatus || normalized.status,
      source: normalized.source,
      reason: component.reason || normalized.note || "서버 제공 판정",
    };
  }

  function provenanceMarkup(stock) {
    const priceProvenance = stock.priceMetric.provenance[0];
    const fundamentalProvenance = firstProvenance([stock.forwardPeMetric, stock.metrics.peGap, stock.metrics.epsGrowth, stock.metrics.debt]);
    const latestMetric = firstDefined(stock.updatedAt, stock.priceMetric.asOf, state.dashboard?.asOf);
    const missing = stock.missingMetrics.length ? stock.missingMetrics.join(", ") : "없음";
    return `
      <ul class="provenance-list">
        <li><span>커버리지</span><strong>${escapeHTML(formatCoverage(stock.coverage))}${stock.coverageCount !== null && stock.coverageTotal !== null ? ` · ${stock.coverageCount}/${stock.coverageTotal}` : ""}</strong></li>
        <li><span>가격 데이터</span><strong title="${escapeAttr(priceProvenance?.sourceUrl || "")}">${escapeHTML(priceProvenance?.source || stock.sources.price || "출처 미표기")}</strong></li>
        <li><span>재무 데이터</span><strong title="${escapeAttr(fundamentalProvenance?.sourceUrl || "")}">${escapeHTML(fundamentalProvenance?.source || stock.sources.fundamentals || "출처 미표기")}</strong></li>
        <li><span>데이터 기준일</span><strong>${escapeHTML(formatDate(latestMetric, { includeTime: false }))}</strong></li>
        <li><span>점수 규칙</span><strong>${escapeHTML(state.rulesMeta?.version ? `v${state.rulesMeta.version}` : "서버 엔진 기준")}</strong></li>
        <li><span>결측/제외 지표</span><strong title="${escapeAttr(missing)}">${escapeHTML(truncate(missing, 48))}</strong></li>
      </ul>`;
  }

  function warningsMarkup(stock) {
    if (!stock.warnings.length && !stock.insufficient) return "";
    const warnings = stock.warnings.length ? stock.warnings : ["필수 회사 지표가 부족해 추천 순위에서 제외됩니다."];
    return `<div class="stock-warnings"><strong>확인할 데이터 경고</strong><ul>${warnings.map((warning) => `<li>${escapeHTML(humanizeWarning(warning))}</li>`).join("")}</ul></div>`;
  }

  function humanizeWarning(warning) {
    const text = String(warning || "").trim();
    const stalePrefix = "Stale cached factors:";
    if (text.startsWith(stalePrefix)) {
      return `기준일이 경과한 캐시 지표: ${text.slice(stalePrefix.length).trim()}`;
    }
    return text;
  }

  function historyChartsMarkup(stock, historyError, context = "detail") {
    const valuationSeries = valuationPeComparisonSeries(stock);
    const valuationMeta = valuationHistoryMeta(stock);
    const valuationError = stock.chartHistory?.valuation?.error;

    const annualEps = [...(stock.history.annual_diluted_eps || [])].sort((a, b) => a.periodEnd.localeCompare(b.periodEnd));
    const epsGrowth = [];
    for (let index = 1; index < annualEps.length; index += 1) {
      const prior = annualEps[index - 1];
      const current = annualEps[index];
      if (prior.value > 0 && current.value >= 0) {
        epsGrowth.push({
          label: historyPeriodLabel(current.periodEnd),
          value: (current.value / prior.value - 1) * 100,
          detail: `${prior.value.toFixed(2)} → ${current.value.toFixed(2)} EPS`,
        });
      }
    }

    const rsiSeries = (stock.chartHistory?.rsi14 || []).map((item) => ({
      label: shortChartDate(item.date),
      value: item.value,
      detail: item.date,
    }));
    const shareSeries = [...(stock.history.annual_diluted_shares || [])]
      .sort((a, b) => a.periodEnd.localeCompare(b.periodEnd))
      .map((item) => ({ label: historyPeriodLabel(item.periodEnd), value: item.value, detail: item.periodEnd }));
    const cashQualitySeries = [...(stock.history.cash_flow_quality_ratio || [])]
      .sort((a, b) => a.periodEnd.localeCompare(b.periodEnd))
      .map((item) => ({ label: historyPeriodLabel(item.periodEnd), value: item.value, detail: `${item.periodEnd} 영업현금흐름 ÷ 순이익` }));

    const summary = [
      { label: "3Y 주주환원", metric: stock.metrics.shareholderReturn, format: formatPercent, showScore: true },
      { label: "평균 배당", metric: stock.dividendYieldAverageMetric, format: formatPercent, showScore: false },
      { label: "평균 순매입", metric: stock.netBuybackAverageMetric, format: formatPercent, showScore: false },
      { label: "3Y 주식수", metric: stock.metrics.sharesChange, format: formatPercent, showScore: true },
    ];
    const historyId = `history-${context}-${stock.ticker}`;
    return `
      <section class="history-section" aria-labelledby="${escapeAttr(historyId)}">
        <div class="detail-section-title history-title">
          <div><h4 id="${escapeAttr(historyId)}">핵심 지표 데이터 흐름</h4><span>PER은 최근 약 5년의 주간 값, 가격 기반 보조지표는 최근 3년을 표시합니다.</span></div>
          <span class="history-basis">${escapeHTML(stock.chartHistory?.asOf ? `${formatDate(stock.chartHistory.asOf, { includeTime: false })} 가격 기준` : "재무 캐시 기준")}</span>
        </div>
        <div class="return-summary-grid">
          ${summary.map((item) => `<div class="return-summary-item"><span>${escapeHTML(item.label)}</span><strong>${item.format(item.metric?.value)}</strong>${item.showScore ? scoreChip(item.metric?.score ?? null, item.metric?.value === null, item.metric?.applicable !== false) : ""}</div>`).join("")}
        </div>
        ${historyError ? `<div class="history-warning" role="status">${escapeHTML(cleanError(historyError))}</div>` : ""}
        ${valuationError ? `<div class="history-warning" role="status">주간 PER 갱신 오류: ${escapeHTML(cleanError(valuationError))} 연간 legacy 슬롯은 주간 데이터로 표시하지 않습니다.</div>` : ""}
        <div class="history-chart-grid">
          ${dualSeriesChartCardMarkup({
            title: "5년 주간 PER · Forward PER",
            subtitle: valuationMeta.subtitle,
            series: valuationSeries,
            unit: "multiple",
            references: stock.peMedianMetric.value === null ? [] : [{ value: stock.peMedianMetric.value, label: "FWD 3Y 중앙값" }],
            className: "is-wide",
            metadata: valuationMeta,
            error: valuationError,
          })}
          ${chartCardMarkup({ title: "RSI(14)", subtitle: historyError ? "가격 히스토리 연결 실패" : "최근 3년 Wilder RSI", points: rsiSeries, unit: "index", fixedDomain: [0, 100], references: [{ value: 30, label: "30" }, { value: 70, label: "70" }], error: historyError })}
          ${chartCardMarkup({ title: "연간 EPS 성장률", subtitle: "비교 가능한 희석 EPS YoY", points: epsGrowth, unit: "percent", references: [{ value: 0, label: "0%" }] })}
          ${chartCardMarkup({ title: "희석가중평균주식수", subtitle: "증가하면 별도 희석 감점", points: shareSeries, unit: "shares" })}
          ${chartCardMarkup({ title: "연간 현금흐름 품질", subtitle: "영업현금흐름 ÷ 순이익 · 누적 3년을 점수화", points: cashQualitySeries, unit: "ratio", references: [{ value: 1, label: "1.0" }], className: "is-wide" })}
        </div>
        <p class="history-footnote">${escapeHTML(valuationMeta.note)} 이 주간 그래프는 점수 산식을 바꾸지 않습니다. 3Y Forward PER 점수는 기존의 검증 가능한 과거 스냅샷 중 가장 최근 비교 가능한 3개만 사용합니다. 현금흐름 품질 점수는 최근 3개 공통 회계연도의 누적 영업현금흐름 ÷ 누적 순이익입니다.</p>
      </section>`;
  }

  function chartCardMarkup({ title, subtitle, points, unit, references = [], fixedDomain = null, error = null, className = "" }) {
    const clean = points.filter((item) => Number.isFinite(item.value));
    const latest = clean[clean.length - 1];
    return `
      <article class="history-chart-card${className ? ` ${escapeAttr(className)}` : ""}">
        <div class="chart-card-heading">
          <div><strong>${escapeHTML(title)}</strong><span>${escapeHTML(subtitle)}</span></div>
          <strong>${latest ? escapeHTML(formatChartValue(latest.value, unit)) : "—"}</strong>
        </div>
        ${clean.length ? miniLineChartMarkup(clean, { title, unit, references, fixedDomain }) : `<div class="chart-empty"><span>표시할 시계열 데이터가 없습니다.</span>${error ? `<small>${escapeHTML(cleanError(error))}</small>` : "<small>전체 갱신 후 다시 확인하세요.</small>"}</div>`}
      </article>`;
  }

  function dualSeriesChartCardMarkup({ title, subtitle, series, unit, references = [], className = "", metadata = null, error = null }) {
    const cleanSeries = series.map((item) => ({
      ...item,
      points: item.points.filter((point) => Number.isFinite(point.value)).sort((left, right) => String(left.key || "").localeCompare(String(right.key || ""))),
    }));
    const hasData = metadata?.available !== false && cleanSeries.some((item) => item.points.length);
    const pointCounts = cleanSeries.map((item) => item.points.length);
    const currentValues = cleanSeries.map((item, index) => {
      const latest = item.points[item.points.length - 1];
      return `<span class="is-${escapeAttr(item.key)}"><i></i><em>${escapeHTML(item.label)}</em><small>${latest ? `${pointCounts[index]}점` : "자료 없음"}</small><b>${latest ? escapeHTML(formatChartValue(latest.value, unit)) : "—"}</b></span>`;
    }).join("");
    const observed = chartSeriesObservationMeta(cleanSeries, metadata);
    const dataMetaMarkup = `<div class="chart-data-meta" aria-label="주간 PER 데이터 범위와 빈도">
      <span class="chart-data-meta-frequency"><b>${escapeHTML(observed.frequencyLabel)}</b></span>
      <span>${escapeHTML(observed.countLabel)}</span>
      <span>${escapeHTML(observed.rangeLabel)}</span>
    </div>`;
    const emptyMessage = metadata?.available === false
      ? "표시할 주간 PER 데이터가 없습니다."
      : "표시할 5년 주간 PER 데이터가 없습니다.";
    const emptyDetail = metadata?.available === false
      ? metadata.note
      : (error ? cleanError(error) : "전체 갱신 후 다시 확인하세요.");
    return `
      <article class="history-chart-card dual-series-card${className ? ` ${escapeAttr(className)}` : ""}">
        <div class="chart-card-heading">
          <div><strong>${escapeHTML(title)}</strong><span>${escapeHTML(subtitle)}</span></div>
        </div>
        <div class="chart-series-summary" aria-label="차트 범례와 최신 값">${currentValues}</div>
        ${dataMetaMarkup}
        ${hasData
          ? dualLineChartMarkup(cleanSeries, { title, unit, references })
          : `<div class="chart-empty" role="status"><span>${escapeHTML(emptyMessage)}</span><small>${escapeHTML(emptyDetail || "상세 화면을 다시 열어 갱신해 보세요.")}</small></div>`}
      </article>`;
  }

  function chartSeriesObservationMeta(series, metadata = null) {
    const rows = series.flatMap((item) => item.points).sort((left, right) => String(left.key || "").localeCompare(String(right.key || "")));
    const keys = [...new Set(rows.map((item) => String(item.key || "")).filter(Boolean))];
    const counts = series.map((item) => item.points.length);
    const total = rows.length;
    const countLabel = total
      ? `${counts.map((count, index) => `${series[index].label} ${count}점`).join(" · ")} · 총 ${total}관측치`
      : "0관측치";
    const start = metadata?.start || keys[0] || null;
    const end = metadata?.end || keys[keys.length - 1] || null;
    const rangeLabel = start && end
      ? `${formatDate(start, { includeTime: false })} ~ ${formatDate(end, { includeTime: false })}`
      : "날짜 범위 없음";
    return {
      frequencyLabel: metadata?.available === false ? "주간 빈도 확인 불가" : (metadata?.frequency === "weekly" ? "주간 · 매주 마지막 거래일" : "주간"),
      countLabel,
      rangeLabel,
    };
  }

  function miniLineChartMarkup(points, { title, unit, references = [], fixedDomain = null }) {
    const width = 420;
    const height = 158;
    const padding = { top: 18, right: 14, bottom: 28, left: 18 };
    const values = [...points.map((item) => item.value), ...references.map((item) => item.value)].filter(Number.isFinite);
    let minimum = fixedDomain ? fixedDomain[0] : Math.min(...values);
    let maximum = fixedDomain ? fixedDomain[1] : Math.max(...values);
    if (minimum === maximum) {
      const spread = Math.max(Math.abs(minimum) * 0.08, 1);
      minimum -= spread;
      maximum += spread;
    } else if (!fixedDomain) {
      const spread = (maximum - minimum) * 0.12;
      minimum -= spread;
      maximum += spread;
    }
    const plotWidth = width - padding.left - padding.right;
    const plotHeight = height - padding.top - padding.bottom;
    const x = (index) => padding.left + (points.length === 1 ? plotWidth / 2 : index / (points.length - 1) * plotWidth);
    const y = (value) => padding.top + (maximum - value) / (maximum - minimum) * plotHeight;
    const path = points.map((item, index) => `${index ? "L" : "M"}${x(index).toFixed(2)},${y(item.value).toFixed(2)}`).join(" ");
    const area = points.length > 1 ? `${path} L${x(points.length - 1).toFixed(2)},${(padding.top + plotHeight).toFixed(2)} L${x(0).toFixed(2)},${(padding.top + plotHeight).toFixed(2)} Z` : "";
    const grid = [0, 0.5, 1].map((ratio) => {
      const lineY = padding.top + ratio * plotHeight;
      return `<line x1="${padding.left}" y1="${lineY.toFixed(2)}" x2="${width - padding.right}" y2="${lineY.toFixed(2)}"></line>`;
    }).join("");
    const referenceMarkup = references.filter((item) => item.value >= minimum && item.value <= maximum).map((item) => {
      const lineY = y(item.value);
      return `<g class="chart-reference"><line x1="${padding.left}" y1="${lineY.toFixed(2)}" x2="${width - padding.right}" y2="${lineY.toFixed(2)}"></line><text x="${width - padding.right}" y="${Math.max(10, lineY - 4).toFixed(2)}" text-anchor="end">${escapeHTML(item.label)}</text></g>`;
    }).join("");
    const showDots = points.length <= 12;
    const dots = points.map((item, index) => {
      if (!showDots && index !== points.length - 1) return "";
      return `<circle cx="${x(index).toFixed(2)}" cy="${y(item.value).toFixed(2)}" r="${index === points.length - 1 ? 3.2 : 2.4}"><title>${escapeHTML(`${item.detail || item.label}: ${formatChartValue(item.value, unit)}`)}</title></circle>`;
    }).join("");
    const first = points[0];
    const last = points[points.length - 1];
    return `
      <svg class="mini-line-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeAttr(`${title} 추세: ${first.label}부터 ${last.label}까지`)}">
        <g class="chart-grid">${grid}</g>
        ${area ? `<path class="chart-area" d="${area}"></path>` : ""}
        ${referenceMarkup}
        <path class="chart-line" d="${path}"></path>
        <g class="chart-dots">${dots}</g>
        <text class="chart-axis-label chart-axis-left" x="${padding.left}" y="${height - 7}">${escapeHTML(first.label)}</text>
        <text class="chart-axis-label chart-axis-right" x="${width - padding.right}" y="${height - 7}" text-anchor="end">${escapeHTML(last.label)}</text>
        <text class="chart-axis-value" x="${padding.left}" y="12">${escapeHTML(formatChartValue(maximum, unit))}</text>
      </svg>`;
  }

  function dualLineChartMarkup(series, { title, unit, references = [] }) {
    const width = 620;
    const height = 220;
    const padding = { top: 22, right: 18, bottom: 34, left: 28 };
    const pointKeys = series.flatMap((item) => item.points.map((point) => point.key || point.label));
    const keys = [...new Set(pointKeys)].sort(compareValuationPeriodKeys);
    const labelByKey = new Map();
    series.forEach((item) => item.points.forEach((point) => labelByKey.set(point.key || point.label, point.label)));
    const values = [
      ...series.flatMap((item) => item.points.map((point) => point.value)),
      ...references.map((item) => item.value),
    ].filter(Number.isFinite);
    let minimum = Math.min(...values);
    let maximum = Math.max(...values);
    if (minimum === maximum) {
      const spread = Math.max(Math.abs(minimum) * 0.08, 1);
      minimum -= spread;
      maximum += spread;
    } else {
      const spread = (maximum - minimum) * 0.12;
      minimum = Math.max(0, minimum - spread);
      maximum += spread;
    }
    const plotWidth = width - padding.left - padding.right;
    const plotHeight = height - padding.top - padding.bottom;
    const x = (index) => padding.left + (keys.length === 1 ? plotWidth / 2 : index / (keys.length - 1) * plotWidth);
    const y = (value) => padding.top + (maximum - value) / (maximum - minimum) * plotHeight;
    const keyIndex = new Map(keys.map((key, index) => [key, index]));
    const calendarGapDays = (previousKey, currentKey) => {
      if (!isIsoCalendarDate(previousKey) || !isIsoCalendarDate(currentKey)) return null;
      const previousTime = Date.parse(`${previousKey}T00:00:00Z`);
      const currentTime = Date.parse(`${currentKey}T00:00:00Z`);
      const gapDays = (currentTime - previousTime) / 86400000;
      return Number.isFinite(gapDays) ? gapDays : null;
    };
    const grid = [0, 0.5, 1].map((ratio) => {
      const lineY = padding.top + ratio * plotHeight;
      return `<line x1="${padding.left}" y1="${lineY.toFixed(2)}" x2="${width - padding.right}" y2="${lineY.toFixed(2)}"></line>`;
    }).join("");
    const referenceMarkup = references.filter((item) => item.value >= minimum && item.value <= maximum).map((item) => {
      const lineY = y(item.value);
      return `<g class="chart-reference"><line x1="${padding.left}" y1="${lineY.toFixed(2)}" x2="${width - padding.right}" y2="${lineY.toFixed(2)}"></line><text x="${width - padding.right}" y="${Math.max(11, lineY - 4).toFixed(2)}" text-anchor="end">${escapeHTML(item.label)}</text></g>`;
    }).join("");
    const lineMarkup = series.map((item) => {
      const tone = item.key === "trailing" ? "trailing" : "forward";
      const ordered = [...item.points].sort((left, right) => {
        const leftIndex = keyIndex.get(left.key || left.label) ?? 0;
        const rightIndex = keyIndex.get(right.key || right.label) ?? 0;
        return leftIndex - rightIndex;
      });
      let pathSegmentCount = 0;
      const path = ordered.map((point, index) => {
        const pointKey = point.key || point.label;
        const pointX = x(keyIndex.get(pointKey) ?? index);
        const previousPoint = ordered[index - 1];
        const previousKey = previousPoint?.key || previousPoint?.label;
        const gapDays = previousPoint ? calendarGapDays(previousKey, pointKey) : null;
        const startsNewSegment = index === 0 || (gapDays !== null && gapDays > 14);
        if (startsNewSegment) pathSegmentCount += 1;
        return `${startsNewSegment ? "M" : "L"}${pointX.toFixed(2)},${y(point.value).toFixed(2)}`;
      }).join(" ");
      const dots = ordered.map((point, index) => {
        const pointX = x(keyIndex.get(point.key || point.label) ?? index);
        const latest = index === ordered.length - 1;
        // Draw every weekly observation.  The previous stride kept only ~24
        // visible dots, which made a 241-week series look like a handful of
        // annual points even though the path contained all rows.
        const radius = latest ? 4 : ordered.length > 180 ? 1.55 : ordered.length > 80 ? 1.8 : 2.2;
        return `<circle class="weekly-observation-point" data-observation-index="${index}" cx="${pointX.toFixed(2)}" cy="${y(point.value).toFixed(2)}" r="${radius}"><title>${escapeHTML(`${item.label} · ${point.detail || point.label}: ${formatChartValue(point.value, unit)}`)}</title></circle>`;
      }).join("");
      const hitPoints = ordered.map((point, index) => {
        const pointX = x(keyIndex.get(point.key || point.label) ?? index);
        return `<circle class="weekly-observation-hit" data-observation-index="${index}" tabindex="0" cx="${pointX.toFixed(2)}" cy="${y(point.value).toFixed(2)}" r="7"><title>${escapeHTML(`${item.label} · ${point.detail || point.label}: ${formatChartValue(point.value, unit)}`)}</title></circle>`;
      }).join("");
      return `<path class="chart-line is-${tone}" data-path-segment-count="${pathSegmentCount}" d="${path}"></path><g class="chart-dots is-${tone}">${dots}</g><g class="chart-hit-points is-${tone}">${hitPoints}</g>`;
    }).join("");
    const axisLabelIndices = new Set();
    const axisLabelCount = Math.min(6, keys.length);
    for (let position = 0; position < axisLabelCount; position += 1) {
      axisLabelIndices.add(Math.round(position * (keys.length - 1) / Math.max(axisLabelCount - 1, 1)));
    }
    const axisLabels = keys.map((key, index) => {
      if (!axisLabelIndices.has(index)) return "";
      return `
        <text class="chart-axis-label" x="${x(index).toFixed(2)}" y="${height - 9}" text-anchor="${index === 0 ? "start" : index === keys.length - 1 ? "end" : "middle"}">${escapeHTML(labelByKey.get(key) || key)}</text>
      `;
    }).join("");
    const observationRug = keys.map((key, index) => {
      const pointX = x(index).toFixed(2);
      return `<line data-observation-index="${index}" x1="${pointX}" y1="${(padding.top + plotHeight + 3).toFixed(2)}" x2="${pointX}" y2="${(padding.top + plotHeight + 8).toFixed(2)}"></line>`;
    }).join("");
    const rangeLabel = keys.length
      ? `${labelByKey.get(keys[0]) || keys[0]}부터 ${labelByKey.get(keys[keys.length - 1]) || keys[keys.length - 1]}까지`
      : "데이터 없음";
    const observationCount = series.reduce((total, item) => total + item.points.length, 0);
    return `
      <svg class="mini-line-chart dual-line-chart" viewBox="0 0 ${width} ${height}" role="img" data-frequency="weekly" data-observation-count="${observationCount}" aria-label="${escapeAttr(`${title} 주간 비교 추세: ${rangeLabel}, ${observationCount}개 관측치`)}">
        <g class="chart-grid">${grid}</g>
        ${referenceMarkup}
        ${lineMarkup}
        <g class="chart-observation-rug" aria-label="주간 관측일 표시">${observationRug}</g>
        ${axisLabels}
        <text class="chart-axis-value" x="${padding.left}" y="13">${escapeHTML(formatChartValue(maximum, unit))}</text>
        <text class="chart-axis-value" x="${padding.left}" y="${padding.top + plotHeight - 4}">${escapeHTML(formatChartValue(minimum, unit))}</text>
      </svg>`;
  }

  function historyPeriodLabel(value) {
    const text = String(value || "");
    if (text === "legacy_current") return "최근";
    const legacy = text.match(/^legacy_fy_minus_(\d+)$/);
    if (legacy) return `T-${legacy[1]}`;
    const year = text.match(/^(\d{4})/);
    return year ? year[1] : text.slice(0, 8);
  }

  function shortChartDate(value) {
    const text = String(value || "");
    const match = text.match(/^(\d{4})-(\d{2})/);
    return match ? `${match[1].slice(2)}.${match[2]}` : text;
  }

  function formatChartValue(value, unit) {
    if (unit === "percent") return formatPercent(value);
    if (unit === "multiple") return formatMultiple(value);
    if (unit === "ratio") return formatRatio(value);
    if (unit === "index") return formatNumber(value, 0);
    if (unit === "shares") return formatShares(value);
    return formatNumber(value, 1);
  }

  function formatShares(value) {
    const number = numberOrNull(value);
    if (number === null) return "—";
    if (Math.abs(number) >= 1e9) return `${formatNumber(number / 1e9, 2)}B`;
    if (Math.abs(number) >= 1e6) return `${formatNumber(number / 1e6, 1)}M`;
    return formatNumber(number, 0);
  }

  function trapDrawerFocus(event) {
    const focusable = Array.from(el.stockDrawer.querySelectorAll('button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), [tabindex]:not([tabindex="-1"])')).filter((node) => !node.hidden && node.offsetParent !== null);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }

  async function refreshData() {
    if (state.refreshing) {
      showToast("이미 데이터 갱신이 진행 중입니다.");
      return;
    }
    const mode = el.refreshMode.value === "quick" ? "quick" : "full";
    const modeLabel = mode === "full" ? "전체 갱신" : "빠른 갱신";
    startRefreshUI(6, `${modeLabel} 요청 중`);
    try {
      const payload = await fetchJSON(API.refresh, { method: "POST", body: JSON.stringify({ mode }) }, 18000);
      const refresh = firstObject(payload.refresh, payload.status, {});
      syncRefreshFromServer(refresh);
      showToast(`${modeLabel}을 시작했습니다.${mode === "full" ? " 펀더멘털까지 갱신합니다." : " 가격·시장 데이터만 갱신합니다."}`);
      scheduleRefreshPoll(0);
    } catch (error) {
      if (error.status === 409) {
        showToast("서버에서 이미 갱신 작업이 진행 중입니다.");
        syncRefreshFromServer(firstObject(error.payload?.refresh, { running: true }));
        scheduleRefreshPoll(0);
      } else {
        finishRefreshUI(false);
        showToast(`갱신 요청 실패 · ${cleanError(error)}`, true);
      }
    }
  }

  function startRefreshUI(progress = 4, label = "데이터 갱신 중") {
    state.refreshing = true;
    state.refreshProgress = clamp(numberOrNull(progress) ?? 4, 2, 92);
    [el.refreshButton, el.mobileRefreshButton].forEach((button) => {
      button.disabled = true;
      button.classList.add("is-refreshing");
    });
    el.refreshButton.querySelector("span").textContent = label;
    el.mobileRefreshButton.querySelector("span").textContent = "갱신 중";
    el.refreshTrack.hidden = false;
    el.refreshProgress.style.width = `${state.refreshProgress}%`;
    window.clearInterval(state.refreshTimer);
    state.refreshTimer = window.setInterval(() => {
      if (state.refreshProgress < 88) {
        state.refreshProgress += state.refreshProgress < 45 ? 4 : state.refreshProgress < 72 ? 2 : 0.5;
        el.refreshProgress.style.width = `${state.refreshProgress}%`;
      }
    }, 700);
  }

  function syncRefreshFromServer(refresh) {
    if (!refresh || typeof refresh !== "object" || !Object.keys(refresh).length) return;
    const running = refresh.running === true || ["queued", "running", "refreshing", "accepted", "in_progress"].includes(String(refresh.status || "").toLowerCase());
    const progress = numberOrNull(firstDefined(refresh.progress, refresh.percent, refresh.progress_pct));
    if (running) {
      startRefreshUI(progress ?? Math.max(state.refreshProgress, 12), refresh.message || "데이터 갱신 중");
      scheduleRefreshPoll();
      return;
    }
    if (refresh.last_error || ["failed", "error"].includes(String(refresh.status || "").toLowerCase())) {
      finishRefreshUI(false);
      showToast(`데이터 갱신 실패 · ${String(refresh.last_error || refresh.message || "서버 오류")}`, true);
      return;
    }
    if (state.refreshing && (refresh.finished_at || ["complete", "completed", "done", "idle"].includes(String(refresh.status || "").toLowerCase()))) {
      finishRefreshUI(true);
      showToast("최신 데이터로 갱신했습니다.");
    }
  }

  function scheduleRefreshPoll(delay = 1800) {
    if (state.refreshPollTimer) return;
    state.refreshPollTimer = window.setTimeout(async () => {
      state.refreshPollTimer = null;
      if (!state.refreshing) return;
      try {
        const payload = await fetchJSON(API.dashboard, {}, 18000);
        const dashboard = normalizeDashboard(payload);
        state.dashboard = dashboard;
        state.stocks = dashboard.stocks;
        state.isDemo = false;
        el.environmentBanner.hidden = true;
        renderDashboard();
        const refresh = dashboard.refresh;
        const stillRunning = refresh?.running === true || ["queued", "running", "refreshing", "in_progress"].includes(String(refresh?.status || "").toLowerCase());
        if (stillRunning) {
          syncRefreshFromServer(refresh);
          scheduleRefreshPoll(1800);
        } else {
          finishRefreshUI(!refresh?.last_error);
          showToast(refresh?.last_error ? `갱신 실패 · ${refresh.last_error}` : "최신 데이터로 갱신했습니다.", Boolean(refresh?.last_error));
        }
      } catch (error) {
        showGlobalAlert("갱신 상태를 확인하지 못했습니다.", cleanError(error));
        scheduleRefreshPoll(3000);
      }
    }, delay);
  }

  function finishRefreshUI(success) {
    window.clearInterval(state.refreshTimer);
    state.refreshTimer = null;
    if (success) {
      state.refreshProgress = 100;
      el.refreshProgress.style.width = "100%";
    }
    window.setTimeout(() => {
      state.refreshing = false;
      state.refreshProgress = 0;
      [el.refreshButton, el.mobileRefreshButton].forEach((button) => {
        button.disabled = false;
        button.classList.remove("is-refreshing");
      });
      el.refreshButton.querySelector("span").textContent = "갱신 실행";
      el.mobileRefreshButton.querySelector("span").textContent = "갱신";
      el.refreshTrack.hidden = true;
      el.refreshProgress.style.width = "0%";
    }, success ? 450 : 0);
  }

  function initializeRefreshMode() {
    let preferred = window.matchMedia?.("(max-width: 720px)").matches ? "quick" : el.refreshMode.value;
    try {
      const saved = window.localStorage.getItem("quant-refresh-mode");
      if (saved === "quick" || saved === "full") preferred = saved;
    } catch (_) {
      // 저장소를 사용할 수 없는 WebView에서도 기본값으로 계속 동작합니다.
    }
    setRefreshMode(preferred, { persist: false });
  }

  function setRefreshMode(mode, { persist = true } = {}) {
    const next = mode === "quick" ? "quick" : "full";
    el.refreshMode.value = next;
    el.mobileRefreshMode.value = next;
    const full = next === "full";
    el.refreshDuration.textContent = full ? "수 분 이상 · 최초 실행 권장" : "약 1–3분 · 가격과 시장만";
    el.refreshButton.setAttribute("aria-label", full ? "전체 데이터 갱신 실행, 펀더멘털 포함" : "빠른 데이터 갱신 실행, 가격과 시장만");
    el.mobileRefreshButton.setAttribute("aria-label", full ? "전체 데이터 갱신 실행, 펀더멘털 포함" : "빠른 데이터 갱신 실행, 가격과 시장만");
    if (persist) {
      try { window.localStorage.setItem("quant-refresh-mode", next); } catch (_) { /* 선택 저장은 필수가 아닙니다. */ }
    }
  }

  function syncNativeShell() {
    const connected = Boolean(window.AndroidQuant && typeof window.AndroidQuant.openSettings === "function");
    document.body.classList.toggle("is-native-shell", connected);
    el.nativeSettingsButtons.forEach((button) => { button.hidden = !connected; });
  }

  function openNativeSettings() {
    try {
      if (!window.AndroidQuant || typeof window.AndroidQuant.openSettings !== "function") {
        showToast("앱 연결 설정은 Android 앱에서 사용할 수 있습니다.");
        return;
      }
      window.AndroidQuant.openSettings();
    } catch (error) {
      showToast(`앱 연결 설정을 열지 못했습니다 · ${cleanError(error)}`, true);
    }
  }

  function registerInstallExperience() {
    window.addEventListener("beforeinstallprompt", (event) => {
      event.preventDefault();
      state.deferredInstallPrompt = event;
      el.installAppButton.hidden = false;
    });
    window.addEventListener("appinstalled", () => {
      state.deferredInstallPrompt = null;
      el.installAppButton.hidden = true;
      showToast("홈 화면에 퀀트 시그널을 설치했습니다.");
    });
    el.installAppButton.addEventListener("click", async () => {
      const promptEvent = state.deferredInstallPrompt;
      if (!promptEvent) return;
      await promptEvent.prompt();
      state.deferredInstallPrompt = null;
      el.installAppButton.hidden = true;
    });
    if ("serviceWorker" in navigator && window.isSecureContext) {
      window.addEventListener("load", () => {
        navigator.serviceWorker.register("./sw.js?v=21", { scope: "./", updateViaCache: "none" }).catch(() => {
          // 로컬 HTTP WebView처럼 서비스 워커가 제한된 환경에서도 본 앱은 정상 동작합니다.
        });
      }, { once: true });
    }
  }

  function normalizeDashboardSources(root, universe, rawStocks) {
    const quality = firstObject(root?.data_quality, root?.dataQuality, {});
    const explicit = firstObject(root?.sources, root?.data_sources, root?.dataSources, {});
    const firstStock = rawStocks.find((stock) => stock && typeof stock === "object") || {};
    const priceProvenance = bestProvenance(firstStock?.metrics?.current_price?.provenance || firstStock?.price?.provenance);
    const fundamentalMetric = firstDefined(firstStock?.metrics?.forward_pe, firstStock?.metrics?.debt_to_equity, firstStock?.fundamentals);
    const fundamentalProvenance = bestProvenance(fundamentalMetric?.provenance);
    const universeProvenance = bestProvenance(universe?.provenance);
    return {
      price: sourceText(firstDefined(quality.price_source, quality.priceSource, explicit.price, explicit.prices, explicit.market, priceProvenance?.source), "출처 미표기"),
      fundamentals: sourceText(firstDefined(quality.fundamental_source, quality.fundamentals_source, quality.fundamentalSource, explicit.fundamentals, explicit.financials, explicit.valuation, fundamentalProvenance?.source), "출처 미표기"),
      universe: sourceText(firstDefined(explicit.universe, universeProvenance?.source), "S&P 500 구성종목 스냅샷"),
      generatedAt: firstDefined(root?.generated_at, root?.generatedAt),
    };
  }

  function normalizeStockSources(raw, metrics, fallback = {}) {
    const explicit = firstObject(raw?.sources, raw?.data_sources, raw?.dataSources, {});
    const priceProvenance = bestProvenance(raw?.metrics?.current_price?.provenance) || metrics.drawdown.provenance.find((item) => item.source !== "calculation");
    const fundamentalProvenance = bestProvenance(raw?.metrics?.forward_pe?.provenance) || bestProvenance(raw?.metrics?.debt_to_equity?.provenance) || metrics.epsGrowth.provenance.find((item) => item.source !== "calculation");
    return {
      price: sourceText(firstDefined(explicit.price, explicit.prices, priceProvenance?.source, fallback?.price), "출처 미표기"),
      fundamentals: sourceText(firstDefined(explicit.fundamentals, explicit.financials, explicit.valuation, fundamentalProvenance?.source, fallback?.fundamentals), "출처 미표기"),
      universe: sourceText(firstDefined(explicit.universe, fallback?.universe), "S&P 500 구성종목 스냅샷"),
    };
  }

  function sourceFromFallback(metricKey, sources) {
    if (!sources) return "";
    const key = normalizeKey(metricKey);
    const isPrice = ["currentprice", "high52week", "drawdown52weekpct", "sma200", "distance200dmapct", "rsi14"].includes(key);
    return isPrice ? sources.price : sources.fundamentals;
  }

  function normalizeProvenance(raw) {
    const list = Array.isArray(raw) ? raw : raw && typeof raw === "object" ? [raw] : raw ? [raw] : [];
    return list.map((item) => {
      if (typeof item === "string") return { source: item, retrievedAt: null, asOf: null, sourceUrl: null, basis: null, official: false, notes: [] };
      return {
        source: String(firstDefined(item?.source, item?.provider, item?.name, "출처 미표기")),
        retrievedAt: firstDefined(item?.retrieved_at, item?.retrievedAt, item?.fetched_at, item?.fetchedAt),
        asOf: firstDefined(item?.as_of, item?.asOf, item?.date),
        sourceUrl: firstDefined(item?.source_url, item?.sourceUrl, item?.url),
        basis: firstDefined(item?.basis, item?.method),
        official: item?.official === true,
        notes: firstArray(item?.notes).map(String),
      };
    });
  }

  function bestProvenance(raw) {
    const items = normalizeProvenance(raw);
    return items.find((item) => item.source !== "calculation" && item.official) || items.find((item) => item.source !== "calculation") || items[0] || null;
  }

  function firstProvenance(metrics) {
    for (const metric of metrics) {
      const preferred = metric?.provenance?.find((item) => item.source !== "calculation") || metric?.provenance?.[0];
      if (preferred) return preferred;
    }
    return null;
  }

  function extractStockPayload(payload, ticker) {
    let root = unwrapObject(payload, ["stock", "signal", "result"]);
    if (Array.isArray(root.signals)) root = root.signals.find((item) => String(item?.symbol || item?.ticker || "").toUpperCase() === ticker) || root.signals[0];
    if (!root || typeof root !== "object" || Array.isArray(root)) throw new Error("종목 상세 응답 형식이 올바르지 않습니다.");
    return root;
  }

  function mergeStockRaw(base, detail) {
    return {
      ...(base || {}),
      ...(detail || {}),
      company: { ...firstObject(base?.company, {}), ...firstObject(detail?.company, {}) },
      metrics: { ...firstObject(base?.metrics, {}), ...firstObject(detail?.metrics, {}) },
      components: firstArray(detail?.components).length ? detail.components : firstArray(base?.components),
      warnings: firstArray(detail?.warnings).length ? detail.warnings : firstArray(base?.warnings),
    };
  }

  function canonicalSignal(raw, score, insufficient = false) {
    if (insufficient) return "insufficient";
    const key = normalizeKey(String(raw || ""));
    const map = {
      strongbuy: "strong-buy", strongpurchase: "strong-buy", 강한매수: "strong-buy", 강력매수: "strong-buy",
      buy: "buy-watch", watchbuy: "buy-watch", buywatch: "buy-watch", 매수: "buy-watch", 매수대기: "buy-watch", 매수관심: "buy-watch",
      neutral: "neutral", hold: "neutral", watch: "neutral", 관망: "neutral", 중립: "neutral",
      watchsell: "sell-watch", sellwatch: "sell-watch", 매도대기: "sell-watch", 매도관심: "sell-watch",
      sell: "sell", 매도: "sell",
      insufficient: "insufficient", insufficientdata: "insufficient", datamissing: "insufficient", 데이터부족: "insufficient",
    };
    if (map[key]) return map[key];
    if (score === null) return "insufficient";
    if (score >= 10) return "strong-buy";
    if (score >= 5) return "buy-watch";
    if (score >= -2) return "neutral";
    if (score >= -7) return "sell-watch";
    return "sell";
  }

  function isInsufficientLabel(raw) {
    const key = normalizeKey(String(raw || ""));
    return ["insufficient", "insufficientdata", "datamissing", "missing", "데이터부족"].includes(key);
  }

  function marketVerdict(score) {
    if (score >= 2) return "매수 우호";
    if (score <= -2) return "주의";
    return "관망";
  }

  function signalBadge(stock) {
    const meta = SIGNALS[stock.signalType];
    const score = stock.insufficient || stock.score === null ? "추천 제외" : formatSigned(stock.score);
    return `<span class="signal-badge is-${stock.signalType}"><span>${escapeHTML(meta.label)}</span><span class="signal-score">${escapeHTML(score)}</span></span>`;
  }

  function metricCell(metric, formatter) {
    const missing = metric.value === null || ["missing", "invalid"].includes(metric.status);
    const value = missing ? "—" : formatter(metric.value);
    const title = [metric.status === "stale" ? "오래된 데이터" : "", metric.reason, metric.note, metric.source].filter(Boolean).join(" · ");
    return `<span class="metric-value${missing ? " is-missing" : ""}" title="${escapeAttr(title)}">${escapeHTML(value)}</span>${scoreChip(metric.score, missing, metric.applicable)}`;
  }

  function plainMetricCell(metric, formatter, fallback = null) {
    const value = numericMetricValue(metric, fallback);
    return `<span class="metric-value${value === null ? " is-missing" : ""}">${escapeHTML(formatter(value))}</span>`;
  }

  function scoreChip(score, missing = false, applicable = true) {
    if (!applicable) return '<span class="score-chip is-zero" title="업종 또는 규칙상 적용 제외">제외</span>';
    if (missing || score === null || score === undefined) return '<span class="score-chip is-zero" title="결측값은 0점으로 대체하지 않고 추천 커버리지에서 제외">N/A</span>';
    const number = numberOrNull(score) ?? 0;
    const tone = number > 0 ? "positive" : number < 0 ? "negative" : "zero";
    return `<span class="score-chip is-${tone}" aria-label="${number > 0 ? "가점" : number < 0 ? "감점" : "점수"} ${formatSigned(number)}">${formatSigned(number)}</span>`;
  }

  function statusLabel(status, applicable = true) {
    if (!applicable) return "업종 기준 적용 제외";
    const labels = { ok: "정상", stale: "기준일 경과", missing: "데이터 결측", invalid: "유효하지 않은 값" };
    return labels[String(status || "").toLowerCase()] || "상태 미표기";
  }

  function formatRuleRange(rule) {
    const scores = rule.bands.map((band) => band.score).filter((value) => value !== null);
    if (!scores.length) return "RULE";
    return `${formatSigned(Math.min(...scores))} ~ ${formatSigned(Math.max(...scores))}`;
  }

  function formatBandCondition(band, unit) {
    if (band.operator === "default" || band.condition === "그 외 구간") return "그 외 구간";
    const value = numberOrNull(band.value);
    if (value === null) return band.condition || "조건 미표기";
    const suffix = { "<=": "이하", "<": "미만", ">=": "이상", ">": "초과", "==": "일 때" }[band.operator] || "";
    return `${formatThreshold(value, unit)} ${suffix}`.trim();
  }

  function formatThreshold(value, unit) {
    const normalized = String(unit || "").toLowerCase();
    if (normalized.includes("percentile")) return `${formatNumber(value, Number.isInteger(value) ? 0 : 1)}백분위`;
    if (normalized.includes("percent") || normalized === "%") return `${formatNumber(value, Number.isInteger(value) ? 0 : 1)}%`;
    if (normalized.includes("ratio")) return formatNumber(value, 2);
    return formatNumber(value, Number.isInteger(value) ? 0 : 1);
  }

  function formatByUnit(value, unit) {
    if (value === null || value === undefined) return "—";
    const normalized = String(unit || "").toLowerCase();
    if (normalized.includes("percentile")) return formatPercentile(value);
    if (normalized.includes("percent") || normalized === "%") return formatPercent(value);
    if (normalized === "usd" || normalized.includes("currency")) return formatPrice(value);
    if (normalized.includes("multiple")) return formatMultiple(value);
    if (normalized.includes("ratio")) return formatRatio(value);
    if (normalized.includes("index")) return formatIndex(value);
    return formatNumber(value, 2);
  }

  function formatPercent(value) {
    const number = numberOrNull(value);
    return number === null ? "—" : `${number > 0 ? "+" : ""}${formatNumber(number, 1)}%`;
  }

  function formatPercentNumber(value, digits = 1) {
    const number = numberOrNull(value);
    return number === null ? "—" : `${formatNumber(number, digits)}%`;
  }

  function formatPercentile(value) {
    const number = numberOrNull(value);
    return number === null ? "—" : `${formatNumber(number, 0)}P`;
  }

  function formatPrice(value) {
    const number = numberOrNull(value);
    if (number === null) return "—";
    return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(number);
  }

  function formatMultiple(value) {
    const number = numberOrNull(value);
    return number === null ? "—" : `${formatNumber(number, 2)}x`;
  }

  function formatRatio(value) {
    const number = numberOrNull(value);
    return number === null ? "—" : formatNumber(number, 2);
  }

  function formatIndex(value) {
    const number = numberOrNull(value);
    return number === null ? "—" : formatNumber(number, 0);
  }

  function formatCoverage(value) {
    const number = numberOrNull(value);
    return number === null ? "미표기" : `${formatNumber(number, 1)}%`;
  }

  function formatSigned(value) {
    const number = numberOrNull(value);
    if (number === null) return "—";
    return `${number > 0 ? "+" : ""}${formatNumber(number, Number.isInteger(number) ? 0 : 1)}`;
  }

  function formatNumber(value, digits = 1) {
    const number = numberOrNull(value);
    if (number === null) return "—";
    return new Intl.NumberFormat("ko-KR", { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(number);
  }

  function formatInteger(value) {
    const number = numberOrNull(value);
    return number === null ? "0" : new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 0 }).format(number);
  }

  function formatDate(value, { includeTime = false } = {}) {
    if (!value) return "기준일 미표기";
    const date = new Date(String(value).length === 10 ? `${value}T00:00:00` : value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat("ko-KR", includeTime
      ? { timeZone: "Asia/Seoul", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }
      : { timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit" }).format(date);
  }

  function normalizeCoverage(raw, count, total) {
    let number = numberOrNull(raw);
    const countNumber = numberOrNull(count);
    const totalNumber = numberOrNull(total);
    const derived = countNumber !== null && totalNumber ? countNumber / totalNumber * 100 : null;
    if (number === null) number = derived;
    else if (number >= 0 && number <= 1 && derived !== null && derived > 1) number = derived;
    else if (number >= 0 && number <= 1) number *= 100;
    return number === null ? null : clamp(number, 0, 100);
  }

  function compareNullable(a, b, direction = 1) {
    const left = numberOrNull(a);
    const right = numberOrNull(b);
    if (left === null && right === null) return 0;
    if (left === null) return 1;
    if (right === null) return -1;
    return (left - right) * direction;
  }

  function showGlobalAlert(title, message) {
    el.globalAlertTitle.textContent = title;
    el.globalAlertMessage.textContent = message;
    el.globalAlert.hidden = false;
  }

  function showToast(message, isError = false) {
    const toast = document.createElement("div");
    toast.className = `toast${isError ? " is-error" : ""}`;
    toast.textContent = message;
    el.toastRegion.appendChild(toast);
    window.setTimeout(() => toast.remove(), 4200);
  }

  function cleanError(error) {
    if (!error) return "알 수 없는 오류";
    if (error instanceof TypeError && /fetch/i.test(error.message)) return "로컬 API 서버가 실행 중인지 확인하세요.";
    return String(error.message || error).replace(/^Error:\s*/, "");
  }

  function unwrapObject(payload, preferredKeys = []) {
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) return {};
    for (const key of preferredKeys) {
      if (payload[key] && typeof payload[key] === "object" && !Array.isArray(payload[key])) return payload[key];
    }
    if (payload.data && typeof payload.data === "object" && !Array.isArray(payload.data)) {
      for (const key of preferredKeys) {
        if (payload.data[key] && typeof payload.data[key] === "object" && !Array.isArray(payload.data[key])) return payload.data[key];
      }
      return payload.data;
    }
    return payload;
  }

  function firstDefined(...values) {
    return values.find((value) => value !== undefined && value !== null && value !== "");
  }

  function firstArray(...values) {
    return values.find((value) => Array.isArray(value)) || [];
  }

  function firstObject(...values) {
    return values.find((value) => value && typeof value === "object" && !Array.isArray(value)) || {};
  }

  function getByAliases(object, aliases) {
    if (!object || typeof object !== "object" || Array.isArray(object)) return undefined;
    for (const alias of aliases) if (Object.prototype.hasOwnProperty.call(object, alias)) return object[alias];
    const entries = Object.entries(object);
    for (const alias of aliases) {
      const normalizedAlias = normalizeKey(alias);
      const match = entries.find(([key]) => normalizeKey(key) === normalizedAlias);
      if (match) return match[1];
    }
    return undefined;
  }

  function normalizeKey(value) {
    return String(value || "").toLocaleLowerCase("en-US").replace(/[\s_.\-/()&]+/g, "");
  }

  function extractValue(value) {
    if (value && typeof value === "object" && !Array.isArray(value)) return firstDefined(value.value, value.score, value.points, value.current);
    return value;
  }

  function numberOrNull(value) {
    if (value === null || value === undefined || value === "") return null;
    if (typeof value === "number") return Number.isFinite(value) ? value : null;
    if (typeof value === "boolean") return value ? 1 : 0;
    let text = String(value).trim();
    if (!text || /^(n\/?a|na|null|none|—|-)$/i.test(text)) return null;
    const negativeParentheses = /^\(.*\)$/.test(text);
    text = text.replace(/[,$%x배]/gi, "").replace(/[()]/g, "").trim();
    const number = Number.parseFloat(text);
    if (!Number.isFinite(number)) return null;
    return negativeParentheses ? -number : number;
  }

  function sourceText(value, fallback) {
    if (value && typeof value === "object") return String(firstDefined(value.name, value.source, value.provider, value.label, fallback));
    return String(firstDefined(value, fallback));
  }

  function avatarText(ticker) {
    return String(ticker).replace(/[^A-Z0-9]/gi, "").slice(0, 3) || "S&P";
  }

  function truncate(value, max) {
    const text = String(value || "");
    return text.length > max ? `${text.slice(0, max - 1)}…` : text;
  }

  function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
  }

  function reducedMotion() {
    return window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true;
  }

  function isTypingTarget(target) {
    return target instanceof HTMLElement && (target.matches("input, textarea, select") || target.isContentEditable);
  }

  function escapeHTML(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character]);
  }

  function escapeAttr(value) {
    return escapeHTML(value).replace(/`/g, "&#96;");
  }

  function toCamel(value) {
    return value.replace(/-([a-z])/g, (_, character) => character.toUpperCase());
  }

  function demoMetric(value, unit, source = DEMO_SOURCE, status = value === null ? "missing" : "ok") {
    return {
      value,
      unit,
      status,
      as_of: DEMO_AS_OF,
      provenance: [{ source, retrieved_at: "2026-07-19T13:30:00Z", as_of: DEMO_AS_OF, official: false, notes: ["demo-only"] }],
      note: "API 연결 전 화면 구조 확인용 샘플",
    };
  }

  function demoComponent(key, label, metricKey, value, unit, points, reason, applicable = true) {
    return { key, label, metric_key: metricKey, value, unit, points, applicable, reason, metric_status: value === null ? "missing" : "ok" };
  }

  function demoStock(ticker, name, sector, price, drawdown, forwardPe, peGap, distance, epsGrowth, rsi, debt, points, totalScore) {
    const labels = [
      ["drawdown_52w", "52주 고점 대비 하락", "drawdown_52_week_pct", drawdown, "percent", points[0]],
      ["pe_gap_3y", "3년 Forward PER 괴리", "pe_gap_3y_pct", peGap, "percent", points[1]],
      ["distance_200dma", "200일 이동평균 이격", "distance_200dma_pct", distance, "percent", points[2]],
      ["eps_growth_yoy", "최근 연간 희석 EPS 성장률", "eps_growth_yoy_pct", epsGrowth, "percent", points[3]],
      ["rsi_14", "RSI(14)", "rsi_14", rsi, "index", points[4]],
      ["shareholder_return_3y", "3년 평균 주주환원율 프록시", "shareholder_return_3y_avg_pct", 0, "percent", 0],
      ["shares_dilution_3y", "최근 3년 희석주식수 증감", "shares_change_3y_pct", 0, "percent", 0],
      ["debt_to_equity", "부채/자기자본", "debt_to_equity", debt, "ratio", points[5]],
      ["cash_flow_quality", "3년 현금흐름 품질", "cash_flow_quality_3y", 0.8, "ratio", 0],
    ];
    const components = labels.map((item) => demoComponent(item[0], item[1], item[2], item[3], item[4], item[5], `DEMO 조건 판정 → ${formatSigned(item[5])}`));
    const companyScore = components.reduce((sum, item) => sum + (item.points ?? 0), 0);
    const recommendation = totalScore >= 10 ? "strong_buy" : totalScore >= 5 ? "watch_buy" : totalScore >= -2 ? "neutral" : totalScore >= -7 ? "watch_sell" : "sell";
    return {
      symbol: ticker,
      company: { symbol: ticker, name, sector, sub_industry: "DEMO" },
      metrics: {
        current_price: demoMetric(price, "USD", "demo-price"),
        drawdown_52_week_pct: demoMetric(drawdown, "percent", "demo-price"),
        forward_pe: demoMetric(forwardPe, "multiple", "demo-fundamentals"),
        pe_gap_3y_pct: demoMetric(peGap, "percent", "demo-fundamentals"),
        distance_200dma_pct: demoMetric(distance, "percent", "demo-price"),
        eps_growth_yoy_pct: demoMetric(epsGrowth, "percent", "demo-fundamentals"),
        rsi_14: demoMetric(rsi, "index", "demo-price"),
        shareholder_return_3y_avg_pct: demoMetric(0, "percent", "demo-fundamentals"),
        shares_change_3y_pct: demoMetric(0, "percent", "demo-fundamentals"),
        debt_to_equity: demoMetric(debt, "ratio", "demo-fundamentals"),
        cash_flow_quality_3y: demoMetric(0.8, "ratio", "demo-fundamentals"),
      },
      components,
      company_score: companyScore,
      market_score: -1,
      total_score: totalScore,
      coverage_count: 9,
      coverage_total: 9,
      coverage_pct: 100,
      eligible_for_ranking: true,
      recommendation,
      recommendation_label: SIGNALS[canonicalSignal(recommendation, totalScore, false)].label,
      market_overlay: DEMO_MARKET,
      warnings: [],
    };
  }

  function demoInsufficientStock() {
    return {
      symbol: "BRK.B",
      company: { symbol: "BRK.B", name: "Berkshire Hathaway", sector: "금융", sub_industry: "복합 금융" },
      metrics: {
        current_price: demoMetric(493.8, "USD", "demo-price"),
        drawdown_52_week_pct: demoMetric(-6.2, "percent", "demo-price"),
        forward_pe: demoMetric(null, "multiple", "demo-fundamentals"),
        pe_gap_3y_pct: demoMetric(null, "percent", "demo-fundamentals"),
        distance_200dma_pct: demoMetric(-1.8, "percent", "demo-price"),
        eps_growth_yoy_pct: demoMetric(null, "percent", "demo-fundamentals"),
        rsi_14: demoMetric(48, "index", "demo-price"),
        shareholder_return_3y_avg_pct: demoMetric(null, "percent", "demo-fundamentals"),
        shares_change_3y_pct: demoMetric(null, "percent", "demo-fundamentals"),
        debt_to_equity: demoMetric(null, "ratio", "demo-fundamentals"),
        cash_flow_quality_3y: demoMetric(null, "ratio", "demo-fundamentals"),
      },
      components: [
        demoComponent("drawdown_52w", "52주 고점 대비 하락", "drawdown_52_week_pct", -6.2, "percent", 1, "-6.2% → +1"),
        demoComponent("pe_gap_3y", "3년 Forward PER 괴리", "pe_gap_3y_pct", null, "percent", null, "N/A — 비교 데이터 없음"),
        demoComponent("distance_200dma", "200일 이동평균 이격", "distance_200dma_pct", -1.8, "percent", 0, "기본 구간 → +0"),
        demoComponent("eps_growth_yoy", "최근 연간 희석 EPS 성장률", "eps_growth_yoy_pct", null, "percent", null, "N/A — 비교 가능한 EPS 없음"),
        demoComponent("rsi_14", "RSI(14)", "rsi_14", 48, "index", 0, "기본 구간 → +0"),
        demoComponent("shareholder_return_3y", "3년 평균 주주환원율 프록시", "shareholder_return_3y_avg_pct", null, "percent", null, "N/A — 비교 데이터 없음"),
        demoComponent("shares_dilution_3y", "최근 3년 희석주식수 증감", "shares_change_3y_pct", null, "percent", null, "N/A — 비교 데이터 없음"),
        demoComponent("debt_to_equity", "부채/자기자본", "debt_to_equity", null, "ratio", null, "업종 기준 적용 제외", false),
        demoComponent("cash_flow_quality", "3년 현금흐름 품질", "cash_flow_quality_3y", null, "ratio", null, "업종 기준 적용 제외", false),
      ],
      company_score: 1,
      market_score: -1,
      total_score: 0,
      coverage_count: 3,
      coverage_total: 7,
      coverage_pct: 33.3,
      eligible_for_ranking: false,
      recommendation: "insufficient_data",
      recommendation_label: "데이터 부족",
      market_overlay: DEMO_MARKET,
      warnings: ["화면 예시: 유효 회사 지표가 최소 5개보다 적어 추천 순위에서 제외됩니다."],
    };
  }
})();
