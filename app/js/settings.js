const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const CDN = "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons@main/png/";
const IMG_TYPES = "image/png,image/jpeg,image/webp,image/gif";
let V = null, S = null, SEC = {}, dirty = false;

async function api(url, opts = {}) {
  const r = await fetch(url, {credentials: "same-origin", ...opts});
  if (r.redirected || (r.headers.get("content-type") || "").indexOf("json") < 0) { location.reload(); throw new Error("session expired"); }
  const j = await r.json();
  if (!r.ok) throw Object.assign(new Error(j.error || r.status), {section: j.section});
  return j;
}
const post = (url, body) => api(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
const upload = f => api("/api/upload", {method: "POST", headers: {"Content-Type": "application/octet-stream"}, body: f});

// the header's height, for the sticky menu and for where a section scrolls to
const setHH = () => document.documentElement.style.setProperty("--hh", document.querySelector("header").offsetHeight + "px");
setHH(); addEventListener("resize", setHH);

// ---------- unsaved changes: the Save bar, and a dot on each section in the menu that has some ----------
const SECTION_NAMES = {setup: "Setup", look: "Look & feel", themes: "Themes", services: "Services", integrations: "Integrations",
                       security: "Security", about: "About"};
const PERSONALITY = ["tagline", "tagline_down", "click_emoji", "greeting", "quotes", "mascot", "mascot_credit", "favicon"];
const slices = d => ({   // which part of the settings each section edits (for the unsaved-changes dots)
  look: [d.site.title, d.site.theme, d.site.hidden_panels],
  themes: [PERSONALITY.map(k => d.site[k]), d.site.themes],
  services: [d.groups, d.apps, d.services, d.skip_hosts, d.via_proxy],
  integrations: [d.qbittorrent, d.seerr, d.tautulli],
  security: [d.known_ips],
});
function changedSections() {
  if (!dirty) return [];
  const a = slices(S), b = slices(V.settings);
  const out = Object.keys(a).filter(k => JSON.stringify(a[k]) !== JSON.stringify(b[k]));
  if (Object.values(SEC).some(Boolean) && !out.includes("integrations")) out.push("integrations");
  return out;
}
const andList = xs => xs.length < 2 ? xs.join("") : xs.slice(0, -1).join(", ") + " and " + xs[xs.length - 1];
function setDirty(d = true) {
  dirty = d;
  const ch = changedSections();
  if (d && !ch.length) dirty = false;   // edited back to what's saved
  $("save").disabled = !dirty || !V.ui_enabled;
  document.querySelectorAll(".set-nav a").forEach(a => a.classList.toggle("unsaved", ch.includes(a.hash.slice(1))));
  $("msg").innerHTML = !V.ui_enabled ? '<span class="warn-t">Read-only: the settings page is turned off (SETTINGS_UI=0).</span>'
    : !V.writable ? '<span class="crit-t">✗ The data folder isn\'t writable, so saving will fail. See Setup.</span>'
    : dirty ? `<span class="warn-t">● Unsaved changes in ${esc(andList(ch.map(k => SECTION_NAMES[k])))}</span>`
    : '<span class="mute">All changes saved.</span>';
}
addEventListener("beforeunload", e => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });
addEventListener("keydown", e => {   // Ctrl+S / ⌘+S saves
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); if (!$("save").disabled) $("save").click(); }
});

// ---------- simple fields (data-k="site.title"; data-list = one entry per line) ----------
const getPath = (o, k) => k.split(".").reduce((a, p) => a && a[p], o);
const setPath = (o, k, v) => { const ps = k.split("."), last = ps.pop(); ps.reduce((a, p) => a[p] = a[p] || {}, o)[last] = v; };
const lines = v => v.split("\n").map(x => x.trim()).filter(Boolean);
function bindFields() {
  document.querySelectorAll("[data-k]").forEach(el => {
    const v = getPath(S, el.dataset.k);
    el.value = el.hasAttribute("data-list") ? (v || []).join("\n") : (v ?? "");
    el.disabled = !V.ui_enabled;
    el.oninput = () => { setPath(S, el.dataset.k, el.hasAttribute("data-list") ? lines(el.value) : el.value); clearErr(el); setDirty(); };
  });
  // set in the environment: shown, locked (the environment always wins)
  if (V.via_proxy_env.length) {
    const f = $("f-via");
    f.value = V.via_proxy_env.join("\n"); f.disabled = true;
    $("d-via").innerHTML = '<span class="lockt">🔒 Set by <code>CHECK_VIA_PROXY</code> in docker-compose.yml, which wins over this page.</span>';
  }
}

// checked when you leave the field (not while typing); the server checks everything again on Save
const CHECKS = {
  // IPv4: a full address or a prefix ending in a dot; IPv6 (has a colon): only rough here, the server checks the rest
  "f-known": l => /^[\d.]+$/.test(l) ? /^(\d{1,3}\.){3}\d{1,3}$|^(\d{1,3}\.){1,3}$/.test(l) : l.includes(":") && /^[0-9a-fA-F:.]{2,45}$/.test(l),
  "f-via": l => /^[A-Za-z0-9.-]{1,253}(\/[A-Za-z0-9._~%+,;=:@\/-]{0,200})?$/.test(l) && !l.split("/").slice(1).join("/").includes(".."),
};
const CHECK_MSG = {
  "f-known": l => `"${l}" isn't an IP address or a prefix ending in a dot or a colon.`,
  "f-via": l => `"${l}" isn't a host name with an optional /path (no spaces, ? or #).`,
};
document.addEventListener("focusout", e => {
  const ok = CHECKS[e.target.id];
  if (!ok || e.target.disabled) return;
  const bad = lines(e.target.value).find(l => !ok(l));
  bad ? fieldErr(e.target, CHECK_MSG[e.target.id](bad)) : clearErr(e.target);
});
function fieldErr(el, msg) {
  clearErr(el);
  const id = (el.id || "f") + "-err", m = document.createElement("p");
  m.className = "ferr"; m.id = id; m.innerHTML = `<span aria-hidden="true">✗ </span>${esc(msg)}`;
  el.setAttribute("aria-invalid", "true");
  el.setAttribute("aria-describedby", [id, el.getAttribute("aria-describedby")].filter(Boolean).join(" "));
  el.insertAdjacentElement("afterend", m);
}
function clearErr(el) {
  const m = $((el.id || "f") + "-err");
  if (!m) return;
  m.remove(); el.removeAttribute("aria-invalid");
  el.setAttribute("aria-describedby", (el.getAttribute("aria-describedby") || "").split(" ").filter(x => x !== m.id).join(" "));
}

// ---------- error summary (a failed save): what's wrong, and a link to where ----------
function showErr(msg, section) {
  const box = $("err-sum");
  box.innerHTML = `<h2>Not saved</h2><p>${esc(msg)}</p>` +
    (SECTION_NAMES[section] ? `<p><a href="#${section}">Go to ${esc(SECTION_NAMES[section])} →</a></p>` : "");
  box.hidden = false;
  box.scrollIntoView({block: "start"}); box.focus({preventScroll: true});
  // point at the field too, when the message names one this page can find
  const field = {"known IP": "f-known", "check through the proxy": "f-via", "title": "f-title"};
  for (const [k, id] of Object.entries(field)) if (msg.startsWith(k + ":")) {
    if (id === "f-via") $("adv-services").open = true;
    fieldErr($(id), msg.slice(k.length + 1).trim());
  }
  const m = msg.match(/^(qbittorrent|seerr|tautulli) URL:|^home IP:/);
  if (m) { const el = document.querySelector(`[data-k="${m[1] ? m[1] + ".url" : "qbittorrent.home_ip"}"]`); if (el) fieldErr(el, msg.split(":").slice(1).join(":").trim()); }
}
function hideErr() { $("err-sum").hidden = true; }

// ---------- Setup checklist ----------
const STATE = {problem: ["✗", "needs fixing"], warn: ["!", "worth fixing"], todo: ["○", "not set up"], ok: ["✓", "ok"]};
const ORDER = {problem: 0, warn: 1, todo: 2, ok: 3};
function codeBlock(code) {
  code = code.replaceAll("%ORIGIN%", location.origin);
  return `<div class="code"><pre><code>${esc(code)}</code></pre><button type="button" class="ibtn" data-copy="${esc(code)}">Copy</button></div>`;
}
function chk(i) {
  const [icon, word] = STATE[i.state] || STATE.todo, link = /^#[a-z]+$/.test(i.link || "") ? i.link : "";
  return `<div class="chk chk-${esc(i.state)}"><span class="chk-ic" aria-hidden="true">${icon}</span><div class="chk-b">
    <h3 class="chk-t">${esc(i.title)}<span class="vh"> (${word})</span>${i.optional ? ' <small class="mute">optional</small>' : ""}</h3>
    <p>${esc(i.detail)}</p>
    ${(i.steps || []).map(s => `<p class="chk-step">${esc(s.text)}</p>${s.code ? codeBlock(s.code) : ""}`).join("")}
    ${link && SECTION_NAMES[link.slice(1)] ? `<a class="chk-go" href="${link}">Go to ${esc(SECTION_NAMES[link.slice(1)])} →</a>` : ""}
  </div></div>`;
}
function renderSetup() {
  const items = [...(V.setup || [])].sort((a, b) => (ORDER[a.state] ?? 2) - (ORDER[b.state] ?? 2));
  const fix = items.filter(i => i.state === "problem" || i.state === "warn"), todo = items.filter(i => i.state === "todo");
  const ok = items.filter(i => i.state === "ok");
  $("nb-setup").hidden = !fix.length; $("nb-setup").textContent = fix.length || "";
  $("nb-setup").setAttribute("aria-label", `${fix.length} to fix`);
  $("setup-list").innerHTML =
    (fix.length ? `<p class="chk-sum bad"><span aria-hidden="true">⚠ </span>${fix.length} thing${fix.length > 1 ? "s" : ""} to fix</p>`
                : `<p class="chk-sum good"><span aria-hidden="true">✓ </span>All set${todo.length ? `, with ${todo.length} optional extra${todo.length > 1 ? "s" : ""} below` : ""}</p>`) +
    fix.map(chk).join("") + todo.map(chk).join("") +
    (ok.length ? `<details class="chk-ok-list"${fix.length + todo.length ? "" : " open"}><summary>${ok.length} check${ok.length > 1 ? "s" : ""} passed</summary>${ok.map(chk).join("")}</details>` : "");
}
$("setup-list").addEventListener("click", async e => {
  const b = e.target.closest("[data-copy]"); if (!b) return;
  try { await navigator.clipboard.writeText(b.dataset.copy); b.textContent = "Copied ✓"; }
  catch { const r = document.createRange(); r.selectNodeContents(b.previousElementSibling); getSelection().removeAllRanges(); getSelection().addRange(r); b.textContent = "Press Ctrl+C"; }
  setTimeout(() => { b.textContent = "Copy"; }, 2500);
});
$("setup-again").onclick = async () => {
  $("setup-again").disabled = true;
  try { V.setup = (await api("/api/settings")).setup; renderSetup(); } catch (err) { showErr("Couldn't check: " + err.message); }
  $("setup-again").disabled = false;
};

// ---------- panels (site.hidden_panels) ----------
function renderPanels() {
  const hidden = new Set(S.site.hidden_panels || []);
  $("panels").innerHTML = Object.entries(V.panels || {}).map(([id, label]) =>
    `<label class="pill"><input type="checkbox" data-panel="${esc(id)}"${hidden.has(id) ? "" : " checked"}${V.ui_enabled ? "" : " disabled"}> ${esc(label)}</label>`).join("");
}
$("panels").addEventListener("change", e => {
  const id = e.target.dataset.panel;
  if (!id) return;
  const hidden = new Set(S.site.hidden_panels || []);
  e.target.checked ? hidden.delete(id) : hidden.add(id);
  S.site.hidden_panels = Object.keys(V.panels).filter(p => hidden.has(p));
  setDirty();
});

// file pickers: a real button opens the (hidden) file input, so the keyboard can reach it
document.addEventListener("click", e => { const b = e.target.closest("[data-pick]"); if (b) $(b.dataset.pick).click(); });

// ---------- themes: the default theme, and each theme's mascot, tab icon and lines (Bubbles = the top-level fields) ----------
let edTheme = "";
function renderThemes() {
  S.site.themes = S.site.themes || {};
  const ids = Object.keys(V.themes);
  $("theme-default").innerHTML = ids.map(id => `<option value="${esc(id)}">${esc(V.themes[id].label)}</option>`).join("");
  $("theme-default").value = S.site.theme || "bubbles";
  $("theme-default").disabled = !V.ui_enabled;
  $("theme-default").onchange = () => { S.site.theme = $("theme-default").value; setDirty(); };
  // start on the theme this browser shows, so opening the page doesn't repaint it
  if (!V.themes[edTheme]) edTheme = V.themes[document.documentElement.dataset.theme] ? document.documentElement.dataset.theme : S.site.theme || ids[0];
  $("theme-tabs").innerHTML = ids.map(id => `<button type="button" aria-pressed="${id === edTheme}" class="${id === edTheme ? "on" : ""}" data-t="${esc(id)}">${esc(V.themes[id].label)}</button>`).join("");
  renderThemeEd();
}
$("theme-tabs").addEventListener("click", e => {
  const b = e.target.closest("[data-t]"); if (!b) return;
  edTheme = b.dataset.t; document.documentElement.dataset.theme = edTheme;   // live preview of the theme being edited
  renderThemes();
});
const tget = k => edTheme === "bubbles" ? S.site[k] : (S.site.themes[edTheme] || {})[k];
function tset(k, v) {
  if (edTheme === "bubbles") S.site[k] = v;
  else S.site.themes[edTheme] = {...(S.site.themes[edTheme] || {}), [k]: v};
  setDirty();
}
function imgPicker(k, label, help) {   // mascot or tab icon of the theme being edited
  const v = tget(k), id = "tp-" + k;
  return `<div class="field"><span class="flabel">${esc(label)}</span><div class="imgpick">
      ${v ? `<img src="${esc(v)}" alt="">` : ""}
      <button type="button" class="pill" data-pick="${id}"${V.ui_enabled ? "" : " disabled"}>${v ? "Replace…" : "Upload…"}</button>
      <input type="file" id="${id}" data-tup="${k}" accept="${IMG_TYPES}" hidden>
      ${v ? `<button type="button" class="ibtn danger" data-trm="${k}">Remove</button>` : ""}</div>
    <small>${esc(help)}</small></div>`;
}
function renderThemeEd() {
  const id = edTheme, def = V.themes[id], bub = id === "bubbles";
  const ph = k => bub ? "" : (def[k] ?? S.site[k] ?? "");
  const tf = (k, label, max, help) => `<div class="field"><label for="tf-${k}">${esc(label)}</label>
      <input type="text" id="tf-${k}" data-tk="${k}" maxlength="${max}" placeholder="${esc(ph(k))}">${help ? `<small>${esc(help)}</small>` : ""}</div>`;
  $("theme-ed").innerHTML = `<div class="form">
    ${imgPicker("mascot", "Mascot", "Shown in the left column" + (bub ? "" : " in this theme") + ". PNG, JPEG, WebP or GIF up to 2 MB; a transparent PNG looks best.")}
    ${tf("mascot_credit", "Art credit", 80, "")}
    ${tf("greeting", "Greeting", 200, "What the mascot says first.")}
    <div class="field"><label for="tf-quotes">Things the mascot says (one per line)</label>
      <textarea id="tf-quotes" data-tk="quotes" rows="4" placeholder="${esc(bub ? "" : (def.quotes || []).join("\n"))}"></textarea>
      <small>One at random every so often.</small></div>
    ${tf("click_emoji", "Click emoji", 16, "Rain down when you click the title or the mascot; several = a random one each time.")}
    ${tf("tagline", "Tagline when everything's up", 120, "")}
    ${tf("tagline_down", "Tagline when something's down", 120, "")}
    ${imgPicker("favicon", "Browser tab icon", "A square PNG works best." + (bub ? " Default: the bubbles." : " Default: this theme's own icon."))}
  </div>`;
  $("theme-ed").querySelectorAll("[data-tk]").forEach(el => {
    const k = el.dataset.tk, v = tget(k);
    el.value = k === "quotes" ? (v || []).join("\n") : (v || "");
    el.disabled = !V.ui_enabled;
    el.oninput = () => tset(k, k === "quotes" ? lines(el.value) : el.value);
  });
}
$("theme-ed").addEventListener("change", async e => {
  const k = e.target.dataset.tup, f = e.target.files && e.target.files[0]; if (!k || !f) return;
  if (f.size > 2 * 1024 * 1024) return showErr("That image is over 2 MB.", "themes");
  try { tset(k, (await upload(f)).url); renderThemeEd(); } catch (err) { showErr("Upload failed: " + err.message, "themes"); }
});
$("theme-ed").addEventListener("click", e => { const k = e.target.dataset.trm; if (k) { tset(k, ""); renderThemeEd(); } });

// ---------- services: groups, labels, icons, hidden ----------
const iconSrc = (host) => {
  const icon = (S.apps[host] || {}).icon || "";
  return icon ? (icon.startsWith("/") ? icon : CDN + icon + ".png") : (V.demo ? "" : "/api/icon?host=" + encodeURIComponent(host));
};
function iconFail(img) { const d = document.createElement("span"); d.className = "lt"; d.textContent = img.dataset.letter || "?"; img.replaceWith(d); }
// broken icons fall back to their letter: one capturing listener, no inline onerror (the CSP allows no inline script)
document.addEventListener("error", e => { if (e.target.tagName === "IMG" && "letter" in e.target.dataset) iconFail(e.target); }, true);
const app = host => S.apps[host] = S.apps[host] || {};
function cleanApp(host) { const a = S.apps[host]; if (a && !a.label && !a.icon && !a.url) delete S.apps[host]; }

function srow(host, gi, idx, len) {
  const a = S.apps[host] || {}, found = V.discovered.includes(host) || S.services.some(x => x.name === host);
  const opts = [`<option value="">move to…</option>`, ...S.groups.map((g, i) => i === gi ? "" : `<option value="${i}">${esc(g.name)}</option>`),
                gi >= 0 ? `<option value="-1">(no group)</option>` : ""].join("");
  const src = iconSrc(host), letter = esc(((a.label || host)[0] || "?").toUpperCase());
  return `<div class="srow" data-host="${esc(host)}">
    ${src ? `<img src="${esc(src)}" alt="" data-letter="${letter}">` : `<span class="lt">${letter}</span>`}
    <div class="host" title="${esc(host)}">${esc(host)}${found ? "" : "<small>not found any more</small>"}</div>
    <input type="text" placeholder="${esc(host.split(".")[0])}" value="${esc(a.label || "")}" data-f="label" aria-label="label for ${esc(host)}">
    <input type="text" placeholder="icon (auto)" value="${esc(a.icon || "")}" data-f="icon" aria-label="icon for ${esc(host)}">
    <span class="acts">
      ${gi >= 0 ? `<button type="button" class="ibtn" data-a="up" ${idx === 0 ? "disabled" : ""} aria-label="move ${esc(host)} up" title="move up">↑</button>`
                + `<button type="button" class="ibtn" data-a="down" ${idx === len - 1 ? "disabled" : ""} aria-label="move ${esc(host)} down" title="move down">↓</button>` : ""}
      <select data-a="move" aria-label="move ${esc(host)} to a group">${opts}</select>
      <button type="button" class="ibtn danger" data-a="hide" title="hide from the dashboard">hide</button>
    </span></div>`;
}

function renderServices() {
  const grouped = new Set(S.groups.flatMap(g => g.hosts)), hidden = new Set(S.skip_hosts);
  const all = [...new Set([...V.discovered, ...S.services.map(x => x.name)])].filter(h => !hidden.has(h));
  $("svc-count").textContent = `${all.length} found`;
  $("svc-empty").hidden = !!(all.length || S.skip_hosts.length);
  $("svc-empty").innerHTML = 'No services found yet. <a href="#setup">Setup</a> says how to connect Nginx Proxy Manager, '
    + 'or add services by hand under <a href="#h-extras">Services not in NPM</a> below.';
  $("groups").innerHTML = S.groups.map((g, gi) => `<div class="grp-ed" data-g="${gi}">
      <div class="gh"><input type="text" value="${esc(g.name)}" data-gname aria-label="group name">
        <button type="button" class="ibtn" data-ga="up" ${gi === 0 ? "disabled" : ""} aria-label="move group ${esc(g.name)} up" title="move group up">↑</button>
        <button type="button" class="ibtn" data-ga="down" ${gi === S.groups.length - 1 ? "disabled" : ""} aria-label="move group ${esc(g.name)} down" title="move group down">↓</button>
        <button type="button" class="ibtn danger" data-ga="del" title="delete group (its services become ungrouped)">delete</button>
        <small class="mute" style="margin-left:auto">${g.hosts.length} service${g.hosts.length === 1 ? "" : "s"}</small></div>
      ${g.hosts.map((h, i) => srow(h, gi, i, g.hosts.length)).join("") || '<div class="hint">Empty. Move services here with "move to…".</div>'}
    </div>`).join("");
  const loose = all.filter(h => !grouped.has(h));
  $("unassigned").innerHTML = loose.length ? `<div class="grp-ed" data-g="-1"><div class="gh"><b>Not in a group</b>
      <small class="mute">shown as "New / other" on the dashboard</small></div>${loose.map(h => srow(h, -1, 0, 0)).join("")}</div>` : "";
  $("hidden-wrap").innerHTML = S.skip_hosts.length ? `<div class="hint" style="margin-bottom:6px">Hidden from the dashboard (click to show again):</div>
      <div class="chiplist">${S.skip_hosts.map(h => `<button type="button" data-unhide="${esc(h)}">${esc(h)} ✕</button>`).join("")}</div>` : "";
  document.querySelectorAll("#groups input, #groups button, #groups select, #unassigned input, #unassigned button, #unassigned select, #hidden-wrap button, #add-group")
    .forEach(el => el.disabled = el.disabled || !V.ui_enabled);
  applySearch();
}

function onServicesEvent(e) {
  const t = e.target, row = t.closest(".srow"), grp = t.closest(".grp-ed");
  const gi = grp ? +grp.dataset.g : null;
  if (t.dataset.unhide) { S.skip_hosts = S.skip_hosts.filter(h => h !== t.dataset.unhide); renderServices(); return setDirty(); }
  if (t.hasAttribute("data-gname")) { if (e.type === "input") { S.groups[gi].name = t.value; setDirty(); } return; }
  if (t.dataset.ga && e.type === "click") {
    const g = S.groups;
    if (t.dataset.ga === "up") [g[gi - 1], g[gi]] = [g[gi], g[gi - 1]];
    if (t.dataset.ga === "down") [g[gi + 1], g[gi]] = [g[gi], g[gi + 1]];
    if (t.dataset.ga === "del" && confirm(`Delete the group "${g[gi].name}"? Its services become ungrouped.`)) g.splice(gi, 1);
    renderServices(); return setDirty();
  }
  if (!row) return;
  const host = row.dataset.host;
  if (t.dataset.f && e.type === "input") {
    app(host)[t.dataset.f] = t.value.trim(); cleanApp(host); setDirty();
    if (t.dataset.f === "icon") {   // preview the new icon right away
      const img = row.querySelector("img,.lt"), src = iconSrc(host);
      if (img && src) img.outerHTML = `<img src="${esc(src)}" alt="" data-letter="?">`;
    }
    return;
  }
  const a = t.dataset.a;
  if (a === "move" && e.type === "change" && t.value !== "") {
    S.groups.forEach(g => g.hosts = g.hosts.filter(h => h !== host));
    if (+t.value >= 0) S.groups[+t.value].hosts.push(host);
  } else if ((a === "up" || a === "down") && e.type === "click") {
    const hs = S.groups[gi].hosts, i = hs.indexOf(host), j = a === "up" ? i - 1 : i + 1;
    [hs[i], hs[j]] = [hs[j], hs[i]];
  } else if (a === "hide" && e.type === "click") {
    S.groups.forEach(g => g.hosts = g.hosts.filter(h => h !== host));
    if (!S.skip_hosts.includes(host)) S.skip_hosts.push(host);
  } else return;
  renderServices(); setDirty();
}
["click", "input", "change"].forEach(ev => {
  $("groups").addEventListener(ev, onServicesEvent); $("unassigned").addEventListener(ev, onServicesEvent); $("hidden-wrap").addEventListener(ev, onServicesEvent);
});
$("add-group").onclick = () => { S.groups.push({name: "New group", hosts: []}); renderServices(); setDirty(); };

// ---------- services not in NPM ----------
function renderExtras() {
  $("extras").innerHTML = S.services.map((x, i) => `<div class="xrow" data-i="${i}">
      <div class="field"><label for="x-name-${i}">Name</label><input type="text" id="x-name-${i}" value="${esc(x.name)}" data-x="name" aria-describedby="x-d-${i}">
        <small id="x-d-${i}">Shown on the tile; it links to https://name.</small></div>
      <div class="field"><label for="x-url-${i}">Address to check</label><input type="url" id="x-url-${i}" value="${esc(x.url)}" data-x="url" placeholder="http://192.168.1.5:5000"></div>
      <label class="xauth"><input type="checkbox" ${x.auth ? "checked" : ""} data-x="auth"> Behind sign-in (SSO)</label>
      <button type="button" class="ibtn danger" data-xdel>Remove</button></div>`).join("") || '<div class="hint">None yet.</div>';
  $("extras").querySelectorAll("input,button").forEach(el => el.disabled = !V.ui_enabled);
  $("add-extra").disabled = !V.ui_enabled;
}
$("extras").addEventListener("input", e => { const row = e.target.closest("[data-i]"); if (!row) return; const x = S.services[+row.dataset.i], k = e.target.dataset.x;
  x[k] = k === "auth" ? e.target.checked : e.target.value.trim(); setDirty(); });
$("extras").addEventListener("click", e => {
  if (!e.target.hasAttribute("data-xdel")) return;
  S.services.splice(+e.target.closest("[data-i]").dataset.i, 1);
  renderExtras(); renderServices(); setDirty();
});
$("add-extra").onclick = () => { S.services.push({name: "", url: "", auth: false}); renderExtras(); setDirty(); $("x-name-" + (S.services.length - 1)).focus(); };

// ---------- integrations ----------
const INTEG = [
  {k: "qbittorrent", title: "qBittorrent", what: "torrents + VPN-leak check", fields: [["qbit_user", "Username", "text"], ["qbit_pass", "Password", "password"]],
   extra: `<div class="field"><label for="i-home">Your real home IP (optional)</label><input type="text" id="i-home" data-k="qbittorrent.home_ip" aria-describedby="i-home-d">`
        + `<small id="i-home-d">If qBittorrent ever reports this as its address, the VPN leaked: big red banner.</small></div>`,
   url: "http://192.168.1.10:8080"},
  {k: "seerr", title: "Seerr / Overseerr / Jellyseerr", what: "approve requests", fields: [["seerr_key", "API key", "password"]], url: "http://overseerr:5055",
   help: "in Seerr: Settings → General → API Key"},
  {k: "tautulli", title: "Tautulli", what: "Plex now playing", fields: [["tautulli_key", "API key", "password"]], url: "http://tautulli:8181",
   help: "in Tautulli: Settings → Web Interface → API Key"},
];
function renderInteg() {
  $("integ").innerHTML = INTEG.map(it => {
    const urlLocked = V.locked_urls[it.k], on = S[it.k].url && it.fields.every(([f]) => V.secrets[f].set);
    return `<div class="integ" data-it="${it.k}">
      <h3><span>${esc(it.title)} <small class="mute">${esc(it.what)}</small></span>
        <span class="${on ? "ok-t" : "mute"} integ-st">${on ? "● on" : "○ off"}</span></h3>
      <div class="field"><label for="i-${it.k}-url">Address</label><input type="url" id="i-${it.k}-url" data-k="${it.k}.url" placeholder="${esc(it.url)}" ${urlLocked ? "disabled" : ""}>
        ${urlLocked ? '<small class="lockt">🔒 set in docker-compose.yml</small>' : ""}</div>
      ${it.fields.map(([f, label, type]) => { const st = V.secrets[f];
        return `<div class="field"><label for="i-${f}">${esc(label)}</label>
          <input type="${type}" id="i-${f}" data-secret="${f}" autocomplete="${type === "password" ? "new-password" : "off"}" ${st.locked ? "disabled" : ""}
            placeholder="${st.locked ? "🔒 set in docker-compose.yml" : st.set ? "•••••• saved (leave empty to keep)" : "not set"}"${it.help && type === "password" ? ` aria-describedby="i-${f}-d"` : ""}>
          ${it.help && type === "password" ? `<small id="i-${f}-d">${esc(it.help)}</small>` : ""}
          ${st.set && !st.locked ? `<label class="clr"><input type="checkbox" data-clear="${f}"> Remove the saved ${esc(label.toLowerCase())}</label>` : ""}</div>`; }).join("")}
      ${it.extra || ""}
      <div class="integ-test"><button type="button" class="ibtn" data-test="${it.k}">Test</button><span class="res" data-res="${it.k}" role="status"></span></div>
    </div>`; }).join("");
}
$("integ").addEventListener("submit", e => e.preventDefault());   // Enter in a field must not reload the page
$("integ").addEventListener("input", e => {
  if (e.target.dataset.secret) { SEC[e.target.dataset.secret] = e.target.value; setDirty(); }
  if (e.target.dataset.clear) { SEC[e.target.dataset.clear + "_clear"] = e.target.checked; setDirty(); }
});
$("integ").addEventListener("click", async e => {
  const k = e.target.dataset.test; if (!k) return;
  const res = document.querySelector(`[data-res="${k}"]`);
  if (dirty) { res.innerHTML = '<span class="warn-t">Save first: Test uses the saved settings.</span>'; return; }
  res.textContent = "testing…";
  try { const r = await post("/api/settings/test", {name: k}); res.innerHTML = `<span class="${r.ok ? "ok-t" : "crit-t"}">${r.ok ? "✓" : "✗"} ${esc(r.detail)}</span>`; }
  catch (err) { res.innerHTML = `<span class="crit-t">✗ ${esc(err.message)}</span>`; }
});

// ---------- About: what's set in docker-compose.yml ----------
function renderInfra() {
  const i = V.infra, a = V.about;
  $("infra").innerHTML = [
    ["Dashboard address", "ORIGIN", i.origins.length ? i.origins.join(", ") : "not set: the address it's opened on"],
    ["Accepts connections from", "TRUSTED_PROXY", i.trusted_proxy || "anyone (not set)"],
    ["Answers on", "LISTEN", a.listen],
    ["Signed-in user header", "USER_HEADER", a.user_header || "not used"],
    ...(V.via_proxy_env.length ? [["Checked through the proxy", "CHECK_VIA_PROXY", V.via_proxy_env.join(", ")]] : []),
    ["Nginx Proxy Manager folder", "NPM_DIR", a.npm_dir],
    ["Data folder", "DATA_DIR", a.data_dir + (V.writable ? " (writable ✓)" : " (NOT writable ✗)")],
    ["Settings page", "SETTINGS_UI", a.settings_ui ? "editable" : "read-only"],
  ].map(([label, env, v]) => `<span>${esc(label)} <code>${esc(env)}</code></span><span>${esc(v)}</span>`).join("");
}

// ---------- the menu: which section you're in (aria-current), and search ----------
const navLinks = [...document.querySelectorAll(".set-nav a")];
// the current section = the last one whose top has passed under the sticky header (+ the menu bar on phones);
// at the very bottom, the last section (it may be too short to ever get there)
const sections = [...document.querySelectorAll(".set-main > section")];
let curId = "";
function markCurrent() {
  const nav = document.querySelector(".set-nav"), barOnTop = getComputedStyle(nav).position === "sticky" && nav.offsetWidth > innerWidth * .8;
  const line = document.querySelector("header").offsetHeight + (barOnTop ? nav.offsetHeight : 0) + 40;
  let cur = sections.filter(s => !s.classList.contains("q-out")).filter(s => s.getBoundingClientRect().top <= line).pop() || sections[0];
  if (innerHeight + scrollY >= document.documentElement.scrollHeight - 4) cur = sections.filter(s => !s.classList.contains("q-out")).pop() || cur;
  if (cur.id === curId) return;
  curId = cur.id;
  navLinks.forEach(a => a.hash === "#" + curId ? a.setAttribute("aria-current", "true") : a.removeAttribute("aria-current"));
  const a = navLinks.find(a => a.hash === "#" + curId), ul = a.closest("ul");
  if (ul.scrollWidth > ul.clientWidth) ul.scrollTo({left: a.offsetLeft - ul.offsetLeft - 16});   // phone: keep it in view
}
let spyQueued = false;
addEventListener("scroll", () => { if (!spyQueued) { spyQueued = true; requestAnimationFrame(() => { spyQueued = false; markCurrent(); }); } }, {passive: true});
addEventListener("resize", markCurrent);

// Search: shows the sections and settings whose text (label, help, current value) contains what you typed.
// A match keeps everything inside it (a matching card keeps all its fields); a service group stays when its name or
// one of its services matches; Advanced boxes with a match open up.
const UNITS = ".field, .integ, .xrow, .chk, .grp-ed";
function applySearch() {
  const q = $("set-q").value.trim().toLowerCase();
  document.querySelectorAll(".q-out").forEach(el => el.classList.remove("q-out"));
  if (!q) { $("q-none").hidden = true; return; }
  const has = el => (el.textContent + " " + [...el.querySelectorAll("input:not([type=checkbox]):not([type=file]),textarea,select")]
    .map(i => `${i.value} ${i.placeholder || ""} ${i.getAttribute("aria-label") || ""}`).join(" ")).toLowerCase().includes(q);
  const reveal = el => { for (let d = el.closest("details"); d; d = d.parentElement.closest("details")) d.open = true; };
  let any = false;
  document.querySelectorAll(".set-main > section").forEach(sec => {
    let hit = sec.querySelector("h2").textContent.toLowerCase().includes(q);
    if (!hit) {
      const hits = [];
      sec.querySelectorAll(UNITS).forEach(u => {
        if (hits.some(h => h.contains(u))) return;            // inside a unit that already matched: keep it
        if (u.matches(".grp-ed")) {
          if (has(u.querySelector(".gh"))) { hits.push(u); return; }
          u.querySelectorAll(".srow").forEach(r => r.classList.toggle("q-out", !has(r)));
          if (u.querySelector(".srow:not(.q-out)")) hits.push(u); else u.classList.add("q-out");
          return;
        }
        if (has(u)) { hits.push(u); reveal(u); } else u.classList.add("q-out");
      });
      hit = hits.length > 0;
    }
    sec.classList.toggle("q-out", !hit);
    any = any || hit;
  });
  $("q-none").hidden = any;
  $("q-none").textContent = `No settings match "${$("set-q").value.trim()}".`;
  markCurrent();
}
$("set-q").addEventListener("input", applySearch);
$("set-q").addEventListener("keydown", e => { if (e.key === "Escape") { $("set-q").value = ""; applySearch(); } });

// ---------- load / save ----------
function render() {
  S = structuredClone(V.settings); SEC = {};
  if (V.settings.site.favicon) document.querySelector("link[rel=icon]").href = V.settings.site.favicon;
  document.title = "Settings · " + S.site.title; $("site-title").textContent = S.site.title; $("demo-tag").hidden = !V.demo;
  renderInteg(); bindFields(); renderPanels(); renderThemes(); renderServices(); renderExtras(); renderInfra(); renderSetup();
  document.querySelectorAll(".ferr").forEach(m => m.remove());
  hideErr(); setDirty(false); applySearch(); markCurrent();
}
async function load() { V = await api("/api/settings"); render(); }
$("reload").onclick = () => { if (!dirty || confirm("Discard your unsaved changes?")) render(); };
$("save").onclick = async () => {
  $("save").disabled = true; $("msg").textContent = "Saving…";
  try {
    S.services = S.services.filter(x => x.name || x.url);
    V = await post("/api/settings", {settings: S, secrets: SEC});
    render(); $("msg").innerHTML = V.demo_not_saved ? '<span class="ok-t">✓ Looks valid! (Demo: nothing is really saved, so the next visitor sees the original.)</span>'
      : '<span class="ok-t">✓ Saved. The dashboard uses the new settings right away.</span>';
  } catch (e) { setDirty(true); showErr(e.message, e.section); }
};
(function bubbles() {
  const box = $("bubbles"); if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  for (let i = 0; i < 12; i++) { const b = document.createElement("i"), s = 14 + Math.random() * 40;
    Object.assign(b.style, {width: s + "px", height: s + "px", left: Math.random() * 100 + "%",
                            animationDuration: 16 + Math.random() * 20 + "s", animationDelay: -Math.random() * 30 + "s"});
    box.appendChild(b); }
})();
load().catch(e => showErr("Couldn't load the settings: " + e.message));
