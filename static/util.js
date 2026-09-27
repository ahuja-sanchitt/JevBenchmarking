// Small helpers shared by the pages and the mock backend.
window.U = (() => {
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // Taxonomy { Query: { Sub: [headings] } } -> flat leaves
  function leaves(tax) {
    const out = [];
    for (const [query, subs] of Object.entries(tax)) {
      for (const [sub_query, heads] of Object.entries(subs)) {
        if (!heads.length) out.push({ query, sub_query, query_heading: "" });
        else for (const query_heading of heads) out.push({ query, sub_query, query_heading });
      }
    }
    return out;
  }
  const leafKey = (c) => `${c.query}|${c.sub_query}|${c.query_heading || ""}`;
  const leafLabel = (c) => [c.query, c.sub_query, c.query_heading].filter(Boolean).join(" › ");
  const isWarranty = (q) => /^warranty$/i.test(q || "");

  // Linear-interpolated percentile, matching the backend's stats().
  function pct(values, p) {
    const v = values.filter((x) => x != null && isFinite(x)).sort((a, b) => a - b);
    if (!v.length) return null;
    const i = (v.length - 1) * p, lo = Math.floor(i), hi = Math.ceil(i);
    return v[lo] + (v[hi] - v[lo]) * (i - lo);
  }
  const mean = (values) => {
    const v = values.filter((x) => x != null && isFinite(x));
    return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null;
  };

  const nf = new Intl.NumberFormat("en-US");
  const int = (n) => (n == null ? "—" : nf.format(Math.round(n)));
  const ms = (n) => (n == null ? "—" : nf.format(Math.round(n)));
  function usd(n) {
    if (n == null) return "—";
    if (n === 0) return "$0";
    if (n >= 1) return "$" + n.toFixed(2);
    // keep 3 significant figures for tiny per-call costs
    const digits = Math.max(2, 2 - Math.floor(Math.log10(Math.abs(n))));
    return "$" + n.toFixed(digits);
  }
  const ratio = (n) => (n == null || !isFinite(n) ? "—" : (n >= 10 ? n.toFixed(0) : n.toFixed(1)) + "×");
  const pctStr = (n) => (n == null ? "—" : Math.round(n * 100) + "%");
  const prob = (p) => (p == null ? "" : p.toFixed(2));

  return { esc, leaves, leafKey, leafLabel, isWarranty, pct, mean, int, ms, usd, ratio, pctStr, prob };
})();
