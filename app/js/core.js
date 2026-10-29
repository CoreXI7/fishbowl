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
  // panels switched off on the settings page: a class per panel, the CSS hides it (style.css, "hidden panels")
  const hidden = new Set(CFG.hidden_panels || []);
  hidden.forEach(p => document.documentElement.classList.add("no-" + p));
  const duo = document.querySelector(".duo"), plexOn = CFG.features.plex && !hidden.has("plex"), reqOn = CFG.features.seerr && !hidden.has("requests");
  if (!plexOn && !reqOn) duo.hidden = true;
  else if (!plexOn || !reqOn) duo.style.gridTemplateColumns = "minmax(0,1fr)";
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

