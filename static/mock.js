// In-browser stand-in for the FastAPI backend, used until the backend exists.
// It follows the API contract in BUILD_SPEC §7 and the Jev/OpenAI answer shapes in §5.
// Answers are keyword-faked and latencies are random draws. Mock mode is always bannered in the UI.
//
// Query params:  ?mock=1            force mock mode
//                ?mockfail=openai   make the OpenAI call fail (also: jev, 429, 503, cap)
//                ?mockreset=1       wipe and reseed the mock history
window.MockBackend = (() => {
  const D = window.MOCK_DATA;
  const params = new URLSearchParams(location.search);
  const FAIL = params.get("mockfail");
  const STORE = "jevrace.mock.runs.v6";

  const OPENAI_MODEL = "gpt-4.1";
  const JEV_MODEL = "typesafe/jev-1.13";
  const JEV_SERVED = "typesafe/jev-1.13-20260917";
  const OPENAI_MODELS = {
    "gpt-4.1": { in: 2.00, cached: 0.50, out: 8.00, speed: 1.0 },
    "gpt-4o-mini": { in: 0.15, cached: 0.075, out: 0.60, speed: 0.6 },
    "gpt-6-luna": { in: 0.10, cached: 0.01, out: 0.50, speed: 0.9 },
    "gpt-6-sol": { in: 2.00, cached: 0.20, out: 10.00, speed: 1.0 },
  };
  const PRICE = { jev: { in: 0.042 } };
  const priceOf = (m) => { const { in: i, cached, out } = OPENAI_MODELS[m]; return { in: i, cached, out }; };
  let activeModel = OPENAI_MODEL; // set per call by runProvider; the mock is synchronous per answer
  const LIMITS = { max_input_chars: 600, rate_limit_per_hour: 20, daily_spend_cap_usd: 2.0 };

  const NOT_LISTED = "my issue is not listed here";
  const LEAVES = U.leaves(D.taxonomy);
  const QUERIES = Object.keys(D.taxonomy);
  const WARRANTY_LEAVES = LEAVES.filter((l) => U.isWarranty(l.query));
  const LEAF_SET = new Set(LEAVES.map(U.leafKey));
  const isCatchAll = (l) => [l.sub_query, l.query_heading].some((x) => (x || "").toLowerCase() === NOT_LISTED);


  const rand = (a, b) => a + Math.random() * (b - a);
  const jitter = (x, j) => Math.min(0.995, Math.max(0.005, x + rand(-j, j)));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const tok = (s) => Math.ceil(String(s).length / 4);

  // ---------- recategorisation ----------
  function leafScore(leaf, text) {
    const t = " " + text.toLowerCase() + " ";
    let s = 0;
    for (const k of D.leafKeywords[U.leafKey(leaf)] || []) if (t.includes(k)) s += 2;
    for (const w of [leaf.sub_query, leaf.query_heading].join(" ").toLowerCase().split(/\W+/)) {
      if (w.length > 4 && t.includes(w)) s += 0.6;
    }
    return s;
  }
  const softmax = (xs, temp = 1) => {
    const m = Math.max(...xs), e = xs.map((x) => Math.exp((x - m) / temp)), z = e.reduce((a, b) => a + b, 0);
    return e.map((x) => x / z);
  };
  const sigmoid = (x) => 1 / (1 + Math.exp(-x));
  const sameTriple = (a, b) => a && b && U.leafKey(a) === U.leafKey(b);

  const trip = (c) => ({ query: c.query, sub_query: c.sub_query, query_heading: c.query_heading || "" });

  // The fused request exactly as the backend builds it (app/recat.py jev_plan), so token counts are honest-ish.
  function optionText(l) {
    if (!isCatchAll(l)) return [l.sub_query, l.query_heading].filter(Boolean).join(" > ");
    if (l.sub_query.toLowerCase() === NOT_LISTED) return `Something else about ${l.query}, not covered by any other option`;
    return `${l.sub_query} > something else, not covered by the other ${l.sub_query} options`;
  }
  function jevRecatQuestions() {
    const qs = {};
    const criteria = Object.fromEntries(QUERIES.map((q, i) => [`q${i}`, `${q}: ` + Object.keys(D.taxonomy[q]).filter((s) => s.toLowerCase() !== NOT_LISTED).slice(0, 10).join("; ")]));
    criteria.qn = "None of these: the comment is too vague or unrelated to place in any support area.";
    qs.query = {
      type: "choice",
      instructions: "Which support area does this customer comment belong to. Warranty covers mechanical, electrical and physical faults with the car itself.",
      criteria,
    };
    WARRANTY_LEAVES.forEach((l, i) => {
      const label = [l.sub_query, l.query_heading].filter(Boolean).join(" > ");
      qs[`w${i}`] = { type: "noul", instructions: `Fault: ${label}`, criteria: { true: "The customer reports this fault.", false: "The customer does not report this fault." } };
    });
    QUERIES.forEach((q, i) => {
      if (U.isWarranty(q)) return;
      const ls = LEAVES.filter((l) => l.query === q);
      qs[`leaf${i}`] = { type: "choice", instructions: `Assuming this comment is about '${q}', which sub-query and heading fits best.`, criteria: Object.fromEntries(ls.map((l, j) => [`o${j}`, optionText(l)])) };
    });
    return qs;
  }

  function jevRecat(comment) {
    const questions = jevRecatQuestions();
    const scores = LEAVES.map((l) => leafScore(l, comment) + rand(0, 0.4));
    const qScores = QUERIES.map((q) => Math.max(...LEAVES.map((l, i) => (l.query === q ? scores[i] : -1))));
    const noneScore = Math.max(...qScores) < 1 ? 2 : -1; // nothing recognisable: "None of these"
    const qp = softmax([...qScores, noneScore], 0.7);
    const qi = qp.indexOf(Math.max(...qp));
    const isNone = qi === QUERIES.length;
    const query = isNone ? null : QUERIES[qi];
    const query_probabilities = Object.fromEntries([...QUERIES, "None of these"].map((q, i) => [q, +qp[i].toFixed(4)]));

    let categories, leafConf = 1, low_confidence = false;
    if (isNone) {
      categories = []; // nothing fits
    } else if (U.isWarranty(query)) {
      const ps = WARRANTY_LEAVES.map((l) => ({ ...l, p: +sigmoid(leafScore(l, comment) * 2.2 - 2.4 + rand(-0.3, 0.3)).toFixed(3) }));
      categories = ps.filter((c) => c.p >= 0.55).sort((a, b) => b.p - a.p);
      if (!categories.length) { categories = [ps.sort((a, b) => b.p - a.p)[0]]; low_confidence = true; }
    } else {
      const ls = LEAVES.map((l, i) => ({ l, s: scores[i] })).filter((x) => x.l.query === query);
      const lp = softmax(ls.map((x) => x.s), 0.6);
      const j = lp.indexOf(Math.max(...lp));
      leafConf = lp[j];
      categories = [{ ...ls[j].l, p: +lp[j].toFixed(3) }];
    }
    if (qp[qi] < 0.45 || leafConf < 0.45) low_confidence = true;

    const nQ = Object.keys(questions).length;
    const input_tokens = Object.values(questions).reduce((a, q) => a + tok(JSON.stringify(q)) + tok(comment) + 12, 0);
    const answer = {
      is_warranty: !isNone && U.isWarranty(query),
      placed: categories.length > 0,
      categories, low_confidence, query,
      query_confidence: +qp[qi].toFixed(3), query_probabilities,
      strategy: "fused", questions: nQ,
    };
    const usage = { input_tokens, output_tokens: nQ * 2, cost: input_tokens * PRICE.jev.in / 1e6, cost_reported: true, calls: 1, strategy: "fused", served_model: JEV_SERVED };
    return { answer, usage };
  }

  function openaiRecat(comment) {
    const scored = LEAVES.map((l) => ({ ...l, s: leafScore(l, comment) + rand(0, 0.8) })).sort((a, b) => b.s - a.s);
    const nothing = scored[0].s < 1.2;
    const is_warranty = !nothing && U.isWarranty(scored[0].query);
    let cats = nothing ? [] : is_warranty ? scored.filter((l) => U.isWarranty(l.query) && l.s >= 1.8).slice(0, 3) : [scored[0]];
    if (!nothing && !cats.length) cats = [scored[0]];
    // Now and then invent a heading that isn't in the taxonomy, so the "not a real category" flag gets exercised.
    if (!nothing && Math.random() < 0.12) cats = [{ ...cats[0], query_heading: "General issue" }, ...cats.slice(1)];
    const categories = cats.map((c) => ({ ...trip(c), valid: LEAF_SET.has(U.leafKey(c)) }));
    const answer = { is_warranty, placed: categories.length > 0, categories };
    const taxTokens = tok(JSON.stringify(D.taxonomy));
    const prompt = 1200 + taxTokens + tok(comment) + 40;
    const cached = Math.random() < 0.5 ? 1024 * Math.floor(prompt / 1024 * 0.8) : 0;
    const reasoning = 0; // gpt-4.1 is not a reasoning model
    return { answer, usage: openaiUsage(prompt, cached, Math.round(rand(70, 260)), reasoning) };
  }

  function openaiUsage(prompt, cached, completion, reasoning) {
    const p = OPENAI_MODELS[activeModel];
    const cost = ((prompt - cached) * p.in + cached * p.cached + completion * p.out) / 1e6;
    return { prompt_tokens: prompt, cached_tokens: cached, completion_tokens: completion, reasoning_tokens: reasoning, cost, cost_reported: false, served_model: activeModel, model: activeModel };
  }

  function compareRecat(j, o) {
    const a = new Set(j.categories.map(U.leafKey)), b = new Set(o.categories.map(U.leafKey));
    const inter = [...a].filter((x) => b.has(x)).length;
    const union = new Set([...a, ...b]).size;
    const warranty_agrees = j.is_warranty === o.is_warranty;
    return { warranty_agrees, exact: inter === a.size && inter === b.size, overlap: union ? inter / union : 1, agrees: warranty_agrees && (inter > 0 || union === 0) };
  }

  // ---------- prioritiser: urgent or not, from the message alone ----------
  const SIGNALS = {
    time_sensitive: /\b(today|right now|now|otp|expir\w*|deadline|abhi|aaj)\b/i,
    escalation: /(court|police|legal|fraud|cheat|manager|senior|pathetic|worst|scam)/i,
    unresolved: /(still not|not received|weeks|months|mahine|haven'?t (got|received)|abhi tak)/i,
    safety: /(won'?t start|not starting|broke ?down|accident|wrong car|unsafe|smoke|stuck)/i,
  };
  const THRESH = { time_sensitive: 0.55, escalation: 0.45, unresolved: 0.55, safety: 0.45 };
  const LABEL = { time_sensitive: "time-critical request", escalation: "escalation or accusation", unresolved: "long unresolved issue", safety: "breakdown or safety issue" };

  // Mirrors compose() in app/prioritiser.py: urgent if any signal clears its threshold, first in order reported.
  function compose(p) {
    for (const k of Object.keys(SIGNALS)) if (p[k] >= THRESH[k]) return { urgent: true, signal: k, reason: `Matched ${LABEL[k]} (p=${p[k].toFixed(2)}).` };
    const top = Object.keys(SIGNALS).reduce((a, b) => (p[a] >= p[b] ? a : b));
    return { urgent: false, signal: "none", reason: `No urgency signal; highest was ${LABEL[top]} (p=${p[top].toFixed(2)}).` };
  }

  function jevPrio(b) {
    const probabilities = {};
    for (const [k, re] of Object.entries(SIGNALS)) probabilities[k] = +(re.test(b.message) ? rand(0.78, 0.98) : rand(0.01, 0.14)).toFixed(3);
    const answer = { ...compose(probabilities), probabilities, thresholds: THRESH, strategy: "fused", questions: 4 };
    const input_tokens = 250 + tok(b.message) + 4 * 60; // measured: ~250 per request + ~60 per question
    return { answer, usage: { input_tokens, output_tokens: 70, cost: input_tokens * PRICE.jev.in / 1e6, cost_reported: true, calls: 1, strategy: "fused", served_model: JEV_SERVED } };
  }

  function openaiPrio(b) {
    const p = {};
    for (const [k, re] of Object.entries(SIGNALS)) p[k] = re.test(b.message) ? 0.9 : 0.1;
    // The baseline occasionally misjudges one signal, so disagreement shows up in the mock.
    if (Math.random() < 0.12) { const k = Object.keys(p)[Math.floor(rand(0, 4))]; p[k] = 1 - p[k]; }
    const r = compose(p);
    const reason = r.urgent ? `The message shows a ${LABEL[r.signal]} that needs immediate attention.` : "The message has no urgency signal and can wait for the normal queue.";
    const prompt = 560 + tok(b.message);
    return { answer: { urgent: r.urgent, signal: r.signal, reason }, usage: openaiUsage(prompt, 0, Math.round(rand(30, 55)), 0) };
  }

  const comparePrio = (j, o) => ({ agrees: j.urgent === o.urgent, same_signal: j.signal === o.signal });

  // ---------- storage + stats ----------
  const load = () => { try { return JSON.parse(localStorage.getItem(STORE)) || null; } catch { return null; } };
  const save = (runs) => { try { localStorage.setItem(STORE, JSON.stringify(runs)); } catch {} };

  function toRow(id, ts, feature, preset_id, o, j, compare, model = OPENAI_MODEL) {
    const both = o.ok && j.ok;
    return {
      id, ts, feature, preset_id,
      openai_ok: o.ok, openai_model: model, openai_requested: model, openai_ms: o.ok ? o.ms : null, openai_cost: o.usage?.cost ?? null,
      openai_prompt_tokens: o.usage?.prompt_tokens ?? null, openai_cached_tokens: o.usage?.cached_tokens ?? null,
      openai_completion_tokens: o.usage?.completion_tokens ?? null, openai_reasoning_tokens: o.usage?.reasoning_tokens ?? null,
      jev_ok: j.ok, jev_model: JEV_SERVED, jev_ms: j.ok ? j.ms : null, jev_cost: j.usage?.cost ?? null,
      jev_input_tokens: j.usage?.input_tokens ?? null, jev_output_tokens: j.usage?.output_tokens ?? null,
      jev_calls: j.usage?.calls ?? null, jev_cost_reported: j.usage?.cost_reported ?? null, jev_strategy: j.usage?.strategy ?? null,
      agrees: both ? compare.agrees : null,
      winner: both ? (j.ms <= o.ms ? "jev" : "openai") : null,
    };
  }

  function statsFor(runs) {
    const paired = runs.filter((r) => r.openai_ok && r.jev_ok);
    const col = (k) => paired.map((r) => r[k]);
    const side = (p) => ({
      n: paired.length,
      ms_p50: U.pct(col(`${p}_ms`), 0.5), ms_p90: U.pct(col(`${p}_ms`), 0.9),
      cost_p50: U.pct(col(`${p}_cost`), 0.5), cost_p90: U.pct(col(`${p}_cost`), 0.9), cost_mean: U.mean(col(`${p}_cost`)),
    });
    return {
      runs: runs.length, paired: paired.length,
      openai: { ...side("openai"), prompt_tokens_p50: U.pct(col("openai_prompt_tokens"), 0.5), completion_tokens_p50: U.pct(col("openai_completion_tokens"), 0.5), reasoning_tokens_p50: U.pct(col("openai_reasoning_tokens"), 0.5) },
      jev: { ...side("jev"), input_tokens_p50: U.pct(col("jev_input_tokens"), 0.5), output_tokens_p50: U.pct(col("jev_output_tokens"), 0.5) },
      jev_faster_share: paired.length ? paired.filter((r) => r.winner === "jev").length / paired.length : null,
      agreement: paired.length ? paired.filter((r) => r.agrees).length / paired.length : null,
    };
  }
  const allStats = (runs) => ({ recat: statsFor(runs.filter((r) => r.feature === "recat")), prioritiser: statsFor(runs.filter((r) => r.feature === "prioritiser")) });

  // ---------- one race, shared by the live stream and seeding ----------
  const latency = {
    openai: { recat: () => rand(1500, 3400), prioritiser: () => rand(650, 1700) },
    jev: { recat: () => rand(650, 1500), prioritiser: () => rand(380, 1100) },
  };

  function runProvider(provider, feature, body) {
    activeModel = body.openai_model || OPENAI_MODEL;
    const ms = latency[provider][feature]() * (provider === "openai" ? OPENAI_MODELS[activeModel].speed : 1);
    if (FAIL === provider) return { ms, error: provider === "openai" ? "OpenAI 503: The server is overloaded. Please try again later." : "OpenRouter 502: upstream provider error" };
    const fn = feature === "recat" ? (provider === "jev" ? jevRecat : openaiRecat) : (provider === "jev" ? jevPrio : openaiPrio);
    const r = feature === "recat" ? fn(body.comment) : fn(body);
    return { ms, ...r };
  }

  function seed() {
    const runs = [];
    let id = 0;
    const t0 = Date.now() - 6 * 864e5;
    for (const feature of ["recat", "prioritiser"]) {
      const presets = D.presets[feature];
      const n = feature === "recat" ? 18 : 24;
      for (let i = 0; i < n; i++) {
        const pr = presets[i % presets.length];
        const body = feature === "recat" ? { comment: pr.comment } : pr;
        const o = runProvider("openai", feature, body), j = runProvider("jev", feature, body);
        const oo = { ok: !o.error && i !== 7, ms: Math.round(o.ms), usage: o.usage }, jj = { ok: !j.error, ms: Math.round(j.ms), usage: j.usage };
        const cmp = oo.ok && jj.ok ? (feature === "recat" ? compareRecat(j.answer, o.answer) : comparePrio(j.answer, o.answer)) : null;
        runs.push(toRow(++id, new Date(t0 + id * 3.1e6).toISOString(), feature, pr.id, oo, jj, cmp));
      }
    }
    return runs.sort((a, b) => a.ts.localeCompare(b.ts)).map((r, i) => ({ ...r, id: i + 1 }));
  }

  // Lazy, so nothing is seeded or stored unless mock mode is actually used.
  let runs = null;
  function ensureRuns() {
    if (runs) return;
    runs = params.get("mockreset") === "1" ? null : load();
    if (!runs) { runs = seed(); save(runs); }
  }

  // ---------- routing ----------
  const json = (data, status = 200, headers = {}) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json", ...headers } });

  function validate(b) {
    if (b.feature === "recat") {
      if (!b.comment || !b.comment.trim()) return "comment is required";
      if (b.comment.length > LIMITS.max_input_chars) return `comment is longer than ${LIMITS.max_input_chars} characters`;
    } else if (b.feature === "prioritiser") {
      if (!b.message || !b.message.trim()) return "message is required";
      if (b.message.length > LIMITS.max_input_chars) return `message is longer than ${LIMITS.max_input_chars} characters`;
    } else return "unknown feature";
    if (b.openai_model && !OPENAI_MODELS[b.openai_model]) return `openai_model must be one of: ${Object.keys(OPENAI_MODELS).join(", ")}`;
    return null;
  }

  function race(b) {
    const bad = validate(b);
    if (bad) return json({ detail: bad }, 400);
    if (FAIL === "503") return json({ detail: "Provider keys are not configured on this server." }, 503);
    if (FAIL === "429") return json({ detail: "Rate limit reached: 20 races per hour." }, 429, { "Retry-After": "1260" });
    if (FAIL === "cap") return json({ detail: "Today's demo budget is used up. It resets at 00:00 UTC." }, 429, { "Retry-After": "30000" });

    const feature = b.feature;
    const enc = new TextEncoder();
    const stream = new ReadableStream({
      async start(ctrl) {
        const send = (event, data) => ctrl.enqueue(enc.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`));
        const model = b.openai_model || OPENAI_MODEL;
        send("start", { feature, openai_model: model, jev_model: JEV_MODEL });
        const t0 = performance.now();
        const results = {};
        let pos = 0;
        await Promise.all(["openai", "jev"].map(async (provider) => {
          const r = runProvider(provider, feature, b);
          await sleep(r.ms);
          const ms = Math.round(performance.now() - t0);
          const res = r.error
            ? { provider, ok: false, ms, answer: null, error: r.error, usage: null, finished_position: ++pos }
            : { provider, ok: true, ms, answer: r.answer, error: null, usage: r.usage, finished_position: ++pos };
          results[provider] = res;
          send("result", res);
        }));
        const o = results.openai, j = results.jev;
        const compare = o.ok && j.ok ? (feature === "recat" ? compareRecat(j.answer, o.answer) : comparePrio(j.answer, o.answer)) : null;
        const point = toRow(runs.length + 1, new Date().toISOString(), feature, b.preset_id || null, o, j, compare, model);
        runs.push(point);
        save(runs);
        send("done", { run_id: point.id, compare, winner: point.winner, point, stats: statsFor(runs.filter((r) => r.feature === feature && r.openai_requested === model)) });
        ctrl.close();
      },
    });
    return new Response(stream, { status: 200, headers: { "content-type": "text/event-stream", "cache-control": "no-cache" } });
  }

  async function fetchMock(url, opts = {}) {
    const u = new URL(url, location.href);
    ensureRuns();
    await sleep(rand(20, 60));
    switch (u.pathname) {
      case "/api/config":
        return json({
          presets: D.presets, taxonomy: D.taxonomy,
          openai_model: OPENAI_MODEL, openai_models: Object.keys(OPENAI_MODELS), jev_model: JEV_MODEL,
          prices: { openai: Object.fromEntries(Object.keys(OPENAI_MODELS).map((m) => [m, priceOf(m)])), jev: PRICE.jev },
          limits: LIMITS, keys_configured: { openai: true, jev: true },
        });
      case "/api/history": {
        const f = u.searchParams.get("feature");
        const limit = Math.min(Number(u.searchParams.get("limit")) || 1000, 5000);
        const m = u.searchParams.get("openai_model");
        return json(runs.filter((r) => (!f || r.feature === f) && (!m || r.openai_requested === m)).slice(-limit));
      }
      case "/api/stats": {
        const m = u.searchParams.get("openai_model");
        return json(allStats(runs.filter((r) => !m || r.openai_requested === m)));
      }
      case "/api/race":
        return race(JSON.parse(opts.body || "{}"));
      default:
        return json({ detail: "Not found" }, 404);
    }
  }

  return { fetch: fetchMock, reset: () => { runs = seed(); save(runs); } };
})();
