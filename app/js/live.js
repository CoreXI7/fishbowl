// ---------- live window (🌡 live on the Temperatures panel): a browser version of the `temps` terminal dashboard ----------
// Polls /api/live every second, only while open. Trends (last 60 samples) and peaks live in the page and start when
// the window opens. Layout rules: every part has a fixed size and is always present ("–" until it has a value), so
// nothing moves while numbers change; if a refresh fails the last good view stays, dimmed, with "Reconnecting…".
const LV = {open: false, timer: 0, hist: {}, peak: {}, lastOk: 0};
const LV_DRIVE_SLOTS = 4;   // the disk card always reserves room for this many drives

function lvPush(key, v) {
  if (v == null || isNaN(v)) return;
  const a = LV.hist[key] || (LV.hist[key] = []);
  a.push(v); if (a.length > 60) a.shift();
  if (!(key in LV.peak) || v > LV.peak[key]) LV.peak[key] = v;
}
const lvMBs = b => (b / 1e6).toFixed(b >= 1e8 ? 0 : 1) + " MB/s";
const lvTime = ts => new Date(ts).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});

// Trend line for the last minute, scaled between that minute's lowest and highest value (a 0-based scale would flatten
// temperatures). minRange keeps tiny wobbles (±0.3 °C) from looking like big swings. 2px line + faint fill; the latest
// value gets a dot (an HTML element, so it stays round while the SVG stretches to the tile's width).
function lvSpark(vals, minRange) {
  if (!vals || vals.length < 2) return "";
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (hi - lo < minRange) { const mid = (hi + lo) / 2; lo = mid - minRange / 2; hi = mid + minRange / 2; }
  const W = 200, H = 30, n = vals.length, y = v => H - 3 - (v - lo) / (hi - lo) * (H - 6);
  const pts = vals.map((v, i) => `${(i / (n - 1) * W).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const lastY = y(vals[n - 1]) / H * 100;
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">` +
    `<polygon points="0,${H} ${pts} ${W},${H}" class="lv-area"/>` +
    `<polyline points="${pts}" class="lv-line" vector-effect="non-scaling-stroke"/></svg>` +
    `<i class="lv-dot" style="top:${lastY.toFixed(1)}%"></i>`;
}

// Status for a reading: a word next to the colour (never colour alone), or nothing when it's normal.
const lvStatus = (v, warn, crit, words = ["warm", "hot"]) =>
  v == null ? "" : v >= crit ? `<span class="lv-status crit-t">● ${words[1]}</span>` : v >= warn ? `<span class="lv-status warn-t">● ${words[0]}</span>` : "";

// One stat tile: label (+ status) · value + unit · level bar · one-minute trend · footnote. Every part is always
// rendered (blank when empty), so every tile is the same height.
function lvTile({label, value, unit = "", status = "", bar = "", trend = "", sub = ""}) {
  return `<div class="lv-tile"><div class="lv-th"><span>${esc(label)}</span>${status}</div>` +
    `<div class="lv-val">${value}${unit ? `<small>${unit}</small>` : ""}</div>` +
    `${bar || '<div class="meter lv-blank"></div>'}<div class="lv-trend">${trend}</div>` +
    `<small class="lv-sub">${sub || "&nbsp;"}</small></div>`;
}
// a temperature as a bar: full at 10 °C above its critical point
const tempPct = t => 100 * t.c / (t.crit + 10);
const tempBar = t => meter(tempPct(t), 100 * t.warn / (t.crit + 10), 100 * t.crit / (t.crit + 10));
const peakNote = (key, fmt, unit) => key in LV.peak ? `peak ${fmt(LV.peak[key])}${unit}` : "";

function renderLive(d) {
  const thr = d.threads || [], mhz = thr.map(t => t.mhz).filter(v => v != null);
  const maxClk = mhz.length ? Math.max(...mhz) : null, avgClk = mhz.length ? mhz.reduce((a, b) => a + b, 0) / mhz.length : null;
  const memUsed = d.mem_total - d.mem_avail, memPct = 100 * memUsed / d.mem_total;
  const cpuTemps = d.temps.cells.filter(t => /^CPU/.test(t.label)), otherTemps = d.temps.cells.filter(t => !/^CPU/.test(t.label));
  const pw = d.power && d.power.pkg_w, pwLimit = d.power && d.power.limit_w;
  // the power bar's full scale: the CPU's own limit, else a bit above the highest reading so far (at least 120 W)
  const pwMax = pwLimit || Math.max((LV.peak.p || 0) * 1.15, (pw || 0) * 1.15, 120);

  cpuTemps.forEach(t => lvPush("temp:" + t.label, t.c));
  lvPush("p", pw); lvPush("u", d.cpu); lvPush("c", maxClk); lvPush("m", memUsed / 2 ** 30);
  lvPush("r", d.io.read_bps / 1e6); lvPush("w", d.io.write_bps / 1e6);
  setText("lv-sub", `${d.cpu_model || ""} · load ${d.load.map(x => x.toFixed(2)).join(" ")}`);

  // current state first: one tile per headline number, each with its trend
  const tiles = [
    ...cpuTemps.map(t => lvTile({label: t.label, value: t.c.toFixed(1), unit: "°C", status: lvStatus(t.c, t.warn, t.crit),
                                 bar: tempBar(t), trend: lvSpark(LV.hist["temp:" + t.label], 5), sub: peakNote("temp:" + t.label, v => v.toFixed(1), "°")})),
    lvTile({label: "Package power", value: pw != null ? pw.toFixed(0) : "–", unit: pw != null ? "W" : "",
            bar: pw == null ? "" : pwLimit ? meter(100 * pw / pwMax, 60, 95) : meter(100 * pw / pwMax, 999, 999),   // no limit: no warning colours
            trend: lvSpark(LV.hist.p, 20),
            sub: pw == null ? "needs the collector" : (pwLimit ? `limit ${pwLimit.toFixed(0)} W · ` : "") + peakNote("p", v => v.toFixed(0), " W")}),
    lvTile({label: "CPU usage", value: (d.cpu || 0).toFixed(1), unit: "%", status: lvStatus(d.cpu, 70, 90, ["busy", "maxed"]),
            bar: meter(d.cpu, 70, 90), trend: lvSpark(LV.hist.u, 10), sub: peakNote("u", v => v.toFixed(0), "%")}),
    lvTile({label: "Clock", value: maxClk ?? "–", unit: maxClk != null ? "MHz" : "", trend: lvSpark(LV.hist.c, 500),
            sub: avgClk != null ? `fastest core · ${Math.round(avgClk)} avg` : ""}),
    lvTile({label: "Memory", value: (memUsed / 2 ** 30).toFixed(1), unit: "GB", status: lvStatus(memPct, 80, 92, ["high", "full"]),
            bar: meter(memPct, 80, 92), trend: lvSpark(LV.hist.m, 1), sub: `of ${size(d.mem_total)}`}),
  ];
  $("lv-top").style.setProperty("--lv-tiles", tiles.length);
  setHTML("lv-top", tiles.join(""));

  // GPU (NVIDIA): from the collector, so it moves every ~10 s
  const g = d.gpu;
  setHTML("lv-gpu", g ? `<h3>${esc(g.name)} <small class="mute">${g.sessions ? `${g.sessions} transcode${g.sessions > 1 ? "s" : ""}` : "GPU"}</small></h3>
      ${kvRow("Temperature", `${g.temp ?? "–"} °C ${lvStatus(g.temp, 75, 85)}`)}
      ${kvRow("Load", `${g.util ?? 0}%`)}${meter(g.util, 80, 95)}
      ${kvRow("NVENC · NVDEC", `${g.enc ?? 0}% · ${g.dec ?? 0}%`)}
      ${kvRow("VRAM", `${((g.mem_used || 0) / 1024).toFixed(1)} of ${((g.mem_total || 0) / 1024).toFixed(0)} GB`)}${meter(100 * (g.mem_used || 0) / (g.mem_total || 1), 85, 95)}
      ${kvRow("Power", g.power != null ? g.power.toFixed(0) + " W" : "–")}`
    : `<h3>GPU</h3><div class="mute">No GPU stats (needs the collector and nvidia-smi).</div>`);

  // disk I/O: totals with their trends, then the busiest drives in a fixed number of slots
  const ioRow = (key, label, bps) => `<div class="lv-io"><span>${label}</span><span class="lv-trend lv-trend-s">${lvSpark(LV.hist[key], 20)}</span><b>${lvMBs(bps)}</b></div>`;
  const busy = d.io.disks.filter(x => x.busy >= 1).slice(0, LV_DRIVE_SLOTS);
  const slots = Array.from({length: LV_DRIVE_SLOTS}, (_, i) => busy[i]);
  setHTML("lv-io", ioRow("r", "Read", d.io.read_bps) + ioRow("w", "Write", d.io.write_bps) + slots.map((x, i) => x
    ? `<div class="lv-drive"><div class="lv-dh"><span>${esc(x.name)}</span><small>${Math.round(x.busy)}% busy · r ${lvMBs(x.read_bps)} · w ${lvMBs(x.write_bps)}</small></div>${meter(x.busy, 60, 90)}</div>`
    : `<div class="lv-drive lv-empty"><div class="lv-dh"><span>${i === 0 ? "All drives idle" : "&nbsp;"}</span></div><div class="meter lv-blank"></div></div>`).join(""));

  // other sensors (NVMe, RAM sticks, ...)
  setHTML("lv-sensors", otherTemps.length ? otherTemps.map(t => `<div class="lv-sensor"><div class="lv-dh"><span>${esc(t.label)}</span>` +
      `<small>${t.c.toFixed(0)} °C ${lvStatus(t.c, t.warn, t.crit)}</small></div>${tempBar(t)}</div>`).join("")
    : `<div class="mute">No other temperature sensors.</div>`);

  // CPU cores, one row per core with each of its threads' load, grouped by CCD (L3 cache)
  const groups = [...new Set(thr.map(t => t.group))].sort((a, b) => a - b);
  const cores = new Map();                                          // core id -> [first thread, sibling...]
  thr.forEach((t, i) => { const k = t.core ?? i; if (!cores.has(k)) cores.set(k, []); cores.get(k).push({...t, i}); });
  const coreRow = list => {
    const fast = Math.max(...list.map(t => t.mhz || 0));
    return `<div class="lv-core"><span class="mute">core ${list[0].i}</span><span class="${fast >= 5000 ? "lv-boost" : fast < 1000 ? "mute" : ""}">${fast || "–"} MHz</span>` +
      `<span class="lv-bars">${list.map(t => meter(t.use, 70, 90)).join("")}</span><span>${list.map(t => t.use.toFixed(0)).join(" · ")}%</span></div>`;
  };
  const smt = thr.some(t => t.smt);
  setText("lv-grid-n", `${cores.size} cores · ${thr.length} threads${groups.length > 1 ? ` · ${groups.length} CCDs` : ""}${smt ? " · each bar is one thread" : ""}`);
  setHTML("lv-grid", groups.map(gid => {
    const mine = [...cores.values()].filter(list => list[0].group === gid);
    return `<div><h4>${groups.length > 1 ? "CCD " + (gid + 1) : "CPU"}</h4>${mine.map(coreRow).join("")}</div>`;
  }).join(""));
}

function setLiveState(ok) {
  if (ok) LV.lastOk = Date.now();
  $("live-ov").classList.toggle("lv-stale", !ok && LV.lastOk > 0);
  setHTML("lv-live", ok ? `<i></i>Live · ${lvTime(LV.lastOk)}` : LV.lastOk ? `<i></i>Reconnecting… last update ${lvTime(LV.lastOk)}` : `<i></i>Connecting…`);
  $("lv-live").className = "lv-live" + (ok ? "" : " lv-off");
}
async function liveTick() {
  if (!LV.open) return;
  try { renderLive(await getJSON("/api/live")); setLiveState(true); }
  catch (e) { setLiveState(false); }                                  // keep the last good view, dimmed
  clearTimeout(LV.timer);
  if (LV.open && !document.hidden) LV.timer = setTimeout(liveTick, 1000);
}
function openLive() {
  LV.open = true; LV.hist = {}; LV.peak = {}; LV.lastOk = 0;
  $("live-ov").hidden = false;
  document.body.style.overflow = "hidden";
  setLiveState(false);
  liveTick();
}
function closeLive() {
  LV.open = false; clearTimeout(LV.timer);
  $("live-ov").hidden = true;
  unlockScroll();
}
$("live-btn").addEventListener("click", openLive);
$("tmp").addEventListener("click", openLive);
$("live-close").addEventListener("click", closeLive);
$("live-ov").addEventListener("click", e => { if (e.target === $("live-ov")) closeLive(); });   // click on the backdrop
addEventListener("keydown", e => { if (e.key === "Escape" && LV.open) closeLive(); });
document.addEventListener("visibilitychange", () => { if (!document.hidden && LV.open) liveTick(); });
if (/[?&]live\b/.test(location.search)) openLive();                                  // deep link: /?live

