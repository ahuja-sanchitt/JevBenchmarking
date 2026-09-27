// Every network call the pages make. Talks to the real backend, or to MockBackend when
// ?mock=1 is set or no backend answers /api/config. The mock returns real Response objects
// (including an SSE stream), so the same parsing path runs in both modes.
window.API = (() => {
  const params = new URLSearchParams(location.search);
  let mode = null; // "live" | "mock"

  async function detect() {
    if (mode) return mode;
    if (params.get("mock") === "1") return (mode = "mock");
    if (params.get("mock") === "0") return (mode = "live");
    try {
      const r = await fetch("/api/config", { headers: { accept: "application/json" } });
      if (r.ok && (r.headers.get("content-type") || "").includes("json")) return (mode = "live");
    } catch {}
    return (mode = "mock");
  }

  const call = (url, opts) => (mode === "mock" ? MockBackend.fetch(url, opts) : fetch(url, opts));

  async function toError(r) {
    let body = {};
    try { body = await r.json(); } catch {}
    const detail = body.detail ?? body.error ?? `HTTP ${r.status}`;
    const e = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    e.status = r.status;
    const ra = Number(r.headers.get("Retry-After"));
    e.retryAfter = Number.isFinite(ra) && ra > 0 ? ra : null;
    return e;
  }

  async function getJSON(url) {
    const r = await call(url, { headers: { accept: "application/json" } });
    if (!r.ok) throw await toError(r);
    return r.json();
  }

  function parseEvent(chunk) {
    let event = "message", data = "";
    for (const line of chunk.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data += (data ? "\n" : "") + line.slice(5).trimStart();
    }
    return data ? { event, data: JSON.parse(data) } : null;
  }

  // EventSource cannot POST, so read the SSE body by hand.
  async function race(body, onEvent) {
    const r = await call("/api/race", {
      method: "POST",
      headers: { "content-type": "application/json", accept: "text/event-stream" },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw await toError(r);
    const reader = r.body.pipeThrough(new TextDecoderStream()).getReader();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += value.replace(/\r\n/g, "\n");
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const ev = parseEvent(buf.slice(0, i));
        buf = buf.slice(i + 2);
        if (ev) onEvent(ev.event, ev.data);
      }
    }
  }

  return {
    detect,
    get mode() { return mode; },
    config: () => getJSON("/api/config"),
    history: (feature, model, limit = 1000) =>
      getJSON(`/api/history?feature=${encodeURIComponent(feature)}&limit=${limit}` + (model ? `&openai_model=${encodeURIComponent(model)}` : "")),
    stats: (model) => getJSON("/api/stats" + (model ? `?openai_model=${encodeURIComponent(model)}` : "")),
    projection: () => getJSON("/api/projection"),
    race,
  };
})();
