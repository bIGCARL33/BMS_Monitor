"""Live browser dashboard, served from the machine holding the link.

A terminal line per sample is fine for logging and bad for watching. This
serves a small page on localhost that polls a JSON endpoint, so the pack can be
watched at a glance while a charge or discharge runs.

It has to be local: the browser cannot reach BLE or a serial port, so the
reading happens here and the page just renders what this process already has.
Standard library only -- ``http.server`` and a thread -- so it adds no
dependency beyond the link itself.

Design notes on the page, because they are decisions and not defaults:

* Cell voltages are drawn as **deviation from the pack mean**, not as bars from
  zero. Four cells within a millivolt of each other produce four identical
  full-height bars, which is worse than no chart. Deviation is what carries the
  information -- it is the balance you actually watch.
* Pack voltage and current get **separate** charts. They are different scales,
  and a dual-axis chart invites reading a crossing point that means nothing.
* Colors come from a validated palette and were checked with the data-viz
  validator for colorblind separation and surface contrast in both themes.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from .decode import Reading
from .verify import VerificationResult

__all__ = ["ReadingBuffer", "build_server", "PAGE"]

#: Samples retained for the history charts. At the default one-second poll this
#: is about 25 minutes, which covers a bench charge segment without growing
#: without bound.
HISTORY = 1500


class ReadingBuffer:
    """Thread-safe latest-reading plus bounded history.

    The reader thread writes; the HTTP handler reads. Everything is copied out
    under the lock so a request can never observe a half-updated sample.
    """

    def __init__(self, history: int = HISTORY) -> None:
        self._lock = threading.Lock()
        self._latest: Reading | None = None
        self._history: deque[dict[str, Any]] = deque(maxlen=history)
        self._verification: VerificationResult | None = None
        self._count = 0
        self._started = datetime.now(timezone.utc)
        self._error: str | None = None

    def add(self, reading: Reading) -> None:
        with self._lock:
            self._latest = reading
            self._count += 1
            self._history.append({
                "t": round(reading.timestamp.timestamp() - self._started.timestamp(), 2),
                "pack_v": reading.pack_v,
                "current_a": reading.current_a,
                "delta_mv": None if reading.cell_delta_v is None
                            else round(reading.cell_delta_v * 1000, 1),
            })

    def set_verification(self, result: VerificationResult) -> None:
        with self._lock:
            self._verification = result

    def set_error(self, message: str | None) -> None:
        with self._lock:
            self._error = message

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            reading = self._latest
            history = list(self._history)
            verification = self._verification
            count = self._count
            error = self._error

        if reading is None:
            return {"ready": False, "samples": 0, "error": error,
                    "elapsed_s": round(
                        (datetime.now(timezone.utc) - self._started).total_seconds(), 1)}

        cells = reading.cells_v
        mean = reading.cell_avg_v
        return {
            "ready": True,
            "error": error,
            "samples": count,
            "timestamp": reading.timestamp.isoformat(timespec="seconds"),
            "elapsed_s": round(
                (datetime.now(timezone.utc) - self._started).total_seconds(), 1),
            "profile": reading.profile,
            "confirmed": bool(verification and verification.trustworthy),
            "failures": [] if verification is None
                        else [f"{c.name}: {c.detail}" for c in verification.failures],
            "pack_v": reading.pack_v,
            "current_a": reading.current_a,
            "power_w": reading.power_w,
            "soc_pct": reading.soc_pct,
            "remaining_ah": reading.remaining_ah,
            "nominal_ah": reading.nominal_ah,
            "cycles": reading.cycles,
            "temp1_c": reading.temp1_c,
            "temp2_c": reading.temp2_c,
            "temp_mos_c": reading.temp_mos_c,
            "cells_v": cells,
            "cell_mean_v": mean,
            "cell_min_v": reading.cell_min_v,
            "cell_max_v": reading.cell_max_v,
            "cell_delta_mv": None if reading.cell_delta_v is None
                             else round(reading.cell_delta_v * 1000, 1),
            "cell_sum_v": reading.cell_sum_v,
            # Deviation from mean in mV -- the quantity the balance chart plots.
            "cell_dev_mv": [] if mean is None
                           else [round((v - mean) * 1000, 1) for v in cells],
            "resistances_ohm": reading.resistances_ohm,
            "history": history,
        }


def build_server(buffer: ReadingBuffer, host: str = "127.0.0.1",
                 port: int = 8765) -> HTTPServer:
    """Create the HTTP server that renders ``buffer``."""

    class Handler(BaseHTTPRequestHandler):
        def _send(self, body: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/favicon.ico":
                # Silence the browser's automatic request; an unexplained 404
                # in the console makes real errors harder to notice.
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif path == "/api/state":
                payload = json.dumps(buffer.snapshot()).encode("utf-8")
                self._send(payload, "application/json")
            else:
                self.send_error(404)

        def log_message(self, *args) -> None:
            """Silence per-request logging; the poll would flood the console."""

    return HTTPServer((host, port), Handler)


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JK BMS Live</title>
<style>
/* Palette from the validated data-viz reference instance. The two series hues
   (blue, orange) were run through the validator for both themes: adjacent CVD
   dE 24.7 light / 26.8 dark, contrast >= 3:1 on both surfaces. */
:root{
  color-scheme: light;
  --surface:#fcfcfb; --panel:#ffffff; --sunken:#f4f4f2;
  --ink:#0b0b0b; --ink-2:#52514e; --ink-3:#7a7975; --rule:#e3e2de;
  --series-1:#2a78d6;          /* pack voltage */
  --series-2:#eb6834;          /* current */
  --diverge-hi:#2a78d6; --diverge-lo:#d03b3b; --diverge-mid:#f0efec;
  --good:#0ca30c; --warning:#fab219; --critical:#d03b3b;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    color-scheme: dark;
    --surface:#1a1a19; --panel:#222221; --sunken:#161615;
    --ink:#ffffff; --ink-2:#c3c2b7; --ink-3:#8f8e86; --rule:#33332f;
    --series-1:#3987e5; --series-2:#d95926;
    --diverge-hi:#3987e5; --diverge-lo:#d03b3b; --diverge-mid:#383835;
  }
}
:root[data-theme="dark"]{
  color-scheme: dark;
  --surface:#1a1a19; --panel:#222221; --sunken:#161615;
  --ink:#ffffff; --ink-2:#c3c2b7; --ink-3:#8f8e86; --rule:#33332f;
  --series-1:#3987e5; --series-2:#d95926;
  --diverge-hi:#3987e5; --diverge-lo:#d03b3b; --diverge-mid:#383835;
}
*{box-sizing:border-box}
body{
  margin:0; background:var(--surface); color:var(--ink);
  font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
  font-size:15px; line-height:1.5;
}
.wrap{max-width:70rem; margin:0 auto; padding:1.5rem 1.25rem 4rem;
      display:flex; flex-direction:column; gap:1.25rem}
header{display:flex; flex-wrap:wrap; gap:.75rem; align-items:baseline;
       justify-content:space-between}
h1{font-size:1.15rem; margin:0; font-weight:600; letter-spacing:-.01em}
.sub{font-size:.8rem; color:var(--ink-3); font-variant-numeric:tabular-nums}
.panel{background:var(--panel); border:1px solid var(--rule); border-radius:10px; padding:1rem 1.1rem}
h2{font-size:.72rem; font-weight:600; letter-spacing:.09em; text-transform:uppercase;
   color:var(--ink-3); margin:0 0 .85rem}

/* stat tiles: a headline number is not a chart */
.tiles{display:grid; grid-template-columns:repeat(auto-fit,minmax(9rem,1fr)); gap:.75rem}
.tile{background:var(--panel); border:1px solid var(--rule); border-radius:10px; padding:.85rem 1rem}
.tile .label{font-size:.7rem; letter-spacing:.08em; text-transform:uppercase; color:var(--ink-3)}
.tile .value{font-size:1.75rem; font-weight:600; line-height:1.15; margin-top:.15rem;
             font-variant-numeric:tabular-nums; letter-spacing:-.02em}
.tile .unit{font-size:.95rem; font-weight:500; color:var(--ink-2); margin-left:.15rem}
.tile .note{font-size:.72rem; color:var(--ink-3); margin-top:.2rem; font-variant-numeric:tabular-nums}

/* cell balance: deviation from mean, diverging around a neutral midpoint */
table.cells{width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums}
table.cells th{font-size:.7rem; text-transform:uppercase; letter-spacing:.07em;
  color:var(--ink-3); font-weight:600; text-align:left; padding:0 .5rem .4rem 0}
table.cells th.num, table.cells td.num{text-align:right}
table.cells td{padding:.3rem .5rem .3rem 0; font-size:.9rem; border-top:1px solid var(--rule)}
.devcell{width:60%; padding-right:0 !important}
.devbar{position:relative; height:16px; background:var(--sunken); border-radius:3px; overflow:hidden}
.devbar .mid{position:absolute; left:50%; top:0; bottom:0; width:1px;
             background:var(--ink-3); opacity:.7; z-index:3}
.devbar .fill{position:absolute; top:3px; bottom:3px; border-radius:2px;
              box-shadow:0 0 0 2px var(--panel); z-index:2}
.legend{display:flex; gap:1rem; font-size:.72rem; color:var(--ink-3); margin-top:.6rem;
        flex-wrap:wrap; align-items:center}
.swatch{display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:.35rem;
        vertical-align:-1px}

.charts{display:grid; grid-template-columns:repeat(auto-fit,minmax(20rem,1fr)); gap:1.25rem}
.plot{position:relative}
svg{display:block; width:100%; height:130px; overflow:visible}
.axis{font-size:10px; fill:var(--ink-3); font-variant-numeric:tabular-nums}
.gridline{stroke:var(--rule); stroke-width:1}
.tip{position:absolute; pointer-events:none; background:var(--panel); color:var(--ink);
     border:1px solid var(--rule); border-radius:6px; padding:.35rem .55rem; font-size:.75rem;
     font-variant-numeric:tabular-nums; opacity:0; transition:opacity .1s; white-space:nowrap;
     box-shadow:0 2px 10px rgba(0,0,0,.12)}

.status{display:flex; align-items:center; gap:.5rem; font-size:.8rem}
.dot{width:8px; height:8px; border-radius:50%; background:var(--ink-3); flex:none}
.dot.live{background:var(--good)}
.dot.stale{background:var(--warning)}
.dot.bad{background:var(--critical)}
.banner{border-left:3px solid var(--warning); background:var(--panel);
        border-radius:0 8px 8px 0; padding:.8rem 1rem; font-size:.85rem}
.banner.bad{border-left-color:var(--critical)}
.banner h3{margin:0 0 .3rem; font-size:.85rem}
.banner ul{margin:.3rem 0 0; padding-left:1.1rem; color:var(--ink-2)}
.muted{color:var(--ink-3)}
table.raw{width:100%; border-collapse:collapse; font-size:.8rem;
          font-variant-numeric:tabular-nums; margin-top:.5rem}
table.raw td{padding:.2rem .6rem .2rem 0; border-top:1px solid var(--rule); color:var(--ink-2)}
table.raw td:first-child{color:var(--ink-3)}
details summary{cursor:pointer; font-size:.75rem; color:var(--ink-3);
                letter-spacing:.07em; text-transform:uppercase; font-weight:600}
</style>
</head>
<body>
<div class="wrap">

  <header>
    <div>
      <h1>JK BMS &mdash; live</h1>
      <div class="sub" id="meta">connecting&hellip;</div>
    </div>
    <div class="status"><span class="dot" id="dot"></span><span id="statustext">waiting</span></div>
  </header>

  <div id="warnings"></div>

  <div class="tiles" id="tiles"></div>

  <section class="panel">
    <h2>Cell balance &mdash; deviation from pack mean</h2>
    <table class="cells">
      <thead><tr>
        <th>Group</th><th class="num">Volts</th><th class="num">Dev</th>
        <th class="devcell"></th>
      </tr></thead>
      <tbody id="cellrows"></tbody>
    </table>
    <div class="legend">
      <span><span class="swatch" style="background:var(--diverge-hi)"></span>above mean</span>
      <span><span class="swatch" style="background:var(--diverge-lo)"></span>below mean</span>
      <span class="muted">vertical line = pack mean</span>
      <span class="muted" id="scalenote"></span>
    </div>
  </section>

  <div class="charts">
    <section class="panel plot">
      <h2>Pack voltage</h2>
      <svg id="chart-v" viewBox="0 0 400 130" preserveAspectRatio="none"></svg>
      <div class="tip" id="tip-v"></div>
    </section>
    <section class="panel plot">
      <h2>Current</h2>
      <svg id="chart-i" viewBox="0 0 400 130" preserveAspectRatio="none"></svg>
      <div class="tip" id="tip-i"></div>
    </section>
  </div>

  <section class="panel">
    <details>
      <summary>Table view &mdash; all decoded fields</summary>
      <table class="raw" id="rawtable"></table>
    </details>
  </section>

</div>

<script>
const $ = (id) => document.getElementById(id);
const fmt = (v, d, dash) => (v === null || v === undefined) ? (dash || "—") : v.toFixed(d);

let lastOk = 0;

function tile(label, value, unit, note) {
  return `<div class="tile"><div class="label">${label}</div>
    <div class="value">${value}<span class="unit">${unit || ""}</span></div>
    ${note ? `<div class="note">${note}</div>` : ""}</div>`;
}

function renderTiles(s) {
  const amps = s.current_a;
  let flow = "idle";
  if (amps !== null && amps !== undefined) {
    if (amps > 0.05) flow = "charging";
    else if (amps < -0.05) flow = "discharging";
  }
  $("tiles").innerHTML = [
    tile("Pack", fmt(s.pack_v, 3), "V", `sum of cells ${fmt(s.cell_sum_v, 3)} V`),
    tile("Current", fmt(s.current_a, 3), "A", flow),
    tile("Power", fmt(s.power_w, 1), "W", ""),
    tile("SOC", fmt(s.soc_pct, 0), "%", "LFP-tuned unless chemistry set"),
    tile("Cell spread", fmt(s.cell_delta_mv, 1), "mV",
         `${fmt(s.cell_min_v,3)}–${fmt(s.cell_max_v,3)} V`),
    tile("Temps", `${fmt(s.temp1_c,1)}/${fmt(s.temp2_c,1)}`, "°C",
         `MOS ${fmt(s.temp_mos_c,1)} °C`),
  ].join("");
}

function renderCells(s) {
  const devs = s.cell_dev_mv || [];
  // Symmetric scale so "above" and "below" are directly comparable, with a
  // 2 mV floor: a perfectly balanced pack must not amplify noise to full width.
  const span = Math.max(2, ...devs.map(Math.abs));
  $("scalenote").textContent = devs.length ? `scale ±${span.toFixed(1)} mV` : "";
  $("cellrows").innerHTML = s.cells_v.map((v, i) => {
    const d = devs[i] === undefined ? 0 : devs[i];
    const half = Math.abs(d) / span * 50;
    const color = d >= 0 ? "var(--diverge-hi)" : "var(--diverge-lo)";
    const style = d >= 0
      ? `left:50%; width:${half}%; background:${color}`
      : `right:50%; width:${half}%; background:${color}`;
    return `<tr>
      <td>Group ${i + 1}</td>
      <td class="num">${v.toFixed(4)}</td>
      <td class="num">${d > 0 ? "+" : ""}${d.toFixed(1)}</td>
      <td class="devcell"><div class="devbar"><div class="mid"></div>
        <div class="fill" style="${style}"></div></div></td>
    </tr>`;
  }).join("");
}

// Chart state lives outside the redraw. The page repaints every second, and
// re-attaching listeners to fresh innerHTML each time would kill any hover in
// progress -- the tooltip would blink out once a second, which is worse than
// having none. So: bind the handler once per chart, and let it read the
// current scales from here.
const charts = {};

function bindHover(svgId, tipId) {
  const svg = $(svgId), tip = $(tipId);
  svg.addEventListener("mousemove", (ev) => {
    const st = charts[svgId];
    if (!st || !st.points.length) return;
    const box = svg.getBoundingClientRect();
    const vx = (ev.clientX - box.left) / box.width * st.W;
    let best = null, bd = Infinity;
    for (const p of st.points) {
      const v = st.accessor(p);
      if (v === null || v === undefined) continue;
      const dd = Math.abs(st.X(p.t) - vx);
      if (dd < bd) { bd = dd; best = p; }
    }
    if (!best) return;
    const bv = st.accessor(best);
    tip.style.opacity = 1;
    tip.textContent = `${bv.toFixed(st.decimals)} ${st.unit} @ ${Math.round(best.t)}s`;
    const left = (st.X(best.t) / st.W) * box.width;
    tip.style.left = Math.max(0, Math.min(box.width - tip.offsetWidth - 4, left + 8)) + "px";
    tip.style.top = Math.max(0, (st.Y(bv) / st.H) * box.height - 30) + "px";
  });
  svg.addEventListener("mouseleave", () => { tip.style.opacity = 0; });
}

function renderChart(svgId, tipId, points, accessor, color, unit, decimals) {
  const svg = $(svgId);
  const usable = points.filter(p => {
    const v = accessor(p);
    return v !== null && v !== undefined;
  });
  if (usable.length < 2) {
    svg.innerHTML = `<text x="8" y="70" class="axis">collecting…</text>`;
    charts[svgId] = null;
    return;
  }
  const vals = usable.map(accessor);
  const W = 400, H = 130, PL = 46, PR = 8, PT = 10, PB = 20;
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (hi - lo < 1e-6) { hi += 0.5; lo -= 0.5; }
  const pad = (hi - lo) * 0.12; lo -= pad; hi += pad;
  const t0 = usable[0].t;
  const t1 = usable[usable.length - 1].t > t0 ? usable[usable.length - 1].t : t0 + 1;
  const X = (tv) => PL + (tv - t0) / (t1 - t0) * (W - PL - PR);
  const Y = (v) => PT + (hi - v) / (hi - lo) * (H - PT - PB);

  let g = "";
  for (let i = 0; i <= 3; i++) {
    const v = lo + (hi - lo) * i / 3, y = Y(v);
    g += `<line class="gridline" x1="${PL}" y1="${y}" x2="${W - PR}" y2="${y}"/>` +
         `<text class="axis" x="${PL - 6}" y="${y + 3}" text-anchor="end">${v.toFixed(decimals)}</text>`;
  }
  // A zero line on the current chart makes charge/discharge readable at a glance.
  if (lo < 0 && hi > 0) {
    g += `<line x1="${PL}" y1="${Y(0)}" x2="${W - PR}" y2="${Y(0)}"
                stroke="var(--ink-3)" stroke-width="1" opacity="0.5"/>`;
  }
  g += `<path d="${usable.map((p, i) => `${i ? "L" : "M"}${X(p.t).toFixed(1)},${Y(accessor(p)).toFixed(1)}`).join("")}"
              fill="none" stroke="${color}" stroke-width="2"
              stroke-linejoin="round" stroke-linecap="round"/>`;
  const last = usable[usable.length - 1];
  g += `<circle cx="${X(last.t)}" cy="${Y(accessor(last))}" r="4" fill="${color}"
                stroke="var(--panel)" stroke-width="2"/>`;
  g += `<text class="axis" x="${PL}" y="${H - 5}">${Math.round(t0)}s</text>` +
       `<text class="axis" x="${W - PR}" y="${H - 5}" text-anchor="end">${Math.round(t1)}s</text>`;
  svg.innerHTML = g;

  charts[svgId] = { points: usable, accessor, X, Y, W, H, unit, decimals };
}

function renderRaw(s) {
  const rows = [
    ["profile", s.profile], ["samples", s.samples],
    ["pack_v", fmt(s.pack_v,3)], ["cell_sum_v", fmt(s.cell_sum_v,3)],
    ["current_a", fmt(s.current_a,3)], ["power_w", fmt(s.power_w,1)],
    ["soc_pct", fmt(s.soc_pct,0)], ["remaining_ah", fmt(s.remaining_ah,3)],
    ["nominal_ah", fmt(s.nominal_ah,3)], ["cycles", fmt(s.cycles,0)],
    ["temp1_c", fmt(s.temp1_c,1)], ["temp2_c", fmt(s.temp2_c,1)],
    ["temp_mos_c", fmt(s.temp_mos_c,1)],
    ["cell_mean_v", fmt(s.cell_mean_v,4)], ["cell_delta_mv", fmt(s.cell_delta_mv,1)],
  ];
  s.cells_v.forEach((v,i) => rows.push([`cell${i+1}_v`, v.toFixed(4)]));
  (s.resistances_ohm||[]).forEach((r,i) => rows.push([`cell${i+1}_ohm`, r.toFixed(4)]));
  $("rawtable").innerHTML = rows.map(([k,v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join("");
}

function renderWarnings(s) {
  let html = "";
  if (!s.confirmed) {
    html += `<div class="banner bad"><h3>Layout not confirmed</h3>
      <div class="muted">These numbers may be decoded at the wrong offsets. Run
      <code>jkbms probe --discover</code> before trusting them.</div>
      ${s.failures.length ? `<ul>${s.failures.map(f=>`<li>${f}</li>`).join("")}</ul>` : ""}</div>`;
  }
  if (s.error) {
    html += `<div class="banner bad"><h3>Link error</h3><div class="muted">${s.error}</div></div>`;
  }
  $("warnings").innerHTML = html;
}

async function poll() {
  try {
    const s = await (await fetch("/api/state")).json();
    if (!s.ready) {
      $("statustext").textContent = s.error ? "error" : "waiting for first frame";
      $("dot").className = "dot" + (s.error ? " bad" : "");
      $("meta").textContent = `${s.elapsed_s.toFixed(0)}s elapsed, no frame yet`;
      if (s.error) renderWarnings(s);
      return;
    }
    lastOk = Date.now();
    $("dot").className = "dot live";
    $("statustext").textContent = s.confirmed ? "live · layout confirmed" : "live · UNCONFIRMED";
    $("meta").textContent =
      `${s.samples} samples · ${s.elapsed_s.toFixed(0)}s · profile ${s.profile}`;
    renderWarnings(s);
    renderTiles(s);
    renderCells(s);
    renderChart("chart-v", "tip-v", s.history, p => p.pack_v, "var(--series-1)", "V", 3);
    renderChart("chart-i", "tip-i", s.history, p => p.current_a, "var(--series-2)", "A", 3);
    renderRaw(s);
  } catch (e) {
    $("dot").className = "dot bad";
    $("statustext").textContent = "server unreachable";
  }
}
bindHover("chart-v", "tip-v");
bindHover("chart-i", "tip-i");
setInterval(poll, 1000);
setInterval(() => {
  if (lastOk && Date.now() - lastOk > 5000) {
    $("dot").className = "dot stale";
    $("statustext").textContent = "no fresh data";
  }
}, 1000);
poll();
</script>
</body>
</html>
"""
