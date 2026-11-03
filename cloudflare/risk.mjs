/** Daily constant-weight risk analysis for the Cloudflare runtime.
 * This implements the documented gs-risk-formula-v1 mathematics in JavaScript;
 * it does not import or execute Python gs-quant or connect to Marquee.
 * fetchHistory(symbol, lookback) supplies adjusted daily [{date, value}] rows.
 */
const DAYS = 252;
const MIN_PRICES = 60;
const FOREIGN_SUFFIXES = new Set(['AS', 'AX', 'BR', 'DE', 'HK', 'JP', 'KS', 'KQ', 'LN', 'L', 'PA', 'SA', 'SI', 'TO']);
const finite = (value) => typeof value === 'number' && Number.isFinite(value) ? value : null;
const mean = (values) => values.reduce((sum, value) => sum + value, 0) / values.length;

function fail(status, code, message, details) {
  const error = new Error(message);
  Object.assign(error, { status, code, ...(details ? { details } : {}) });
  throw error;
}

function symbol(value) {
  if (typeof value !== 'string') return null;
  const result = value.trim().toUpperCase();
  if (!/^[A-Z0-9][A-Z0-9.\-]{0,14}$/.test(result)) return null;
  if (result.includes('.') && FOREIGN_SUFFIXES.has(result.split('.').at(-1))) return null;
  return result;
}

function numeric(value) {
  if (typeof value !== 'number' && typeof value !== 'string') return null;
  if (typeof value === 'string' && !value.trim()) return null;
  return finite(Number(value));
}

function validate(body) {
  const invalid = (message, details) => fail(400, 'validation_error', message, details);
  if (!body || typeof body !== 'object' || Array.isArray(body)) invalid('Request must be a JSON object');
  const extra = Object.keys(body).filter((key) => !['positions', 'benchmark', 'lookback', 'risk_free_rate_pct'].includes(key));
  if (extra.length) invalid('Request contains unsupported fields', { fields: extra });
  if (!Array.isArray(body.positions) || body.positions.length < 2 || body.positions.length > 12) invalid('positions must contain 2 to 12 unique US symbols');
  const seen = new Set();
  const positions = body.positions.map((position) => {
    const ticker = symbol(position?.symbol);
    const weight = numeric(position?.weight_pct);
    if (!ticker) invalid('Each position must contain a valid US symbol');
    if (seen.has(ticker)) invalid('positions must contain unique symbols');
    seen.add(ticker);
    if (weight === null || weight <= 0) invalid('Each position weight_pct must be finite and positive');
    return { symbol: ticker, weight_pct: weight };
  });
  const total = positions.reduce((sum, position) => sum + position.weight_pct, 0);
  if (Math.abs(total - 100) > 0.5 + 1e-10) invalid('Position weights must total 100% within 0.5 percentage points');
  positions.forEach((position) => { position.weight_pct = position.weight_pct * 100 / total; });
  positions.at(-1).weight_pct = 100 - positions.slice(0, -1).reduce((sum, position) => sum + position.weight_pct, 0);
  const benchmark = symbol(body.benchmark);
  if (!benchmark) invalid('benchmark must be a valid US symbol');
  if (!['1y', '3y', '5y'].includes(body.lookback)) invalid('lookback must be one of 1y, 3y, or 5y');
  const rate = numeric(body.risk_free_rate_pct);
  if (rate === null || rate < 0 || rate > 20) invalid('risk_free_rate_pct must be between 0 and 20');
  return { positions, benchmark, lookback: body.lookback, risk_free_rate_pct: rate };
}

function marketClock(now) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23', weekday: 'short',
  }).formatToParts(now).map(({ type, value }) => [type, value]));
  return { date: `${parts.year}-${parts.month}-${parts.day}`, settled: Number(parts.hour) * 60 + Number(parts.minute) >= 975, weekday: !['Sat', 'Sun'].includes(parts.weekday) };
}

function cleanHistory(rows, ticker, clock, cutoff, warnings) {
  const values = new Map();
  let dropped = 0;
  let incomplete = 0;
  for (const row of Array.isArray(rows) ? rows : []) {
    const date = typeof row?.date === 'string' ? row.date.slice(0, 10) : '';
    const value = numeric(row?.value);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !Number.isFinite(Date.parse(date)) || new Date(date).toISOString().slice(0, 10) !== date || value === null || value <= 0) {
      dropped++;
      continue;
    }
    if (date > clock.date || (date === clock.date && clock.weekday && !clock.settled)) { incomplete++; continue; }
    if (date < cutoff) continue;
    if (values.has(date)) dropped++;
    values.set(date, value);
  }
  if (dropped) warnings.push(`${ticker}: removed ${dropped} invalid or duplicate adjusted-price rows`);
  if (incomplete) warnings.push(`${ticker}: excluded incomplete/future US daily bars until 16:15 America/New_York`);
  return { values: new Map([...values].sort(([a], [b]) => a.localeCompare(b))), dropped, incomplete };
}

function covariance(left, right) {
  const leftMean = mean(left);
  const rightMean = mean(right);
  return left.reduce((sum, value, index) => sum + (value - leftMean) * (right[index] - rightMean), 0) / (left.length - 1);
}

function correlation(left, right) {
  const varianceLeft = covariance(left, left);
  const varianceRight = covariance(right, right);
  return varianceLeft > 0 && varianceRight > 0 ? finite(Math.max(-1, Math.min(1, covariance(left, right) / Math.sqrt(varianceLeft * varianceRight)))) : null;
}

function beta(left, right) {
  const variance = covariance(right, right);
  return variance > 0 ? finite(covariance(left, right) / variance) : null;
}

function levels(returns) {
  const result = [1];
  for (const value of returns) result.push(result.at(-1) * (1 + value));
  return result;
}

function drawdown(prices) {
  let peak = prices[0];
  let worst = 0;
  for (const price of prices) {
    peak = Math.max(peak, price);
    worst = Math.min(worst, price / peak - 1);
  }
  return finite(worst * 100);
}

function quantile(values, probability) {
  const sorted = [...values].sort((a, b) => a - b);
  const index = (sorted.length - 1) * probability;
  const lower = Math.floor(index);
  return sorted[lower] + (sorted[Math.ceil(index)] - sorted[lower]) * (index - lower);
}

function commonMetrics(prices, returns, benchmarkReturns) {
  const total = finite((prices.at(-1) / prices[0] - 1) * 100);
  const vol = finite(Math.sqrt(covariance(returns, returns) * DAYS) * 100);
  const maxDrawdown = drawdown(prices);
  return { total_return_pct: total, total_return: total, volatility_pct: vol, volatility: vol,
    beta: beta(returns, benchmarkReturns), correlation: correlation(returns, benchmarkReturns),
    max_drawdown_pct: maxDrawdown, max_drawdown: maxDrawdown };
}

function formulas(rate) {
  return {
    version: 'gs-risk-formula-v1', trading_days_per_year: DAYS, confidence_level: 0.95,
    risk_free_rate_pct: rate, portfolio_method: 'daily_constant_weight', minimum_common_price_observations: MIN_PRICES,
    session_policy: 'completed_us_market_sessions_only',
    assumptions: [
      'JavaScript implementation of the documented formulas; Python gs-quant is not executed.',
      'Adjusted daily prices are intersected by date; same-day US bars are excluded before 16:15 America/New_York.',
      'Weights are normalized and rebalanced daily; fees, taxes and trading costs are omitted.',
      'Portfolio annualized return is arithmetic mean daily return * 252; asset annualized return is calendar-day CAGR.',
      'Historical one-day 95% VaR/ES are nonnegative losses, using linear-interpolated empirical quantiles.',
      'Historical results and a beta-based scenario are estimates, not forecasts or investment advice.',
    ],
    measures: {
      returns: { engine: 'javascript', formula: 'P_t / P_(t-1) - 1', type: 'simple' },
      volatility: { engine: 'javascript', formula: 'sample_std(daily_returns, ddof=1) * sqrt(252)', unit: 'percent' },
      beta: { engine: 'javascript', formula: 'cov(asset_returns, benchmark_returns) / var(benchmark_returns)' },
      correlation: { engine: 'javascript', formula: 'Pearson correlation of simple daily returns' },
      max_drawdown: { engine: 'javascript', formula: 'minimum of indexed level / running peak - 1', unit: 'percent' },
      portfolio_return: { engine: 'javascript', formula: 'sum_i(weight_i * return_i,t)' },
      sharpe: { engine: 'javascript', formula: '(mean(daily_returns) * 252 - annual_risk_free_rate) / annualized_volatility' },
      var: { engine: 'javascript', formula: 'max(0, -quantile(daily_returns, 0.05, linear))', unit: 'percent' },
      expected_shortfall: { engine: 'javascript', formula: 'max(0, -mean(returns <= empirical_5th_percentile))', unit: 'percent' },
      risk_contribution: { engine: 'javascript', formula: 'weight_i * (Sigma w)_i / (w\' Sigma w)', unit: 'percent_of_portfolio_variance' },
    },
  };
}

/** The optional fourth argument is a server/test-only clock, never request input. */
export async function analyzeRisk(body, marketData = { prices: {} }, fetchHistory, { now = () => new Date() } = {}) {
  const request = validate(body);
  const generated = new Date(now());
  const generatedAt = generated.toISOString();
  const clock = marketClock(generated);
  const start = new Date(`${clock.date}T00:00:00Z`);
  start.setUTCFullYear(start.getUTCFullYear() - Number(request.lookback[0]));
  const cutoff = start.toISOString().slice(0, 10);
  const symbols = request.positions.map((position) => position.symbol);
  const allSymbols = [...new Set([...symbols, request.benchmark])];
  const warnings = ['Cloudflare uses JavaScript formulas, not the Python gs-quant runtime; no Goldman Sachs Marquee connection.'];
  const histories = {};
  // At most 12 positions plus one benchmark; providers are supplied by the worker.
  // No URL, host or provider configuration is accepted from the client request.
  await Promise.all(allSymbols.map(async (ticker) => {
    let rows;
    try {
      rows = typeof fetchHistory === 'function' ? await fetchHistory(ticker, request.lookback) : marketData?.prices?.[ticker];
    } catch {
      fail(502, 'provider_failure', 'Adjusted daily history provider failed', { symbols: [ticker] });
    }
    histories[ticker] = cleanHistory(rows, ticker, clock, cutoff, warnings);
  }));
  const missing = allSymbols.filter((ticker) => !histories[ticker].values.size);
  if (missing.length) fail(422, missing.length === allSymbols.length ? 'missing_data' : 'partial_data', 'Adjusted daily history is missing for requested symbols', { symbols: missing });
  const dates = [...histories[allSymbols[0]].values.keys()].filter((date) => allSymbols.every((ticker) => histories[ticker].values.has(date)));
  if (dates.length < MIN_PRICES) fail(422, 'insufficient_overlap', 'Not enough common adjusted daily observations for the requested portfolio', { observations: dates.length, minimum: MIN_PRICES });
  const prices = Object.fromEntries(allSymbols.map((ticker) => [ticker, dates.map((date) => histories[ticker].values.get(date))]));
  const returns = Object.fromEntries(allSymbols.map((ticker) => [ticker, prices[ticker].slice(1).map((price, index) => price / prices[ticker][index] - 1)]));
  if (Object.values(returns).some((values) => values.some((value) => !Number.isFinite(value)))) fail(422, 'invalid_data', 'Adjusted prices produced nonfinite daily returns');
  const weights = request.positions.map((position) => position.weight_pct / 100);
  const portfolioReturns = returns[symbols[0]].map((_, index) => symbols.reduce((sum, ticker, column) => sum + weights[column] * returns[ticker][index], 0));
  const benchmarkReturns = returns[request.benchmark];
  const portfolioLevels = levels(portfolioReturns);
  const benchmarkLevels = levels(benchmarkReturns);
  const portfolio = commonMetrics(portfolioLevels, portfolioReturns, benchmarkReturns);
  const annualized = finite(mean(portfolioReturns) * DAYS * 100);
  const sharpe = portfolio.volatility_pct > 0 && annualized !== null ? finite((annualized - request.risk_free_rate_pct) / portfolio.volatility_pct) : null;
  const boundary = quantile(portfolioReturns, 0.05);
  const tail = portfolioReturns.filter((value) => value <= boundary);
  const valueAtRisk = finite(Math.max(0, -boundary * 100));
  const expectedShortfall = finite(Math.max(0, -mean(tail) * 100));
  Object.assign(portfolio, { observations: portfolioReturns.length, annualized_return_pct: annualized, annualized_return: annualized,
    sharpe, sharpe_ratio: sharpe, var_95_1d_pct: valueAtRisk, historical_var_95_1d_pct: valueAtRisk, var_95_1d: valueAtRisk,
    expected_shortfall_95_1d_pct: expectedShortfall, es_95_1d_pct: expectedShortfall, expected_shortfall_95_1d: expectedShortfall, risk_free_rate_pct: request.risk_free_rate_pct });
  const calendarDays = (Date.parse(dates.at(-1)) - Date.parse(dates[0])) / 86400000;
  const assetMetrics = (ticker) => {
    const asset = commonMetrics(prices[ticker], returns[ticker], benchmarkReturns);
    const annualReturn = finite(((prices[ticker].at(-1) / prices[ticker][0]) ** (365.25 / calendarDays) - 1) * 100);
    return { symbol: ticker, observations: dates.length, return_observations: dates.length - 1, ...asset, annualized_return_pct: annualReturn, annualized_return: annualReturn };
  };
  let covarianceValues = symbols.map((left) => symbols.map((right) => finite(covariance(returns[left], returns[right]))));
  const marginal = covarianceValues.map((row) => row.reduce((sum, value, index) => sum + value * weights[index], 0));
  const variance = marginal.reduce((sum, value, index) => sum + weights[index] * value, 0);
  const validVariance = Number.isFinite(variance) && variance > 0 && covarianceValues.every((row) => row.every((value) => value !== null));
  if (!validVariance) {
    warnings.push('Covariance risk contribution is unavailable because portfolio variance is not positive');
    covarianceValues = symbols.map(() => symbols.map(() => null));
  }
  const positions = request.positions.map((position, index) => {
    const contribution = validVariance ? finite(weights[index] * marginal[index] / variance * 100) : null;
    const metrics = { ...assetMetrics(position.symbol), risk_contribution_pct: contribution, risk_contribution: contribution };
    return { ...metrics, weight_pct: position.weight_pct, metrics: { ...metrics } };
  });
  if ([...Object.values(portfolio), ...positions.flatMap((position) => Object.values(position.metrics))].some((value) => value === null)) warnings.push('Some risk measures are undefined for zero-variance or nonfinite histories and are returned as null');
  const bySymbol = Object.fromEntries(positions.map((position) => [position.symbol, position]));
  const contributions = Object.fromEntries(positions.map((position) => [position.symbol, position.risk_contribution_pct]));
  const correlationValues = symbols.map((left) => symbols.map((right) => correlation(returns[left], returns[right])));
  const correlationMatrix = { symbols, values: correlationValues,
    matrix: Object.fromEntries(symbols.map((left, index) => [left, Object.fromEntries(symbols.map((right, column) => [right, correlationValues[index][column]]))])),
    rows: symbols.map((ticker, index) => ({ symbol: ticker, values: correlationValues[index] })) };
  const asOf = dates.at(-1);
  const stale = (Date.parse(generatedAt.slice(0, 10)) - Date.parse(asOf)) / 86400000 > 7;
  if (stale) warnings.push('Aligned history is more than seven calendar days older than retrieval time');
  const provider = typeof fetchHistory === 'function' ? 'Yahoo Finance chart adjusted close' : (marketData?.source || 'Published adjusted daily history');
  const data = { provider, source: provider, source_name: provider, source_kind: 'vendor', source_url: 'https://finance.yahoo.com/',
    source_role: 'adjusted daily price history for derived risk calculations', adjustment: 'vendor adjusted close (splits and dividends)', price_adjustment: 'vendor adjusted close (splits and dividends)',
    source_disclosure_version: 'source-disclosure-v1', contract_version: 'gs-lab-api-v1', frequency: 'daily', session_policy: 'completed_us_market_sessions_only', lookback: request.lookback,
    period_start: dates[0], period_end: asOf, as_of: asOf, retrieved_at: generatedAt, symbols: allSymbols,
    observations: dates.length, return_observations: portfolioReturns.length, aligned_observations: dates.length, aligned_return_observations: portfolioReturns.length,
    latest_by_symbol: Object.fromEntries(allSymbols.map((ticker) => [ticker, [...histories[ticker].values.keys()].at(-1)])),
    observations_by_symbol: Object.fromEntries(allSymbols.map((ticker) => [ticker, histories[ticker].values.size])),
    partial: allSymbols.some((ticker) => histories[ticker].dropped > 0), warnings_count: warnings.length,
    freshness: { status: stale ? 'stale' : 'fresh', stale, as_of: asOf, retrieved_at: generatedAt, note: 'Fresh means the latest common date is at most seven calendar days old, not a real-time exchange quote.' },
    source_disclosure: { version: 'source-disclosure-v1', source_name: provider, source_kind: 'vendor', source_url: 'https://finance.yahoo.com/', role: 'Vendor adjusted daily prices; not official exchange or issuer filing data.' } };
  const metrics = { contract_version: 'gs-risk-metric-contract-v1', portfolio, benchmark: assetMetrics(request.benchmark), positions,
    position_metrics_by_symbol: bySymbol, risk_contributions: contributions, correlation_matrix: correlationMatrix, covariance_matrix: { symbols, values: covarianceValues },
    ...Object.fromEntries(Object.entries(portfolio).map(([key, value]) => [`portfolio_${key}`, value])) };
  const formula = formulas(request.risk_free_rate_pct);
  return { schema_version: 'gs-risk-lab-v1', contract_versions: { api: 'gs-lab-api-v1', metrics: 'gs-risk-metric-contract-v1', source_disclosure: 'source-disclosure-v1' },
    generated_at: generatedAt, as_of: asOf, request, normalized_request: request,
    engine: { name: 'JavaScript risk formulas', library: 'cloudflare/risk.mjs', version: '1', mode: 'cloudflare_javascript', gs_quant_runtime: false, marquee_connected: false, marquee_credentials_required: false, functions: ['returns', 'volatility', 'beta', 'correlation', 'max_drawdown'] },
    data, data_provenance: data, metrics,
    chart: { frequency: 'daily', unit: 'indexed_level', base: 100, dates, portfolio: portfolioLevels.map((value) => finite(value * 100)), benchmark: benchmarkLevels.map((value) => finite(value * 100)), positions: Object.fromEntries(symbols.map((ticker) => [ticker, prices[ticker].map((value) => finite(value / prices[ticker][0] * 100))])) },
    per_position: positions, per_position_metrics: positions, per_position_by_symbol: bySymbol, positions, risk_contributions: contributions, correlation_matrix: correlationMatrix,
    formula, formula_metadata: formula, warnings: [...new Set(warnings)] };
}
