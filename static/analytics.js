// Page analytics (Umami: cookieless, no personal data). Off unless the server sends an analytics config.
// Events carry only choices from fixed lists (feature, model, preset id, outcome), never text a visitor typed.
window.Analytics = (() => {
  let ready = false;
  const queue = [];

  function init(cfg) {
    if (!cfg || cfg.provider !== "umami" || !cfg.website_id || API.mode === "mock") return;
    const s = document.createElement("script");
    s.defer = true;
    s.src = cfg.script_url;
    s.dataset.websiteId = cfg.website_id;
    s.onload = () => { ready = true; queue.splice(0).forEach(([n, d]) => send(n, d)); };
    document.head.appendChild(s);
  }

  function send(name, data) {
    try { window.umami?.track(name, data); } catch {}
  }

  // track("race_finished", { feature: "recat", ... }): queued until the script loads.
  function track(name, data = {}) {
    if (ready) send(name, data); else queue.push([name, data]);
  }

  return { init, track };
})();
