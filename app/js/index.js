// =====================================================================================================
// homelab-dash page script. No framework: each panel has a load*() that fetches its /api/... endpoint and
// a render step that builds HTML strings. Polling is at the bottom (startPolling).
//
// Efficiency rules used throughout:
//   - setHTML() only touches the DOM when the HTML actually changed, so a refresh with the same data costs
//     no layout or repaint (the wall display refreshes every few seconds, all day).
//   - poll() never lets a refresh overlap the previous one, and stops while the tab is hidden.
// =====================================================================================================

// ---------- small helpers ----------
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const noTyping = e => !e.ctrlKey && !e.metaKey && !e.altKey && !/INPUT|TEXTAREA|SELECT/.test(e.target.tagName);
const reducedMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

// Write HTML/text into an element only if it differs from what we wrote last time.
function setHTML(el, html) {
  if (typeof el === "string") el = $(el);
  if (el._html === html) return false;
  el._html = html;
  el.innerHTML = html;
  return true;
}
function setText(el, text) {
  if (typeof el === "string") el = $(el);
  if (el.textContent !== text) el.textContent = text;
}

// ---------- number / time formatting ----------
// The formatters coerce to a number first: whatever an upstream sends, they only ever return digits and units.
const size = b => {                       // bytes -> "1.5 GB"
  if (b == null || !isFinite(b = +b)) return "–";
  const u = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0;
  while (b >= 1024 && i < 5) { b /= 1024; i++; }
  return (i ? b.toFixed(b < 10 ? 2 : 1) : b) + " " + u[i];
};
const speed = b => b ? size(b) + "/s" : "0";
const bits = b => {                       // bytes/s -> "12 Mb/s"
  if (b == null) return "–";
  let v = b * 8, i = 0;
  const u = ["b/s", "Kb/s", "Mb/s", "Gb/s"];
  while (v >= 1000 && i < 3) { v /= 1000; i++; }
  return v.toFixed(v < 10 && i ? 1 : 0) + " " + u[i];
};
const eta = s => {                        // seconds -> "2d 4h" / "3h 10m" / "5m"
  if (!(s = +s) || !isFinite(s) || s >= 8640000) return "∞";
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
};
const dur = s => {                        // uptime seconds -> "4d 2h" / "3h 5m"
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return d ? `${d}d ${h}h` : `${h}h ${m}m`;
};
const ago = iso => {
  const s = (Date.now() - new Date(iso)) / 1000;
  return s < 3600 ? Math.round(s / 60) + "m ago" : s < 86400 ? Math.round(s / 3600) + "h ago" : Math.round(s / 86400) + "d ago";
};
const ago2 = ts => ago(ts * 1000);        // unix seconds
const shortTime = iso => new Date(iso).toLocaleString([], {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"});

// ---------- shared UI bits ----------
// warn/crit colouring: lvl() for meter bars, tcls() for text
const lvl = (v, w, c) => v == null ? "" : v >= c ? "crit" : v >= w ? "warn" : "";
const tcls = (v, w, c) => v == null ? "" : v >= c ? "crit-t" : v >= w ? "warn-t" : "ok-t";
const meter = (pct, w = 80, c = 90) => `<div class="meter"><i class="${lvl(pct, w, c)}" style="width:${Math.min(100, pct || 0).toFixed(1)}%"></i></div>`;
const kvRow = (left, right) => `<div class="kv"><span>${left}</span><span>${right}</span></div>`;

// One of the summary tiles across the top. state: "ok" | "warn" | "bad" | "".
function kpi(id, state, value, sub) {
  const k = $(id), cls = "kpi " + (state || "");
  if (k.className !== cls) k.className = cls;
  setHTML(k.querySelector(".kv2"), value);
  setHTML(k.querySelector(".ks"), sub || "&nbsp;");
}

async function getJSON(url) {
  const r = await fetch(url, {credentials: "same-origin"});
  // An auth proxy (Authelia etc.) answering instead of us means the login expired: reload to log in again.
  if (r.redirected || (r.headers.get("content-type") || "").indexOf("json") < 0) { location.reload(); throw new Error("session expired"); }
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.status);
  return j;
}

async function postJSON(url, body) {
  const r = await fetch(url, {method: "POST", credentials: "same-origin", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.status);
  return j;
}

// ---------- config (from /api/config: settings page + server-side theme merge) ----------
let CFG = {groups: [], apps: {}, features: {}, quotes: []};
const CDN = "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons@main/png/";

function applyConfig() {
  document.title = CFG.title;
  $("site-title").textContent = CFG.title;
  $("gear").hidden = !CFG.settings_ui;
  if (CFG.demo) {
    $("demo-pill").hidden = false;
    if (CFG.repo) $("demo-pill").href = CFG.repo; else $("demo-pill").removeAttribute("href");
  }
  // hide panels for integrations that aren't set up; let a lone Plex/Requests panel take the full row
  document.querySelectorAll("[data-feature]").forEach(el => el.hidden = !CFG.features[el.dataset.feature]);
  const duo = document.querySelector(".duo");
  if (!CFG.features.plex && !CFG.features.seerr) duo.hidden = true;
  else if (!CFG.features.plex || !CFG.features.seerr) duo.style.gridTemplateColumns = "minmax(0,1fr)";
  let saved = "";
  try { saved = localStorage.getItem("theme") || ""; } catch (e) {}
  setTheme(CFG.themes && CFG.themes[saved] ? saved : CFG.theme);
}

// ---------- themes ----------
// The look is CSS (html[data-theme]); each theme also brings its own mascot, quotes, click emoji, taglines and
// tab icon (CFG.themes, merged on the server). The 🎨 button or T cycles; the choice is saved per browser.
let THEME = "";
function setTheme(id, save) {
  if (!CFG.themes || !CFG.themes[id]) id = CFG.theme || "bubbles";
  THEME = id;
  document.documentElement.dataset.theme = id;
  Object.assign(CFG, CFG.themes ? CFG.themes[id] : {});
  if (save) try { localStorage.setItem("theme", id); } catch (e) {}
  const icon = document.querySelector("link[rel=icon]");
  icon.dataset.default = icon.dataset.default || icon.getAttribute("href");
  icon.href = CFG.favicon || icon.dataset.default;
  $("mascot").hidden = !CFG.mascot;
  if (CFG.mascot) { $("mascot-img").src = CFG.mascot; $("mascot-credit").textContent = CFG.mascot_credit || ""; }
  $("say").textContent = CFG.greeting || CFG.quotes[0] || "Hi!";
  $("say").dataset.sad = "";
  setMood(lastDown);
  const ids = Object.keys(CFG.themes || {}), next = CFG.themes && CFG.themes[ids[(ids.indexOf(id) + 1) % ids.length]];
  $("theme-btn").hidden = ids.length < 2;
  $("theme-btn").title = `Theme: ${CFG.label || id}${next ? ` (click for ${next.label})` : ""} · T`;
}
function nextTheme() {
  const ids = Object.keys(CFG.themes || {});
  if (ids.length > 1) setTheme(ids[(ids.indexOf(THEME) + 1) % ids.length], true);
}
$("theme-btn").addEventListener("click", nextTheme);
addEventListener("keydown", e => { if (e.key.toLowerCase() === "t" && noTyping(e)) nextTheme(); });

// ---------- services (one tile per NPM proxy host, grouped as configured) ----------
// Icon fallback chain: configured icon -> the service's own favicon (via /api/icon) -> first letter.
// One capturing listener for every icon (error events don't bubble), instead of an inline onerror on each <img>:
// the Content-Security-Policy allows no inline script at all.
document.addEventListener("error", e => { if (e.target.tagName === "IMG" && "letter" in e.target.dataset) iconFail(e.target); }, true);
function iconFail(img) {
  const step = +(img.dataset.step || 0);
  img.dataset.step = step + 1;
  if (step === 0 && img.dataset.fav) { img.src = img.dataset.fav; return; }
  const d = document.createElement("div");
  d.className = "lt";
  d.textContent = img.dataset.letter;
  img.replaceWith(d);
}

function serviceTile(s) {
  const app = CFG.apps[s.name] || {}, label = app.label || s.name.split(".")[0], icon = app.icon || "";
  const letter = esc(label[0].toUpperCase());
  const fav = CFG.demo ? "" : "/api/icon?host=" + encodeURIComponent(s.name);
  const src = icon ? (/^(\/|https?:|data:)/.test(icon) ? icon : CDN + icon + ".png") : fav;
  const status = s.up ? `online · ${s.ms} ms${s.code ? " · HTTP " + s.code : ""}` : `OFFLINE (${s.err || "HTTP " + s.code})`;
  const tip = `${s.name}\n${status}${s.auth ? "\nbehind SSO" : "\npublic (no SSO)"}`;
  const img = src
    ? `<img src="${esc(src)}" alt="" loading="lazy" data-fav="${icon ? esc(fav) : ""}" data-letter="${letter}">`
    : `<span class="lt">${letter}</span>`;
  // healthy tiles stay quiet (the group header and the Services KPI say "all up"); only problems get a badge,
  // marked by shape and text too, not just colour
  return `<a class="app ${s.up ? "" : "down"}" href="${esc(app.url || "https://" + s.name)}" target="_blank" rel="noopener" title="${esc(tip)}">
    <span class="ic">${img}${s.up ? "" : `<span class="st down" aria-hidden="true">!</span>`}</span>
    <span class="nm">${esc(label)}${s.up ? "" : `<small> · down</small>`}</span></a>`;
}

async function loadServices() {
  try {
    const d = await getJSON("/api/services");
    const down = d.services.filter(s => !s.up);
    // configured groups first, then anything not in a group under "New / other"
    const byName = Object.fromEntries(d.services.map(s => [s.name, s])), used = new Set();
    const groups = CFG.groups.map(g => [g.name, (g.hosts || []).filter(h => byName[h]).map(h => (used.add(h), byName[h]))]);
    const other = d.services.filter(s => !used.has(s.name));
    if (other.length) groups.push([CFG.groups.length ? "✨ New / other" : "All services", other]);
    setHTML("svc", groups.filter(([, list]) => list.length).map(([name, list]) => {
      const dn = list.filter(s => !s.up).length;
      return `<div class="grp" style="--c:${Math.ceil(list.length / 2)}"><h3><span>${esc(name)}</span><span class="${dn ? "crit-t" : "ok-t"}">${dn ? dn + " down" : list.length + " ✓"}</span></h3>
        <div class="apps">${list.map(serviceTile).join("")}</div></div>`;
    }).join("") || `<div class="empty">No services found. Mount NPM's proxy_host folder or add [[services]] to config.toml.</div>`);
    setText("svc-n", `${d.services.length - down.length}/${d.services.length} up · checked ${new Date(d.checked * 1000).toLocaleTimeString()}`);
    setMood(down.length);
    kpi("k-svc", down.length ? "bad" : "ok", `${d.services.length - down.length}/${d.services.length}`,
        down.length ? "down: " + down.slice(0, 3).map(s => esc(s.name.split(".")[0])).join(", ") : "all online");
  } catch (e) {
    setHTML("svc", `<div class="err">${esc(e.message)}</div>`);
    kpi("k-svc", "warn", "?", esc(e.message));
  }
}

// ---------- qBittorrent ----------
const TOR_STATE = {downloading: "downloading", stalledDL: "stalled", metaDL: "fetching metadata", forcedDL: "forced",
                   queuedDL: "queued", checkingDL: "checking", pausedDL: "paused", stoppedDL: "stopped"};

function torTab(which) {
  ["dl", "seed"].forEach(w => { $(w).hidden = w !== which; $("t-" + w).classList.toggle("on", w === which); });
}
$("t-dl").addEventListener("click", () => torTab("dl"));
$("t-seed").addEventListener("click", () => torTab("seed"));

function torRow(t, dl) {
  const pct = (+t.progress * 100 || 0).toFixed(1);
  const stats = dl
    ? `<span>${pct}% of ${size(t.size)}</span><span>↓ ${speed(t.dlspeed)}</span><span>ETA ${eta(t.eta)}</span><span>${esc(TOR_STATE[t.state] || t.state)}</span><span>${esc(t.num_seeds)} seeds</span>`
    : `<span>↑ ${speed(t.upspeed)}</span><span>${esc(t.num_leechs)} peers</span><span>ratio ${(+t.ratio || 0).toFixed(2)}</span><span>${size(t.size)}</span>`;
  return `<div class="tor"><div class="t" title="${esc(t.name)}">${esc(t.name)}</div>
    ${dl ? `<div class="bar"><i style="width:${pct}%"></i></div>` : ""}
    <div class="s">${stats}${t.category ? `<span>${esc(t.category)}</span>` : ""}</div></div>`;
}

async function loadQbit() {
  if (!CFG.features.qbit) return;
  try {
    const q = await getJSON("/api/qbit");
    setHTML("dl", q.downloading.length ? q.downloading.map(t => torRow(t, true)).join("") : `<div class="empty">Nothing downloading.</div>`);
    setHTML("seed", q.seeding.length ? q.seeding.map(t => torRow(t, false)).join("") : `<div class="empty">No active uploads right now.</div>`);
    setText("t-dl", `Downloading (${q.downloading.length})`);
    setText("t-seed", `Seeding (${q.seeding_active})`);
    setText("tor-n", `· ${q.total} torrents · lifetime ↑ ${size(q.up_total)} ↓ ${size(q.dl_total)}`);
    // VPN check: qbit's external IP equal to your home IP means it's NOT going through the VPN
    const connOk = q.connection !== "disconnected" && !q.vpn_leak && q.external_ip;
    const mbs = b => (b / 1048576).toFixed(b >= 10485760 ? 0 : 1);
    kpi("k-net", q.vpn_leak ? "bad" : connOk ? "ok" : "warn", `↓ ${mbs(q.dl_speed)} ↑ ${mbs(q.up_speed)} <small style="font-size:.55em">MB/s</small>`,
        q.vpn_leak ? "<b>VPN LEAK: home IP!</b>" : `${q.vpn_check ? "VPN" : "IP"} ${esc(q.external_ip || "?")} · ${esc(q.connection)}`);
    $("banner").hidden = !q.vpn_leak;
    if (q.vpn_leak) setText("banner", "qBittorrent is using your home IP. The VPN is not protecting it!");
  } catch (e) {
    setHTML("dl", `<div class="err">qBittorrent: ${esc(e.message)}</div>`);
    kpi("k-net", "warn", "qbit ?", "unreachable");
  }
}

// ---------- Seerr requests (approve / decline right from the dashboard) ----------
function requestCard(r) {
  const poster = r.poster && r.poster.startsWith("data:") ? r.poster : "https://image.tmdb.org/t/p/w154" + r.poster;
  const id = esc(r.id);                   // ids go into attributes only; the click handler reads data-id
  return `<div class="req" id="r${id}">
    ${r.poster ? `<img loading="lazy" src="${esc(poster)}" alt="">` : `<div class="ph"></div>`}
    <div class="b"><div><b>${esc(r.title)}</b> ${r.year ? `<span class="mute">(${esc(r.year)})</span>` : ""}<span class="tag">${r.type === "tv" ? "TV" : "Movie"}</span>${r.is4k ? '<span class="tag">4K</span>' : ""}</div>
      <div class="mute" style="font-size:12px">${esc(r.user)} · ${ago(r.created)}${r.seasons.length ? " · season " + r.seasons.map(esc).join(", ") : ""}</div>
      <div class="ov">${esc(r.overview)}</div>
      <button class="ok" data-act="approve" data-id="${id}">Approve</button> <button class="no" data-act="decline" data-id="${id}">Decline</button>
      <span class="err" id="e${id}"></span></div></div>`;
}
$("req").addEventListener("click", e => {
  const b = e.target.closest("button[data-act]");
  if (b) act(b.dataset.id, b.dataset.act, b);
});

async function loadRequests() {
  if (!CFG.features.seerr) return;
  try {
    const d = await getJSON("/api/requests"), n = d.pending.length;
    setHTML("req", n ? d.pending.map(requestCard).join("") : `<div class="empty">No pending requests. 🎉</div>`);
    setText("req-n", n ? String(n) : "");
    kpi("k-req", n ? "warn" : "ok", n ? `${n} waiting` : "none", n ? esc(d.pending.slice(0, 2).map(r => r.title).join(", ")) : "all caught up 🎉");
  } catch (e) {
    setHTML("req", `<div class="err">Seerr: ${esc(e.message)}</div>`);
    kpi("k-req", "warn", "?", "Seerr unreachable");
  }
}

async function act(id, action, btn) {
  if (action === "decline" && !confirm("Decline this request?")) return;
  const buttons = btn.parentElement.querySelectorAll("button");
  buttons.forEach(b => b.disabled = true);
  try {
    await postJSON(`/api/requests/${encodeURIComponent(id)}/${action}`);
    $("r" + id).style.opacity = .4;
    setTimeout(loadRequests, 600);
  } catch (e) {
    $("e" + id).textContent = " " + e.message;
    buttons.forEach(b => b.disabled = false);
  }
}

// ---------- system: CPU/RAM, temperatures, GPU, drives (all from /api/system every few seconds) ----------
async function loadSystem() {
  try {
    const s = await getJSON("/api/system"), hw = s.hw;
    renderSystem(s, hw);
    renderTemps(s, hw);
    renderGpu(hw && hw.gpu);
    renderDrives(hw);
    renderSecurity(hw);
    $("usage-btn").hidden = !(hw && hw.usage && hw.usage.groups);
    if (hw && hw.usage && hw.usage.groups) usageData(hw.usage);
  } catch (e) {
    setHTML("sys", `<div class="err">${esc(e.message)}</div>`);
  }
}

function renderSystem(s, hw) {
  const memUsed = s.mem_total - s.mem_avail, memPct = 100 * memUsed / s.mem_total, cpuT = s.temps.cpu;
  setText("sys-up", "up " + dur(s.uptime));
  const cores = `<div class="cores" title="per-thread load (${s.cores.length} threads)" style="grid-template-columns:repeat(${Math.min(16, s.cores.length)},1fr)">`
    + s.cores.map(c => `<i title="${c.toFixed(0)}%"><b style="height:${c}%"></b></i>`).join("") + `</div>`;
  const stopped = hw && hw.containers ? hw.containers.total - hw.containers.running : 0;
  setHTML("sys", [
    kvRow(`CPU${s.cpu_model ? " · " + esc(s.cpu_model) : ""}`, `load ${s.load.map(x => x.toFixed(2)).join(" ")}`),
    `<div class="big">${s.cpu.toFixed(0)}%</div>${meter(s.cpu, 70, 90)}`,
    cores,
    kvRow("Memory", `${size(memUsed)} / ${size(s.mem_total)}`) + meter(memPct, 80, 92),
    s.swap_total ? kvRow("Swap", `${size(s.swap_total - s.swap_free)} / ${size(s.swap_total)}`) : "",
    hw && hw.net ? kvRow(`Network (${esc(hw.net.if)})`, `↓ ${bits(hw.net.rx_bps)} &nbsp;↑ ${bits(hw.net.tx_bps)}`) : "",
    hw && hw.containers ? kvRow("Containers", `${hw.containers.running} running${stopped > 0 ? ` · <span class="warn-t">${stopped} stopped</span>` : ""}`) : "",
    !hw ? kvRow(`<span class="mute">Install the collector for drives, GPU and security</span>`, "")
      : hw.stale ? kvRow(`<span class="err">Collector data is stale</span>`, "") : "",
  ].join(""));
  const state = s.cpu >= 90 || memPct >= 92 || cpuT >= 90 ? "bad" : s.cpu >= 70 || memPct >= 80 || cpuT >= 80 ? "warn" : "ok";
  kpi("k-sys", state, `${s.cpu.toFixed(0)}% · ${memPct.toFixed(0)}%`,
      `${cpuT != null ? "CPU " + Math.round(cpuT) + "° · " : ""}RAM ${size(memUsed)} · up ${dur(s.uptime)}`);
}

function renderTemps(s, hw) {
  const cell = (label, v, w, c, tip) => `<div class="tc" title="${esc(tip || label)}"><small>${esc(label)}</small><b class="${tcls(v, w, c)}">${v == null ? "–" : Math.round(v) + "°"}</b></div>`;
  const cells = s.temps.cells.map(t => cell(t.label, t.c, t.warn, t.crit));
  const g = hw && hw.gpu, smart = (hw && hw.smart) || {};
  if (g && g.temp != null) cells.push(cell("GPU", g.temp, 75, 85));
  // hard drives (not NVMe) get one hottest + one average cell instead of eight separate ones
  const hdd = (hw ? hw.disks : []).filter(d => d.dev && !/nvme|mmcblk/.test(d.dev)).map(d => smart[d.dev] && smart[d.dev].temp).filter(v => v != null);
  if (hdd.length) cells.push(cell("HDD max", Math.max(...hdd), 45, 50, "hottest hard drive"),
                             cell("HDD avg", hdd.reduce((a, b) => a + b, 0) / hdd.length, 45, 50));
  setHTML("tmp", cells.join("") || `<div class="empty">No temperature sensors visible.</div>`);
}

function renderGpu(g) {
  $("s-gpu").hidden = !g;
  if (!g) return;
  setText("gpu-n", g.name);
  setHTML("gpu", [
    kvRow("Load", `${g.util}%`) + meter(g.util, 80, 95),
    kvRow("NVENC / NVDEC", `${g.enc}% / ${g.dec}%`),
    kvRow("Encode sessions", `${g.sessions ?? 0}`),
    kvRow("VRAM", `${(g.mem_used / 1024).toFixed(1)} / ${(g.mem_total / 1024).toFixed(0)} GB`) + meter(100 * g.mem_used / g.mem_total, 85, 95),
    kvRow("Power · Fan", `${g.power != null ? g.power.toFixed(0) + " W" : "–"} · ${g.fan ?? "–"}%`),
  ].join(""));
}

function renderDrives(hw) {
  if (!(hw && hw.disks && hw.disks.length)) {
    setHTML("drv", `<div class="empty">Drive stats need the collector service.</div>`);
    kpi("k-disk", "", "–", "needs the collector");
    return;
  }
  const smart = hw.smart || {};
  // the totals count data drives only (the system disk would skew "free space"), unless that's all there is
  const data = hw.disks.filter(d => !d.system && d.total);
  const pool = data.length ? data : hw.disks.filter(d => d.total);
  const tot = pool.reduce((a, d) => a + d.total, 0), used = pool.reduce((a, d) => a + d.used, 0);
  setText("drv-n", `${size(used)} / ${size(tot)}`);

  const smartBad = hw.disks.some(d => d.missing || (smart[d.dev] || {}).passed === false);
  const full = hw.disks.some(d => d.total && d.used / (d.used + d.free) > .95);
  const sectors = hw.disks.filter(d => { const x = smart[d.dev] || {}; return x.realloc > 0 || x.pending > 0; }).length;
  kpi("k-disk", smartBad ? "bad" : full || sectors ? "warn" : "ok", `${size(tot - used)} free`,
      `${(100 * used / tot).toFixed(0)}% of ${size(tot)}${smartBad ? " · <b>drive problem!</b>" : sectors ? ` · ${sectors} drive${sectors > 1 ? "s" : ""} with bad sectors` : ""}`);

  const drive = d => {
    if (d.missing) return `<div class="drv"><div class="h"><b class="crit-t">${esc(d.name)}</b><span class="chip crit-t">NOT MOUNTED</span></div></div>`;
    const x = smart[d.dev] || {}, pct = 100 * d.used / (d.used + d.free);   // same % as df
    const bad = x.passed === false || x.realloc > 0 || x.pending > 0 || x.uncorrect > 0;
    const health = x.standby ? `<span class="chip">asleep</span>`
      : x.passed === false ? `<span class="chip crit-t">SMART FAIL</span>`
      : bad ? `<span class="chip warn-t" title="reallocated ${x.realloc ?? 0} · pending ${x.pending ?? 0} · uncorrectable ${x.uncorrect ?? 0}">⚠ sectors</span>` : "";
    const warm = /nvme/.test(d.dev) ? [65, 75] : [45, 50];
    const tip = [x.model || d.model, d.dev, d.mount,
                 x.hours != null ? `${Math.round(x.hours / 24 / 365 * 10) / 10} years powered on` : "",
                 x.wear != null ? `wear ${x.wear}%` : "",
                 x.realloc != null ? `realloc ${x.realloc} · pending ${x.pending ?? 0}` : ""].filter(Boolean).join("\n");
    return `<div class="drv" title="${esc(tip)}"><div class="h"><b>${esc(d.name)}</b><span>${health} <span class="chip ${tcls(x.temp, ...warm)}">${x.temp != null ? x.temp + "°C" : "–"}</span></span></div>
      <div class="md">${esc(x.model || d.model)} · ${esc(d.dev)}</div>${meter(pct, 90, 95)}
      <div class="s"><span>${size(d.used)} / ${size(d.total)}</span><span>${pct.toFixed(0)}% · ${size(d.free)} free</span></div></div>`;
  };
  setHTML("drv", kvRow(data.length ? "All data drives" : "Total", `${size(tot - used)} free`) + meter(100 * used / tot, 90, 95) + hw.disks.map(drive).join(""));
}

// ---------- Plex now playing (via Tautulli) ----------
function streamCard(s) {
  const ep = s.media_type === "episode", tc = s.transcode_decision === "transcode", pct = +s.progress_percent || 0;
  const title = ep ? s.grandparent_title : s.title, sub = ep ? `S${s.parent_media_index}·E${s.media_index} — ${s.title}` : (s.year || "");
  const icon = s.state === "paused" ? "⏸" : s.state === "buffering" ? "⏳" : "▶";
  const mode = tc && s.video_decision !== "transcode" ? `<span class="chip good">Audio transcode</span>`
    : tc ? `<span class="chip mid">Transcode${s.transcode_hw_encoding ? " (HW)" : " (CPU!)"}</span>`
    : s.transcode_decision === "copy" ? `<span class="chip good">Direct Stream</span>` : `<span class="chip good">Direct Play</span>`;
  const res = s.stream_video_full_resolution || s.video_full_resolution;
  const left = s.duration ? " · " + eta(Math.max(0, (s.duration - s.view_offset) / 1000)) + " left" : "";
  return `<div class="stream">${s.art ? `<img src="${esc(s.art)}" alt="" loading="lazy">` : `<div class="ph"></div>`}
    <div class="b"><div class="t" title="${esc(s.full_title)}">${esc(title)}</div><div class="sub">${esc(sub)}</div>
      <div class="sub">👤 ${esc(s.friendly_name)} · ${esc(s.product)} on ${esc(s.player)}</div>
      <div class="bar"><i style="width:${pct}%"></i></div>
      <div class="sub">${icon} ${esc(s.state)} · ${pct}%${left}</div>
      <div class="chips">${mode}${res ? `<span class="chip">${esc(res)}</span>` : ""}<span class="chip">${(s.bandwidth / 1000).toFixed(1)} Mbps</span>
        <span class="chip ${s.location === "wan" ? "" : "good"}">${s.location === "wan" ? "remote" : "home"}</span></div></div></div>`;
}

async function loadPlex() {
  if (!CFG.features.plex) return;
  try {
    const d = await getJSON("/api/plex");
    const summary = `${(+d.bandwidth / 1000 || 0).toFixed(1)} Mbps · ${esc(d.transcodes)} transcoding`;
    // Only a VIDEO transcode can use the GPU; audio-only transcodes (e.g. AC3 -> AAC) are always CPU and cheap.
    const cpuTranscode = d.sessions.some(s => s.video_decision === "transcode" && !s.transcode_hw_encoding);
    kpi("k-plex", cpuTranscode ? "warn" : "ok", d.stream_count ? `${d.stream_count} watching` : "idle", d.stream_count ? summary : "nobody streaming");
    setText("plex-n", d.stream_count ? `${d.stream_count} stream${d.stream_count > 1 ? "s" : ""} · ${summary}` : "");
    if (setHTML("plex", d.sessions.length ? d.sessions.map(streamCard).join("") : `<div class="empty">Nobody's watching right now. 🍿</div>`))
      requestAnimationFrame(plexHint);
  } catch (e) {
    setHTML("plex", `<div class="err">Tautulli: ${esc(e.message)}</div>`);
    kpi("k-plex", "warn", "?", "Tautulli unreachable");
  }
}

// More streams than fit (wall mode)? "+N more" gives Now Playing the whole row and moves Requests aside;
// clicking it again (or the Plex tile, in wall mode) brings Requests back. Remembered per browser.
let plexWide = false;
try { plexWide = localStorage.getItem("plexWide") === "1"; } catch (e) {}
function setPlexWide(on) {
  plexWide = on;
  document.querySelector(".duo").classList.toggle("plex-wide", on);
  try { localStorage.setItem("plexWide", on ? "1" : "0"); } catch (e) {}
  plexHint();
}
function plexHint() {
  const bottom = $("plex").getBoundingClientRect().bottom;   // the list's own box (it scrolls on the wall)
  const cut = [...$("plex").querySelectorAll(".stream")].filter(el => el.getBoundingClientRect().bottom > bottom + 1).length;
  $("plex-more").hidden = !cut && !plexWide;
  setText("plex-more", plexWide ? `${cut ? `+${cut} more · ` : ""}show requests` : `+${cut} more ▸`);
}
setPlexWide(plexWide);
$("plex-more").addEventListener("click", () => setPlexWide(!plexWide));
$("plex").addEventListener("scroll", plexHint, {passive: true});
$("k-plex").addEventListener("click", e => { if (isWall()) { e.preventDefault(); setPlexWide(!plexWide); } });
// recount whenever the box or the list changes size (window resize, posters and fonts settling after a refresh)
if ("ResizeObserver" in window) { const ro = new ResizeObserver(() => plexHint()); ro.observe($("s-plex")); ro.observe($("plex")); }
else addEventListener("resize", () => requestAnimationFrame(plexHint));

// ---------- security & backups ----------
// Every box is optional: it only shows if the collector reports it.
// "known" is decided on the server (config known_ips), so your list of trusted IPs never reaches the browser.
const known = item => !!item.known;
const flagged = item => !item.known && !item.acked;   // unknown and not yet marked "looks fine"

async function ackLogins(btn) {
  btn.disabled = true;
  btn.textContent = "…";
  try { await postJSON("/api/security/ack"); loadSystem(); }
  catch (e) { btn.disabled = false; btn.textContent = "failed: " + e.message; }
}

$("sec").addEventListener("click", e => {          // the card's buttons (no inline onclick: see the CSP)
  const b = e.target.closest("button[data-sec]");
  if (b && b.dataset.sec === "ack") ackLogins(b);
  else if (b && b.dataset.sec === "cron") approveCron(b.dataset.hash, b);
});

async function approveCron(hash, btn) {
  btn.disabled = true;
  btn.textContent = "approving…";
  try {
    await postJSON("/api/cron/approve", {hash});
    setTimeout(loadSystem, 6000);    // the root collector applies it on its next round
    setTimeout(loadSystem, 11000);
  } catch (e) { btn.disabled = false; btn.textContent = "failed: " + e.message; }
}

function renderSecurity(hw) {
  const s = hw && hw.security, b = hw && hw.backups;
  if (!s || s.error) {
    setHTML("sec", `<div class="empty">${s && s.error ? esc(s.error) : "Security checks need the collector service."}</div>`);
    kpi("k-sec", "", "–", s && s.error ? "collector error" : "needs the collector");
    return;
  }
  // ✓ / ✗ label
  const ok = (good, txt, badTxt) => `<span class="${good ? "ok-t" : "crit-t"}">${good ? "✓ " + txt : "✗ " + badTxt}</span>`;
  const row = (left, right) => `<div class="row"><span>${left}</span><span>${right}</span></div>`;

  // overall verdict for the header + summary tile
  const unknownLogins = s.ssh ? s.ssh.logins.filter(flagged) : [];
  const locksOk = !s.locks || s.locks.every(l => l.locked), cronBad = s.cron && s.cron.ok === false;
  const fwOk = !s.firewall || /^(active|running)/i.test(s.firewall.status);
  const issues = [!locksOk && "lock missing", s.procs && s.procs.found.length && "bad process", unknownLogins.length && "unknown SSH login",
                  b && b.ok === false && "backup problem", !fwOk && "firewall off", cronBad && "cron changed"].filter(Boolean);
  setHTML("sec-n", issues.length ? `<span class="crit-t">⚠ ${esc(issues.join(" · "))}</span>` : `<span class="ok-t">all clear ✓</span>`);
  const summary = [s.f2b && `${s.f2b.banned_now} banned`, b && `backup ${b.ok ? "ok" : b.ok === false ? "!" : "?"}`, s.locks && "locks ok"].filter(Boolean).join(" · ");
  kpi("k-sec", issues.length ? "bad" : "ok", issues.length ? `${issues.length} issue${issues.length > 1 ? "s" : ""}` : "all clear",
      issues.length ? esc(issues.join(" · ")) : summary || "no issues");

  const boxes = [];
  if (s.f2b || s.firewall) {
    const fw = s.firewall ? esc(s.firewall.name + " " + s.firewall.status) : "";
    boxes.push(`<div class="box"><h3>${sdLink("fw", [s.firewall && "Firewall", s.f2b && "fail2ban"].filter(Boolean).join(" &amp; "))}
        <span>${s.firewall ? ok(fwOk, fw, fw) : ""}</span></h3>
      ${s.f2b ? row(`Banned right now (${esc(s.f2b.jail)})`, s.f2b.banned_now) + row("Banned total (since restart)", s.f2b.banned_total) : ""}
      ${s.ssh ? row("SSH failures, 24h", s.ssh.failed_24h) : ""}
      ${s.f2b && s.f2b.ips.length ? `<details data-k="f2b-ips"><summary>banned IPs</summary><div class="mono">${s.f2b.ips.map(esc).join("<br>")}</div></details>` : ""}</div>`);
  }
  if (s.ssh) {
    const login = l => {
      const tip = known(l) ? "known IP" : l.acked ? "not on your known_ips list, but you marked it as fine" : "NOT on your known_ips list";
      return `<div class="row"><span>${esc(shortTime(l.time))} · ${esc(l.user)}</span><span class="mono ${flagged(l) ? "unk" : ""}" title="${tip}">${esc(l.ip)}${!known(l) && l.acked ? " ✓" : ""}</span></div>`;
    };
    boxes.push(`<div class="box"><h3>${sdLink("ssh", "SSH logins")} <span>${unknownLogins.length ? `<span class="crit-t">${unknownLogins.length} unknown</span>` : '<span class="ok-t">all known ✓</span>'}</span></h3>
      ${s.ssh.logins.slice(0, 5).map(login).join("") || '<div class="row"><span>none in 7 days</span></div>'}
      ${unknownLogins.length ? `<div class="row" style="margin-top:6px"><span>That was you?</span><button class="ok" data-sec="ack" title="Clears this alert. A new login from somewhere unfamiliar will flag again.">Looks fine ✓</button></div>` : ""}</div>`);
  }
  if (s.authelia) {
    const fail = f => `<div class="row"><span>${esc(shortTime(f.time))} · ${esc(f.factor)} ${esc(f.user || "?")}</span><span class="mono ${known(f) ? "" : "unk"}">${esc(f.ip)}</span></div>`;
    boxes.push(`<div class="box"><h3>${sdLink("auth", "Authelia")} <span>${s.authelia.failed_24h ? `<span class="warn-t">${s.authelia.failed_24h} failed</span>` : '<span class="ok-t">no failures ✓</span>'}</span></h3>
      ${s.authelia.recent.slice(0, 4).map(fail).join("") || row("failed logins, last 24h", 0)}</div>`);
  }
  if (s.procs) {
    boxes.push(`<div class="box"><h3>${sdLink("procs", "Process watch")} <span>${ok(!s.procs.found.length, "clean", "FOUND: " + esc(s.procs.found.join(", ")))}</span></h3>
      ${s.procs.top.map(p => `<div class="row" title="PID ${p.pid}"><span>${esc(p.name)}</span><span>${p.cpu.toFixed(0)}%</span></div>`).join("")}</div>`);
  }
  if (s.cron) {
    // a changed crontab stays flagged until you approve exactly what's there now (the collector checks the hash)
    const change = c => `<div class="row"><span class="mono" style="white-space:normal">${esc(c.file)}${c.new_file ? " (new file)" : c.deleted ? " (deleted)" : ""}</span></div>
      ${c.added.map(l => `<div class="mono" style="color:var(--down);word-break:break-all">+ ${esc(l)}</div>`).join("")}
      ${c.removed.map(l => `<div class="mono" style="color:var(--mute);word-break:break-all">− ${esc(l)}</div>`).join("")}`;
    const body = cronBad
      ? s.cron.changes.map(change).join("")
        + `<div class="row" style="margin-top:8px"><span>If you made this change:</span><button class="ok" data-sec="cron" data-hash="${esc(s.cron.hash)}">Looks good ✓</button></div>
           <div class="mute" style="font-size:11px">Didn't make it? Don't approve. Check it (possible malware persistence).</div>`
      : row(`${s.cron.files ?? "–"} cron files watched`, s.cron.approved_at ? "approved " + ago2(s.cron.approved_at) : "");
    boxes.push(`<div class="box"><h3>${sdLink("cron", "Cron jobs")} <span>${ok(!cronBad, "as approved", "CHANGED")}</span></h3>${body}</div>`);
  }
  if (s.locks) {
    boxes.push(`<div class="box"><h3>${sdLink("locks", "Immutable files")} <span>${ok(locksOk, "intact", "MISSING")}</span></h3>
      ${s.locks.map(l => row(esc(l.file), l.locked ? "🔒" : '<span class="crit-t">unlocked</span>')).join("")}</div>`);
  }
  if (b) {
    const artifact = a => `<div class="row"><span>${esc(a.label)}${a.count ? ` (${a.count})` : ""}</span><span class="${a.fresh ? "" : "crit-t"}">${a.count ? ago2(a.newest) + " · " + size(a.size) : "none found"}</span></div>`;
    boxes.push(`<div class="box"><h3>${sdLink("backups", "Backups")} <span>${b.ok == null ? "" : ok(b.ok, "ok", "PROBLEM")}</span></h3>
      ${b.last_run ? row("Last run", esc(b.last_run)) : ""}
      ${(b.checks || []).map(c => row(esc(c.label), ok(c.ok, c.count + " ✓", c.count + " found"))).join("")}
      ${(b.artifacts || []).map(artifact).join("")}
      ${b.log && b.log.length ? `<details data-k="backup-log"><summary>log</summary><div class="mono">${b.log.map(esc).join("<br>")}</div></details>` : ""}</div>`);
  }

  // When the panel does change, keep open dropdowns (log, banned IPs) open and where you'd scrolled in them.
  const open = {};
  $("sec").querySelectorAll("details[open]").forEach(d => open[d.dataset.k] = (d.querySelector(".mono") || {}).scrollTop || 0);
  if (!setHTML("sec", boxes.join("") || `<div class="empty">All security checks are switched off in collector.toml.</div>`)) return;
  $("sec").querySelectorAll("details").forEach(d => {
    if (!(d.dataset.k in open)) return;
    d.open = true;
    const m = d.querySelector(".mono");
    if (m) m.scrollTop = open[d.dataset.k];
  });
}

// ---------- mascot, bubbles, confetti ----------
let lastDown = 0;   // how many services are down (the mascot gets sad)
function setMood(down) {
  lastDown = down;
  setText("quote", down ? CFG.tagline_down : CFG.tagline);
  $("mascot").classList.toggle("sad", !!down);
  if (down) setText("say", `${CFG.tagline_down} (${down} down)`);
  else if ($("say").dataset.sad) setText("say", CFG.greeting || CFG.quotes[0] || "All good!");
  $("say").dataset.sad = down ? "1" : "";
}
function mascotSay() {
  if (lastDown || !CFG.mascot) return;
  const plex = $("plex-n").textContent, req = parseInt($("req-n").textContent) || 0;
  const extra = [plex && `Now playing: ${plex}`, req && `${req} request${req > 1 ? "s" : ""} waiting for you!`].filter(Boolean);
  const pool = CFG.quotes.concat(extra, extra);   // live facts twice: they come up more often than plain quotes
  if (pool.length) $("say").textContent = pool[Math.floor(Math.random() * pool.length)];
}
function bubbles() {
  const box = $("bubbles");
  if (!box || reducedMotion()) return;
  for (let i = 0; i < 18; i++) {
    const b = document.createElement("i"), s = 14 + Math.random() * 46;
    Object.assign(b.style, {width: s + "px", height: s + "px", left: Math.random() * 100 + "%",
                            animationDuration: 14 + Math.random() * 20 + "s", animationDelay: -Math.random() * 30 + "s"});
    box.appendChild(b);
  }
}
// "🎵🥬💙" rains a random one of them per drop (graphemes, so flags and skin tones stay whole)
function pickEmoji(str) {
  const parts = "Segmenter" in Intl ? [...new Intl.Segmenter().segment(str || "")].map(x => x.segment).filter(x => x.trim()) : Array.from(str || "");
  return parts.length ? parts[Math.floor(Math.random() * parts.length)] : "🫧";
}
function confetti() {
  if (reducedMotion()) return;
  for (let i = 0; i < 16; i++) setTimeout(() => {
    const m = document.createElement("div");
    m.className = "confetti";
    m.textContent = pickEmoji(CFG.click_emoji);
    m.style.left = Math.random() * 96 + "vw";
    m.style.animationDuration = 2.2 + Math.random() * 1.8 + "s";
    document.body.appendChild(m);
    setTimeout(() => m.remove(), 4500);
  }, i * 90);
}
$("title").addEventListener("click", confetti);
$("mascot-img").addEventListener("click", () => { confetti(); mascotSay(); });

// ---------- wall mode: ?wall in the URL, the 🖥 button, or the W key ----------
let idleTimer, reloadTimer;
const isWall = () => document.body.classList.contains("wall");
function wake() {   // in wall mode, show the cursor (and the button) while the mouse moves; hide them after 3 s of stillness
  document.body.classList.remove("idle");
  clearTimeout(idleTimer);
  if (isWall()) idleTimer = setTimeout(() => document.body.classList.add("idle"), 3000);
}
function setWall(on) {
  document.body.classList.toggle("wall", on);
  $("wall-btn").title = on ? "Leave wall mode (W)" : "Wall mode for a TV or spare screen (W)";
  $("wall-btn").setAttribute("aria-pressed", on);
  const url = new URL(location.href);
  if (on) url.searchParams.set("wall", ""); else url.searchParams.delete("wall");
  history.replaceState(null, "", url.pathname + url.search.replace("wall=", "wall").replace(/\?$/, "") + url.hash);
  clearTimeout(reloadTimer);
  if (on) reloadTimer = setTimeout(() => location.reload(), 6 * 3600 * 1000);   // wall display: fresh page every 6 h clears browser memory
  wake();
  tick();
}
["mousemove", "mousedown", "touchstart", "keydown"].forEach(e => addEventListener(e, wake, {passive: true}));
$("wall-btn").addEventListener("click", () => setWall(!isWall()));
addEventListener("keydown", e => { if (e.key.toLowerCase() === "w" && noTyping(e)) setWall(!isWall()); });
if (/[?&#]wall\b/.test(location.search + location.hash)) setWall(true);

// header clock (wall mode shows weekday + minutes only, so it only really changes once a minute there)
function tick() {
  setText("p-time", isWall() ? new Date().toLocaleString([], {weekday: "short", hour: "numeric", minute: "2-digit"}) : new Date().toLocaleTimeString());
}

// ---------- CPU / memory breakdown: live treemap (opens from the ⚡ breakdown button or the CPU tile) ----------
// U.hist keeps the last 60 samples per group for the sparkline in the detail pane.
const U = {mode: "cpu", sel: null, data: null, hist: {}, open: false};
const ukey = g => g.kind + ":" + g.name;
const UPAL = ["#6366f1", "#8b5cf6", "#ec4899", "#e11d48", "#ea580c", "#0d9488", "#0284c7", "#16a34a", "#a21caf", "#2563eb", "#c026d3", "#0891b2", "#7c3aed", "#db2777"];
const UHOST = ["#64748b", "#6b7c95", "#57708a", "#7a7f9e", "#5f7a7a"];
function hue(str) { let h = 0; for (const c of str) h = (h * 31 + c.charCodeAt(0)) >>> 0; return h % 360; }
function ucolor(g) {   // stable colour per name: bright for containers, greys for host programs and the kernel
  if (g.kind === "kernel" || g.kind === "other") return "#6b7080";
  const h = hue(g.name);
  return g.kind === "container" ? UPAL[h % UPAL.length] : UHOST[h % UHOST.length];
}
function usageData(u) {
  const first = !U.data;
  U.data = u;
  if (first && /[?&]breakdown\b/.test(location.search)) setTimeout(openUsage, 0);   // deep link: /?breakdown
  for (const g of u.groups) {
    const a = U.hist[ukey(g)] || (U.hist[ukey(g)] = []);
    a.push(U.mode === "mem" ? g.mem : g.cpu);
    if (a.length > 60) a.shift();
  }
  if (U.open) renderUsage();
}
// Squarified treemap: lay rectangles out in rows that keep each tile as close to square as possible.
function squarify(items, x, y, w, h) {
  const out = [], total = items.reduce((a, b) => a + b.v, 0);
  if (!total || w <= 0 || h <= 0) return out;
  const scale = w * h / total, rest = items.map(i => ({...i, a: i.v * scale}));
  while (rest.length) {
    const side = Math.min(w, h), row = [];
    let worst = Infinity;
    while (rest.length) {   // keep adding tiles to this row while it makes the worst aspect ratio better
      const next = [...row, rest[0]], sum = next.reduce((a, b) => a + b.a, 0);
      const mx = Math.max(...next.map(r => r.a)), mn = Math.min(...next.map(r => r.a));
      const ratio = Math.max(side * side * mx / (sum * sum), sum * sum / (side * side * mn));
      if (row.length && ratio > worst) break;
      row.push(rest.shift());
      worst = ratio;
    }
    const sum = row.reduce((a, b) => a + b.a, 0), thick = sum / side;
    let off = 0;
    for (const r of row) {
      const len = r.a / thick;
      out.push(w >= h ? {...r, x: x, y: y + off, w: thick, h: len} : {...r, x: x + off, y: y, w: len, h: thick});
      off += len;
    }
    if (w >= h) { x += thick; w -= thick; } else { y += thick; h -= thick; }
  }
  return out;
}
const fmtCpu = (pct, n) => `${pct.toFixed(pct < 10 ? 1 : 0)}%` + (n ? ` · ${(pct * n / 100).toFixed(2)} cores` : "");
function spark(vals) {
  if (!vals || vals.length < 2) return "";
  const mx = Math.max(...vals, 0.0001), W = 260, H = 46;
  const pts = vals.map((v, i) => `${(i / (vals.length - 1) * W).toFixed(1)},${(H - v / mx * (H - 4) - 2).toFixed(1)}`).join(" ");
  return `<svg viewBox="0 0 ${W} ${H}" class="spark" preserveAspectRatio="none"><polyline points="${pts}" fill="none" stroke="currentColor" stroke-width="2"/></svg>`;
}
function renderUsage() {
  const u = U.data;
  if (!u) return;
  const mem = U.mode === "mem", val = g => mem ? g.mem : g.cpu;
  const groups = u.groups.filter(g => val(g) > (mem ? 1024 * 1024 : 0.005));
  const busy = u.groups.reduce((a, g) => a + g.cpu, 0), memSum = u.groups.reduce((a, g) => a + g.mem, 0);
  setText("u-what", mem ? "memory" : "CPU");
  setText("u-sum", mem ? `${size(memSum)} in programs` : `${busy.toFixed(1)}% busy · ${(busy * u.ncpu / 100).toFixed(1)} of ${u.ncpu} threads`);

  // tiles are reused between refreshes (matched by data-k), so they glide to their new size instead of flickering
  const box = $("u-map"), rects = squarify(groups.map(g => ({g, v: val(g)})).sort((a, b) => b.v - a.v), 0, 0, box.clientWidth, box.clientHeight);
  const seen = new Set();
  for (const r of rects) {
    const k = ukey(r.g);
    seen.add(k);
    let el = box.querySelector(`[data-k="${CSS.escape(k)}"]`);
    if (!el) {
      el = document.createElement("button");
      el.className = "u-tile";
      el.dataset.k = k;
      el.style.left = r.x + r.w / 2 + "px";   // new tiles grow out of their centre
      el.style.top = r.y + r.h / 2 + "px";
      box.appendChild(el);
    }
    Object.assign(el.style, {left: r.x + "px", top: r.y + "px", width: Math.max(r.w - 3, 0) + "px", height: Math.max(r.h - 3, 0) + "px", background: ucolor(r.g)});
    el.classList.toggle("sel", U.sel === k);
    el.classList.toggle("tiny", r.w < 70 || r.h < 34);
    el.title = `${r.g.name} (${r.g.kind})\n${mem ? size(r.g.mem) : fmtCpu(r.g.cpu, u.ncpu)}`;
    setHTML(el, `<b>${esc(r.g.name)}</b><span>${mem ? size(r.g.mem) : fmtCpu(r.g.cpu)}</span>` + (r.h > 70 && r.g.top && r.g.top[0] ? `<small>${esc(r.g.top[0].name)}</small>` : ""));
  }
  box.querySelectorAll(".u-tile").forEach(el => { if (!seen.has(el.dataset.k)) el.remove(); });
  if (!rects.length) box.innerHTML = `<div class="empty" style="padding:20px">Nothing measurable right now.</div>`;

  // side list (top 25) and the detail pane for the selected tile
  const list = [...u.groups].sort((a, b) => val(b) - val(a)).slice(0, 25), top = val(list[0] || {cpu: 1, mem: 1}) || 1;
  setHTML("u-list", list.map(g => `<button class="u-row ${U.sel === ukey(g) ? "sel" : ""}" data-k="${esc(ukey(g))}">
      <i style="background:${ucolor(g)}"></i><span class="n">${esc(g.name)}</span><span class="v">${mem ? size(g.mem) : fmtCpu(g.cpu)}</span>
      <span class="b"><i style="width:${(100 * val(g) / top).toFixed(1)}%;background:${ucolor(g)}"></i></span></button>`).join(""));
  const g = u.groups.find(x => ukey(x) === U.sel);
  setHTML("u-detail", g ? `<h3><i style="background:${ucolor(g)}"></i>${esc(g.name)} <small class="mute">${esc(g.kind === "process" ? "host program" : g.kind)}</small></h3>
      ${kvRow("CPU", fmtCpu(g.cpu, u.ncpu))}${kvRow("Memory", size(g.mem))}${kvRow("Processes", g.procs)}
      <div class="u-spark" style="color:${ucolor(g)}">${spark(U.hist[ukey(g)])}<small class="mute">${mem ? "memory" : "CPU"}, last few minutes</small></div>
      ${g.top && g.top.length ? `<div class="mute" style="font-size:12px;margin-top:8px">busiest inside</div>` + g.top.map(t => kvRow(esc(t.name), fmtCpu(t.cpu))).join("") : ""}`
    : `<div class="mute">Click a tile or a row for details. Bigger tile = more ${mem ? "memory" : "CPU"}.</div>`);
}
function openUsage() {
  U.open = true;
  $("usage-ov").hidden = false;
  document.body.style.overflow = "hidden";
  requestAnimationFrame(() => requestAnimationFrame(renderUsage));   // measure the map after the window has laid out
  loadSystem();
}
function closeUsage() {
  U.open = false;
  $("usage-ov").hidden = true;
  unlockScroll();
}
// the page scrolls again only when no window (breakdown, live, security details) is still open
function unlockScroll() { if (!document.querySelector(".usage-ov:not([hidden])")) document.body.style.overflow = ""; }
$("usage-btn").addEventListener("click", openUsage);
$("k-sys").addEventListener("click", e => { if (U.data) { e.preventDefault(); openUsage(); } });
$("usage-close").addEventListener("click", closeUsage);
$("usage-ov").addEventListener("click", e => {
  if (e.target === $("usage-ov")) return closeUsage();   // click on the dimmed backdrop
  const t = e.target.closest("[data-k]");
  if (t) { U.sel = U.sel === t.dataset.k ? null : t.dataset.k; renderUsage(); }
  const m = e.target.closest("[data-um]");                 // CPU / Memory tabs
  if (m) {
    U.mode = m.dataset.um;
    U.hist = {};
    document.querySelectorAll("[data-um]").forEach(b => b.classList.toggle("on", b === m));
    $("u-map").innerHTML = "";
    renderUsage();
  }
});
addEventListener("keydown", e => { if (e.key === "Escape" && U.open) closeUsage(); });
// redraw whenever the map's real size changes (window resize, fonts/scrollbar settling after it opens)
if ("ResizeObserver" in window) new ResizeObserver(() => { if (U.open) renderUsage(); }).observe($("u-map"));
else addEventListener("resize", () => { if (U.open) renderUsage(); });

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

// ---------- Security & backups details (click a box's title on the card, or /?security=ssh) ----------
// Read-only views of what the root collector found: /api/security/detail returns its sec-detail.json (rewritten every
// minute). Reuses the breakdown window's frame. Refetches every 30 s while open; ↻ loads the latest check right away.
const SD = {open: false, tab: "fw", data: null, timer: 0, seq: 0, err: "", opener: null};
const SD_TABS = [["fw", "Firewall & fail2ban", "firewall"], ["ssh", "SSH logins", "ssh"], ["auth", "Authelia", "authelia"],
                 ["procs", "Process watch", "procs"], ["cron", "Cron jobs", "cron"], ["locks", "Immutable files", "locks"], ["backups", "Backups", "backups"]];
const SD_KEY = Object.fromEntries(SD_TABS.map(t => [t[0], t[2]]));   // tab -> section of the collector's data
const sdFwOn = fw => !fw || /^(active|running)/i.test(fw.status);
function sdLink(tab, title) { return `<a class="sd-open" href="?security=${tab}" data-sd="${tab}" title="Show the details">${title}<i aria-hidden="true">›</i></a>`; }
const sdWhen = ts => ts ? new Date(ts * 1000).toLocaleString([], {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"}) : "–";
const sdAgo = ts => ts ? ago2(ts) : "never";
const sdDur = s => s == null ? "–" : s < 90 ? `${Math.round(s)} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`;
const sdChip = (cls, txt) => `<span class="chip ${cls}">${txt}</span>`;
const sdSec = (title, sub, body, cls) => `<section class="sd-sec ${cls || ""}"><h3>${title}${sub ? ` <small>${sub}</small>` : ""}</h3>${body}</section>`;
const sdNone = txt => `<div class="mute sd-empty">${txt}</div>`;
// a table that turns into stacked cards on a phone (each cell shows its column name)
function sdTable(head, rows, empty) {
  if (!rows.length) return sdNone(empty || "nothing here");
  return `<div class="sd-tw"><table class="sd-t"><thead><tr>${head.map(h => `<th>${h}</th>`).join("")}</tr></thead><tbody>` +
    rows.map(r => `<tr class="${r.cls || ""}">${r.cells.map((c, i) => `<td data-l="${esc(head[i])}">${c ?? ""}</td>`).join("")}</tr>`).join("") +
    "</tbody></table></div>";
}
// the yellow "not on your list" tag; with the settings page on, clicking it offers to put that IP on the known list
const sdAddChip = (ip, cls, txt) => CFG.settings_ui ? `<button type="button" class="chip ${cls} sd-add" data-ip="${esc(ip)}"
  title="Add ${esc(ip)} to your known IPs">${txt} <b aria-hidden="true">+</b></button>` : sdChip(cls, txt);
const sdIP = (ip, item) => `<span class="mono">${esc(ip)}</span>${item && item.known ? ` ${sdChip("good", "known")}` : ""}`;

// [class, text] for a tab's header chip and the little dot on its tab
function sdState(tab, d) {
  const x = d && d[SD_KEY[tab]];
  if (tab === "fw") {
    const fw = d.firewall, jails = (d.fail2ban || {}).jails || [];
    if (!fw && !jails.length) return ["", "not checked"];
    if (fw && fw.error) return ["bad", "check failed"];
    const on = sdFwOn(fw), banned = jails.reduce((n, j) => n + j.banned_now, 0);
    return [on ? "good" : "bad", `${fw ? (on ? "✓ " : "✗ ") + esc(fw.name + " " + fw.status) : "fail2ban"} · ${banned} banned`];
  }
  if (!x) return ["", "not checked"];
  if (x.error) return ["bad", "check failed"];
  if (tab === "ssh") {
    // red only when the card is red too (an unknown login it lists that you haven't marked fine); the rest is a note
    const unk = x.logins.filter(l => flagged(l) && l.on_card).length, other = (x.sources || []).filter(g => !g.known);
    return unk ? ["bad", `✗ ${unk} login${unk > 1 ? "s" : ""} from IPs not on your list`]
      : other.length ? ["mid", `${other.length} login source${other.length > 1 ? "s" : ""} not on your list (7 days)`]
      : ["good", `✓ all logins known · ${x.attempts_24h} failed tries in 24 h`];
  }
  if (tab === "auth") return (x.bans || []).length ? ["bad", `✗ ${x.bans.length} locked out`] : x.fail_24h ? ["mid", `${x.fail_24h} failed in 24 h`] : ["good", "✓ no failures in 24 h"];
  if (tab === "procs") {
    const w = x.watcher;
    if (x.found.length) return ["bad", "✗ FOUND: " + esc(x.found.join(", "))];
    if (w && w.late) return ["mid", `${esc(w.label)} hasn't run lately`];
    return w && w.alert_count ? ["mid", `clean now · ${w.alert_count} past alert${w.alert_count > 1 ? "s" : ""}`] : ["good", "✓ clean"];
  }
  if (tab === "cron") return x.unapproved || x.removed ? ["bad", `✗ ${x.unapproved} not approved${x.removed ? ` · ${x.removed} removed` : ""}`] : ["good", "✓ as approved"];
  if (tab === "locks") {
    const bad = x.files.filter(f => !f.locked || !f.exists).length;
    return bad ? ["bad", `✗ ${bad} lock${bad > 1 ? "s" : ""} missing`] : ["good", `✓ all ${x.files.length} locked`];
  }
  if (tab === "backups") {
    const stale = x.jobs.filter(j => j.stale).length, errs = x.run && x.run.errors.length;
    return errs ? ["bad", "✗ last run had errors"] : stale ? ["bad", `✗ ${stale} job${stale > 1 ? "s" : ""} with a problem`] : ["good", `✓ all ${x.jobs.length} jobs on time`];
  }
  return ["", ""];
}

function sdFirewall(d) {
  const fw = d.firewall, f = d.fail2ban, out = [];
  if (fw && !fw.error) {
    const by = {};
    fw.rules.forEach(r => by[r.action] = (by[r.action] || 0) + 1);
    out.push(sdSec(esc(fw.name), `${fw.rules.length} rules`,
      kvRow("Status", sdChip(sdFwOn(fw) ? "good" : "bad", esc(fw.status))) +
      (fw.default ? kvRow("Default policy", esc(fw.default)) : "") + (fw.logging ? kvRow("Logging", esc(fw.logging)) : "") +
      kvRow("Rules", Object.entries(by).map(([a, n]) => `${n} × ${esc(a)}`).join(" · ") || "none") +
      (fw.rules.length ? `<details class="sd-more"${fw.rules.length <= 12 ? " open" : ""}><summary>all rules</summary>` +
        sdTable(["To", "Action", "From", "Note"], fw.rules.map(r => ({cls: /DENY|REJECT/.test(r.action) ? "dim" : "",
          cells: [`<span class="mono">${esc(r.to)}</span>`, esc(r.action), `<span class="mono">${esc(r.from)}</span>`, esc(r.note)]}))) + "</details>" : "") +
      (fw.zones ? `<pre class="mono sd-pre">${esc(fw.zones)}</pre>` : "")));
  } else if (fw && fw.error) out.push(sdSec("Firewall", "", `<div class="sd-alert">${esc(fw.error)}</div>`));
  if (f && !f.error) {
    out.push(sdSec("fail2ban jails", `${f.jails.length} jail${f.jails.length === 1 ? "" : "s"}`,
      sdTable(["Jail", "Banned now", "Banned total", "Failing now", "Failures total"],
        f.jails.map(j => ({cells: [esc(j.jail), esc(j.banned_now), esc(j.banned_total), esc(j.failed_now), esc(j.failed_total)]})), "no jails running") +
      f.jails.filter(j => j.ips.length).map(j => `<div class="sd-note">Banned right now in ${esc(j.jail)}:</div>
        <div class="chiplist sd-ips">${j.ips.map(ip => `<span class="chip mono">${esc(ip)}</span>`).join("")}</div>`).join("")));
    out.push(sdSec("Recent bans", "last 7 days", sdTable(["When", "Jail", "What", "IP"],
      (f.events || []).map(e => ({cells: [sdWhen(e.time), esc(e.jail), esc(e.action), sdIP(e.ip)]})), "no bans or unbans in 7 days")));
    if ((f.found_7d || []).length) out.push(sdSec("Most failures seen by fail2ban", "last 7 days", sdTable(["IP", "Jail", "Failures", "Last"],
      f.found_7d.map(x => ({cells: [sdIP(x.ip), esc(x.jail), esc(x.count), sdAgo(x.last)]})))));
  } else if (f && f.error) out.push(sdSec("fail2ban", "", `<div class="sd-alert">${esc(f.error)}</div>`));
  return out.join("") || sdNone("The firewall and fail2ban checks are switched off in collector.toml.");
}

function sdSSH(x) {
  const unk = x.logins.filter(l => flagged(l) && l.on_card), other = (x.sources || []).filter(g => !g.known);
  const newIPs = [...new Set([...(x.sources || []), ...x.logins].filter(g => !g.known && g.ip).map(g => g.ip))];   // what "Add all" adds
  return (unk.length ? `<div class="sd-alert">⚠ ${unk.length} login${unk.length > 1 ? "s" : ""} from an IP that isn't on your known list. If that wasn't you, act now.</div>` : "") +
    sdSec("Who logged in", `${x.login_count ?? x.logins.length} logins in 7 days, by user, IP and key`, sdTable(["User", "From", "Key", "Logins", "Last", "First"],
      (x.sources || []).map(g => ({cls: g.known ? "" : "warn", cells: [esc(g.user),
        `${sdIP(g.ip, g)}${g.known ? "" : " " + sdAddChip(g.ip, "mid", "not on your list")}`, esc(g.key || g.method), esc(g.count), sdWhen(g.last), sdWhen(g.first)]})),
      "no logins in 7 days") + (other.length ? `<div class="sd-note">Not on your list: check the Key column. Your own key from a new
      network (a phone on mobile data) is expected; a key you don't recognise is not. Fixed places you use can go on the known IPs list: click the tag next to the IP.${CFG.settings_ui && newIPs.length ?
      ` <button type="button" class="sd-add-all" data-ips="${esc(newIPs.join(" "))}">Add all ${newIPs.length} to known IPs</button>` : ""}</div>` : "")) +
    sdSec("Latest logins", "newest first", sdTable(["When", "User", "From", "Key"],
      x.logins.slice(0, 15).map(l => ({cls: flagged(l) ? (l.on_card ? "bad" : "warn") : "", cells: [sdWhen(l.time), esc(l.user),
        `${sdIP(l.ip, l)}${!l.known ? " " + sdAddChip(l.ip, l.acked ? "mid" : l.on_card ? "bad" : "mid", l.acked ? "not on list · marked fine" : "NOT on your list") : ""}`, esc(l.key || l.method)]})),
      "no logins in 7 days")) +
    sdSec("Failed attempts by IP", `${x.attempts_24h} tries in 24 h · ${x.attempts_7d} in 7 days · ${x.failed_ip_count} IPs`,
      sdTable(["IP", "Tries (7 d)", "24 h", "Scans", "Users tried", "Last"], x.failed_ips.map(g => ({cells: [sdIP(g.ip, g),
        esc(g.attempts), esc(g.attempts_24h), esc(g.probes), esc((g.users || []).join(", ")) || "–", sdAgo(g.last)]})), "no failed attempts in 7 days") +
      `<div class="sd-note">A try = one connection that offered a user name. A scan = a connection that left before that.${
        x.truncated ? " The log was very busy: only its newest 100,000 lines were read." : ""}</div>`);
}

function sdAuth(x) {
  const bans = x.bans || [];
  return (bans.length ? `<div class="sd-alert">⚠ ${bans.length} locked out right now</div>` : "") +
    `<div class="sd-grid">` + sdSec("Sign-ins", "a 2FA sign-in counts once", kvRow("Last 24 h", `${esc(x.ok_24h)} ok · <span class="${x.fail_24h ? "warn-t" : ""}">${esc(x.fail_24h)} failed</span>`) +
      kvRow("Last 7 days", `${esc(x.ok_7d)} ok · <span class="${x.fail_7d ? "warn-t" : ""}">${esc(x.fail_7d)} failed</span>`)) +
    sdSec("Locked out", "Authelia's own bans", sdTable(["Who", "Since", "Until", "Why"], bans.map(b => ({cls: "bad",
      cells: [`${esc(b.who)} <small class="mute">${esc(b.kind)}</small>`, sdWhen(b.since), b.until ? sdWhen(b.until) : "until lifted", esc(b.reason || b.source)]})),
      "nobody is locked out ✓")) + `</div>` +
    `<div class="sd-grid">` + sdSec("Failures by user", "7 days", sdTable(["User", "Failures", "Last"],
      (x.fail_by_user || []).map(g => ({cells: [esc(g.key), esc(g.count), sdAgo(g.last)]})), "no failures")) +
    sdSec("Failures by IP", "7 days", sdTable(["IP", "Failures", "Last"],
      (x.fail_by_ip || []).map(g => ({cells: [sdIP(g.key, g), esc(g.count), sdAgo(g.last)]})), "no failures")) + `</div>` +
    sdSec("Recent sign-ins", "newest first", sdTable(["When", "Result", "User", "Step", "From", "Site"], x.events.map(e => ({
      cls: e.ok ? "" : "warn", cells: [sdWhen(e.time), e.ok ? sdChip("good", "✓ ok") : sdChip(e.banned ? "bad" : "mid", e.banned ? "✗ banned" : "✗ failed"),
        esc(e.user), esc(e.factor), sdIP(e.ip, e), esc(e.site)]})), "no sign-ins in 7 days"));
}

function sdProcs(x) {
  const w = x.watcher, out = [];
  if (x.found.length) out.push(`<div class="sd-alert">⚠ Found running: ${esc(x.found.join(", "))}</div>`);
  if (w) out.push(sdSec(esc(w.label), `every ${esc(w.every_minutes)} min`,
    kvRow("Last run", `<span class="${w.late ? "crit-t" : ""}">${sdAgo(w.last_run)}${w.late ? " (late!)" : ""}</span>`) +
    ((w.checks || []).length ? `<div class="sd-note">What it checks:</div><ul class="sd-list">${w.checks.map(c => `<li>${esc(c)}</li>`).join("")}</ul>` : "") +
    (w.alert_count ? `<div class="sd-note">Problems it has alerted on (each one once):</div>` +
      sdTable(["Kind", "Detail"], (w.alerts || []).map(a => ({cells: [esc(a.kind), `<span class="mono">${esc(a.detail)}</span>`]})))
      : kvRow("Alerts so far", sdChip("good", "none ✓")))));
  out.push(sdSec("Dashboard check", "every minute", kvRow("Watching for", `<span class="chiplist">${x.names.map(n => `<span class="chip mono">${esc(n)}</span>`).join("")}</span>`) +
    kvRow("Found now", x.found.length ? `<span class="crit-t">${esc(x.found.join(", "))}</span>` : sdChip("good", "nothing ✓")) +
    `<div class="sd-note">Busiest processes${x.top_at ? ` (measured ${sdAgo(x.top_at)})` : ""}:</div>` +
    sdTable(["Process", "User", "CPU", "Memory", "PID"], (x.top || []).map(p => ({cells: [esc(p.name), esc(p.user), esc(Math.round(p.cpu)) + "%",
      p.mem != null ? esc(Number(p.mem).toFixed(1)) + "%" : "–", esc(p.pid)]})))));
  return out.join("");
}

function sdCron(x) {
  const files = x.files.map(f => {
    const rows = f.entries.map(e => ({cls: !e.approved && !e.setting ? "bad" : e.setting ? "dim" : "",
      cells: [`<span class="mono">${esc(e.when) || "–"}</span>`, esc(e.user) || "–", `<span class="mono">${esc(e.cmd)}</span>`,
        e.setting ? sdChip("", "setting") : e.approved ? sdChip("good", "✓ approved") : sdChip("bad", "NOT approved")]}));
    return sdSec(`<span class="mono">${esc(f.file)}</span>`, f.new_file ? "new file" : f.deleted ? "deleted" : f.user ? `crontab of ${esc(f.user)}` : "",
      sdTable(["Schedule", "User", "Command", "Status"], rows, f.deleted ? "file was deleted" : "no active lines") +
      (f.removed.length ? `<div class="sd-note">Removed since you approved:</div>` + f.removed.map(l => `<div class="mono sd-removed">− ${esc(l)}</div>`).join("") : ""));
  });
  return (x.unapproved || x.removed ? `<div class="sd-alert">⚠ Changes you haven't approved. Didn't make them? Check them (possible malware persistence). Approve on the card.</div>` : "") +
    kvRow("Approved snapshot", x.approved_at ? `${sdWhen(x.approved_at)} (${sdAgo(x.approved_at)})` : "–") + files.join("") +
    sdSec("Not covered by the approval check", "shown for information",
      (x.folders || []).map(f => `<div class="sd-note"><span class="mono">${esc(f.folder)}</span></div><div class="chiplist sd-ips">${
        f.scripts.map(sc => `<span class="chip mono" title="changed ${sdWhen(sc.mtime)}">${esc(sc.name)}</span>`).join("") || '<span class="mute">empty</span>'}</div>`).join("") +
      ((x.timers || []).length ? `<div class="sd-note">systemd timers:</div>` + sdTable(["Timer", "Runs", "Last", "Next"],
        x.timers.map(t => ({cells: [`<span class="mono">${esc(t.unit)}</span>`, `<span class="mono">${esc(t.runs)}</span>`, t.last ? sdAgo(t.last) : "–", sdWhen(t.next)]}))) : ""));
}

function sdLocks(x) {
  const bad = x.files.filter(f => !f.locked || !f.exists);
  return (bad.length ? `<div class="sd-alert">⚠ ${bad.length} file${bad.length > 1 ? "s have" : " has"} lost the lock. Did you remove it? If not, check what changed.</div>` : "") +
    sdSec("Locked files", "chattr +i: not even root can change them until the flag is removed", sdTable(["File", "Status", "Attributes", "Last changed"],
      x.files.map(f => ({cls: !f.locked || !f.exists ? "bad" : "", cells: [`<span class="mono">${esc(f.path)}</span>${f.symlink ? " " + sdChip("mid", "symlink") : ""}`,
        !f.exists ? sdChip("bad", "MISSING") : f.locked ? sdChip("good", "🔒 locked") : sdChip("bad", "✗ unlocked"),
        `<span class="mono">${esc(f.attrs) || "–"}</span>`, f.mtime ? sdWhen(f.mtime) : "–"]}))));
}

function sdBackups(x) {
  const r = x.run, out = [];
  if (r && r.errors.length) out.push(`<div class="sd-alert">⚠ The last run logged errors:<div class="mono">${r.errors.map(esc).join("<br>")}</div></div>`);
  if (r) out.push(sdSec("Last run", sdWhen(r.start), kvRow("Started", sdWhen(r.start)) + kvRow("Finished", sdWhen(r.end)) +
    kvRow("Took", sdDur(r.end - r.start)) + kvRow("Errors", r.errors.length ? `<span class="crit-t">${r.errors.length}</span>` : sdChip("good", "none ✓")) +
    (r.warning_count ? `<details class="sd-more"><summary>${r.warning_count} warning${r.warning_count > 1 ? "s" : ""}</summary><div class="mono sd-pre">${r.warnings.map(esc).join("<br>")}</div></details>` : "")));
  const job = j => {
    const it = (j.items || []).filter(i => i.name || i.size || i.took_text);
    const last = (j.items || []).slice(-1)[0], took = last && last.took_text ? esc(last.took_text) : j.took != null ? "≈ " + sdDur(j.took) : "";
    const paths = (label, list) => list && list.length ? `<div class="sd-note crit-t">${label} (${list.length}):</div><div class="sd-items">` +
      list.map(m => `<span class="chip mono" title="${esc(m)}">${esc(m.split("/").slice(-2).join("/"))}</span>`).join("") + "</div>" : "";
    const d = j.dest;
    return sdSec(esc(j.label), esc(j.every), kvRow("Status", j.stale ? sdChip("bad", "✗ " + esc((j.problems || ["problem"]).join(" · "))) : sdChip("good", "✓ on time")) +
      kvRow("Last success", j.last_ok ? `${sdWhen(j.last_ok)} <small class="mute">(${sdAgo(j.last_ok)})</small>` : "–") +
      (took ? kvRow("Took", took) : "") +
      (it.length ? `<div class="sd-items">${it.map(i => `<span class="chip">${esc(i.name || "")}${i.name && i.size ? " · " : ""}${esc(i.size || "")}${
        i.took_text ? " · " + esc(i.took_text) : ""}</span>`).join("")}</div>` : "") +
      ((j.fail_lines || []).length ? `<div class="sd-note crit-t">From the last run's log:</div>` + j.fail_lines.map(l => `<div class="mono sd-removed">${esc(l)}</div>`).join("") : "") +
      (j.files ? kvRow("Files", `${esc(j.files.count)} · ${size(j.files.bytes)} · newest ${sdAgo(j.files.newest)}${
        j.files.count > 1 ? ` · oldest ${sdAgo(j.files.oldest)}` : ""}`) : "") +
      paths("Missing", j.missing) + paths(`Older than ${esc(j.max_age_hours)} h`, j.old_files) +
      (d && d.missing ? kvRow("Destination", `<span class="crit-t">${esc(d.path)} missing</span>`) : "") +
      (d && !d.missing ? kvRow("Destination", `<span class="mono">${esc(d.path)}</span>`) +
        (d.size ? kvRow("Backup size", d.size.bytes != null ? `${size(d.size.bytes)} <small class="mute">(measured ${sdAgo(d.size.at)})</small>` : "–")
          : "size" in d ? kvRow("Backup size", '<small class="mute">measured after tonight\'s run</small>') : "") +
        kvRow("Drive free", `${size(d.free)} of ${size(d.total)}`) +
        `<div class="meter"><i class="${d.free / d.total < .05 ? "crit" : d.free / d.total < .12 ? "warn" : ""}" style="width:${(100 - 100 * d.free / d.total).toFixed(1)}%"></i></div>` : "") +
      (j.remote ? `<div class="sd-note">${esc(j.remote)}</div>` : ""), j.stale ? "sd-bad" : "");
  };
  out.push(`<div class="sd-grid">${x.jobs.map(job).join("")}</div>`);
  return out.join("");
}

function renderSec() {
  const d = SD.data, tabFocus = $("sd-tabs").contains(document.activeElement);
  setHTML("sd-tabs", SD_TABS.map(([k, label]) => {
    const st = d && !d.missing ? sdState(k, d) : ["", ""];
    return `<button role="tab" aria-selected="${k === SD.tab}" class="${k === SD.tab ? "on" : ""}" data-sdt="${k}">` +
      `<i class="dot ${st[0] === "good" ? "up" : st[0] === "bad" ? "down" : st[0] === "mid" ? "warn" : ""}"></i>${esc(label)}</button>`;
  }).join(""));
  const on = $("sd-tabs").querySelector(".on");          // the buttons were just rebuilt: keep the selected one in view + focused
  if (on) { on.scrollIntoView({block: "nearest", inline: "nearest"}); if (tabFocus) on.focus(); }
  const label = (SD_TABS.find(t => t[0] === SD.tab) || SD_TABS[0])[1];
  setText("sd-title", label);
  if (!d) return setHTML("sd-body", sdNone(SD.err ? "Couldn't load: " + esc(SD.err) : "loading…"));
  if (d.missing) {
    setHTML("sd-status", "");
    return setHTML("sd-body", sdNone(d.error ? esc(d.error)
      : "No details yet: the collector writes them (sec-detail.json) once its newer version is installed."));
  }
  const st = sdState(SD.tab, d), sec = d[SD_KEY[SD.tab]];
  setHTML("sd-status", st[1] ? sdChip(st[0], st[1]) : "");
  const checked = SD.tab === "fw" ? Math.max((d.firewall || {}).checked || 0, (d.fail2ban || {}).checked || 0) : sec && sec.checked;
  setHTML("sd-when", `<i></i>${SD.err ? "Reconnecting… " : ""}checked ${checked ? ago2(checked) : "–"}`);
  $("sd-when").className = "lv-live" + (SD.err || d.stale ? " lv-off" : "");
  $("sec-ov").classList.toggle("lv-stale", !!(SD.err || d.stale));
  let body;
  if (SD.tab === "fw") body = sdFirewall(d);
  else if (!sec) body = sdNone("This check is switched off in collector.toml.");
  else if (sec.error) body = `<div class="sd-alert">The collector couldn't run this check: ${esc(sec.error)}</div>`;
  else body = {ssh: sdSSH, auth: sdAuth, procs: sdProcs, cron: sdCron, locks: sdLocks, backups: sdBackups}[SD.tab](sec);
  if (d.stale) body = `<div class="sd-alert">The collector hasn't written new details for a few minutes: this may be out of date.</div>` + body;
  // keep open dropdowns (all rules, warnings) open across refreshes
  const open = [...$("sd-body").querySelectorAll("details[open] summary")].map(x => x.textContent);
  setHTML("sd-body", body);
  $("sd-body").querySelectorAll("details summary").forEach(x => { if (open.includes(x.textContent)) x.parentElement.open = true; });
}
async function loadSec() {
  clearTimeout(SD.timer);
  const seq = ++SD.seq;                                     // open, ↻ and a tab coming back can overlap: newest call wins
  let d;
  try { d = await getJSON("/api/security/detail"); }
  catch (e) { if (seq === SD.seq) SD.err = e.message; }    // keep the last good view, dimmed
  if (seq !== SD.seq) return;
  if (d) { SD.data = d; SD.err = ""; }
  try { if (SD.open) renderSec(); }
  catch (e) { setHTML("sd-body", sdNone("Couldn't show this view: " + esc(e.message))); }   // e.g. dash and collector versions differ
  clearTimeout(SD.timer);
  if (SD.open && !document.hidden) SD.timer = setTimeout(loadSec, 30000);
}
function openSec(tab) {
  SD.tab = Object.hasOwn(SD_KEY, tab) ? tab : "fw";
  if (!SD.open) SD.opener = document.activeElement;
  SD.open = true;
  $("sec-ov").hidden = false;
  document.body.style.overflow = "hidden";
  $("sd-body").scrollTop = 0;
  renderSec();
  $("sec-close").focus();
  loadSec();
}
function closeSec() {
  SD.open = false;
  SD.seq++;
  clearTimeout(SD.timer);
  $("sec-ov").hidden = true;
  unlockScroll();
  if (SD.opener && SD.opener.isConnected) SD.opener.focus();
}
$("sec").addEventListener("click", e => {
  const a = e.target.closest("[data-sd]");
  if (a && !e.ctrlKey && !e.metaKey && !e.shiftKey) { e.preventDefault(); openSec(a.dataset.sd); }
});
$("sd-tabs").addEventListener("click", e => {
  const b = e.target.closest("[data-sdt]");
  if (b) { SD.tab = b.dataset.sdt; $("sd-body").scrollTop = 0; renderSec(); }
});
$("sd-body").addEventListener("click", async e => {
  const b = e.target.closest(".sd-add, .sd-add-all");
  if (!b || b.disabled) return;
  const all = b.classList.contains("sd-add-all"), ip = b.dataset.ip;
  const what = all ? `these ${b.dataset.ips.split(" ").length} IPs to your known IPs?\n\n${b.dataset.ips.split(" ").join("\n")}` : `${ip} to your known IPs?`;
  if (!confirm(`Add ${what}\n\nLogins from ${all ? "them" : "it"} will show as "known" from now on. Only do this for places you trust (home, work), not a phone on mobile data: those IPs change.`)) return;
  b.disabled = true;
  try { await postJSON("/api/security/known", all ? {all: true} : {ip}); await loadSec(); loadSystem(); }
  catch (err) { b.disabled = false; b.textContent = "failed: " + err.message; }
});
$("sd-refresh").addEventListener("click", async e => {
  e.target.disabled = true;
  await loadSec();
  setTimeout(() => e.target.disabled = false, 800);
});
$("sec-close").addEventListener("click", closeSec);
$("sec-ov").addEventListener("click", e => { if (e.target === $("sec-ov")) closeSec(); });   // click on the backdrop
addEventListener("keydown", e => { if (e.key === "Escape" && SD.open) closeSec(); });
document.addEventListener("visibilitychange", () => { if (!document.hidden && SD.open) loadSec(); });
{ const m = location.search.match(/[?&]security(?:=(\w+))?(?=&|$)/); if (m) openSec(m[1] || "fw"); }   // deep link: /?security=ssh

// ---------- polling ----------
// Each panel refreshes on its own timer. A refresh never overlaps the previous one (a slow upstream can't pile up
// requests), timers stop while the tab is hidden, and everything refreshes at once when it's shown again.
const pollers = [];
function poll(fn, ms) {
  const p = {timer: 0, busy: false};
  p.run = async () => {
    clearTimeout(p.timer);
    if (p.busy) return;                 // still running: it reschedules itself when done
    p.busy = true;
    try { await fn(); } finally { p.busy = false; }
    if (!document.hidden) p.timer = setTimeout(p.run, ms);
  };
  pollers.push(p);
  p.run();
}
document.addEventListener("visibilitychange", () => pollers.forEach(p => document.hidden ? clearTimeout(p.timer) : p.run()));

(async () => {
  try { CFG = await getJSON("/api/config"); }
  catch (e) { setHTML("svc", `<div class="err">config: ${esc(e.message)}</div>`); }
  applyConfig();
  if (SD.open) renderSec();   // a /?security= deep link drew before the config (settings_ui) arrived
  bubbles();
  poll(loadSystem, 3000);     // CPU/RAM/temps/drives/security (the collector writes every 5 s)
  poll(loadQbit, 5000);
  poll(loadPlex, 10000);
  poll(loadServices, 30000);  // the server checks every service at most every 25 s anyway
  poll(loadRequests, 30000);
  tick();
  setInterval(tick, 1000);
  setInterval(mascotSay, 20000);
})();
