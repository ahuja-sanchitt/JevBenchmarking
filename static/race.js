// Page 1: the race. Renders only what the API returns; nothing about timing or order is decided here.
(() => {
  const { esc, leaves, leafKey, leafLabel, isWarranty, int, ms, usd, ratio, pctStr, prob } = U;
  const $ = (id) => document.getElementById(id);
  const PROVIDERS = ["openai", "jev"];
  const NAME = { openai: "OpenAI", jev: "Jev" };
  const FEATURE_NAME = { recat: "Ticket recategorisation", prioritiser: "Message prioritiser" };
  const SIGNAL_LABEL = {
    time_sensitive: "Time-critical", escalation: "Escalation / accusation", unresolved: "Long unresolved", safety: "Breakdown / safety", none: "None",
  };

  const MODEL_KEY = "jevrace.openai_model";
  const S = {
    cfg: null,
    model: null, // the OpenAI model Jev races against; history and stats are scoped to it
    statsAll: {},
    feature: location.hash === "#prioritiser" ? "prioritiser" : "recat",
    metric: "latency",
    grouped: false,
    history: { recat: [], prioritiser: [] },
    stats: {},
    running: false,
    run: null, // { results: {openai, jev}, done, request }
    charts: {},
    leafSet: new Set(),
  };

  // ---------------------------------------------------------------- boot
  async function boot() {
    const mode = await API.detect();
    $("mock-banner").hidden = mode !== "mock";
    try {
      S.cfg = await API.config();
    } catch (e) {
      showFormError(`Could not load configuration: ${e.message}`);
      return;
    }
    S.leafSet = new Set(leaves(S.cfg.taxonomy).map(leafKey));
    const models = S.cfg.openai_models || [S.cfg.openai_model];
    let saved = null;
    try { saved = localStorage.getItem(MODEL_KEY); } catch {}
    S.model = models.includes(saved) ? saved : S.cfg.openai_model;
    renderModelSelect();
    if (!S.cfg.keys_configured?.openai || !S.cfg.keys_configured?.jev) showFormError("This server has no provider keys configured, so races cannot run. History is still shown below.");

    bindUI();
    setupChartDefaults();
    await Promise.all([loadHistory("recat"), loadHistory("prioritiser"), loadStats()]);
    renderFeature();
  }

  async function loadHistory(feature) {
    try { S.history[feature] = await API.history(feature, S.model); } catch { S.history[feature] = []; }
  }
  async function loadStats() {
    try { [S.stats, S.statsAll] = await Promise.all([API.stats(S.model), API.stats()]); } catch { S.stats = {}; S.statsAll = {}; }
  }

  // ---------------------------------------------------------------- model choice
  const price = (m) => S.cfg.prices?.openai?.[m];
  const fmtPrice = (m) => { const p = price(m); return p ? ` · $${p.in} / $${p.out} per 1M` : ""; };

  function renderModelSelect() {
    const models = S.cfg.openai_models || [S.cfg.openai_model];
    $("model").innerHTML = models
      .map((m) => `<option value="${esc(m)}">${esc(m)}${m === S.cfg.openai_model ? " (production)" : ""}</option>`).join("");
    $("model").value = S.model;
    renderModelLabels();
  }

  function renderModelLabels() {
    $("hero-models").innerHTML = `<span class="c-openai">${esc(S.model)}</span> vs <span class="c-jev">${esc(S.cfg.jev_model)}</span>`;
    const def = S.cfg.openai_model;
    $("model-note").innerHTML = `<strong>${esc(def)}</strong> is selected by default because it's the model this runs on in production. `
      + `Pick another to race Jev against it${S.model !== def ? ` (now: <strong>${esc(S.model)}</strong>${esc(fmtPrice(S.model))})` : ""}. History and stats are kept per model.`;
  }

  async function onModelChange() {
    if (S.running) { $("model").value = S.model; return; }
    S.model = $("model").value;
    try { localStorage.setItem(MODEL_KEY, S.model); } catch {}
    renderModelLabels();
    await Promise.all([loadHistory("recat"), loadHistory("prioritiser"), loadStats()]);
    renderExplainer();
    if (!S.run) renderIdleCards();
    renderClaims();
    renderTotals();
    renderCommunity();
  }

  function bindUI() {
    document.querySelectorAll(".feature-tab").forEach((b) =>
      b.addEventListener("click", () => {
        if (S.running || S.feature === b.dataset.feature) return;
        S.feature = b.dataset.feature;
        history.replaceState(null, "", S.feature === "prioritiser" ? "#prioritiser" : location.pathname + location.search);
        S.run = null;
        renderFeature();
      })
    );
    document.querySelectorAll("#metric-tabs button").forEach((b) =>
      b.addEventListener("click", () => { S.metric = b.dataset.metric; renderCommunity(); })
    );
    $("group-toggle").addEventListener("change", (e) => { S.grouped = e.target.checked; renderMainChart(); });
    $("preset").addEventListener("change", onPresetChange);
    $("model").addEventListener("change", onModelChange);
    $("race-form").addEventListener("submit", (e) => { e.preventDefault(); runRace(); });
  }

  // Everything that depends on the selected feature.
  function renderFeature() {
    document.querySelectorAll(".feature-tab").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.feature === S.feature)));
    renderExplainer();
    renderPresetSelect();
    renderFields();
    applyPreset(S.cfg.presets[S.feature][0]);
    renderIdleCards();
    $("verdict-row").hidden = true;
    renderClaims();
    renderTotals();
    renderCommunity();
  }

  // ---------------------------------------------------------------- hero claims
  function renderClaims() {
    const st = S.stats[S.feature];
    const box = $("claims");
    if (!st || !st.paired) {
      box.innerHTML = claim("Faster", "—", "Run a race to measure") + claim("Cheaper", "—", "") + claim("Same answer", "—", "");
      return;
    }
    const sp = st.openai.ms_p50 / st.jev.ms_p50;
    const cr = st.openai.cost_mean / st.jev.cost_mean;
    box.innerHTML =
      claim(sp >= 1 ? "Jev is faster" : "OpenAI is faster", ratio(sp >= 1 ? sp : 1 / sp), `median latency · ${st.paired} paired races`, sp >= 1 ? "jev" : "openai") +
      claim(cr >= 1 ? "Jev is cheaper" : "OpenAI is cheaper", ratio(cr >= 1 ? cr : 1 / cr), "mean cost per call", cr >= 1 ? "jev" : "openai") +
      claim("Same answer", pctStr(st.agreement), S.feature === "recat" ? "category sets overlap" : "same urgent / not-urgent call");
  }
  const claim = (label, value, sub, who) =>
    `<div class="claim"><p class="eyebrow">${esc(label)}</p><div class="claim__value ${who ? "c-" + who : ""}">${esc(value)}</div><div class="claim__sub">${esc(sub)}</div></div>`;

  function renderTotals() {
    const n = (S.statsAll.recat?.runs || 0) + (S.statsAll.prioritiser?.runs || 0);
    $("total-runs").textContent = `${int(n)} total runs`;
  }

  // ---------------------------------------------------------------- explainer
  function renderExplainer() {
    const tax = S.cfg.taxonomy;
    const lv = leaves(tax);
    const queries = Object.keys(tax);
    const warranty = lv.filter((l) => isWarranty(l.query)).length;
    const nonWarranty = queries.filter((q) => !isWarranty(q)).length;
    const nQuestions = 1 + warranty + nonWarranty;

    const content = S.feature === "recat"
      ? {
          what: `A customer describes their problem in their own words. This feature reads the comment and places it in the right queue: <strong>Query › Sub-query › Heading</strong>, out of ${lv.length} categories across ${queries.length} support areas. Warranty issues get every matching fault, not just one. If nothing fits, it says so.`,
          why: "A ticket in the wrong queue, or in none, waits to be triaged by hand before the right team even sees it. Both sides get exactly the same input: the comment and the category list.",
          openai: [
            `<strong>The production prompt</strong>, called the way production calls it: instructions and all ${lv.length} categories as the system message, the comment as the user message.`,
            `The model writes the answer as JSON, and every token it reads and writes is billed.`,
            `It writes the category names itself, so it <strong>can return one that doesn't exist</strong>. Those are flagged below.`,
          ],
          jev: [
            `The taxonomy becomes <strong>${nQuestions} small questions in one request</strong>: which of the ${queries.length} areas; for each of the ${nonWarranty} non-warranty areas, which sub-query and heading; and a yes/no for each of the ${warranty} warranty faults.`,
            `Code reads the area answer and keeps only that branch. You get the accuracy of asking step by step, but you only wait for <strong>one round trip</strong>.`,
            `Every option has a <strong>probability</strong>, output is free, and it <strong>can only pick real categories</strong>.`,
          ],
        }
      : {
          what: "Reads a single customer message and decides whether the ticket is <strong>urgent</strong>: something due today, a threat or accusation, a problem pending for weeks, or a breakdown or safety issue. Everything else, like thanks, greetings, callback requests and status checks, is not urgent.",
          why: "An urgent message stuck in the normal queue (a car that won't start, a legal threat) costs far more than a false alarm. It has to work in English and Hinglish.",
          openai: [
            `<strong>One prompt</strong> with the four urgency definitions, and the customer's message.`,
            `The model decides and writes a one-sentence reason.`,
            `One answer, <strong>no confidence</strong> per signal.`,
          ],
          jev: [
            `<strong>Four yes/no questions</strong>, one per urgency signal, using the same definitions as OpenAI's prompt. Hinglish included.`,
            `Urgent if any signal clears its threshold. Escalation and safety have a lower bar, because missing those costs most.`,
            `Every signal comes back with a <strong>probability</strong>; the reason is built from them.`,
          ],
        };

    $("explainer").innerHTML = `
      <div><p class="eyebrow">What it does</p><h3>${esc(FEATURE_NAME[S.feature])}</h3><p>${content.what}</p></div>
      <div><p class="eyebrow">Why it matters</p><h3>&nbsp;</h3><p>${content.why}</p></div>
      <div class="explainer__how">
        <div class="how-col how-col--openai"><p class="eyebrow">How OpenAI does it · ${S.feature === "recat" ? "production prompt" : "same definitions as Jev"}</p><h4 class="c-openai">${esc(S.model)}</h4><ul>${content.openai.map((x) => `<li>${x}</li>`).join("")}</ul></div>
        <div class="how-col how-col--jev"><p class="eyebrow">How Jev does it · decisions API</p><h4 class="c-jev">${esc(S.cfg.jev_model)}</h4><ul>${content.jev.map((x) => `<li>${x}</li>`).join("")}</ul></div>
      </div>`;
  }

  // ---------------------------------------------------------------- inputs
  const presetLabel = (p) => p.label || (p.comment || p.message || p.id).slice(0, 60);

  function renderPresetSelect() {
    const presets = S.cfg.presets[S.feature];
    $("preset").innerHTML =
      presets.map((p) => `<option value="${esc(p.id)}">${esc(presetLabel(p))}</option>`).join("") +
      `<option value="__custom">Custom… write your own</option>`;
  }

  function renderFields() {
    const max = S.cfg.limits.max_input_chars;
    if (S.feature === "recat") {
      $("fields").innerHTML = `
        <div class="fields-grid">
          <label class="field">
            <span class="field__meta"><span class="eyebrow">Customer comment</span><span class="count" id="count"></span></span>
            <textarea class="control" id="f-text" rows="3" maxlength="${max * 2}" placeholder="Describe the problem as a customer would, in English or Hinglish"></textarea>
          </label>
        </div>`;
    } else {
      $("fields").innerHTML = `
        <div class="fields-grid">
          <label class="field">
            <span class="field__meta"><span class="eyebrow">Customer message</span><span class="count" id="count"></span></span>
            <textarea class="control" id="f-text" rows="2" maxlength="${max * 2}" placeholder="Write a customer message, in English or Hinglish"></textarea>
          </label>
        </div>`;
    }
    $("f-text").addEventListener("input", () => { updateCount(); markCustom(); });
    $("f-text").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) runRace(); });
  }

  function updateCount() {
    const n = $("f-text").value.length, max = S.cfg.limits.max_input_chars;
    const el = $("count");
    el.textContent = `${n} / ${max}`;
    el.classList.toggle("count--over", n > max);
  }

  let applying = false;
  function markCustom() { if (!applying) $("preset").value = "__custom"; }

  function onPresetChange() {
    const id = $("preset").value;
    if (id === "__custom") { $("f-text").focus(); return; }
    applyPreset(S.cfg.presets[S.feature].find((p) => p.id === id));
  }

  function applyPreset(p) {
    if (!p) return;
    applying = true;
    $("preset").value = p.id;
    $("f-text").value = S.feature === "recat" ? p.comment : p.message;
    updateCount();
    applying = false;
  }

  function buildRequest() {
    const max = S.cfg.limits.max_input_chars;
    const text = $("f-text").value.trim();
    const preset_id = $("preset").value === "__custom" ? null : $("preset").value;
    if (!text) throw new Error(S.feature === "recat" ? "Write a customer comment first." : "Write a customer message first.");
    if (text.length > max) throw new Error(`Keep it under ${max} characters (currently ${text.length}).`);
    const base = { feature: S.feature, preset_id, openai_model: S.model };
    return S.feature === "recat" ? { ...base, comment: text } : { ...base, message: text };
  }

  function showFormError(msg) { const el = $("form-error"); el.textContent = msg; el.hidden = !msg; }

  function retryText(sec) {
    if (!sec) return "";
    if (sec < 90) return ` Try again in ${Math.ceil(sec)} seconds.`;
    if (sec < 5400) return ` Try again in ${Math.ceil(sec / 60)} minutes.`;
    return ` Try again in about ${Math.round(sec / 3600)} hours.`;
  }

  // ---------------------------------------------------------------- race
  async function runRace() {
    if (S.running) return;
    showFormError("");
    let body;
    try { body = buildRequest(); } catch (e) { showFormError(e.message); return; }

    setRunning(true);
    S.run = { request: body, results: {}, done: null };
    $("verdict-row").hidden = true;
    $("run-title").textContent = "This run";

    try {
      await API.race(body, onEvent);
      if (!S.run.done && !S.run.fatal) showFormError("The connection closed before the race finished. It may still have been saved; reload to check history.");
    } catch (e) {
      const prefix = e.status === 429 ? "" : e.status === 503 ? "Unavailable: " : e.status === 400 ? "Check the input: " : "Race failed: ";
      showFormError(prefix + e.message + (e.status === 429 ? retryText(e.retryAfter) : ""));
      if (!Object.keys(S.run.results).length) renderIdleCards();
    } finally {
      setRunning(false);
    }
  }

  function setRunning(on) {
    S.running = on;
    const btn = $("run-btn");
    btn.disabled = on;
    btn.querySelector(".btn-run__icon").textContent = on ? "⧗" : "▶";
    btn.querySelector(".btn-run__label").textContent = on ? "Running…" : S.run?.done ? "Run again" : "Run race";
    document.querySelectorAll(".feature-tab").forEach((b) => (b.disabled = on));
    $("model").disabled = on;
  }

  function onEvent(event, data) {
    if (event === "start") {
      S.run.start = data;
      PROVIDERS.forEach((p) => renderLoadingCard(p, p === "openai" ? data.openai_model : data.jev_model));
    } else if (event === "result") {
      S.run.results[data.provider] = data;
      // Re-render every finished card so token bars share one scale across both lanes.
      PROVIDERS.forEach((p) => S.run.results[p] && renderResultCard(p));
      if (data.provider === "openai" && data.ok && data.usage?.cached_tokens > 0) showCacheToast(data.usage);
    } else if (event === "done") {
      S.run.done = data;
      $("run-title").textContent = `This run — #${data.run_id}`;
      if (data.point) S.history[S.feature].push(data.point);
      if (data.stats) S.stats[S.feature] = data.stats;
      if (S.statsAll[S.feature]) S.statsAll[S.feature].runs += 1;
      PROVIDERS.forEach((p) => S.run.results[p] && renderResultCard(p));
      renderVerdict();
      renderClaims();
      renderTotals();
      renderCommunity();
    } else if (event === "fatal") {
      S.run.fatal = data;
      showFormError(`The race ran but could not be saved: ${data.error}`);
    }
  }

  // ---------------------------------------------------------------- cards
  function cardHead(p, model, tags) {
    return `<header class="card__head">
      <div class="card__name"><span class="eyebrow">Provider</span><strong>${NAME[p]}</strong><span class="card__model">${esc(model || "")}</span></div>
      <div class="card__tags">${tags}</div>
    </header>`;
  }

  function renderIdleCards() {
    PROVIDERS.forEach((p) => {
      const model = p === "openai" ? S.model : S.cfg.jev_model;
      $(`card-${p}`).innerHTML = cardHead(p, model, "") + `<div class="card__idle">Pick a scenario and press Run.<br>Both requests leave at the same instant.</div>`;
    });
  }

  function renderLoadingCard(p, model) {
    $(`card-${p}`).innerHTML =
      cardHead(p, model, `<span class="tag tag--live tag--${p}">Live</span>`) +
      `<div class="loader" role="status"><div class="spinner" aria-hidden="true"></div><span class="loader__label">Calling API…</span></div>`;
  }

  // OpenAI caches prompts of 1,024+ tokens automatically; a hit bills those tokens at the cached rate.
  function showCacheToast(u) {
    const prices = price(S.run?.request?.openai_model || S.model);
    const saved = prices ? (u.cached_tokens * (prices.in - prices.cached)) / 1e6 : null;
    const share = u.prompt_tokens ? Math.round((u.cached_tokens / u.prompt_tokens) * 100) : 0;
    const el = document.createElement("div");
    el.className = "toast";
    el.setAttribute("role", "status");
    el.innerHTML = `
      <div class="toast__head"><span class="eyebrow c-openai">OpenAI prompt cache hit</span><button class="toast__close" aria-label="Dismiss">×</button></div>
      <p class="toast__body"><strong class="num">${int(u.cached_tokens)}</strong> of ${int(u.prompt_tokens)} input tokens (${share}%) were served from OpenAI's cache${
        prices ? `, billed at $${prices.cached.toFixed(2)}/M instead of $${prices.in.toFixed(2)}/M. <strong class="num">${usd(saved)}</strong> saved on this call` : ""}.</p>
      <p class="toast__note">Hits happen when the same prompt prefix was sent in the last few minutes. The cost shown already includes this discount.</p>`;
    const close = () => el.remove();
    el.querySelector(".toast__close").addEventListener("click", close);
    $("toasts").appendChild(el);
    setTimeout(close, 9000);
  }

  function renderResultCard(p) {
    const r = S.run.results[p];
    const other = S.run.results[p === "openai" ? "jev" : "openai"];
    const model = r.usage?.served_model || (p === "openai" ? S.run.start?.openai_model : S.run.start?.jev_model);
    const bothOk = r.ok && other?.ok;
    let tags = "";
    if (r.ok) {
      const pos = r.finished_position === 1 ? `<span class="tag tag--first tag--${p}">${bothOk ? "Faster" : "1st"}</span>` : `<span class="tag tag--second">2nd</span>`;
      const cache = p === "openai" && r.usage?.cached_tokens > 0 ? `<span class="tag tag--cache" title="${int(r.usage.cached_tokens)} input tokens served from OpenAI's prompt cache">Cache hit</span>` : "";
      tags = cache + pos + `<span class="tag tag--done">✓ Done</span>`;
    } else {
      tags = `<span class="tag tag--error">✕ Error</span>`;
    }

    if (!r.ok) {
      $(`card-${p}`).innerHTML = cardHead(p, model, tags) +
        `<div class="error-box"><strong>${NAME[p]} failed after ${ms(r.ms)} ms</strong>${esc(r.error || "Unknown error")}</div>
         <div class="card__idle">This race has no winner.</div>`;
      return;
    }

    $(`card-${p}`).innerHTML = cardHead(p, model, tags) + metricsHTML(p, r) + tokensHTML(p, r) + answerHTML(p, r);
  }

  // What each side is billed for: OpenAI input + output; Jev input only (its output is free).
  function billedTokens(p, u) {
    if (!u) return null;
    return p === "openai" ? u.prompt_tokens + u.completion_tokens : u.input_tokens;
  }

  function costProvenance(p, u) {
    if (p === "openai") return "computed · list price";
    return u.cost_reported === false ? "estimated · usage.cost missing" : "reported · OpenRouter";
  }

  function metricsHTML(p, r) {
    const u = r.usage;
    return `<div class="metrics">
      <div class="metric"><p class="eyebrow">Billed tokens</p><div class="metric__value">${int(billedTokens(p, u))}</div><div class="metric__prov">${p === "openai" ? "input + output" : `input only · +${int(u.output_tokens || 0)} output free`}</div></div>
      <div class="metric"><p class="eyebrow">Latency</p><div class="metric__value">${ms(r.ms)}<span class="metric__unit">ms</span></div></div>
      <div class="metric"><p class="eyebrow">Cost</p><div class="metric__value">${usd(u.cost)}</div><div class="metric__prov">${costProvenance(p, u)}</div></div>
    </div>`;
  }

  // Shared scale across both cards so the bars can be compared lane to lane.
  function tokenScale() {
    let max = 1;
    for (const p of PROVIDERS) {
      const u = S.run.results[p]?.usage;
      if (!u) continue;
      max = Math.max(max, ...(p === "openai" ? [u.prompt_tokens, u.completion_tokens] : [u.input_tokens, u.output_tokens || 0]));
    }
    return max;
  }

  function tbar(label, value, max, cls = "") {
    const w = Math.max(value > 0 ? 0.6 : 0, (value / max) * 100);
    return `<div class="tbar ${cls}"><span class="tbar__label ${cls.includes("sub") ? "tbar__label--sub" : ""}">${label}</span><div class="tbar__track"><div class="tbar__fill" style="width:${w}%"></div></div><span class="tbar__value">${int(value)}</span></div>`;
  }

  function tokensHTML(p, r) {
    const u = r.usage, max = tokenScale();
    if (p === "openai") {
      const share = u.completion_tokens ? Math.round((u.reasoning_tokens / u.completion_tokens) * 100) : 0;
      return `<section class="card__section"><p class="eyebrow">Token breakdown</p>
        ${tbar("input", u.prompt_tokens, max, "tbar--in")}
        ${tbar("output", u.completion_tokens, max)}
        ${tbar("↳ reasoning", u.reasoning_tokens, max, "tbar--sub")}
        <p class="tbar-note">${u.reasoning_tokens ? `${share}% of output is hidden reasoning, billed at the output rate.` : "No hidden reasoning tokens on this model."}${u.cached_tokens ? ` ${int(u.cached_tokens)} input tokens were cached (cheaper rate).` : ""}</p>
      </section>`;
    }
    const split = u.strategy && u.strategy !== "fused" ? ` · ${esc(u.strategy)}` : "";
    return `<section class="card__section"><p class="eyebrow">Token breakdown</p>
      ${tbar("input", u.input_tokens, max, "tbar--in")}
      ${tbar("output", u.output_tokens || 0, max)}
      <p class="tbar-note">Output tokens are not billed. ${int(u.calls || 1)} request${(u.calls || 1) > 1 ? "s" : ""}${split}${r.answer?.questions ? `, ${r.answer.questions} questions` : ""}.</p>
    </section>`;
  }

  // ---------------------------------------------------------------- answers
  function answerHTML(p, r) {
    return `<section class="card__section"><p class="eyebrow">Answer</p>${S.feature === "recat" ? recatAnswer(p, r.answer) : prioAnswer(p, r.answer)}</section>`;
  }

  function recatAnswer(p, a) {
    const invalid = (a.categories || []).filter((c) => p === "openai" && c.valid === false);
    const chips = (a.categories || []).map((c) => {
      const bad = p === "openai" && c.valid === false;
      return `<div class="chip ${bad ? "chip--invalid" : ""}"><span class="chip__label">${esc(leafLabel(c))}${bad ? `<span class="chip__flag">⚠ not a real category</span>` : ""}</span>${c.p != null ? `<span class="chip__p">p ${prob(c.p)}</span>` : ""}</div>`;
    }).join("");

    const badges = [
      !a.placed ? `<span class="badge">No match</span>` : a.is_warranty ? `<span class="badge badge--solid">Warranty</span>` : `<span class="badge">Not warranty</span>`,
      a.is_warranty && a.categories?.length > 1 ? `<span class="badge">${a.categories.length} faults</span>` : "",
      p === "jev" && a.low_confidence ? `<span class="badge badge--warn">Low confidence</span>` : "",
    ].join("");

    let extraBlock = "";
    if (p === "jev" && a.query_probabilities) {
      const top = Object.entries(a.query_probabilities).sort((x, y) => y[1] - x[1]).slice(0, 3);
      extraBlock = `<div class="pbars" aria-label="Support area probabilities">${top.map(([q, v], i) => pbar(q, v, null, i === 0)).join("")}</div><p class="legend-line">Top support areas by probability</p>`;
    }
    const flags = [
      invalid.length ? `<div class="flag">${invalid.length} returned categor${invalid.length > 1 ? "ies don't" : "y doesn't"} exist in the taxonomy.</div>` : "",
      p === "jev" && a.low_confidence ? `<div class="flag">Low confidence: the top area or leaf scored under 0.45, or no warranty fault cleared 0.55. A human should check.</div>` : "",
    ].join("");

    return `<div class="answer__badges">${badges}</div>
      <div class="chips">${chips || `<p class="muted">No category fits this comment.</p>`}</div>
      ${extraBlock}${flags}`;
  }

  function pbar(label, v, threshold, hit) {
    const tick = threshold != null ? `<span class="pbar__tick" style="left:${threshold * 100}%" title="threshold ${threshold}"></span>` : "";
    return `<div class="pbar ${hit ? "pbar--hit" : ""}"><span class="pbar__label" title="${esc(label)}">${esc(label)}</span><div class="pbar__track"><div class="pbar__fill" style="width:${v * 100}%"></div>${tick}</div><span class="pbar__v">${prob(v)}</span></div>`;
  }

  function prioAnswer(p, a) {
    const yes = a.urgent === true;
    let signals = "";
    if (p === "jev" && a.probabilities) {
      const th = a.thresholds || {};
      signals = `<div class="pbars" aria-label="Signal probabilities">${Object.entries(a.probabilities)
        .map(([k, v]) => pbar(SIGNAL_LABEL[k] || k, v, th[k] ?? 0.55, v >= (th[k] ?? 0.55))).join("")}</div>
        <p class="legend-line">Bar = probability · tick = threshold · bold = fired. Urgent if any signal fires.</p>`;
    }
    return `<div><span class="decision ${yes ? "decision--yes" : ""}">${yes ? "▲ Urgent" : "▽ Not urgent"}</span></div>
      <dl class="kv"><dt>Signal</dt><dd>${esc(SIGNAL_LABEL[a.signal] || a.signal)}</dd></dl>
      <p class="reason">“${esc(a.reason)}”</p>${signals}`;
  }

  // ---------------------------------------------------------------- verdict
  function renderVerdict() {
    const o = S.run.results.openai, j = S.run.results.jev, d = S.run.done;
    $("verdict-row").hidden = false;
    const both = o?.ok && j?.ok;

    // Metric comparison: each metric on its own scale.
    if (both) {
      const groups = [
        ["Billed tokens", "OpenAI in + out; Jev input only", billedTokens("openai", o.usage), billedTokens("jev", j.usage), int],
        ["Latency", "ms, lower is better", o.ms, j.ms, (v) => ms(v) + " ms"],
        ["Cost", "USD per call", o.usage.cost, j.usage.cost, usd],
      ];
      $("compare-panel").innerHTML = `<p class="eyebrow">This run — metric comparison</p>` + groups.map(([name, unit, ov, jv, fmt]) => {
        const max = Math.max(ov, jv) || 1;
        return `<div class="cmp-group"><div class="cmp-group__head"><span>${name}</span><span>${unit}</span></div>
          <div class="cmp-bar cmp-bar--openai"><span class="cmp-bar__name">OpenAI</span><div class="cmp-bar__track"><div class="cmp-bar__fill" style="width:${(ov / max) * 100}%"></div></div><span class="cmp-bar__v c-openai">${fmt(ov)}</span></div>
          <div class="cmp-bar cmp-bar--jev"><span class="cmp-bar__name">Jev</span><div class="cmp-bar__track"><div class="cmp-bar__fill" style="width:${(jv / max) * 100}%"></div></div><span class="cmp-bar__v c-jev">${fmt(jv)}</span></div>
        </div>`;
      }).join("") + `<p class="tbar-note">OpenAI cost computed from list price; Jev cost reported by OpenRouter.</p>`;
    } else {
      $("compare-panel").innerHTML = `<p class="eyebrow">This run — metric comparison</p><p class="empty">Not compared: ${[o, j].filter((r) => r && !r.ok).map((r) => NAME[r.provider]).join(" and ")} failed.</p>`;
    }

    const v = $("verdict");
    if (!both) {
      v.className = "panel verdict verdict--none";
      v.innerHTML = `<p class="eyebrow">This run — verdict</p><div class="v-stat"><div class="v-stat__value">No winner</div><p class="eyebrow v-stat__label">A race with a failure is not scored</p></div>`;
      return;
    }

    const sp = o.ms / j.ms;
    const jevFaster = sp >= 1;
    const cr = o.usage.cost / j.usage.cost;
    const saved = o.usage.cost - j.usage.cost;
    v.className = `panel verdict ${jevFaster ? "" : "verdict--openai"}`;

    const cmp = d.compare || {};
    let agreeRows;
    if (S.feature === "recat") {
      agreeRows = [
        ["Same answer", cmp.exact ? "✓ Exact match" : cmp.agrees ? "≈ Overlapping" : "✕ Different"],
        ["Warranty call", cmp.warranty_agrees ? "✓ Agree" : "✕ Disagree"],
        ["Confidence scores", `<span class="c-jev">Jev: per option</span> · <span class="c-openai">OpenAI: none</span>`],
        ["Invented categories", `<span class="c-openai">${(o.answer.categories || []).filter((c) => c.valid === false).length}</span> vs <span class="c-jev">0</span>`],
      ];
    } else {
      agreeRows = [
        ["Urgent / not urgent", cmp.agrees ? "✓ Agree" : "✕ Disagree"],
        ["Same signal", cmp.same_signal ? "✓ Agree" : "✕ Different"],
        ["Explanation", `<span class="c-jev">Jev: 4 signal scores</span> · <span class="c-openai">OpenAI: free text</span>`],
      ];
    }

    v.innerHTML = `<p class="eyebrow">This run — verdict</p>
      <div class="v-stat"><div class="v-stat__value ${jevFaster ? "c-jev" : "c-openai"}">${ratio(jevFaster ? sp : 1 / sp)}</div><p class="eyebrow v-stat__label">${jevFaster ? "Jev faster" : "OpenAI faster"}</p></div>
      <div class="v-stat"><div class="v-stat__value ${cr >= 1 ? "c-jev" : "c-openai"}">${ratio(cr >= 1 ? cr : 1 / cr)}</div><p class="eyebrow v-stat__label">${cr >= 1 ? "Jev cheaper" : "OpenAI cheaper"} · ${saved >= 0 ? usd(saved) + " saved this call" : usd(-saved) + " more this call"}</p></div>
      <div class="v-agree">${agreeRows.map(([k, val]) => `<div class="v-agree__row"><span>${k}</span><span>${val}</span></div>`).join("")}</div>`;
  }

  // ---------------------------------------------------------------- community
  function renderCommunity() {
    document.querySelectorAll("#metric-tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.metric === S.metric)));
    const st = S.stats[S.feature] || { runs: 0, paired: 0, openai: {}, jev: {} };
    $("community-feature").textContent = `${FEATURE_NAME[S.feature]} · Jev vs ${S.model}`;
    $("community-summary").innerHTML = st.runs
      ? `Aggregated across <span class="c-jev">${int(st.runs)} runs</span> · ${int(st.paired)} where both answered · Jev answered first in ${pctStr(st.jev_faster_share)} · same answer ${pctStr(st.agreement)}`
      : "No races yet for this feature. Be the first.";

    const o = st.openai, j = st.jev;
    const tile = (label, value, sub, who) => `<div class="tile"><p class="eyebrow">${label}</p><div class="tile__value ${who ? "c-" + who : ""}">${value}</div><div class="tile__sub">${sub || ""}</div></div>`;
    const change = (a, b) => (a && b ? `${b <= a ? "−" : "+"}${Math.abs(Math.round(((b - a) / a) * 100))}%` : "—");
    let tiles;
    if (S.metric === "latency") {
      tiles = tile("Median · OpenAI", `${ms(o.ms_p50)}ms`, `p90 ${ms(o.ms_p90)}ms`, "openai") +
        tile("Median · Jev", `${ms(j.ms_p50)}ms`, `p90 ${ms(j.ms_p90)}ms`, "jev") +
        tile("Latency change", change(o.ms_p50, j.ms_p50), "Jev vs OpenAI, median", "jev") +
        tile("Jev answered first", pctStr(st.jev_faster_share), "share of paired races");
    } else if (S.metric === "tokens") {
      const ot = o.prompt_tokens_p50 != null ? o.prompt_tokens_p50 + o.completion_tokens_p50 : null;
      const jt = j.input_tokens_p50;
      tiles = tile("Median · OpenAI", int(ot), `in ${int(o.prompt_tokens_p50)} · out ${int(o.completion_tokens_p50)}`, "openai") +
        tile("Median · Jev", int(jt), `input only · +${int(j.output_tokens_p50)} output free`, "jev") +
        tile("Token change", change(ot, jt), "Jev vs OpenAI, median", "jev") +
        tile("OpenAI reasoning", int(o.reasoning_tokens_p50), "median hidden tokens per call");
    } else {
      tiles = tile("Mean · OpenAI", usd(o.cost_mean), "computed · list price", "openai") +
        tile("Mean · Jev", usd(j.cost_mean), "reported · OpenRouter", "jev") +
        tile("Cost ratio", o.cost_mean && j.cost_mean ? ratio(o.cost_mean / j.cost_mean) : "—", "OpenAI ÷ Jev, mean", "jev") +
        tile("Same answer", pctStr(st.agreement), "paired races");
    }
    $("community-tiles").innerHTML = tiles;
    renderMainChart();
    renderScatter();
  }

  // ---------------------------------------------------------------- charts
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

  function setupChartDefaults() {
    if (!window.Chart) return;
    Chart.defaults.font.family = css("--font");
    Chart.defaults.font.size = 11;
    Chart.defaults.color = css("--ink-3");
    Chart.defaults.borderColor = css("--line");
    Chart.defaults.plugins.legend.labels.boxWidth = 12;
    Chart.defaults.plugins.legend.labels.boxHeight = 12;
    Chart.defaults.plugins.tooltip.backgroundColor = "#000";
    Chart.defaults.plugins.tooltip.borderColor = css("--line-strong");
    Chart.defaults.plugins.tooltip.borderWidth = 1;
    Chart.defaults.plugins.tooltip.titleFont = { weight: "600" };
    if (matchMedia("(prefers-reduced-motion: reduce)").matches) Chart.defaults.animation = false;
  }

  const METRIC = {
    latency: {
      title: "Response time per race (ms)",
      note: "Linear scale. Gaps are races where that provider failed.",
      openai: (r) => r.openai_ms, jev: (r) => r.jev_ms, fmt: (v) => `${ms(v)} ms`, log: false,
    },
    tokens: {
      title: "Billed tokens per race",
      note: "Dashed line: the part of OpenAI's output that is hidden reasoning. Jev output is not billed.",
      openai: (r) => (r.openai_prompt_tokens != null ? r.openai_prompt_tokens + r.openai_completion_tokens : null),
      jev: (r) => r.jev_input_tokens,
      reasoning: (r) => r.openai_reasoning_tokens, fmt: (v) => `${int(v)} tokens`, log: false,
    },
    cost: {
      title: "Cost per race (USD) · log scale",
      note: "Log scale: each gridline is 10× the one below. OpenAI computed from list price; Jev reported by OpenRouter.",
      openai: (r) => r.openai_cost, jev: (r) => r.jev_cost, fmt: usd, log: true,
    },
  };

  function renderMainChart() {
    if (!window.Chart) return;
    const m = METRIC[S.metric];
    const rows = S.history[S.feature];
    $("main-chart-title").textContent = m.title + (S.grouped ? " · average of 5" : "");
    $("main-chart-note").textContent = m.note;
    const val = (r, p) => (r[`${p}_ok`] ? m[p](r) : null);

    let labels, series;
    if (S.grouped) {
      labels = []; series = { openai: [], jev: [], reasoning: [] };
      for (let i = 0; i < rows.length; i += 5) {
        const g = rows.slice(i, i + 5);
        labels.push(`#${g[0].id}–${g[g.length - 1].id}`);
        series.openai.push(U.mean(g.map((r) => val(r, "openai"))));
        series.jev.push(U.mean(g.map((r) => val(r, "jev"))));
        if (m.reasoning) series.reasoning.push(U.mean(g.map((r) => (r.openai_ok ? m.reasoning(r) : null))));
      }
    } else {
      labels = rows.map((r) => `#${r.id}`);
      series = {
        openai: rows.map((r) => val(r, "openai")),
        jev: rows.map((r) => val(r, "jev")),
        reasoning: m.reasoning ? rows.map((r) => (r.openai_ok ? m.reasoning(r) : null)) : [],
      };
    }

    const type = S.grouped ? "bar" : "line";
    const ds = (label, data, color, extra = {}) => ({
      label, data, borderColor: color, backgroundColor: color, borderWidth: 2, pointRadius: S.grouped ? 0 : 3, pointHoverRadius: 5, spanGaps: false, tension: 0, ...extra,
    });
    const datasets = [ds("OpenAI", series.openai, css("--openai")), ds("Jev", series.jev, css("--jev-fill"))];
    if (m.reasoning) datasets.splice(1, 0, ds("OpenAI reasoning", series.reasoning, css("--openai"), S.grouped ? { backgroundColor: css("--openai-soft") } : { borderDash: [5, 4], pointRadius: 0, borderWidth: 1.5 }));

    drawChart("main", "main-chart", {
      type,
      data: { labels, datasets },
      options: {
        maintainAspectRatio: false, responsive: true,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: true, position: "bottom" },
          tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${c.raw == null ? "failed" : m.fmt(c.raw)}` } },
        },
        scales: {
          x: { grid: { display: false }, ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 12 } },
          y: m.log
            ? { type: "logarithmic", grid: { color: css("--line") }, ticks: { callback: (v) => (Math.abs(v / 10 ** Math.floor(Math.log10(v)) - 1) < 1e-6 ? usd(v) : "") } }
            : { beginAtZero: true, grid: { color: css("--line") }, ticks: { callback: (v) => int(v) } },
        },
      },
    }, rows.length ? null : "No races yet.");
  }

  function renderScatter() {
    if (!window.Chart) return;
    const rows = S.history[S.feature].filter((r) => r.openai_ok && r.jev_ok).slice(-60);
    const pts = rows.map((r) => ({ x: r.openai_ms - r.jev_ms, y: r.openai_cost - r.jev_cost, id: r.id }));
    const zero = { id: "zeroLines", afterDraw(chart) {
      const { ctx, chartArea: a, scales: { x, y } } = chart;
      ctx.save(); ctx.strokeStyle = css("--ink-3"); ctx.lineWidth = 1;
      const x0 = x.getPixelForValue(0), y0 = y.getPixelForValue(0);
      if (x0 >= a.left && x0 <= a.right) { ctx.beginPath(); ctx.moveTo(x0, a.top); ctx.lineTo(x0, a.bottom); ctx.stroke(); }
      if (y0 >= a.top && y0 <= a.bottom) { ctx.beginPath(); ctx.moveTo(a.left, y0); ctx.lineTo(a.right, y0); ctx.stroke(); }
      ctx.restore();
    } };
    drawChart("scatter", "scatter-chart", {
      type: "scatter",
      data: { datasets: [{ label: "One race", data: pts, backgroundColor: css("--ink-2"), pointRadius: 4, pointHoverRadius: 6 }] },
      options: {
        maintainAspectRatio: false, responsive: true,
        plugins: {
          legend: { display: true, position: "bottom" },
          tooltip: { callbacks: { label: (c) => `#${c.raw.id}: ${c.raw.x >= 0 ? "Jev " + ms(c.raw.x) + " ms faster" : "OpenAI " + ms(-c.raw.x) + " ms faster"}, ${c.raw.y >= 0 ? "Jev " + usd(c.raw.y) + " cheaper" : "OpenAI " + usd(-c.raw.y) + " cheaper"}` } },
        },
        scales: {
          x: { title: { display: true, text: "Time saved by Jev (ms)" }, grid: { color: css("--line") }, ticks: { callback: (v) => int(v) } },
          y: { title: { display: true, text: "Cost saved by Jev (USD)" }, grid: { color: css("--line") }, ticks: { callback: (v) => usd(v) } },
        },
      },
      plugins: [zero],
    }, pts.length ? null : "No paired races yet.");
  }

  function drawChart(key, canvasId, config, emptyMsg) {
    const canvas = $(canvasId);
    const box = canvas.parentElement;
    box.querySelector(".empty")?.remove();
    if (S.charts[key]) { S.charts[key].destroy(); S.charts[key] = null; }
    if (emptyMsg) {
      canvas.hidden = true;
      box.insertAdjacentHTML("beforeend", `<p class="empty">${esc(emptyMsg)}</p>`);
      return;
    }
    canvas.hidden = false;
    S.charts[key] = new Chart(canvas, config);
  }

  boot();
})();
