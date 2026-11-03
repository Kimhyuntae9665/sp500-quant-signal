import test from 'node:test';
import assert from 'node:assert/strict';
import { analyzeRisk } from './risk.mjs';

const clock = { now: () => new Date('2026-11-03T22:00:00Z') };
const request = () => ({ positions: [{ symbol: 'AAA', weight_pct: 60 }, { symbol: 'BBB', weight_pct: 40 }], benchmark: 'SPY', lookback: '1y', risk_free_rate_pct: 4 });
function dates(count = 61, start = '2026-08-03') {
  const result = [];
  const day = new Date(`${start}T00:00:00Z`);
  while (result.length < count) {
    if (![0, 6].includes(day.getUTCDay())) result.push(day.toISOString().slice(0, 10));
    day.setUTCDate(day.getUTCDate() + 1);
  }
  return result;
}
function series(amplitude, labels = dates()) {
  let value = 100;
  return labels.map((date, index) => {
    if (index) value *= 1 + (index % 2 ? amplitude : -amplitude);
    return { date, value };
  });
}
const history = (labels = dates()) => ({ prices: { AAA: series(0.02, labels), BBB: series(0.01, labels), SPY: series(0.005, labels) } });
function close(actual, expected, tolerance = 1e-9) {
  assert.ok(typeof actual === 'number' && Math.abs(actual - expected) < tolerance, `${actual} differs from ${expected}`);
}
function assertFiniteTree(value) {
  if (typeof value === 'number') assert.ok(Number.isFinite(value), `Nonfinite JSON number ${value}`);
  else if (Array.isArray(value)) value.forEach(assertFiniteTree);
  else if (value && typeof value === 'object') Object.values(value).forEach(assertFiniteTree);
}

test('closed-form alternating returns reproduce documented risk mathematics', async () => {
  const result = await analyzeRisk(request(), history(), undefined, clock);
  const metrics = result.metrics.portfolio;
  const volatility = Math.sqrt(60 * 0.016 ** 2 / 59 * 252) * 100;
  close(metrics.annualized_return_pct, 0);
  close(metrics.volatility_pct, volatility);
  close(metrics.sharpe_ratio, -4 / volatility);
  close(metrics.beta, 3.2);
  close(metrics.correlation, 1);
  close(metrics.total_return_pct, ((1 - 0.016 ** 2) ** 30 - 1) * 100);
  close(metrics.max_drawdown_pct, ((1 - 0.016) * (1 - 0.016 ** 2) ** 29 - 1) * 100);
  close(metrics.var_95_1d_pct, 1.6);
  close(metrics.expected_shortfall_95_1d_pct, 1.6);
  close(result.risk_contributions.AAA, 75);
  close(result.risk_contributions.BBB, 25);
  close(result.per_position_by_symbol.AAA.beta, 4);
  close(result.per_position_by_symbol.BBB.beta, 2);
  close(result.metrics.covariance_matrix.values[0][1], 60 * 0.02 * 0.01 / 59);
  close(result.correlation_matrix.values[0][1], 1);
  assert.equal(metrics.observations, 60);
  assert.equal(result.data.aligned_observations, 61);
  assertFiniteTree(result);
});

test('response retains the browser metric, chart, contribution, matrix and metadata contract', async () => {
  const labels = dates();
  const result = await analyzeRisk(request(), history(labels), undefined, clock);
  assert.equal(result.schema_version, 'gs-risk-lab-v1');
  assert.equal(result.contract_versions.api, 'gs-lab-api-v1');
  for (const key of ['annualized_return_pct', 'volatility_pct', 'max_drawdown_pct', 'sharpe_ratio', 'beta', 'observations', 'var_95_1d_pct', 'expected_shortfall_95_1d_pct']) {
    assert.equal(typeof result.metrics.portfolio[key], 'number', key);
    assert.equal(result.metrics[`portfolio_${key}`], result.metrics.portfolio[key]);
  }
  assert.deepEqual(result.chart.dates, labels);
  for (const values of [result.chart.portfolio, result.chart.benchmark, result.chart.positions.AAA]) {
    assert.equal(values.length, labels.length);
    assert.equal(values[0], 100);
  }
  assert.deepEqual(result.correlation_matrix.symbols, ['AAA', 'BBB']);
  assert.equal(result.per_position_metrics[0].weight_pct, 60);
  assert.equal(result.engine.gs_quant_runtime, false);
  assert.equal(result.engine.marquee_connected, false);
  assert.match(result.engine.name, /JavaScript/);
  assert.equal(result.formula.measures.volatility.engine, 'javascript');
  assert.equal(result.formula.trading_days_per_year, 252);
  assert.equal(result.data.session_policy, 'completed_us_market_sessions_only');
  assert.equal(result.as_of, labels.at(-1));
  assert.equal(JSON.parse(JSON.stringify(result)).metrics.portfolio.beta, result.metrics.portfolio.beta);
});

test('asset annualized return is calendar-day CAGR, unlike arithmetic portfolio return', async () => {
  const labels = dates();
  const result = await analyzeRisk(request(), history(labels), undefined, clock);
  const dayCount = (Date.parse(labels.at(-1)) - Date.parse(labels[0])) / 86400000;
  close(result.per_position_by_symbol.AAA.annualized_return_pct, (((1 - 0.02 ** 2) ** 30) ** (365.25 / dayCount) - 1) * 100);
});

test('5th percentile uses linear interpolation and ES uses observations at or below it', async () => {
  const labels = dates();
  const daily = Array.from({ length: 60 }, (_, i) => (i - 30) / 1000);
  let level = 100;
  const rows = labels.map((date, index) => ({ date, value: index ? (level *= 1 + daily[index - 1]) : level }));
  const result = await analyzeRisk(request(), { prices: { AAA: rows, BBB: rows, SPY: rows } }, undefined, clock);
  close(result.metrics.portfolio.var_95_1d_pct, 2.705);
  close(result.metrics.portfolio.expected_shortfall_95_1d_pct, 2.9);
});

test('normalizes accepted totals and symbol case without mutating the request', async () => {
  const body = request();
  body.positions = [{ symbol: ' aaa ', weight_pct: '60' }, { symbol: 'bbb', weight_pct: 39.5 }];
  body.benchmark = 'spy';
  const original = structuredClone(body);
  const result = await analyzeRisk(body, history(), undefined, clock);
  assert.deepEqual(body, original);
  assert.equal(result.request.positions[0].symbol, 'AAA');
  close(result.request.positions[0].weight_pct, 60 * 100 / 99.5);
  close(result.request.positions.reduce((sum, position) => sum + position.weight_pct, 0), 100);
});

test('validation rejects dangerous symbols, foreign suffixes, duplicates, unsupported fields and malformed values before provider calls', async () => {
  let calls = 0;
  const provider = async () => { calls++; return []; };
  const invalid = [null, [], {}, { ...request(), url: 'https://example.org' }, { ...request(), positions: [request().positions[0]] },
    { ...request(), positions: [{ symbol: 'https://a', weight_pct: 60 }, request().positions[1]] },
    { ...request(), positions: [{ symbol: '005930.KS', weight_pct: 60 }, request().positions[1]] },
    { ...request(), positions: [{ symbol: 'AAA', weight_pct: 60 }, { symbol: 'aaa', weight_pct: 40 }] },
    { ...request(), positions: [{ symbol: 'AAA', weight_pct: true }, request().positions[1]] },
    { ...request(), positions: [{ symbol: 'AAA', weight_pct: Infinity }, request().positions[1]] },
    { ...request(), positions: [{ symbol: 'AAA', weight_pct: -5 }, { symbol: 'BBB', weight_pct: 105 }] },
    { ...request(), positions: [{ symbol: 'AAA', weight_pct: 59 }, request().positions[1]] },
    { ...request(), benchmark: '^GSPC' }, { ...request(), lookback: '10y' },
    ...[-1, 21, null, false, ''].map((rate) => ({ ...request(), risk_free_rate_pct: rate }))];
  for (const body of invalid) await assert.rejects(analyzeRisk(body, {}, provider, clock), (error) => error.status === 400 && error.code === 'validation_error');
  assert.equal(calls, 0);
});

test('at most thirteen fixed-symbol provider calls and no duplicate benchmark fetch', async () => {
  const body = request();
  body.positions = Array.from({ length: 12 }, (_, index) => ({ symbol: `A${index}`, weight_pct: 100 / 12 }));
  const calls = [];
  const result = await analyzeRisk(body, {}, async (ticker, lookback) => { calls.push([ticker, lookback]); return series(0.01); }, clock);
  assert.equal(calls.length, 13);
  assert.ok(calls.every(([, lookback]) => lookback === '1y'));
  assert.equal(result.per_position.length, 12);
  body.benchmark = 'A0';
  calls.length = 0;
  await analyzeRisk(body, {}, async (ticker) => { calls.push(ticker); return series(0.01); }, clock);
  assert.equal(calls.length, 12);
});

test('provider failures and partial/missing histories return stable safe errors', async () => {
  await assert.rejects(analyzeRisk(request(), {}, async () => { throw new Error('secret provider token'); }, clock), (error) => error.status === 502 && error.code === 'provider_failure' && !error.message.includes('secret'));
  await assert.rejects(analyzeRisk(request(), {}, undefined, clock), (error) => error.status === 422 && error.code === 'missing_data');
  await assert.rejects(analyzeRisk(request(), { prices: { AAA: series(0.01) } }, undefined, clock), (error) => error.code === 'partial_data' && error.details.symbols.includes('SPY'));
});

test('minimum sixty common prices is enforced after alignment and cleaning', async () => {
  const input = history();
  input.prices.BBB = input.prices.BBB.slice(2);
  await assert.rejects(analyzeRisk(request(), input, undefined, clock), (error) => error.code === 'insufficient_overlap' && error.details.observations === 59);
  const minimum = await analyzeRisk(request(), history(dates(60)), undefined, clock);
  assert.equal(minimum.chart.dates.length, 60);
});

test('completed US sessions enforce 16:15 ET across DST and exclude future labels', async () => {
  for (const [today, before, after] of [
    ['2026-11-03', '2026-11-03T21:14:59Z', '2026-11-03T21:15:00Z'],
    ['2026-07-14', '2026-07-14T20:14:59Z', '2026-07-14T20:15:00Z'],
  ]) {
    const labels = dates(61, today.startsWith('2026-11') ? '2026-07-01' : '2026-03-02');
    const input = history([...labels, today, '2027-01-01']);
    const pending = await analyzeRisk(request(), input, undefined, { now: () => new Date(before) });
    const settled = await analyzeRisk(request(), input, undefined, { now: () => new Date(after) });
    assert.equal(pending.chart.dates.length, 61);
    assert.equal(settled.chart.dates.length, 62);
    assert.equal(settled.as_of, today);
    assert.ok(pending.warnings.some((warning) => warning.includes('16:15')));
    assert.ok(!settled.chart.dates.includes('2027-01-01'));
  }
});

test('invalid/duplicate rows are removed, dates sorted, stale state explicit, and lookback respected', async () => {
  const input = history();
  for (const ticker of Object.keys(input.prices)) input.prices[ticker].push(
    { date: '2020-01-01', value: 100 }, { date: '2026-02-30', value: 100 },
    { date: 'invalid', value: 100 }, { date: '2026-10-30', value: 0 },
    { ...input.prices[ticker][0] });
  input.prices.AAA.reverse();
  const result = await analyzeRisk(request(), input, undefined, clock);
  assert.equal(result.chart.dates.length, 61);
  assert.equal(result.chart.dates[0], '2026-08-03');
  assert.equal(result.data.partial, true);
  assert.equal(result.data.freshness.stale, true);
  assert.ok(result.warnings.some((warning) => warning.includes('invalid or duplicate')));
});

test('zero variance returns null instead of inventing beta, correlation, Sharpe or risk contribution', async () => {
  const rows = dates().map((date) => ({ date, value: 100 }));
  const result = await analyzeRisk(request(), { prices: { AAA: rows, BBB: rows, SPY: rows } }, undefined, clock);
  const metrics = result.metrics.portfolio;
  assert.equal(metrics.beta, null);
  assert.equal(metrics.correlation, null);
  assert.equal(metrics.sharpe_ratio, null);
  assert.equal(metrics.volatility_pct, 0);
  assert.equal(metrics.var_95_1d_pct, 0);
  assert.equal(result.risk_contributions.AAA, null);
  assert.equal(result.correlation_matrix.values[0][0], null);
  assertFiniteTree(result);
});

test('hedging contributions may be negative and still sum to portfolio variance share 100%', async () => {
  const input = history();
  input.prices.BBB = series(-0.01);
  const result = await analyzeRisk(request(), input, undefined, clock);
  close(result.risk_contributions.AAA, 150);
  close(result.risk_contributions.BBB, -50);
  close(result.risk_contributions.AAA + result.risk_contributions.BBB, 100);
  close(result.correlation_matrix.values[0][1], -1);
  close(result.metrics.portfolio.beta, 1.6);
});

test('all-positive daily returns produce zero historical loss VaR and ES', async () => {
  const rows = dates().map((date, index) => ({ date, value: 100 * 1.001 ** index }));
  const result = await analyzeRisk({ ...request(), risk_free_rate_pct: 0 }, { prices: { AAA: rows, BBB: rows, SPY: rows } }, undefined, clock);
  assert.equal(result.metrics.portfolio.var_95_1d_pct, 0);
  assert.equal(result.metrics.portfolio.expected_shortfall_95_1d_pct, 0);
});

test('one-, three- and five-year windows retain only dates in the requested window', async () => {
  const labels = [...dates(61, '2022-08-01'), ...dates(61, '2024-08-01'), ...dates()];
  for (const [lookback, count] of [['1y', 61], ['3y', 122], ['5y', 183]]) {
    const result = await analyzeRisk({ ...request(), lookback }, history(labels), undefined, clock);
    assert.equal(result.chart.dates.length, count);
    assert.equal(result.request.lookback, lookback);
  }
});
