#!/usr/bin/env python3
"""Turn a strategy run (JSON from stock_adaptive.py --dump) into an interactive HTML report.

Usage:
    python scripts/visualize.py data/viz/discovery.json            # writes reports/discovery.html
    python scripts/visualize.py data/viz/discovery.json -o out.html

The page is self-contained (no internet needed): account value against
buy-and-hold, and for each stock its price, the mode the bot chose each day,
and every trade.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
.viz-root {
  color-scheme: light;
  --surface-1: #fcfcfb; --surface-2: #f0efec; --border: #dcdbd6;
  --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #77766f;
  --grid: #e8e7e3; --price: #52514e;
  --series-1: #2a78d6; --series-2: #eb6834; --series-3: #1baf7a; --series-4: #eda100;
  --filter-band: rgba(82, 81, 78, 0.10);
  --level-0: #86b6ef; --level-1: #3987e5; --level-2: #1c5cab; --level-3: #0d366b;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) .viz-root {
    color-scheme: dark;
    --surface-1: #1a1a19; --surface-2: #262624; --border: #3a3a37;
    --text-primary: #f0efec; --text-secondary: #c3c2b7; --text-muted: #9a998f;
    --grid: #2e2e2c; --price: #c3c2b7;
    --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70; --series-4: #c98500;
    --filter-band: rgba(195, 194, 183, 0.10);
    --level-0: #184f95; --level-1: #2a78d6; --level-2: #5598e7; --level-3: #9ec5f4;
  }
}
:root[data-theme="dark"] .viz-root {
  color-scheme: dark;
  --surface-1: #1a1a19; --surface-2: #262624; --border: #3a3a37;
  --text-primary: #f0efec; --text-secondary: #c3c2b7; --text-muted: #9a998f;
  --grid: #2e2e2c; --price: #c3c2b7;
  --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70; --series-4: #c98500;
  --filter-band: rgba(195, 194, 183, 0.10);
  --level-0: #184f95; --level-1: #2a78d6; --level-2: #5598e7; --level-3: #9ec5f4;
}
html, body { margin: 0; }
.viz-root { background: var(--surface-1); color: var(--text-primary);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; min-height: 100vh; }
.wrap { max-width: 1180px; margin: 0 auto; padding: 24px 20px 48px; }
h1 { font-size: 20px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 32px 0 4px; }
.sub { color: var(--text-secondary); margin: 0 0 16px; }
.note { color: var(--text-muted); font-size: 12px; margin: 6px 0 0; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin: 16px 0 8px; }
.tile { background: var(--surface-2); border-radius: 8px; padding: 12px 14px; }
.tile .label { color: var(--text-secondary); font-size: 12px; }
.tile .value { font-size: 24px; font-weight: 600; font-variant-numeric: tabular-nums; }
.tile .cmp { color: var(--text-muted); font-size: 12px; font-variant-numeric: tabular-nums; }
.legend { display: flex; flex-wrap: wrap; gap: 16px; color: var(--text-secondary); font-size: 12px; margin: 8px 0; align-items: center; }
.key { display: inline-flex; align-items: center; gap: 6px; }
.swatch { width: 14px; height: 3px; border-radius: 2px; display: inline-block; }
.swatch.block { height: 10px; width: 14px; }
.chart { position: relative; }
svg { display: block; width: 100%; height: auto; overflow: visible; }
svg text { fill: var(--text-muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.tooltip { position: absolute; pointer-events: none; background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 6px; padding: 8px 10px; font-size: 12px; box-shadow: 0 2px 8px rgba(0,0,0,.12);
  display: none; white-space: nowrap; z-index: 2; color: var(--text-primary); }
.tooltip .muted { color: var(--text-secondary); }
.controls { display: flex; gap: 12px; align-items: center; margin: 8px 0 4px; flex-wrap: wrap; }
select, button { font: inherit; color: var(--text-primary); background: var(--surface-2);
  border: 1px solid var(--border); border-radius: 6px; padding: 5px 8px; }
table { border-collapse: collapse; width: 100%; font-size: 12px; font-variant-numeric: tabular-nums; margin-top: 8px; }
th, td { text-align: right; padding: 5px 8px; border-bottom: 1px solid var(--grid); }
th:first-child, td:first-child { text-align: left; }
th { color: var(--text-secondary); font-weight: 600; position: sticky; top: 0; background: var(--surface-1); }
tbody tr { cursor: pointer; }
tbody tr:hover, tbody tr.sel { background: var(--surface-2); }
.scroll { max-height: 340px; overflow: auto; border: 1px solid var(--grid); border-radius: 6px; }
.pos { color: var(--text-primary); } .neg { color: var(--text-primary); }
.mark-up::before { content: "▲ "; } .mark-down::before { content: "▼ "; }
.theme { position: absolute; top: 20px; right: 20px; }
</style>
</head>
<body>
<div class="viz-root">
<div class="wrap" style="position:relative">
  <button class="theme" id="theme" type="button">Toggle dark / light</button>
  <h1 id="title"></h1>
  <p class="sub" id="subtitle"></p>
  <div class="tiles" id="tiles"></div>

  <h2>Account value</h2>
  <p class="sub" id="equitysub">The strategy's account against buying the same stocks in equal amounts and holding them.</p>
  <div class="legend">
    <span class="key"><span class="swatch" style="background:var(--series-1)"></span>Strategy account</span>
    <span class="key"><span class="swatch" style="background:var(--series-2)"></span><span id="holdlabel">Hold the same stocks</span></span>
    <span class="key" id="filterkey"><span class="swatch block" style="background:var(--filter-band)"></span><span id="filterlabel">Market filter on (S&amp;P 500 below its 200-day average: no new buys)</span></span>
  </div>
  <div class="chart" id="equity"></div>

  <h2 id="stockhead">One stock at a time</h2>
  <p class="sub" id="stocksub">Price, the mode the bot chose for this stock each day, and every trade. Pick a stock, or click a row in the table below.</p>
  <div class="controls">
    <label for="pick">Stock</label> <select id="pick"></select>
    <span class="note" id="stocknote"></span>
  </div>
  <div class="legend" id="stocklegend"></div>
  <div class="chart" id="stock"></div>

  <h2 id="tradeshead">Trades in this stock</h2>
  <div class="scroll"><table id="trades"></table></div>

  <div id="allstocks">
  <h2>All stocks</h2>
  <p class="sub">How much of the time each stock spent in each mode, and what its trades made compared with holding it.</p>
  <div class="scroll"><table id="summary"></table></div>
  </div>
  <p class="note" id="foot"></p>
</div>
</div>
<script type="application/json" id="data">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById("data").textContent);
const $ = (id) => document.getElementById(id);
const NS = "http://www.w3.org/2000/svg";
const fmtMoney = (v) => (D.kind === "tilt" ? "€" : "$") + Math.round(v).toLocaleString("en-US");
const fmtPct = (v, d = 1) => (v * 100).toFixed(d) + "%";
const fmtSigned = (v) => (v >= 0 ? "+" : "−") + "$" + Math.abs(v).toFixed(2);
const TILT = D.kind === "tilt";
const MODE_NAME = TILT ? D.mode_names : {0: "Standing aside", 1: "Trend", 2: "Dip-buying"};
const MODE_VAR = TILT ? {0: "var(--level-0)", 1: "var(--level-1)", 2: "var(--level-2)", 3: "var(--level-3)"}
                      : {1: "var(--series-3)", 2: "var(--series-4)"};
$("stocklegend").innerHTML = `<span class="key"><span class="swatch" style="background:var(--price)"></span>Price</span>` +
  Object.entries(MODE_NAME).map(([k, n]) => MODE_VAR[k]
    ? `<span class="key"><span class="swatch block" style="background:${MODE_VAR[k]}"></span>${n}</span>`
    : `<span class="key"><span class="swatch block" style="background:var(--surface-2);outline:1px solid var(--border)"></span>${n}</span>`).join("") +
  (TILT ? `<span class="key">▲ invest more &nbsp; ▼ invest less</span>` : `<span class="key">▲ buy &nbsp; ▼ sell (colored by the mode that bought)</span>`);
if (TILT) {
  $("allstocks").style.display = "none"; $("tradeshead").textContent = "Orders";
  $("equitysub").textContent = "The strategy's account against holding the index at 100% the whole time.";
  $("filterlabel").textContent = "Index below its 200-day average (exposure halved)";
  $("holdlabel").textContent = "Hold the index";
  $("stockhead").textContent = "How much was invested, day by day";
  $("stocksub").textContent = "Price, the share of the account invested each day (darker = more), and every order that changed it.";
}
const COMP_VAR = {trend: "var(--series-3)", revert: "var(--series-4)"};
const COMP_NAME = {trend: "Trend", revert: "Dip-buying"};
const dates = D.dates.map((d) => new Date(d + "T00:00:00Z"));
const N = dates.length;
const idxOf = {}; D.dates.forEach((d, i) => idxOf[d] = i);

function el(tag, attrs = {}, parent) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (parent) parent.appendChild(e);
  return e;
}
function niceTicks(lo, hi, n = 5) {
  const span = hi - lo || 1, step0 = span / n, mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= n) || 10 * mag;
  const out = []; for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(v);
  return out;
}

// ---------------------------------------------------------------- header
$("title").textContent = D.title;
const a = D.account;
$("subtitle").textContent = TILT
  ? `${D.dates[0]} to ${D.dates[N - 1]} · account €${a.equity.toLocaleString("en-US")}, €${a.fee} per order · ${D.note || ""} · generated ${D.generated}`
  : `${D.dates[0]} to ${D.dates[N - 1]} · ${Object.keys(D.stocks).length} stocks · account ${fmtMoney(a.equity)}, up to ${a.slots} positions of ${fmtMoney(a.slot_value)} · generated ${D.generated}`;
const S = D.stats.strategy, B = D.stats.benchmark;
const tiles = [
  ["Yearly return (CAGR)", fmtPct(S.cagr), `hold: ${fmtPct(B.cagr)}`],
  ["Return per unit of risk (Sharpe)", S.sharpe.toFixed(2), `hold: ${B.sharpe.toFixed(2)}`],
  ["Worst drop from a peak", fmtPct(S.max_dd), `hold: ${fmtPct(B.max_dd)}`],
  ["End value", fmtMoney(D.equity[N - 1]), `hold: ${fmtMoney(D.benchmark[N - 1])}`],
  TILT ? ["Orders", D.orders.length.toLocaleString("en-US"), `fees: ${fmtMoney(D.orders.reduce((x, o) => x + o.cost, 0))}`]
       : ["Trades", D.trades.length.toLocaleString("en-US"), `wins: ${D.trades.filter(t => t.pnl > 0).length}`],
];
$("tiles").innerHTML = tiles.map(([l, v, c]) => `<div class="tile"><div class="label">${l}</div><div class="value">${v}</div><div class="cmp">${c}</div></div>`).join("");

// ---------------------------------------------------------------- generic time chart
function timeChart(container, opts) {
  container.innerHTML = "";
  const W = 1140, H = opts.height, m = {l: 64, r: 120, t: 10, b: opts.strip ? 52 : 28};
  const svg = el("svg", {viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": opts.label}, container);
  const tip = document.createElement("div"); tip.className = "tooltip"; container.appendChild(tip);
  const x = (i) => m.l + (i / (N - 1)) * (W - m.l - m.r);
  const vals = opts.series.flatMap((s) => s.values.filter((v) => v != null));
  let lo = Math.min(...vals), hi = Math.max(...vals); const pad = (hi - lo) * 0.06 || 1; lo -= pad; hi += pad;
  const plotBottom = H - m.b;
  const y = (v) => m.t + (1 - (v - lo) / (hi - lo)) * (plotBottom - m.t);

  // filter bands
  if (opts.bands) {
    let start = null;
    for (let i = 0; i <= N; i++) {
      const on = i < N && opts.bands[i];
      if (on && start === null) start = i;
      if (!on && start !== null) {
        el("rect", {x: x(start), y: m.t, width: Math.max(1, x(i - 1) - x(start)), height: plotBottom - m.t, fill: "var(--filter-band)"}, svg);
        start = null;
      }
    }
  }
  // grid + y ticks
  for (const t of niceTicks(lo, hi)) {
    el("line", {x1: m.l, x2: W - m.r, y1: y(t), y2: y(t), stroke: "var(--grid)", "stroke-width": 1}, svg);
    el("text", {x: m.l - 8, y: y(t) + 4, "text-anchor": "end"}, svg).textContent = opts.yfmt(t);
  }
  // x ticks: Januaries
  let lastYear = null;
  dates.forEach((d, i) => {
    const yr = d.getUTCFullYear();
    if (yr !== lastYear) { lastYear = yr;
      if (i > 0) el("text", {x: x(i), y: plotBottom + 16, "text-anchor": "middle"}, svg).textContent = yr; }
  });
  // mode strip
  if (opts.strip) {
    const sy = plotBottom + 24, sh = 12;
    el("rect", {x: m.l, y: sy, width: W - m.l - m.r, height: sh, fill: "var(--surface-2)", rx: 2}, svg);
    let start = null, cur = null;
    for (let i = 0; i <= N; i++) {
      const v = i < N ? opts.strip[i] : null;
      if (v !== cur) {
        if (cur != null && MODE_VAR[cur] && start !== null) el("rect", {x: x(start), y: sy, width: Math.max(1, x(i - 1) - x(start)), height: sh, fill: MODE_VAR[cur]}, svg);
        cur = v; start = i;
      }
    }
    el("text", {x: m.l - 8, y: sy + 10, "text-anchor": "end"}, svg).textContent = "mode";
  }
  // lines
  opts.series.forEach((s) => {
    let d = "", pen = false;
    s.values.forEach((v, i) => {
      if (v == null) { pen = false; return; }
      d += (pen ? "L" : "M") + x(i).toFixed(1) + "," + y(v).toFixed(1); pen = true;
    });
    el("path", {d, fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round"}, svg);
    if (s.label) {
      let last = s.values.length - 1; while (last > 0 && s.values[last] == null) last--;
      el("text", {x: x(last) + 8, y: y(s.values[last]) + 4, style: "fill:var(--text-secondary)"}, svg).textContent = s.label;
    }
  });
  // markers
  const markerLayer = el("g", {}, svg);
  (opts.markers || []).forEach((mk) => {
    const cx = x(mk.i), cy = y(mk.v), s = 6;
    const pts = mk.up ? `${cx},${cy - s} ${cx - s},${cy + s * 0.8} ${cx + s},${cy + s * 0.8}`
                      : `${cx},${cy + s} ${cx - s},${cy - s * 0.8} ${cx + s},${cy - s * 0.8}`;
    el("polygon", {points: pts, fill: mk.color, stroke: "var(--surface-1)", "stroke-width": 2}, markerLayer);
    const hit = el("circle", {cx, cy, r: 10, fill: "transparent", style: "cursor:pointer"}, markerLayer);
    hit.addEventListener("mouseenter", (e) => { showTip(mk.html, cx, cy); e.stopPropagation(); mk.hover = true; });
    hit.addEventListener("mouseleave", () => { mk.hover = false; tip.style.display = "none"; });
  });
  // crosshair
  const cross = el("line", {y1: m.t, y2: plotBottom, stroke: "var(--text-muted)", "stroke-width": 1, "stroke-dasharray": "3 3", visibility: "hidden"}, svg);
  const dots = opts.series.map((s) => el("circle", {r: 4, fill: s.color, stroke: "var(--surface-1)", "stroke-width": 2, visibility: "hidden"}, svg));
  svg.appendChild(markerLayer);
  function showTip(html, px, py) {
    tip.innerHTML = html; tip.style.display = "block";
    const rect = svg.getBoundingClientRect(), k = rect.width / W;
    let left = px * k + 14, top = py * k - 10;
    if (left + tip.offsetWidth > rect.width) left = px * k - tip.offsetWidth - 14;
    tip.style.left = left + "px"; tip.style.top = Math.max(0, top) + "px";
  }
  const overlay = el("rect", {x: m.l, y: m.t, width: W - m.l - m.r, height: plotBottom - m.t + (opts.strip ? 40 : 0), fill: "transparent"}, svg);
  svg.insertBefore(overlay, markerLayer);
  overlay.addEventListener("mousemove", (e) => {
    const rect = svg.getBoundingClientRect(), k = W / rect.width;
    const px = (e.clientX - rect.left) * k;
    const i = Math.max(0, Math.min(N - 1, Math.round((px - m.l) / (W - m.l - m.r) * (N - 1))));
    cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
    opts.series.forEach((s, k2) => {
      const v = s.values[i];
      if (v == null) { dots[k2].setAttribute("visibility", "hidden"); return; }
      dots[k2].setAttribute("cx", x(i)); dots[k2].setAttribute("cy", y(v)); dots[k2].setAttribute("visibility", "visible");
    });
    showTip(opts.tip(i), x(i), Math.min(...opts.series.map((s) => s.values[i] == null ? plotBottom : y(s.values[i]))));
  });
  overlay.addEventListener("mouseleave", () => {
    cross.setAttribute("visibility", "hidden"); dots.forEach((d) => d.setAttribute("visibility", "hidden")); tip.style.display = "none";
  });
}

// ---------------------------------------------------------------- equity chart
timeChart($("equity"), {
  height: 340, label: "Account value over time",
  series: [
    {values: D.equity, color: "var(--series-1)", label: "Strategy"},
    {values: D.benchmark, color: "var(--series-2)", label: "Hold"},
  ],
  bands: D.market_ok.map((ok) => !ok),
  yfmt: (v) => fmtMoney(v),
  tip: (i) => `<b>${D.dates[i]}</b><br><span class="muted">Strategy</span> ${fmtMoney(D.equity[i])}<br>` +
              `<span class="muted">Hold</span> ${fmtMoney(D.benchmark[i])}` +
              (D.market_ok[i] ? "" : `<br><span class="muted">Market filter on: no new buys</span>`),
});

// ---------------------------------------------------------------- per-stock data
const bySym = {};
for (const t of D.trades) (bySym[t.symbol] ||= []).push(t);
const symbols = Object.keys(D.stocks);
const summary = symbols.map((s) => {
  const st = D.stocks[s], tr = bySym[s] || [];
  const modes = st.mode.filter((m) => m != null);
  const share = (k) => modes.length ? modes.filter((m) => m === k).length / modes.length : 0;
  const first = st.close.findIndex((v) => v != null);
  const hold = first >= 0 ? st.close[N - 1] / st.close[first] - 1 : 0;
  return {s, trades: tr.length, pnl: tr.reduce((x, t) => x + t.pnl, 0), wins: tr.filter((t) => t.pnl > 0).length,
          trend: share(1), revert: share(2), cash: share(0), hold};
}).sort((p, q) => q.trades - p.trades || p.s.localeCompare(q.s));

$("pick").innerHTML = summary.map((r) => `<option value="${r.s}">${r.s}${TILT ? "" : ` (${r.trades} trades)`}</option>`).join("");
$("summary").innerHTML = `<thead><tr><th>Stock</th><th>Trades</th><th>Won</th><th>Trade P&amp;L</th><th>Trend</th><th>Dip-buying</th><th>Aside</th><th>Stock's own move</th></tr></thead><tbody>` +
  summary.map((r) => `<tr data-s="${r.s}"><td>${r.s}</td><td>${r.trades}</td><td>${r.wins}</td><td>${fmtSigned(r.pnl)}</td>` +
    `<td>${fmtPct(r.trend, 0)}</td><td>${fmtPct(r.revert, 0)}</td><td>${fmtPct(r.cash, 0)}</td><td>${fmtPct(r.hold, 0)}</td></tr>`).join("") + "</tbody>";
$("summary").querySelectorAll("tbody tr").forEach((tr) => tr.addEventListener("click", () => { $("pick").value = tr.dataset.s; drawStock(); window.scrollTo({top: $("stock").offsetTop - 120, behavior: "smooth"}); }));

function drawStock() {
  const sym = $("pick").value, st = D.stocks[sym], tr = bySym[sym] || [];
  const row = summary.find((r) => r.s === sym);
  $("stocknote").textContent = `${row.trades} trades, ${row.wins} won, trade P&L ${fmtSigned(row.pnl)}; the stock itself moved ${fmtPct(row.hold, 0)}`;
  const markers = [];
  for (const t of tr) {
    const pnlTxt = `${fmtSigned(t.pnl)} after costs`;
    const body = `${COMP_NAME[t.component]} trade · ${t.shares} shares<br><span class="muted">Bought</span> ${t.entry_date} at $${t.entry.toFixed(2)}<br>` +
                 `<span class="muted">Sold</span> ${t.exit_date} at $${t.exit.toFixed(2)} (${t.reason.replace("_", " ")})<br><b>${pnlTxt}</b>`;
    if (idxOf[t.entry_date] != null) markers.push({i: idxOf[t.entry_date], v: t.entry, up: true, color: COMP_VAR[t.component], html: `<b>▲ Buy ${sym}</b><br>` + body});
    if (idxOf[t.exit_date] != null) markers.push({i: idxOf[t.exit_date], v: t.exit, up: false, color: COMP_VAR[t.component], html: `<b>▼ Sell ${sym}</b><br>` + body});
  }
  timeChart($("stock"), {
    height: 360, label: `${sym} price, mode and trades`,
    series: [{values: st.close, color: "var(--price)", label: sym}],
    strip: st.mode, markers, yfmt: (v) => "$" + (Number.isInteger(v) ? v : v.toFixed(2)),
    tip: (i) => `<b>${D.dates[i]}</b><br><span class="muted">Price</span> ${st.close[i] == null ? "–" : "$" + st.close[i].toFixed(2)}` +
                `<br><span class="muted">Mode</span> ${st.mode[i] == null ? "not enough history" : MODE_NAME[st.mode[i]]}`,
  });
  if (TILT) { drawOrders(sym, st); return; }
  $("trades").innerHTML = `<thead><tr><th>Mode</th><th>Bought</th><th>Price</th><th>Sold</th><th>Price</th><th>Shares</th><th>Why sold</th><th>P&amp;L</th></tr></thead><tbody>` +
    (tr.length ? tr.map((t) => `<tr><td>${COMP_NAME[t.component]}</td><td>${t.entry_date}</td><td>$${t.entry.toFixed(2)}</td><td>${t.exit_date}</td><td>$${t.exit.toFixed(2)}</td>` +
      `<td>${t.shares}</td><td>${t.reason.replace("_", " ")}</td><td class="${t.pnl > 0 ? "mark-up" : "mark-down"}">${fmtSigned(t.pnl)}</td></tr>`).join("")
      : `<tr><td colspan="8">No trades in this stock.</td></tr>`) + "</tbody>";
  document.querySelectorAll("#summary tbody tr").forEach((r) => r.classList.toggle("sel", r.dataset.s === sym));
}
function drawOrders(sym, st) {
  const pct = (v) => Math.round(v * 100) + "%";
  const markers = D.orders.filter((o) => idxOf[o.date] != null).map((o) => ({
    i: idxOf[o.date], v: st.close[idxOf[o.date]], up: o.to > o.from, color: MODE_VAR[[0.25, 0.5, 0.75, 1].indexOf(o.to)] || "var(--text-muted)",
    html: `<b>${o.to > o.from ? "▲ Invest more" : "▼ Invest less"}</b><br><span class="muted">${o.date}</span> ${pct(o.from)} → ${pct(o.to)}<br>` +
          `<span class="muted">Why</span> ${o.reason}<br><span class="muted">Cost</span> €${o.cost.toFixed(2)}`}));
  $("stocknote").textContent = `${D.orders.length} orders; average invested ${pct(st.exposure.reduce((x, v) => x + v, 0) / st.exposure.length)}`;
  timeChart($("stock"), {
    height: 360, label: `${sym} price, exposure and orders`,
    series: [{values: st.close, color: "var(--price)", label: sym}], strip: st.mode, markers,
    yfmt: (v) => "$" + (Number.isInteger(v) ? v : v.toFixed(2)),
    tip: (i) => `<b>${D.dates[i]}</b><br><span class="muted">Price</span> $${st.close[i].toFixed(2)}<br><span class="muted">Invested</span> ${pct(st.exposure[i])}`,
  });
  $("trades").innerHTML = `<thead><tr><th>Date</th><th>From</th><th>To</th><th>Why</th><th>Price</th><th>Cost</th></tr></thead><tbody>` +
    D.orders.map((o) => `<tr><td>${o.date}</td><td>${pct(o.from)}</td><td class="${o.to > o.from ? "mark-up" : "mark-down"}">${pct(o.to)}</td>` +
      `<td>${o.reason}</td><td>$${o.price.toFixed(2)}</td><td>€${o.cost.toFixed(2)}</td></tr>`).join("") + "</tbody>";
}
if (D.market_ok.every(Boolean)) $("filterkey").style.display = "none";
$("pick").addEventListener("change", drawStock);
// Link parameters: #stock=INTC&theme=dark
const params = new URLSearchParams(location.hash.slice(1));
if (params.get("theme")) document.documentElement.dataset.theme = params.get("theme");
if (params.get("stock") && D.stocks[params.get("stock")]) $("pick").value = params.get("stock");
drawStock();

$("foot").textContent = "Simulation on daily bars: signals on the close, orders at the next open, IBKR US stock commissions plus 5 bps slippage. Past results; not a forecast.";
$("theme").addEventListener("click", () => {
  const root = document.documentElement;
  const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  root.dataset.theme = dark ? "light" : "dark";
});
</script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("json", type=Path)
    parser.add_argument("-o", "--out", type=Path)
    args = parser.parse_args()

    data = json.loads(args.json.read_text())
    out = args.out or ROOT / "reports" / (args.json.stem + ".html")
    out.parent.mkdir(parents=True, exist_ok=True)
    html = TEMPLATE.replace("__TITLE__", data["title"]).replace(
        "__DATA__", json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    )
    out.write_text(html, encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
