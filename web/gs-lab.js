(() => {
  "use strict";

  const API = Object.freeze({
    analyze: "/api/gs-quant/analyze",
    portfolioCompose: "/api/portfolio/compose",
  });
  const STORAGE_KEY = "sp500-quant-signal.portfolio.v1";
  const MIN_POSITIONS = 2;
  const MAX_POSITIONS = 12;
  const TICKER_RE = /^[A-Z0-9][A-Z0-9.\-]{0,14}$/;
  const LOOKBACKS = new Set(["1y", "3y", "5y"]);
  const DEFAULT_SETTINGS = Object.freeze({ benchmark: "SPY", lookback: "3y", riskFreeRate: 4 });
  const DEFAULT_POSITIONS = Object.freeze([
    { symbol: "CVX", weight: 12 },
    { symbol: "PEP", weight: 12 },
    { symbol: "TLT", weight: 14 },
    { symbol: "SPGI", weight: 12 },
    { symbol: "KMB", weight: 10 },
    { symbol: "PRU", weight: 12 },
    { symbol: "WFC", weight: 12 },
    { symbol: "PG", weight: 8 },
    { symbol: "VICI", weight: 8 },
  ]);

  const state = {
    positions: DEFAULT_POSITIONS.map((position) => ({ ...position })),
    settings: { ...DEFAULT_SETTINGS },
    result: null,
    loading: false,
    importing: false,
    requestId: 0,
    scenarioShock: -10,
  };

  const refs = {};

  class ApiError extends Error {
    constructor(message, status = 0, code = "api_error") {
      super(message);
      this.name = "ApiError";
      this.status = status;
      this.code = code;
    }
  }

  function init() {
    refs.form = document.getElementById("risk-form");
    refs.positionRows = document.getElementById("position-rows");
    refs.positionCount = document.getElementById("position-count");
    refs.weightTotal = document.getElementById("weight-total");
    refs.weightState = document.getElementById("weight-state");
    refs.weightTotalWrap = document.querySelector(".weight-total");
    refs.formError = document.getElementById("form-error");
    refs.benchmark = document.getElementById("benchmark");
    refs.lookback = document.getElementById("lookback");
    refs.riskFreeRate = document.getElementById("risk-free-rate");
    refs.analyzeButton = document.getElementById("analyze-button");
    refs.addPosition = document.getElementById("add-position");
    refs.equalWeight = document.getElementById("equal-weight");
    refs.normalizeWeight = document.getElementById("normalize-weight");
    refs.resetExample = document.getElementById("reset-example");
    refs.importButton = document.getElementById("import-portfolio");
    refs.importStatus = document.getElementById("import-status");
    refs.resultState = document.getElementById("result-state");
    refs.results = document.getElementById("results");
    refs.partialNotice = document.getElementById("partial-notice");
    refs.schemaVersion = document.getElementById("schema-version");
    refs.shockRange = document.getElementById("shock-range");
    refs.shockValue = document.getElementById("shock-value");
    refs.shockImpact = document.getElementById("shock-impact");
    refs.scenarioContrib = document.getElementById("scenario-position-contrib");
    refs.cumulativeChart = document.getElementById("cumulative-chart");
    refs.cumulativeAlt = document.getElementById("cumulative-table-alt");
    refs.cumulativeObservations = document.getElementById("cumulative-observations");
    refs.drawdownChart = document.getElementById("drawdown-chart");
    refs.drawdownAlt = document.getElementById("drawdown-table-alt");
    refs.drawdownObservations = document.getElementById("drawdown-observations");
    refs.riskBars = document.getElementById("risk-bars");
    refs.riskTableBody = document.querySelector("#risk-table tbody");
    refs.correlationScroll = document.getElementById("correlation-scroll");
    refs.metadataSource = document.getElementById("metadata-source");
    refs.metadataAsOf = document.getElementById("metadata-as-of");
    refs.metadataEngine = document.getElementById("metadata-engine");
    refs.metadataMarquee = document.getElementById("metadata-marquee");
    refs.engineMode = document.getElementById("engine-mode");
    refs.metadataWarnings = document.getElementById("metadata-warnings");

    if (!refs.form || !refs.positionRows) return;

    refs.form.addEventListener("submit", handleAnalyze);
    refs.positionRows.addEventListener("click", handlePositionClick);
    refs.positionRows.addEventListener("input", handlePositionInput);
    refs.positionRows.addEventListener("change", handlePositionInput);
    refs.addPosition?.addEventListener("click", addPosition);
    refs.equalWeight?.addEventListener("click", equalizeWeights);
    refs.normalizeWeight?.addEventListener("click", normalizeWeights);
    refs.resetExample?.addEventListener("click", resetExample);
    refs.importButton?.addEventListener("click", importStoredPortfolio);
    refs.shockRange?.addEventListener("input", handleShockInput);
    refs.benchmark?.addEventListener("input", clearFormErrorOnInput);
    refs.riskFreeRate?.addEventListener("input", clearFormErrorOnInput);

    renderPositionRows(state.positions);
    setSettingsFromState();
    renderAnalysisSurface(null);
    updateShockRail();
    updatePositionSummary();
  }

  function handlePositionClick(event) {
    const button = event.target.closest("[data-remove-position]");
    if (!button || !refs.positionRows.contains(button)) return;
    const index = Number(button.dataset.removePosition);
    if (!Number.isInteger(index)) return;
    removePosition(index);
  }

  function handlePositionInput() {
    clearFormErrorOnInput();
    updatePositionSummary();
  }

  function clearFormErrorOnInput() {
    if (refs.formError) refs.formError.textContent = "";
    document.querySelectorAll("[aria-invalid=\"true\"]").forEach((element) => {
      element.setAttribute("aria-invalid", "false");
    });
  }

  function renderPositionRows(positions) {
    state.positions = positions.map((position) => ({
      symbol: String(position.symbol || "").toUpperCase(),
      weight: Number.isFinite(Number(position.weight)) ? Number(position.weight) : 0,
    }));
    refs.positionRows.innerHTML = state.positions.map((position, index) => {
      const symbol = escapeAttr(position.symbol);
      const weight = escapeAttr(formatInputNumber(position.weight));
      const rowNumber = String(index + 1).padStart(2, "0");
      return `
        <tr data-position-row="${index}">
          <td class="position-index">${rowNumber}</td>
          <td>
            <label class="sr-only" for="symbol-${index}">포지션 ${index + 1} 티커</label>
            <input class="row-input symbol-input" id="symbol-${index}" data-position-symbol="${index}" name="symbol-${index}" type="text" value="${symbol}" maxlength="15" autocomplete="off" spellcheck="false" aria-describedby="symbol-error-${index}">
            <span class="field-error row-error" id="symbol-error-${index}" role="alert" hidden></span>
          </td>
          <td>
            <label class="sr-only" for="weight-${index}">${escapeHTML(position.symbol || `포지션 ${index + 1}`)} 비중</label>
            <span class="input-with-suffix input-with-suffix--row">
              <input class="row-input weight-input" id="weight-${index}" data-position-weight="${index}" name="weight-${index}" type="number" min="0.01" max="100" step="0.01" value="${weight}" inputmode="decimal" aria-describedby="weight-error-${index}">
              <span aria-hidden="true">%</span>
            </span>
            <span class="field-error row-error" id="weight-error-${index}" role="alert" hidden></span>
          </td>
          <td><button class="remove-row" type="button" data-remove-position="${index}" aria-label="${escapeAttr(position.symbol || `포지션 ${index + 1}`)} 포지션 삭제"${state.positions.length <= MIN_POSITIONS ? " disabled" : ""}>×</button></td>
        </tr>`;
    }).join("");
    if (refs.positionCount) refs.positionCount.textContent = String(state.positions.length);
    updatePositionSummary();
  }

  function collectPositionInputs() {
    return Array.from(refs.positionRows.querySelectorAll("[data-position-row]")).map((row, index) => {
      const symbolInput = row.querySelector("[data-position-symbol]");
      const weightInput = row.querySelector("[data-position-weight]");
      const symbol = String(symbolInput?.value || "").trim().toUpperCase();
      const rawWeight = String(weightInput?.value ?? "").trim();
      const weight = rawWeight === "" ? NaN : Number(rawWeight);
      return { index, row, symbol, rawWeight, weight, symbolInput, weightInput };
    });
  }

  function updatePositionSummary() {
    if (!refs.positionRows) return;
    const rows = collectPositionInputs();
    const total = rows.reduce((sum, row) => sum + (Number.isFinite(row.weight) ? row.weight : 0), 0);
    const sumOk = rows.length >= MIN_POSITIONS && rows.length <= MAX_POSITIONS && Math.abs(total - 100) < 0.011 && rows.every((row) => row.symbol && TICKER_RE.test(row.symbol) && Number.isFinite(row.weight) && row.weight > 0);
    if (refs.positionCount) refs.positionCount.textContent = String(rows.length);
    if (refs.weightTotal) refs.weightTotal.textContent = Number.isFinite(total) ? `${formatNumber(total, 2)}%` : "—";
    if (refs.weightState) refs.weightState.textContent = sumOk ? "분석 가능" : "합계·입력 확인 필요";
    refs.weightTotalWrap?.setAttribute("data-state", sumOk ? "valid" : "invalid");
    state.positions = rows.map((row) => ({ symbol: row.symbol, weight: Number.isFinite(row.weight) ? row.weight : 0 }));
  }

  function addPosition() {
    const rows = collectPositionInputs();
    if (rows.length >= MAX_POSITIONS) {
      showFormMessage("포지션은 최대 12개까지 입력할 수 있습니다.", null);
      return;
    }
    const next = rows.map((row) => ({ symbol: row.symbol, weight: Number.isFinite(row.weight) ? row.weight : 0 }));
    next.push({ symbol: "", weight: 0 });
    renderPositionRows(next);
    const input = document.querySelector(`[data-position-symbol="${next.length - 1}"]`);
    input?.focus();
  }

  function removePosition(index) {
    const rows = collectPositionInputs();
    if (rows.length <= MIN_POSITIONS) {
      showFormMessage("분석에는 최소 2개 포지션이 필요합니다.", null);
      return;
    }
    if (!rows[index]) return;
    const next = rows.filter((_, rowIndex) => rowIndex !== index).map((row) => ({ symbol: row.symbol, weight: Number.isFinite(row.weight) ? row.weight : 0 }));
    renderPositionRows(next);
    const focusIndex = Math.min(index, next.length - 1);
    document.querySelector(`[data-position-symbol="${focusIndex}"]`)?.focus();
  }

  function equalizeWeights() {
    const rows = collectPositionInputs();
    if (!rows.length) return;
    const count = rows.length;
    const base = Math.floor((100 / count) * 100) / 100;
    const equal = rows.map(() => base);
    equal[equal.length - 1] = Number((100 - base * (count - 1)).toFixed(2));
    renderPositionRows(rows.map((row, index) => ({ symbol: row.symbol, weight: equal[index] })));
    showFormMessage("모든 포지션을 동일 비중으로 100%에 맞췄습니다.", null);
  }

  function normalizeWeights() {
    const rows = collectPositionInputs();
    const positiveTotal = rows.reduce((sum, row) => sum + (Number.isFinite(row.weight) && row.weight > 0 ? row.weight : 0), 0);
    if (positiveTotal <= 0) {
      equalizeWeights();
      return;
    }
    const weights = rows.map((row) => (Number.isFinite(row.weight) && row.weight > 0 ? row.weight / positiveTotal * 100 : 0));
    const rounded = weights.map((weight) => Number(weight.toFixed(2)));
    const lastPositive = rounded.reduce((last, weight, index) => (weight > 0 ? index : last), -1);
    if (lastPositive >= 0) {
      const others = rounded.reduce((sum, weight, index) => sum + (index === lastPositive ? 0 : weight), 0);
      rounded[lastPositive] = Number((100 - others).toFixed(2));
    }
    renderPositionRows(rows.map((row, index) => ({ symbol: row.symbol, weight: rounded[index] })));
    showFormMessage("입력된 비율을 비례 조정해 합계 100%로 맞췄습니다.", null);
  }

  function resetExample() {
    state.settings = { ...DEFAULT_SETTINGS };
    setSettingsFromState();
    clearFormErrorOnInput();
    renderPositionRows(DEFAULT_POSITIONS);
    clearAnalysisSurface();
    setImportStatus("");
    showFormMessage("최근 검토용 예시 바스켓으로 되돌렸습니다.", null);
    document.querySelector("[data-position-symbol=\"0\"]")?.focus();
  }

  function setSettingsFromState() {
    if (refs.benchmark) refs.benchmark.value = state.settings.benchmark;
    if (refs.lookback) refs.lookback.value = state.settings.lookback;
    if (refs.riskFreeRate) refs.riskFreeRate.value = formatInputNumber(state.settings.riskFreeRate);
  }

  function validatePositions() {
    clearValidationMessages();
    const rows = collectPositionInputs();
    if (rows.length < MIN_POSITIONS || rows.length > MAX_POSITIONS) {
      return { valid: false, message: `포지션은 ${MIN_POSITIONS}–${MAX_POSITIONS}개 범위여야 합니다.`, field: null };
    }
    const seen = new Set();
    for (const row of rows) {
      if (!row.symbol) return invalidPosition(row, "티커를 입력하세요.", "symbol");
      if (!TICKER_RE.test(row.symbol)) return invalidPosition(row, "영문·숫자·점·하이픈 형식의 티커를 입력하세요.", "symbol");
      if (seen.has(row.symbol)) return invalidPosition(row, "같은 티커는 한 번만 입력하세요.", "symbol");
      seen.add(row.symbol);
      if (!Number.isFinite(row.weight) || row.weight <= 0 || row.weight > 100) return invalidPosition(row, "비중은 0보다 크고 100 이하인 숫자여야 합니다.", "weight");
    }
    const total = rows.reduce((sum, row) => sum + row.weight, 0);
    if (Math.abs(total - 100) > 0.011) {
      return { valid: false, message: `비중 합계가 ${formatNumber(total, 2)}%입니다. 합계 100%로 맞추세요.`, field: null };
    }
    rows.forEach((row) => {
      row.symbolInput.value = row.symbol;
      row.weightInput.value = formatInputNumber(row.weight);
    });
    state.positions = rows.map((row) => ({ symbol: row.symbol, weight: row.weight }));
    return { valid: true, rows, message: "", field: null };
  }

  function invalidPosition(row, message, type) {
    const input = type === "symbol" ? row.symbolInput : row.weightInput;
    const error = document.getElementById(`${type}-error-${row.index}`);
    if (input) input.setAttribute("aria-invalid", "true");
    if (error) {
      error.hidden = false;
      error.textContent = message;
    }
    return { valid: false, message, field: input };
  }

  function validateSettings() {
    const benchmark = String(refs.benchmark?.value || "").trim().toUpperCase();
    const lookback = String(refs.lookback?.value || "");
    const riskFreeText = String(refs.riskFreeRate?.value ?? "").trim();
    const riskFreeRate = riskFreeText === "" ? NaN : Number(riskFreeText);
    if (!benchmark || !TICKER_RE.test(benchmark)) {
      setFieldError(refs.benchmark, document.getElementById("benchmark-error"), "벤치마크 티커를 확인하세요.");
      return { valid: false, message: "벤치마크 티커를 확인하세요." };
    }
    if (!LOOKBACKS.has(lookback)) return { valid: false, message: "가격 구간은 1y, 3y, 5y 중 하나여야 합니다." };
    if (!Number.isFinite(riskFreeRate) || riskFreeRate < 0 || riskFreeRate > 20) {
      setFieldError(refs.riskFreeRate, document.getElementById("risk-free-error"), "무위험 수익률은 0–20% 범위의 숫자여야 합니다.");
      return { valid: false, message: "무위험 수익률을 확인하세요." };
    }
    state.settings = { benchmark, lookback, riskFreeRate };
    if (refs.benchmark) refs.benchmark.value = benchmark;
    return { valid: true, benchmark, lookback, riskFreeRate, message: "" };
  }

  function setFieldError(input, errorElement, message) {
    input?.setAttribute("aria-invalid", "true");
    if (errorElement) {
      errorElement.hidden = false;
      errorElement.textContent = message;
    }
  }

  function clearValidationMessages() {
    document.querySelectorAll(".field-error").forEach((element) => {
      element.hidden = true;
      element.textContent = "";
    });
    document.querySelectorAll("[aria-invalid=\"true\"]").forEach((element) => element.setAttribute("aria-invalid", "false"));
    if (refs.formError) refs.formError.textContent = "";
  }

  function showFormMessage(message, field) {
    if (refs.formError) refs.formError.textContent = message || "";
    if (field && typeof field.focus === "function") field.focus();
  }

  async function handleAnalyze(event) {
    event.preventDefault();
    if (state.loading) return;
    clearValidationMessages();
    const positions = validatePositions();
    if (!positions.valid) {
      showFormMessage(positions.message, positions.field);
      setResultState("validation", "입력값을 확인하세요.", positions.message);
      positions.field?.focus();
      return;
    }
    const settings = validateSettings();
    if (!settings.valid) {
      showFormMessage(settings.message, null);
      setResultState("validation", "분석 조건을 확인하세요.", settings.message);
      const invalid = document.querySelector("#benchmark[aria-invalid=\"true\"], #risk-free-rate[aria-invalid=\"true\"]");
      invalid?.focus();
      return;
    }

    const requestPayload = {
      positions: positions.rows.map((row) => ({ symbol: row.symbol, weight_pct: Number(row.weight.toFixed(6)) })),
      benchmark: settings.benchmark,
      lookback: settings.lookback,
      risk_free_rate_pct: Number(settings.riskFreeRate.toFixed(6)),
    };
    const requestId = ++state.requestId;
    state.loading = true;
    setLoading(true);
    setResultState("loading", "리스크 계열을 계산하는 중입니다.", "가격·상관·기여도 응답을 기다리고 있습니다…");
    try {
      const payload = await postJson(API.analyze, requestPayload, 50000);
      if (requestId !== state.requestId) return;
      state.result = normalizeAnalysis(payload, requestPayload);
      renderAnalysisSurface(state.result);
      const isPartial = state.result.partial || state.result.missing.length > 0;
      const isMissing = state.result.missing.length >= 4 && !Object.keys(state.result.metrics).length && !state.result.cumulative.length && !state.result.drawdown.length;
      if (isMissing) {
        setResultState("missing", "응답에 분석 계열이 없습니다.", "서버가 빈 결과를 반환했습니다. 제공자 상태와 응답의 missing 필드를 확인하세요.");
      } else if (isPartial) {
        setResultState("partial", "분석은 도착했지만 일부 필드가 비어 있습니다.", state.result.missing.concat(state.result.warnings).join(" · ") || "응답의 결측 필드를 확인하세요.");
      } else {
        setResultState("success", "분석 응답을 반영했습니다.", `${state.result.observationCount || "반환된"}개 가격 관측치와 포지션별 리스크를 표시합니다.`);
      }
    } catch (error) {
      if (requestId !== state.requestId) return;
      state.result = null;
      clearAnalysisSurface();
      const validationError = isValidationError(error);
      const dataGap = isDataGapError(error);
      const stateType = dataGap ? (error.code === "missing_data" ? "missing" : "partial") : validationError ? "validation" : "provider-error";
      const title = dataGap ? "분석에 필요한 가격 구간이 비어 있습니다." : validationError ? "API가 요청을 검증하지 못했습니다." : "분석 제공자 또는 서버에 연결하지 못했습니다.";
      const detail = error instanceof ApiError ? error.message : "네트워크 상태와 서버 로그를 확인한 뒤 다시 실행하세요.";
      setResultState(stateType, title, detail);
      showFormMessage(detail, null);
    } finally {
      if (requestId === state.requestId) {
        state.loading = false;
        setLoading(false);
      }
    }
  }

  function setLoading(isLoading) {
    refs.analyzeButton?.classList.toggle("is-loading", isLoading);
    if (refs.analyzeButton) {
      refs.analyzeButton.disabled = isLoading;
      refs.analyzeButton.querySelector(".button-label").textContent = isLoading ? "분석 계산 중…" : "리스크 분석 실행";
      refs.analyzeButton.setAttribute("aria-busy", String(isLoading));
    }
    refs.results?.setAttribute("aria-busy", String(isLoading));
  }

  async function postJson(url, payload, timeoutMs) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      let response;
      try {
        response = await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify(payload),
          signal: controller.signal,
        });
      } catch (error) {
        if (error?.name === "AbortError") throw new ApiError("요청 시간이 초과되었습니다. 데이터 제공자 상태를 확인하세요.", 504, "request_timeout");
        throw new ApiError("서버에 연결하지 못했습니다. 로컬 서버가 실행 중인지 확인하세요.", 0, "network_error");
      }
      const text = await response.text();
      let body = {};
      if (text) {
        try { body = JSON.parse(text); } catch (_) { body = { message: text }; }
      }
      if (!response.ok) {
        const apiError = body?.error || body;
        throw new ApiError(String(apiError?.message || `서버가 ${response.status} 응답을 반환했습니다.`), response.status, String(apiError?.code || "api_error"));
      }
      return body;
    } finally {
      window.clearTimeout(timer);
    }
  }

  function isValidationError(error) {
    return Boolean(error && !isDataGapError(error) && (error.status === 400 || error.status === 422 || String(error.code || "").startsWith("invalid_")));
  }

  function isDataGapError(error) {
    return Boolean(error && ["missing_data", "partial_data", "insufficient_overlap"].includes(String(error.code || "")));
  }

  function setResultState(type, title, detail) {
    if (!refs.resultState) return;
    const marks = { empty: "○", loading: "◌", success: "✓", partial: "△", missing: "△", validation: "!", "provider-error": "×" };
    refs.resultState.className = `state-banner state-${type}`;
    refs.resultState.dataset.state = type;
    refs.resultState.setAttribute("role", type === "validation" || type === "provider-error" ? "alert" : "status");
    refs.resultState.replaceChildren();
    const mark = document.createElement("span");
    mark.className = "state-banner__mark";
    mark.setAttribute("aria-hidden", "true");
    mark.textContent = marks[type] || "○";
    const body = document.createElement("div");
    const strong = document.createElement("strong");
    strong.textContent = title;
    const paragraph = document.createElement("p");
    paragraph.textContent = detail;
    body.append(strong, paragraph);
    refs.resultState.append(mark, body);
  }

  function renderAnalysisSurface(result) {
    if (!result) {
      if (refs.schemaVersion) refs.schemaVersion.textContent = "gs-risk-lab-v1";
      renderMetrics(null);
      renderChart(refs.cumulativeChart, refs.cumulativeAlt, [], "cumulative", "누적 경로");
      renderChart(refs.drawdownChart, refs.drawdownAlt, [], "drawdown", "낙폭 경로");
      renderRiskContributions([]);
      renderCorrelation(null);
      renderMetadata(null);
      if (refs.partialNotice) refs.partialNotice.hidden = true;
      setResultState("empty", "아직 분석 결과가 없습니다.", "비중과 조건을 확인한 뒤 리스크 분석을 실행하세요. 이 영역은 API 응답이 도착하면 채워집니다.");
      return;
    }
    if (refs.schemaVersion) refs.schemaVersion.textContent = result.schemaVersion || "gs-risk-lab-v1";
    renderMetrics(result);
    renderChart(refs.cumulativeChart, refs.cumulativeAlt, result.cumulative, "cumulative", "누적 경로");
    renderChart(refs.drawdownChart, refs.drawdownAlt, result.drawdown, "drawdown", "낙폭 경로");
    renderRiskContributions(result.riskContributions);
    renderCorrelation(result.correlation);
    renderMetadata(result);
    updateShockRail();
    const notices = result.missing.concat(result.warnings);
    if (refs.partialNotice) {
      refs.partialNotice.hidden = notices.length === 0;
      const noticeLabel = result.partial || result.missing.length ? "부분 응답 / 결측" : "데이터 정책 / 경고";
      refs.partialNotice.textContent = notices.length ? `${noticeLabel}: ${notices.join(" · ")}` : "";
    }
  }

  function clearAnalysisSurface() {
    state.result = null;
    renderAnalysisSurface(null);
    updateShockRail();
  }

  function renderMetrics(result) {
    const definitions = [
      { key: "annualized_return_pct", aliases: ["annualized_return_pct", "annual_return_pct", "cagr_pct", "return_pct", "annualized_return"], unit: "pct" },
      { key: "annualized_volatility_pct", aliases: ["annualized_volatility_pct", "annual_volatility_pct", "volatility_pct", "annualized_volatility", "volatility"], unit: "pct" },
      { key: "max_drawdown_pct", aliases: ["max_drawdown_pct", "maximum_drawdown_pct", "drawdown_pct", "max_drawdown", "maximum_drawdown"], unit: "pct" },
      { key: "sharpe_ratio", aliases: ["sharpe_ratio", "sharpe"], unit: "ratio" },
      { key: "beta", aliases: ["portfolio_beta", "beta"], unit: "ratio" },
      { key: "observations", aliases: ["observations", "observation_count", "n_observations", "sample_size", "rows"], unit: "count" },
      { key: "var_95_1d_pct", aliases: ["var_95_1d_pct", "historical_var_95_1d_pct", "var_95_1d", "historical_var"], unit: "pct" },
      { key: "expected_shortfall_95_1d_pct", aliases: ["expected_shortfall_95_1d_pct", "es_95_1d_pct", "expected_shortfall_95_1d", "expected_shortfall"], unit: "pct" },
    ];
    definitions.forEach((definition) => {
      const target = document.querySelector(`[data-metric="${definition.key}"]`);
      if (!target) return;
      const metric = result ? readMetric(result.metrics, definition.aliases) : null;
      target.textContent = metric ? formatMetric(metric.value, definition.unit, metric.unit) : "—";
      target.parentElement?.setAttribute("data-present", String(Boolean(metric)));
    });
  }

  function normalizeAnalysis(payload, requestPayload) {
    const root = unwrapAnalysis(payload);
    const metrics = isObject(root.metrics) ? root.metrics : {};
    const cumulative = normalizeSeries(root, "cumulative", requestPayload.benchmark);
    const returnedDrawdown = normalizeSeries(root, "drawdown", requestPayload.benchmark);
    const drawdownDerived = Boolean(!returnedDrawdown.length && cumulative.length);
    const drawdown = drawdownDerived ? deriveDrawdownSeries(cumulative) : returnedDrawdown;
    const riskContributions = normalizeRiskContributions(root, requestPayload);
    const correlation = normalizeCorrelation(root, requestPayload.positions.map((position) => position.symbol));
    const warnings = collectStrings(root.warnings, payload?.warnings, root.partial?.warnings, root.data_quality?.warnings);
    const missing = collectStrings(root.missing, root.partial?.missing, root.data_quality?.missing, root.errors?.missing);
    if (!cumulative.length) missing.push("누적 시계열");
    if (!drawdown.length) missing.push("낙폭 시계열");
    if (!riskContributions.length) missing.push("위험기여");
    if (!correlation) missing.push("상관 행렬");
    const uniqueMissing = uniqueStrings(missing);
    const engine = isObject(root.engine) ? root.engine : {};
    const observationCount = Math.max(
      cumulative.reduce((max, line) => Math.max(max, line.points.length), 0),
      drawdown.reduce((max, line) => Math.max(max, line.points.length), 0),
      numberOrNull(readMetric(metrics, ["observations", "observation_count", "n_observations"])?.value) || 0,
    );
    const portfolioBeta = readMetric(metrics, ["portfolio_beta", "beta"]);
    const scenarioRows = mergeScenarioRows(requestPayload.positions, riskContributions, root);
    return {
      schemaVersion: String(payload?.schema_version || root.schema_version || "gs-risk-lab-v1"),
      request: requestPayload,
      metrics,
      cumulative,
      drawdown,
      riskContributions,
      correlation,
      scenarioRows,
      portfolioBeta: portfolioBeta ? portfolioBeta.value : null,
      engine,
      source: firstDefined(root.source, root.data_source, root.data_vendor, root.data?.source, root.data?.provider, root.data?.source_name, root.data_provenance?.source, root.provenance?.source, payload?.source),
      asOf: firstDefined(root.as_of, root.source_as_of, root.data?.as_of, root.provenance?.as_of),
      retrievedAt: firstDefined(root.generated_at, root.data?.retrieved_at, root.provenance?.retrieved_at),
      formulas: firstDefined(root.formulas, root.formula, root.formula_metadata, root.method, root.provenance?.formula),
      drawdownDerived,
      warnings: uniqueStrings(warnings),
      missing: uniqueMissing,
      observationCount,
      partial: Boolean(root.partial || root.status === "partial" || root.data_quality?.partial || uniqueMissing.length),
    };
  }

  function unwrapAnalysis(payload) {
    if (!isObject(payload)) return {};
    const candidates = [payload.data, payload.result, payload.analysis, payload.risk_lab, payload];
    return candidates.find((candidate) => isObject(candidate) && (candidate.metrics || candidate.series || candidate.charts || candidate.risk_contributions || candidate.correlation_matrix)) || payload;
  }

  function renderChart(container, altContainer, lines, kind, title) {
    if (!container) return;
    if (!lines || !lines.length || !lines.some((line) => line.points.length)) {
      container.innerHTML = `<div class="chart-empty"><span aria-hidden="true">${kind === "drawdown" ? "⌁" : "／"}</span><strong>${escapeHTML(title)} 대기 중</strong><small>${kind === "drawdown" ? "결측값을 0으로 대체하지 않습니다." : "API가 반환한 배열로만 선을 그립니다."}</small></div>`;
      if (altContainer) altContainer.innerHTML = "";
      updateObservationLabel(kind, 0);
      return;
    }
    const width = 920;
    const height = 320;
    const padding = { top: 25, right: 20, bottom: 41, left: 54 };
    const plotWidth = width - padding.left - padding.right;
    const plotHeight = height - padding.top - padding.bottom;
    const points = lines.flatMap((line) => line.points.map((point) => point.value)).filter(Number.isFinite);
    let min = Math.min(...points);
    let max = Math.max(...points);
    if (kind === "drawdown") {
      min = Math.min(min, 0);
      max = Math.max(max, 0);
    }
    if (Math.abs(max - min) < 1e-9) {
      min -= 1;
      max += 1;
    } else {
      const pad = (max - min) * .08;
      min -= pad;
      max += pad;
    }
    const maxLength = Math.max(...lines.map((line) => line.points.length), 2);
    const xFor = (index) => padding.left + (index / Math.max(maxLength - 1, 1)) * plotWidth;
    const yFor = (value) => padding.top + (max - value) / (max - min) * plotHeight;
    const gridValues = [0, .25, .5, .75, 1].map((ratio) => max - (max - min) * ratio);
    const grid = gridValues.map((value) => {
      const y = yFor(value).toFixed(2);
      return `<line class="chart-grid-line" x1="${padding.left}" y1="${y}" x2="${width - padding.right}" y2="${y}"></line><text class="chart-axis-label" x="${padding.left - 9}" y="${Number(y) + 4}" text-anchor="end">${escapeHTML(formatChartValue(value, kind))}</text>`;
    }).join("");
    const baseline = kind === "drawdown" && min <= 0 && max >= 0
      ? `<line class="chart-baseline" x1="${padding.left}" y1="${yFor(0).toFixed(2)}" x2="${width - padding.right}" y2="${yFor(0).toFixed(2)}"></line>`
      : "";
    const paths = lines.map((line, index) => {
      const d = line.points.map((point, pointIndex) => `${pointIndex === 0 ? "M" : "L"} ${xFor(pointIndex).toFixed(2)} ${yFor(point.value).toFixed(2)}`).join(" ");
      return `<path class="chart-line chart-line-${index % 4}" d="${d}" vector-effect="non-scaling-stroke"><title>${escapeHTML(line.label)}</title></path>`;
    }).join("");
    const firstDate = lines[0].points[0]?.date || "시작";
    const lastDate = lines[0].points[lines[0].points.length - 1]?.date || "끝";
    const middleDate = lines[0].points[Math.floor((lines[0].points.length - 1) / 2)]?.date || "";
    const xLabels = [
      { date: firstDate, x: padding.left, anchor: "start" },
      { date: middleDate, x: width / 2, anchor: "middle" },
      { date: lastDate, x: width - padding.right, anchor: "end" },
    ].map((item) => `<text class="chart-axis-label chart-date-label" x="${item.x}" y="${height - 13}" text-anchor="${item.anchor}">${escapeHTML(shortDate(item.date))}</text>`).join("");
    const legend = lines.map((line, index) => `<span class="chart-legend__item"><i class="chart-legend__swatch chart-legend__swatch--${index % 4}" aria-hidden="true"></i>${escapeHTML(line.label)}</span>`).join("");
    const accessibleLabel = `${title}. ${lines.map((line) => `${line.label} ${line.points.length}개 반환 관측치`).join(", ")}`;
    container.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeAttr(accessibleLabel)}" focusable="false"><title>${escapeHTML(title)}</title><g class="chart-grid">${grid}</g>${baseline}<g class="chart-lines">${paths}</g>${xLabels}</svg><div class="chart-legend" aria-label="계열 범례">${legend}</div>`;
    renderChartTable(altContainer, lines, title);
    updateObservationLabel(kind, Math.max(...lines.map((line) => line.points.length)));
  }

  function updateObservationLabel(kind, count) {
    const target = kind === "drawdown" ? refs.drawdownObservations : refs.cumulativeObservations;
    if (target) target.textContent = count ? `${count} observations` : "— observations";
  }

  function renderChartTable(container, lines, title) {
    if (!container) return;
    const maxLength = Math.max(...lines.map((line) => line.points.length), 0);
    const head = lines.map((line) => `<th scope="col">${escapeHTML(line.label)}</th>`).join("");
    const body = Array.from({ length: maxLength }, (_, index) => {
      const date = lines.find((line) => line.points[index]?.date)?.points[index]?.date || String(index + 1);
      const values = lines.map((line) => `<td>${line.points[index] ? escapeHTML(formatChartValue(line.points[index].value, "table")) : "—"}</td>`).join("");
      return `<tr><th scope="row">${escapeHTML(shortDate(date))}</th>${values}</tr>`;
    }).join("");
    container.innerHTML = `<details><summary>반환 계열을 표로 읽기</summary><table class="chart-data-table"><caption>${escapeHTML(title)} 접근성 대체 표</caption><thead><tr><th scope="col">기준일</th>${head}</tr></thead><tbody>${body}</tbody></table></details>`;
  }

  function renderRiskContributions(rows) {
    if (!refs.riskBars || !refs.riskTableBody) return;
    if (!rows || !rows.length) {
      refs.riskBars.innerHTML = `<div class="section-empty"><strong>위험기여 데이터 없음</strong><small>응답에 risk contribution 배열이 없거나 결측입니다.</small></div>`;
      refs.riskTableBody.innerHTML = `<tr><td colspan="5" class="table-empty">반환 데이터 없음</td></tr>`;
      return;
    }
    const values = rows.map((row) => Math.abs(row.contribution)).filter(Number.isFinite);
    const maxValue = Math.max(...values, 0);
    refs.riskBars.innerHTML = rows.map((row) => {
      const value = Number.isFinite(row.contribution) ? row.contribution : null;
      const width = value === null || maxValue <= 0 ? 0 : Math.min(100, Math.abs(value) / maxValue * 100);
      return `<div class="risk-bar-row${value !== null && value < 0 ? " is-negative" : ""}"><span class="risk-bar-row__symbol">${escapeHTML(row.symbol)}</span><span class="risk-bar-row__track" aria-hidden="true"><i class="risk-bar-row__fill" style="--bar-width:${width.toFixed(2)}%"></i></span><span class="risk-bar-row__value">${value === null ? "N/A" : escapeHTML(formatSignedPercent(value))}</span></div>`;
    }).join("");
    refs.riskTableBody.innerHTML = rows.map((row) => {
      const contribution = Number.isFinite(row.contribution) ? row.contribution : null;
      const beta = Number.isFinite(row.beta) ? formatNumber(row.beta, 2) : "N/A";
      const weight = Number.isFinite(row.weight) ? formatPercent(row.weight, 2) : "N/A";
      const className = contribution !== null ? (contribution < 0 ? "risk-negative" : "risk-positive") : "";
      return `<tr><th scope="row">${escapeHTML(row.symbol)}</th><td>${escapeHTML(weight)}</td><td>${escapeHTML(beta)}</td><td class="${className}">${contribution === null ? "N/A" : escapeHTML(formatSignedPercent(contribution))}</td><td class="table-status">${contribution === null ? "결측" : "returned"}</td></tr>`;
    }).join("");
  }

  function renderCorrelation(correlation) {
    if (!refs.correlationScroll) return;
    if (!correlation || !correlation.symbols.length || !correlation.matrix.length) {
      refs.correlationScroll.innerHTML = `<div class="section-empty"><strong>상관 행렬 데이터 없음</strong><small>응답의 correlation matrix를 확인하세요.</small></div>`;
      return;
    }
    const heads = correlation.symbols.map((symbol) => `<th scope="col">${escapeHTML(symbol)}</th>`).join("");
    const rows = correlation.symbols.map((symbol, rowIndex) => {
      const cells = correlation.symbols.map((_, colIndex) => {
        const value = numericFrom(correlation.matrix[rowIndex]?.[colIndex]);
        if (value === null) return `<td aria-label="결측">N/A</td>`;
        const color = correlationColor(value);
        return `<td style="background-color:${color.background};color:${color.foreground}" aria-label="${escapeAttr(`${symbol}와 ${correlation.symbols[colIndex]} 상관 ${formatNumber(value, 2)}`)}">${escapeHTML(formatNumber(value, 2))}</td>`;
      }).join("");
      return `<tr><th scope="row">${escapeHTML(symbol)}</th>${cells}</tr>`;
    }).join("");
    refs.correlationScroll.innerHTML = `<table class="correlation-table"><caption>포지션 상관 행렬</caption><thead><tr><th scope="col">종목</th>${heads}</tr></thead><tbody>${rows}</tbody></table>`;
  }

  function renderMetadata(result) {
    if (!result) {
      refs.metadataSource && (refs.metadataSource.textContent = "분석 실행 후 응답에 표시됩니다.");
      refs.metadataAsOf && (refs.metadataAsOf.textContent = "—");
      refs.metadataEngine && (refs.metadataEngine.textContent = "—");
      refs.metadataMarquee && (refs.metadataMarquee.textContent = "Marquee institutional API 연결 없음");
      refs.engineMode && (refs.engineMode.textContent = "open_source_local");
      refs.metadataWarnings && (refs.metadataWarnings.textContent = "응답의 source, as-of, formula 필드를 확인하고 결측·부분 성공 여부를 함께 읽으세요.");
      return;
    }
    refs.metadataSource && (refs.metadataSource.textContent = describeMetadata(result.source) || "응답에 source가 없습니다.");
    const asOf = describeMetadata(result.asOf);
    const retrievedAt = describeMetadata(result.retrievedAt);
    refs.metadataAsOf && (refs.metadataAsOf.textContent = asOf ? `${asOf}${retrievedAt ? ` / 조회 ${retrievedAt}` : ""}` : "응답에 as-of가 없습니다.");
    const engineName = describeMetadata(result.engine?.name || result.engine?.engine || "gs-quant");
    const engineMode = describeMetadata(result.engine?.mode || result.engine?.runtime || "open_source_local");
    refs.metadataEngine && (refs.metadataEngine.textContent = `${engineName} · ${engineMode}`);
    const marqueeConnected = result.engine?.marquee_connected === true || result.engine?.marqueeConnected === true;
    refs.metadataMarquee && (refs.metadataMarquee.textContent = marqueeConnected ? "응답 표시: 연결됨" : "연결 없음 · open source local");
    refs.engineMode && (refs.engineMode.textContent = engineMode || "open_source_local");
    const formula = describeMetadata(result.formulas);
    const issueText = result.warnings.concat(result.missing);
    const derivedText = result.drawdownDerived ? "낙폭 계열은 반환된 indexed_level 누적 계열에서 고점 대비로 계산" : "";
    refs.metadataWarnings && (refs.metadataWarnings.textContent = formula ? `응답 산식: ${formula}${derivedText ? ` · ${derivedText}` : ""}${issueText.length ? ` · ${issueText.join(" · ")}` : ""}` : derivedText || (issueText.length ? issueText.join(" · ") : "응답의 formula 필드를 제공자와 함께 확인하세요."));
  }

  function handleShockInput() {
    state.scenarioShock = Number(refs.shockRange?.value || -10);
    updateShockRail();
  }

  function updateShockRail() {
    const shock = Number.isFinite(state.scenarioShock) ? state.scenarioShock : -10;
    if (refs.shockRange) {
      refs.shockRange.value = String(shock);
      refs.shockRange.setAttribute("aria-valuetext", `SPY ${formatSignedPercent(shock)}`);
    }
    if (refs.shockValue) refs.shockValue.textContent = formatSignedPercent(shock, 1);
    const result = state.result;
    if (!result) {
      if (refs.shockImpact) refs.shockImpact.textContent = "분석 실행 후 반환된 β로 계산";
      if (refs.scenarioContrib) refs.scenarioContrib.innerHTML = `<div class="scenario-empty">분석을 실행하면 포지션별 기여가 이곳에 표시됩니다.</div>`;
      return;
    }
    const rows = result.scenarioRows || [];
    const portfolioBeta = Number.isFinite(result.portfolioBeta) ? result.portfolioBeta : null;
    const calculated = rows.map((row) => {
      const beta = Number.isFinite(row.beta) ? row.beta : portfolioBeta;
      const fallback = !Number.isFinite(row.beta) && Number.isFinite(portfolioBeta);
      const contribution = Number.isFinite(beta) && Number.isFinite(row.weight) ? row.weight / 100 * beta * shock : null;
      return { ...row, beta, fallback, contribution };
    });
    const contributions = calculated.map((row) => row.contribution).filter(Number.isFinite);
    const total = contributions.length ? contributions.reduce((sum, value) => sum + value, 0) : null;
    if (refs.shockImpact) {
      refs.shockImpact.textContent = total === null ? "β 데이터가 없어 추정할 수 없음" : `SPY ${formatSignedPercent(shock, 1)} → ${formatSignedPercent(total, 2)}p 추정`;
    }
    if (!refs.scenarioContrib) return;
    if (!calculated.length) {
      refs.scenarioContrib.innerHTML = `<div class="scenario-empty">반환된 포지션 β가 없습니다.</div>`;
      return;
    }
    const maxAbs = Math.max(...calculated.map((row) => Math.abs(row.contribution || 0)), 0);
    const fallbackUsed = calculated.some((row) => row.fallback);
    const rowsMarkup = calculated.map((row) => {
      const value = row.contribution;
      const width = value === null || maxAbs <= 0 ? 0 : Math.min(100, Math.abs(value) / maxAbs * 100);
      const label = value === null ? `${row.symbol} β 데이터 없음` : `${row.symbol} 추정 기여 ${formatSignedPercent(value, 2)}p`;
      return `<div class="scenario-row${value !== null && value >= 0 ? " is-positive" : ""}" title="${escapeAttr(label)}"><span class="scenario-row__symbol">${escapeHTML(row.symbol)}</span><span class="scenario-row__track" aria-hidden="true"><i class="scenario-row__fill" style="--scenario-width:${width.toFixed(2)}%"></i></span><span class="scenario-row__value">${value === null ? "N/A" : escapeHTML(formatSignedPercent(value, 2) + "p")}</span></div>`;
    }).join("");
    const note = fallbackUsed ? `<div class="scenario-empty">포지션별 β가 없는 행은 반환된 포트폴리오 β를 참고값으로 적용했습니다.</div>` : "";
    refs.scenarioContrib.innerHTML = `${rowsMarkup}${note}`;
  }

  async function importStoredPortfolio() {
    if (state.importing) return;
    let stored;
    try {
      const raw = window.localStorage.getItem(STORAGE_KEY);
      if (!raw) {
        setImportStatus("저장된 포트폴리오가 없습니다.");
        return;
      }
      stored = JSON.parse(raw);
    } catch (_) {
      setImportStatus("저장된 포트폴리오를 읽지 못했습니다.");
      return;
    }
    if (!isObject(stored) || !Array.isArray(stored.holdings)) {
      setImportStatus("저장 형식이 올바르지 않습니다.");
      return;
    }
    const usHoldings = stored.holdings.filter((holding) => normalizeMarket(holding?.market || holding?.group_key || holding?.groupKey) === "us").filter((holding) => String(holding.symbol || "").trim());
    if (!usHoldings.length) {
      setImportStatus("저장 목록에 미국 포지션이 없습니다. 한국·금·현금은 이 Lab에서 제외합니다.");
      return;
    }
    state.importing = true;
    if (refs.importButton) {
      refs.importButton.disabled = true;
      refs.importButton.textContent = "US 가격 구성 중…";
    }
    setImportStatus("저장된 미국 수량을 /api/portfolio/compose로 구성하는 중입니다…");
    try {
      const composePayload = {
        holdings: usHoldings.map((holding) => ({
          market: "us",
          symbol: String(holding.symbol).trim().toUpperCase(),
          name: String(holding.name || holding.symbol).trim(),
          quantity: Number(holding.quantity),
        })).filter((holding) => Number.isFinite(holding.quantity) && holding.quantity > 0),
        cash_krw: 0,
      };
      if (!composePayload.holdings.length) throw new ApiError("유효한 미국 수량이 없습니다.", 400, "invalid_holdings");
      const payload = await postJson(API.portfolioCompose, composePayload, 30000);
      const imported = normalizeComposedUS(payload, composePayload.holdings);
      if (imported.length < MIN_POSITIONS) throw new ApiError("구성 응답에서 분석 가능한 미국 자산이 2개 미만입니다.", 422, "insufficient_us_positions");
      const limited = imported.sort((a, b) => b.value - a.value).slice(0, MAX_POSITIONS);
      const weights = normalizeImportedWeights(limited.map((holding) => holding.value));
      renderPositionRows(limited.map((holding, index) => ({ symbol: holding.symbol, weight: weights[index] })));
      clearAnalysisSurface();
      const omitted = imported.length > MAX_POSITIONS ? ` · ${imported.length - MAX_POSITIONS}개는 12행 제한으로 제외` : "";
      setImportStatus(`US-only 재정규화 완료 · ${limited.length}개 자산 · 평가액 기준 합계 100%${omitted}. 현금·비미국 자산은 제외했습니다.`);
      showFormMessage("저장된 미국 포지션을 불러왔습니다. 조건을 확인하고 분석을 실행하세요.", null);
    } catch (error) {
      const detail = error instanceof ApiError ? error.message : "포트폴리오 구성 응답을 읽지 못했습니다.";
      setImportStatus(`가져오지 못했습니다. ${detail}`);
    } finally {
      state.importing = false;
      if (refs.importButton) {
        refs.importButton.disabled = false;
        refs.importButton.textContent = "US 포지션 가져오기";
      }
    }
  }

  function normalizeComposedUS(payload, sourceHoldings) {
    const root = unwrapCompose(payload);
    let raw = firstArray(root.priced, root.holdings, root.items, payload?.priced, payload?.holdings);
    if (!raw.length && Array.isArray(root.groups)) {
      raw = root.groups.flatMap((group) => Array.isArray(group?.holdings) ? group.holdings : []);
    }
    const sourceBySymbol = new Map(sourceHoldings.map((holding) => [String(holding.symbol).toUpperCase(), holding]));
    return raw.map((item) => {
      const symbol = String(firstDefined(item?.symbol, item?.ticker, item?.code, "") || "").trim().toUpperCase();
      if (!symbol || !sourceBySymbol.has(symbol)) return null;
      const value = readComposedValue(item);
      const source = sourceBySymbol.get(symbol);
      return value !== null ? { symbol, value } : null;
    }).filter(Boolean).filter((item) => item.value > 0);
  }

  function unwrapCompose(payload) {
    if (!isObject(payload)) return {};
    return [payload.portfolio, payload.result, payload.data, payload].find((candidate) => isObject(candidate) && (candidate.priced || candidate.holdings || candidate.groups || candidate.items)) || payload;
  }

  function readComposedValue(item) {
    const direct = readMetric(item, ["value_krw", "valueKrw", "market_value_krw", "marketValueKrw", "current_value_krw", "currentValueKrw", "valuation_krw", "amount_krw", "value"]);
    if (direct) return direct.value;
    const price = readMetric(item, ["price_krw", "priceKrw"]);
    const quantity = readMetric(item, ["quantity", "units"]);
    if (price && quantity) return price.value * quantity.value;
    const weight = readMetric(item, ["weight_pct", "weightPct", "percentage"]);
    return weight ? weight.value : null;
  }

  function normalizeImportedWeights(values) {
    const total = values.reduce((sum, value) => sum + (Number.isFinite(value) && value > 0 ? value : 0), 0);
    if (total <= 0) return values.map(() => 0);
    const weights = values.map((value) => Number((value / total * 100).toFixed(2)));
    const last = weights.length - 1;
    weights[last] = Number((100 - weights.slice(0, last).reduce((sum, value) => sum + value, 0)).toFixed(2));
    return weights;
  }

  function setImportStatus(message) {
    if (refs.importStatus) refs.importStatus.textContent = message || "";
  }

  function normalizeRiskContributions(root, requestPayload) {
    const raw = firstDefined(
      root.risk_contributions,
      root.riskContributions,
      root.risk_contribution,
      root.riskContribution,
      root.risk?.contributions,
      root.risk?.risk_contributions,
      root.metrics?.risk_contributions,
      root.metrics?.riskContributions,
    );
    const records = normalizeRiskRaw(raw).map(normalizeRiskRecord).filter((row) => row.symbol);
    const responsePositions = firstArray(root.per_position_metrics, root.perPositionMetrics, root.per_position, root.positions, root.position_contributions, root.positionContributions, root.metrics?.positions);
    const positionRows = responsePositions.map(normalizeRiskRecord).filter((row) => row.symbol);
    const bySymbol = new Map(positionRows.map((row) => [row.symbol, row]));
    records.forEach((row) => {
      const current = bySymbol.get(row.symbol) || { symbol: row.symbol, weight: null, beta: null, contribution: null };
      bySymbol.set(row.symbol, {
        symbol: row.symbol,
        weight: Number.isFinite(row.weight) ? row.weight : current.weight,
        beta: Number.isFinite(row.beta) ? row.beta : current.beta,
        contribution: Number.isFinite(row.contribution) ? row.contribution : current.contribution,
      });
    });
    const combined = Array.from(bySymbol.values());
    if (combined.some((row) => Number.isFinite(row.beta) || Number.isFinite(row.contribution) || Number.isFinite(row.weight))) return dedupeRiskRows(combined);
    return [];
  }

  function normalizeRiskRaw(raw) {
    if (Array.isArray(raw)) return raw;
    if (isObject(raw)) {
      if (Array.isArray(raw.rows)) return raw.rows;
      if (Array.isArray(raw.items)) return raw.items;
      if (isObject(raw.by_symbol)) return Object.entries(raw.by_symbol).map(([symbol, value]) => ({ symbol, ...(isObject(value) ? value : { contribution: value }) }));
      if (isObject(raw.bySymbol)) return Object.entries(raw.bySymbol).map(([symbol, value]) => ({ symbol, ...(isObject(value) ? value : { contribution: value }) }));
      return Object.entries(raw).filter(([key, value]) => isObject(value) || Number.isFinite(Number(value))).map(([symbol, value]) => ({ symbol, ...(isObject(value) ? value : { contribution: value }) }));
    }
    return [];
  }

  function normalizeRiskRecord(raw) {
    if (Array.isArray(raw)) return { symbol: String(raw[0] || "").toUpperCase(), weight: numericFrom(raw[1]), beta: numericFrom(raw[2]), contribution: numericFrom(raw[3]) };
    const symbol = String(firstDefined(raw?.symbol, raw?.ticker, raw?.name, "") || "").trim().toUpperCase();
    const weightMetric = readMetric(raw, ["weight_pct", "weightPct", "weight", "portfolio_weight", "portfolioWeight"]);
    const contributionMetric = readMetric(raw, ["risk_contribution_pct", "riskContributionPct", "contribution_pct", "contributionPct", "risk_contribution", "riskContribution", "contribution", "marginal_contribution_pct", "marginalContributionPct"]);
    const betaMetric = readMetric(raw, ["beta", "position_beta", "positionBeta"]);
    return {
      symbol,
      weight: normalizeWeightValue(weightMetric),
      beta: betaMetric ? betaMetric.value : null,
      contribution: normalizeContributionValue(contributionMetric),
    };
  }

  function dedupeRiskRows(rows) {
    const seen = new Set();
    return rows.filter((row) => {
      if (!row.symbol || seen.has(row.symbol)) return false;
      seen.add(row.symbol);
      return true;
    });
  }

  function mergeScenarioRows(requestPositions, riskRows, root) {
    const riskBySymbol = new Map(riskRows.map((row) => [row.symbol, row]));
    const responsePositions = firstArray(root.per_position_metrics, root.perPositionMetrics, root.per_position, root.positions, root.position_contributions, root.positionContributions, root.metrics?.positions).map(normalizeRiskRecord).filter((row) => row.symbol);
    responsePositions.forEach((row) => riskBySymbol.set(row.symbol, { ...riskBySymbol.get(row.symbol), ...row }));
    const rows = requestPositions.map((position) => {
      const found = riskBySymbol.get(position.symbol);
      return { symbol: position.symbol, weight: Number.isFinite(found?.weight) ? found.weight : position.weight_pct, beta: Number.isFinite(found?.beta) ? found.beta : null };
    });
    riskRows.forEach((row) => {
      if (!rows.some((item) => item.symbol === row.symbol)) rows.push({ symbol: row.symbol, weight: row.weight, beta: row.beta });
    });
    return rows.slice(0, MAX_POSITIONS);
  }

  function normalizeCorrelation(root, fallbackSymbols) {
    const raw = firstDefined(root.correlation_matrix, root.correlationMatrix, root.correlations, root.correlation, root.risk?.correlation_matrix, root.risk?.correlations);
    if (Array.isArray(raw)) return isObject(raw[0]) ? normalizeCorrelationRows(raw, fallbackSymbols) : normalizeCorrelationArray(raw, fallbackSymbols);
    if (!isObject(raw)) return null;
    const symbols = firstArray(raw.symbols, raw.labels, raw.tickers, raw.names).map((symbol) => String(symbol));
    const matrix = firstArray(raw.matrix, raw.values, raw.data, raw.rows);
    if (matrix.length && Array.isArray(matrix[0])) return normalizeCorrelationArray(matrix, symbols.length ? symbols : fallbackSymbols);
    if (matrix.length && isObject(matrix[0])) return normalizeCorrelationRows(matrix, symbols.length ? symbols : fallbackSymbols);
    const keys = Object.keys(raw).filter((key) => isObject(raw[key]) || Array.isArray(raw[key]));
    if (keys.length) {
      const labels = symbols.length ? symbols : keys;
      const table = labels.map((left) => labels.map((right) => numericFrom(raw[left]?.[right])));
      if (table.some((row) => row.some((value) => value !== null))) return { symbols: labels, matrix: table };
    }
    return null;
  }

  function normalizeCorrelationArray(matrix, symbols) {
    const size = matrix.length;
    const labels = (symbols.length >= size ? symbols : Array.from({ length: size }, (_, index) => fallbackLabel(index, symbols))).slice(0, size);
    return { symbols: labels, matrix: matrix.slice(0, size).map((row) => Array.from({ length: size }, (_, index) => numericFrom(row?.[index]))) };
  }

  function normalizeCorrelationRows(rows, symbols) {
    const labels = rows.map((row, index) => String(firstDefined(row?.symbol, row?.ticker, symbols[index], fallbackLabel(index, symbols))));
    const matrix = rows.map((row) => {
      const values = firstArray(row?.values, row?.correlations, row?.row);
      return labels.map((label, index) => numericFrom(row?.[label] ?? values[index]));
    });
    return { symbols: labels, matrix };
  }

  function normalizeSeries(root, kind, benchmarkSymbol) {
    const candidates = kind === "drawdown"
      ? [root.series?.drawdown, root.series?.drawdowns, root.drawdown_series, root.drawdownSeries, root.drawdown, root.charts?.drawdown, root.charts?.drawdowns, root.charts?.drawdown_contour]
      : [root.series?.cumulative, root.series?.cumulative_returns, root.cumulative_series, root.cumulativeSeries, root.cumulative_returns, root.cumulativeReturns, root.cumulative, root.chart, root.chart_series, root.aligned_chart_series, root.charts?.cumulative, root.charts?.cumulative_returns, root.charts?.performance, root.charts?.portfolio_contour, root.series];
    for (const candidate of candidates) {
      const lines = normalizeSeriesCandidate(candidate, kind, benchmarkSymbol);
      if (lines.length) return lines;
    }
    return [];
  }

  function normalizeSeriesCandidate(candidate, kind, benchmarkSymbol) {
    if (candidate === null || candidate === undefined) return [];
    if (isObject(candidate) && candidate.series !== undefined) {
      const nested = normalizeSeriesCandidate(candidate.series, kind, benchmarkSymbol);
      if (nested.length) return nested;
    }
    if (isObject(candidate) && candidate.data !== undefined) {
      const nested = normalizeSeriesCandidate(candidate.data, kind, benchmarkSymbol);
      if (nested.length) return nested;
    }
    if (Array.isArray(candidate)) {
      if (!candidate.length) return [];
      if (Array.isArray(candidate[0])) return normalizeTupleSeries(candidate, kind, benchmarkSymbol);
      if (isObject(candidate[0])) return normalizeObjectSeries(candidate, kind, benchmarkSymbol);
      if (candidate.every((item) => numericFrom(item) !== null)) return [{ label: "포트폴리오", points: candidate.map((item, index) => ({ date: String(index + 1), value: numericFrom(item) })) }];
      return [];
    }
    if (!isObject(candidate)) return [];
    const dates = firstArray(candidate.dates, candidate.timestamps, candidate.index, candidate.date);
    const namedEntries = Object.entries(candidate).filter(([key, value]) => Array.isArray(value) && !["dates", "timestamps", "index", "date", "labels"].includes(key));
    if (namedEntries.length) {
      const lines = namedEntries.map(([key, value]) => normalizeNamedLine(key, value, dates, benchmarkSymbol)).filter((line) => line.points.length);
      if (lines.length) return lines;
    }
    if (Array.isArray(candidate.values)) return [normalizeNamedLine("portfolio", candidate.values, dates, benchmarkSymbol)].filter((line) => line.points.length);
    return [];
  }

  function deriveDrawdownSeries(cumulativeLines) {
    return cumulativeLines.map((line) => {
      let highWaterMark = null;
      const points = line.points.map((point) => {
        if (!Number.isFinite(point.value)) return null;
        highWaterMark = highWaterMark === null ? point.value : Math.max(highWaterMark, point.value);
        const drawdown = highWaterMark === 0 ? 0 : (point.value / highWaterMark - 1) * 100;
        return { date: point.date, value: Number.isFinite(drawdown) ? drawdown : null };
      }).filter(Boolean);
      return { label: line.label, points };
    }).filter((line) => line.points.length);
  }

  function normalizeObjectSeries(rows, kind, benchmarkSymbol) {
    const dateKey = findKey(rows[0], ["date", "as_of", "timestamp", "time", "x", "period"]);
    const explicitPortfolio = findKey(rows[0], kind === "drawdown" ? ["portfolio", "portfolio_drawdown", "drawdown", "value", "portfolio_value"] : ["portfolio", "portfolio_cumulative", "cumulative", "portfolio_value", "value", "return", "total_return"]);
    const explicitBenchmark = findKey(rows[0], ["benchmark", "benchmark_value", "benchmark_cumulative", "benchmark_return", benchmarkSymbol, benchmarkSymbol.toLowerCase()]);
    const excluded = new Set([dateKey, "date", "as_of", "timestamp", "time", "x", "period", "label"]);
    const numericKeys = Object.keys(rows[0]).filter((key) => !excluded.has(key) && rows.some((row) => numericFrom(row?.[key]) !== null));
    const portfolioKey = explicitPortfolio || numericKeys[0];
    const benchmarkKey = explicitBenchmark || numericKeys.find((key) => key !== portfolioKey);
    const lineKeys = [portfolioKey, benchmarkKey].filter(Boolean);
    if (!lineKeys.length) return [];
    return lineKeys.map((key) => {
      const label = key === benchmarkKey && key !== portfolioKey ? benchmarkSymbol : key === portfolioKey ? "포트폴리오" : key;
      const points = rows.map((row, index) => {
        const value = numericFrom(row?.[key]);
        return value === null ? null : { date: normalizeDate(firstDefined(row?.[dateKey], row?.date, row?.timestamp, row?.time, row?.x), index), value };
      }).filter(Boolean);
      return { label, points };
    }).filter((line) => line.points.length);
  }

  function normalizeTupleSeries(rows, kind, benchmarkSymbol) {
    const firstIsDate = typeof rows[0][0] === "string" && numericFrom(rows[0][0]) === null;
    const portfolioPoints = [];
    const benchmarkPoints = [];
    rows.forEach((row, index) => {
      const date = normalizeDate(firstIsDate ? row[0] : index + 1, index);
      const portfolioValue = numericFrom(row[firstIsDate ? 1 : 0]);
      const benchmarkValue = numericFrom(row[firstIsDate ? 2 : 1]);
      if (portfolioValue !== null) portfolioPoints.push({ date, value: portfolioValue });
      if (benchmarkValue !== null) benchmarkPoints.push({ date, value: benchmarkValue });
    });
    const lines = [];
    if (portfolioPoints.length) lines.push({ label: "포트폴리오", points: portfolioPoints });
    if (benchmarkPoints.length) lines.push({ label: benchmarkSymbol, points: benchmarkPoints });
    return lines;
  }

  function normalizeNamedLine(key, values, dates, benchmarkSymbol) {
    const label = key.toLowerCase().includes("benchmark") || key.toUpperCase() === benchmarkSymbol.toUpperCase() ? benchmarkSymbol : key.toLowerCase().includes("portfolio") || key === "value" || key === "cumulative" ? "포트폴리오" : key;
    const source = isObject(values) && Array.isArray(values.values) ? values.values : values;
    const sourceDates = isObject(values) && Array.isArray(values.dates) ? values.dates : dates;
    const points = Array.isArray(source) ? source.map((item, index) => {
      const value = numericFrom(item?.value ?? item?.y ?? item?.return ?? item?.cumulative ?? item?.drawdown ?? item);
      if (value === null) return null;
      return { date: normalizeDate(firstDefined(item?.date, item?.timestamp, sourceDates?.[index]), index), value };
    }).filter(Boolean) : [];
    return { label, points };
  }

  function renderCorrelationUnavailable() {
    if (refs.correlationScroll) refs.correlationScroll.innerHTML = `<div class="section-empty"><strong>상관 행렬 데이터 없음</strong><small>응답의 correlation matrix를 확인하세요.</small></div>`;
  }

  function readMetric(object, aliases) {
    if (!isObject(object)) return null;
    const containers = [object, object.portfolio, object.metrics].filter(isObject);
    const expandedAliases = Array.from(new Set([...aliases, ...aliases.map((alias) => `portfolio_${alias}`)]));
    for (const container of containers) {
      for (const alias of expandedAliases) {
        if (!Object.prototype.hasOwnProperty.call(container, alias)) continue;
        const raw = container[alias];
        const value = numericFrom(raw);
        if (value !== null) return { value, unit: isObject(raw) ? String(raw.unit || raw.units || "") : "", key: alias };
      }
    }
    return null;
  }

  function numericFrom(value) {
    if (value === null || value === undefined || typeof value === "boolean") return null;
    if (typeof value === "number") return Number.isFinite(value) ? value : null;
    if (typeof value === "string") {
      const cleaned = value.replace(/,/g, "").replace(/%$/, "").trim();
      if (!cleaned) return null;
      const number = Number(cleaned);
      return Number.isFinite(number) ? number : null;
    }
    if (isObject(value)) {
      for (const key of ["value", "val", "estimate", "amount", "percentage", "pct", "number"]) {
        if (Object.prototype.hasOwnProperty.call(value, key)) {
          const number = numericFrom(value[key]);
          if (number !== null) return number;
        }
      }
    }
    return null;
  }

  function normalizeWeightValue(metric) {
    if (!metric) return null;
    const value = metric.value;
    return metric.key === "weight" || metric.key === "portfolio_weight" || metric.key === "portfolioWeight" ? (Math.abs(value) <= 1 ? value * 100 : value) : value;
  }

  function normalizeContributionValue(metric) {
    if (!metric) return null;
    const value = metric.value;
    return metric.key === "contribution" || metric.key === "risk_contribution" || metric.key === "riskContribution" ? (Math.abs(value) <= 1 ? value * 100 : value) : value;
  }

  function formatMetric(value, unit, sourceUnit = "") {
    if (!Number.isFinite(value)) return "N/A";
    if (unit === "count") return formatNumber(value, 0);
    if (unit === "pct") {
      const normalized = sourceUnit.toLowerCase() === "decimal" || sourceUnit.toLowerCase() === "fraction" ? value * 100 : value;
      return formatNumber(normalized, 2) + "%";
    }
    return formatNumber(value, 2);
  }

  function formatNumber(value, digits = 2) {
    if (!Number.isFinite(Number(value))) return "N/A";
    const normalized = Math.abs(Number(value)) < .0000001 ? 0 : Number(value);
    return new Intl.NumberFormat("ko-KR", { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(normalized);
  }

  function formatPercent(value, digits = 2) {
    return Number.isFinite(Number(value)) ? `${formatNumber(Number(value), digits)}%` : "N/A";
  }

  function formatSignedPercent(value, digits = 2) {
    if (!Number.isFinite(Number(value))) return "N/A";
    const number = Number(value);
    return `${number > 0 ? "+" : ""}${formatNumber(number, digits)}%`;
  }

  function formatInputNumber(value) {
    return Number.isFinite(Number(value)) ? String(Number(Number(value).toFixed(2))) : "0";
  }

  function formatChartValue(value, kind) {
    if (!Number.isFinite(Number(value))) return "N/A";
    return formatNumber(Number(value), kind === "table" ? 4 : 2);
  }

  function shortDate(value) {
    const text = String(value ?? "");
    return text.length > 16 ? text.slice(0, 10) : text;
  }

  function normalizeDate(value, index) {
    if (value === null || value === undefined || value === "") return String(index + 1);
    return String(value);
  }

  function findKey(object, candidates) {
    if (!isObject(object)) return null;
    return candidates.find((key) => Object.prototype.hasOwnProperty.call(object, key)) || null;
  }

  function correlationColor(value) {
    const clamped = Math.max(-1, Math.min(1, Number(value)));
    const intensity = Math.round(22 + Math.abs(clamped) * 31);
    if (clamped < 0) return { background: `rgb(242, ${Math.min(245, 226 + intensity)}, ${Math.min(245, 223 + intensity)})`, foreground: "#843B37" };
    return { background: `rgb(${Math.max(200, 232 - intensity)}, ${Math.max(218, 237 - intensity)}, ${Math.min(250, 245 + Math.round(intensity / 4))})`, foreground: "#153A68" };
  }

  function fallbackLabel(index, symbols) {
    return symbols[index] || `P${index + 1}`;
  }

  function normalizeMarket(value) {
    const normalized = String(value || "").trim().toLowerCase();
    return ["us", "usa", "nyse", "nasdaq", "american"].includes(normalized) ? "us" : normalized;
  }

  function describeMetadata(value) {
    if (value === null || value === undefined || value === "") return "";
    if (Array.isArray(value)) return value.map(describeMetadata).filter(Boolean).join(" · ");
    if (isObject(value)) {
      const pieces = [value.source, value.provider, value.vendor, value.name, value.version, value.portfolio_method, value.method, value.formula, value.value, value.as_of, value.asOf, value.retrieved_at, value.retrievedAt, value.trading_days_per_year].filter((item) => item !== undefined && item !== null && item !== "");
      return pieces.length ? pieces.map(String).join(" · ") : "응답 메타데이터";
    }
    return String(value);
  }

  function collectStrings(...values) {
    return values.flatMap((value) => {
      if (Array.isArray(value)) return value.map((item) => String(item)).filter(Boolean);
      if (value === null || value === undefined || value === "") return [];
      return [String(value)];
    });
  }

  function uniqueStrings(values) {
    return Array.from(new Set(values.map((value) => String(value).trim()).filter(Boolean)));
  }

  function firstArray(...values) {
    return values.find((value) => Array.isArray(value)) || [];
  }

  function firstDefined(...values) {
    return values.find((value) => value !== undefined && value !== null && value !== "");
  }

  function numberOrNull(value) {
    const number = numericFrom(value);
    return number === null ? null : number;
  }

  function isObject(value) {
    return Boolean(value && typeof value === "object" && !Array.isArray(value));
  }

  function escapeHTML(value) {
    return String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character]));
  }

  function escapeAttr(value) {
    return escapeHTML(value);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }
})();
