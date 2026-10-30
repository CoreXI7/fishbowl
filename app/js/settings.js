const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const CDN = "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons@main/png/";
const IMG_TYPES = "image/png,image/jpeg,image/webp,image/gif";
let V = null, S = null, SEC = {}, dirty = false;

async function api(url, opts = {}) {
  const r = await fetch(url, {credentials: "same-origin", ...opts});
  if (r.status === 401) {   // signed out (expired, or ended elsewhere): keep this page and its unsaved changes
    if (!V) { location.href = "/login?next=/settings"; throw new Error("signed out"); }
    throw new Error("You're signed out. Open the dashboard in another tab and sign in there, then try again here: your changes are kept.");
  }
  if (r.redirected || (r.headers.get("content-type") || "").indexOf("json") < 0) { location.reload(); throw new Error("session expired"); }
  const j = await r.json();
  if (!r.ok) throw Object.assign(new Error(j.error || r.status), {section: j.section, field: j.field, restart: j.restart});
  return j;
}
const post = (url, body) => api(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
const upload = f => api("/api/upload", {method: "POST", headers: {"Content-Type": "application/octet-stream"}, body: f});

// the header's height, for the sticky menu and for where a section scrolls to
// (it can grow after load: a long title, the demo tag or a late font wraps it)
const setHH = () => document.documentElement.style.setProperty("--hh", document.querySelector("header").offsetHeight + "px");
setHH(); new ResizeObserver(setHH).observe(document.querySelector("header"));

// ---------- unsaved changes: the Save bar, and a dot on each section in the menu that has some ----------
const SECTION_NAMES = {setup: "Setup", look: "Look & feel", themes: "Themes", services: "Services", integrations: "Integrations",
                       drives: "Drives & hardware", security: "Security", backups: "Backups", account: "Account", about: "About"};
const secName = k => Object.hasOwn(SECTION_NAMES, k) ? SECTION_NAMES[k] : "";
const narrow = matchMedia("(max-width:600px)");
const PERSONALITY = ["tagline", "tagline_down", "click_emoji", "greeting", "quotes", "mascot", "mascot_credit", "favicon"];
const slices = d => ({   // which part of the settings each section edits (for the unsaved-changes dots)
  look: [d.site.title, d.site.hidden_panels],
  themes: [d.site.theme, d.site.motion, PERSONALITY.map(k => d.site[k]), d.site.themes],
  services: [d.groups, d.apps, d.services, d.skip_hosts, d.via_proxy],
  integrations: [d.qbittorrent, d.seerr, d.tautulli],
  security: [d.known_ips],
});
function changedSections() {
  if (!dirty) return [];
  const a = slices(S), b = slices(V.settings);
  const out = Object.keys(a).filter(k => JSON.stringify(a[k]) !== JSON.stringify(b[k]));
  for (const k of CKEYS) if (cchanged(k) && !out.includes(csection(k))) out.push(csection(k));
  if (Object.values(SEC).some(Boolean) && !out.includes("integrations")) out.push("integrations");
  return out;
}
const andList = xs => xs.length < 2 ? xs.join("") : xs.slice(0, -1).join(", ") + " and " + xs[xs.length - 1];
function setMsg(html) { if ($("msg").innerHTML !== html) $("msg").innerHTML = html; }   // a status region: only announce real changes
function setDirty(d = true) {
  dirty = d;
  const ch = changedSections();
  if (d && !ch.length) dirty = false;   // edited back to what's saved
  $("save").disabled = !dirty || !V.ui_enabled;
  $("reload").disabled = !dirty;
  document.querySelectorAll(".set-nav a").forEach(a => a.classList.toggle("unsaved", ch.includes(a.hash.slice(1))));
  setMsg(!V.ui_enabled ? '<span class="warn-t">Read-only: the settings page is turned off (SETTINGS_UI=0).</span>'
    : !V.writable ? '<span class="crit-t">✗ The data folder isn\'t writable, so saving will fail. See Setup.</span>'
    : dirty ? (narrow.matches ? `<span class="warn-t">● ${ch.length} section${ch.length > 1 ? "s" : ""} unsaved</span>`
                              : `<span class="warn-t">● Unsaved changes in ${esc(andList(ch.map(secName)))}</span>`)
    : '<span class="mute">No unsaved changes.</span>');
}
addEventListener("beforeunload", e => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });
addEventListener("keydown", e => {   // Ctrl+S / ⌘+S saves
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); if (!$("save").disabled) $("save").click(); }
});

if (/Mac|iPhone|iPad/.test(navigator.platform)) $("save").title = "Save (⌘S)";

// ---------- simple fields (data-k="site.title"; data-list = one entry per line) ----------
// a.b.c paths into plain objects; a built-in name (__proto__, constructor, prototype) never walks into Object.prototype
const BUILTIN = new Set(["__proto__", "constructor", "prototype"]);
const getPath = (o, k) => k.split(".").reduce((a, p) => a && !BUILTIN.has(p) && Object.hasOwn(a, p) ? a[p] : undefined, o);
const setPath = (o, k, v) => { const ps = k.split("."), last = ps.pop(); if ([...ps, last].some(p => BUILTIN.has(p))) return;
  ps.reduce((a, p) => Object.hasOwn(a, p) && a[p] && typeof a[p] === "object" ? a[p] : (a[p] = {}), o)[last] = v; };
const lines = v => v.split("\n").map(x => x.trim()).filter(Boolean);
function bindFields() {
  document.querySelectorAll("[data-k]").forEach(el => {
    const v = getPath(S, el.dataset.k);
    el.value = el.hasAttribute("data-list") ? (v || []).join("\n") : (v ?? "");
    el.disabled = el.disabled || !V.ui_enabled;   // keeps a lock from the renderer (an address set in the environment)
    el.oninput = () => { setPath(S, el.dataset.k, el.hasAttribute("data-list") ? lines(el.value) : el.value); clearErr(el); setDirty(); };
  });
  // set in the environment: shown, locked (the environment always wins)
  if (V.via_proxy_env.length) {
    const f = $("f-via");
    f.value = V.via_proxy_env.join("\n"); f.disabled = true;
    $("d-via").innerHTML = '<span class="lockt">🔒 Set by <code>CHECK_VIA_PROXY</code> in docker-compose.yml / .env, which wins over this page.</span>';
  }
}

// checked when you leave the field (not while typing); the server checks everything again on Save
const CHECKS = {
  // IPv4: a full address or a prefix ending in a dot; IPv6 (has a colon): only rough here, the server checks the rest
  "f-known": l => /^[\d.]+$/.test(l) ? /^(\d{1,3}\.){3}\d{1,3}$|^(\d{1,3}\.){1,3}$/.test(l) : l.includes(":") && /^[0-9a-fA-F:.]{2,45}$/.test(l),
  "f-via": l => /^[A-Za-z0-9.-]{1,253}(\/[A-Za-z0-9._~%+,;=:@\/-]{0,200})?$/.test(l) && !/\.\.|%2e/i.test(l.split("/").slice(1).join("/")),
};
const CHECK_MSG = {
  "f-known": l => `"${l}" isn't an IP address or a prefix ending in a dot or a colon.`,
  "f-via": l => `"${l}" isn't a host name with an optional /path (no spaces, ?, # or "..").`,
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
  const id = (el.id || "f") + "-err";
  $(id)?.remove();
  el.removeAttribute("aria-invalid");
  if (el.hasAttribute("aria-describedby")) el.setAttribute("aria-describedby", el.getAttribute("aria-describedby").split(" ").filter(x => x !== id).join(" "));
}

// ---------- error summary (a failed save, upload or load): what's wrong, and a link to the field or section ----------
const FIELD_OF = {"known IP": "f-known", "check through the proxy": "f-via", "title": "f-title", "home IP": "i-home",
                  "qbittorrent URL": "i-qbittorrent-url", "seerr URL": "i-seerr-url", "tautulli URL": "i-tautulli-url"};
const FIELD_LABEL = {"known IP": "Your own IPs", "check through the proxy": "Check these through the proxy", "title": "Title",
                     "home IP": "Your home's public IP", "qbittorrent URL": "qBittorrent address", "seerr URL": "Seerr address",
                     "tautulli URL": "Tautulli address"};
function showErr(msg, section, title = "Not saved") {
  const k = Object.keys(FIELD_OF).find(k => msg.startsWith(k + ":")), field = k && $(FIELD_OF[k]);
  if (field) {
    if (field.closest("details")) field.closest("details").open = true;
    fieldErr(field, msg.slice(k.length + 1).trim());
  }
  const box = $("err-sum");
  const shown = k ? `${FIELD_LABEL[k]}: ${msg.slice(k.length + 1).trim()}` : msg;
  box.innerHTML = `<h2>${esc(title)}</h2><p>${esc(shown)}</p>` +
    (field ? `<p><a href="#${field.id}" data-focus="${field.id}">Go to the field →</a></p>`
     : secName(section) ? `<p><a href="#${section}">Go to ${esc(secName(section))} →</a></p>` : "");
  box.hidden = false;
  box.scrollIntoView({block: "start"}); box.focus({preventScroll: true});
}
$("err-sum").addEventListener("click", e => {   // the link to a field also puts the cursor in it
  const a = e.target.closest("[data-focus]"); if (!a) return;
  e.preventDefault(); $(a.dataset.focus).focus();
});
function hideErr() { $("err-sum").hidden = true; }

// ---------- Setup checklist ----------
const STATE = {problem: ["✗", "needs fixing"], warn: ["!", "worth fixing"], todo: ["○", "not set up"], ok: ["✓", "ok"]};
const ORDER = {problem: 0, warn: 1, todo: 2, ok: 3};
function codeBlock(code) {
  code = code.replaceAll("%ORIGIN%", location.origin);
  return `<div class="code"><pre${code.includes("\n") ? ' class="multi"' : ""}><code>${esc(code)}</code></pre><button type="button" class="ibtn" data-copy="${esc(code)}"
    aria-label="Copy: ${esc(code.split("\n")[0])}">Copy</button></div>`;
}
function chk(i) {
  const [icon, word] = STATE[i.state] || STATE.todo, link = /^#[a-z]+$/.test(i.link || "") ? i.link : "";
  const doc = /^https:\/\/github\.com\/[\w./#-]+$/.test(i.doc || "") ? i.doc : "";
  return `<div class="chk chk-${esc(i.state)}"><span class="chk-ic" aria-hidden="true">${icon}</span><div class="chk-b">
    <h3 class="chk-t">${esc(i.title)}<span class="vh"> (${word})</span>${i.optional ? ' <small class="mute">optional</small>' : ""}</h3>
    <p>${esc(i.detail)}</p>
    ${i.why ? `<details class="chk-why"><summary>Why?</summary><p>${esc(i.why)}</p></details>` : ""}
    ${(i.steps || []).length ? `<ol class="chk-steps">${i.steps.map(s => `<li>${s.text ? `<p>${esc(s.text)}</p>` : ""}${s.code ? codeBlock(s.code) : ""}</li>`).join("")}</ol>` : ""}
    ${i.alt ? `<p class="chk-alt">${esc(i.alt)}</p>` : ""}
    ${link && secName(link.slice(1)) ? `<a class="chk-go" href="${link}">Go to ${esc(secName(link.slice(1)))} →</a>` : ""}
    ${doc ? `<a class="chk-go" href="${esc(doc)}" target="_blank" rel="noopener">How to, in the docs ↗</a>` : ""}
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
    ([...fix, ...todo].some(i => i.compose) ? `<div class="chk-note"><p>When you've made the docker-compose.yml changes below, run this in its folder, then click <b>Check again</b>:</p>${codeBlock("docker compose up -d")}</div>` : "") +
    fix.map(chk).join("") + todo.map(chk).join("") +
    (ok.length ? `<details class="chk-ok-list"><summary>${ok.length} check${ok.length > 1 ? "s" : ""} passed</summary>${ok.map(chk).join("")}</details>` : "");
}
$("setup-list").addEventListener("click", async e => {
  const b = e.target.closest("[data-copy]"); if (!b) return;
  try { await navigator.clipboard.writeText(b.dataset.copy); b.textContent = "Copied ✓"; $("copy-st").textContent = "Copied"; }
  catch { const r = document.createRange(); r.selectNodeContents(b.previousElementSibling); getSelection().removeAllRanges(); getSelection().addRange(r);
          b.textContent = "Press Ctrl+C"; $("copy-st").textContent = "Selected: press Ctrl+C to copy"; }
  setTimeout(() => { b.textContent = "Copy"; $("copy-st").textContent = ""; }, 2500);
});
$("setup-again").onclick = async () => {
  $("setup-again").disabled = true;
  try { V.setup = (await api("/api/settings")).setup; renderSetup(); applySearch(); } catch (err) { showErr(err.message, "", "Couldn't check"); }
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
  renderThemes(); $("theme-tabs").querySelector(`[data-t="${CSS.escape(edTheme)}"]`).focus();
});
const tget = k => edTheme === "bubbles" ? S.site[k] : (S.site.themes[edTheme] || {})[k];
function tset(k, v) {
  if (edTheme === "bubbles") S.site[k] = v;
  else {   // an emptied field = the theme's own text again (the server stores only what's set)
    const t = {...(S.site.themes[edTheme] || {}), [k]: v};
    if (!v || (Array.isArray(v) && !v.length)) delete t[k];
    if (Object.keys(t).length) S.site.themes[edTheme] = t; else delete S.site.themes[edTheme];
  }
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
  // what an empty field falls back to (as the dashboard does): the theme's own text, and for these four the Bubbles value
  const ph = k => bub ? "" : (def[k] ?? (["tagline", "tagline_down", "click_emoji", "favicon"].includes(k) ? S.site[k] : "") ?? "");
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
  applySearch();
}
$("theme-ed").addEventListener("change", async e => {
  const k = e.target.dataset.tup, f = e.target.files && e.target.files[0]; if (!k || !f) return;
  if (f.size > 2 * 1024 * 1024) return showErr("That image is over 2 MB.", "themes", "Not uploaded");
  try { tset(k, (await upload(f)).url); renderThemeEd(); } catch (err) { showErr(err.message, "themes", "Upload failed"); }
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
const app = host => Object.hasOwn(S.apps, host) ? S.apps[host] : host === "__proto__" ? {} : (S.apps[host] = {});
function cleanApp(host) { const a = S.apps[host]; if (a && !a.label && !a.icon && !a.url) delete S.apps[host]; }

function srow(host, gi, idx, len) {
  const a = S.apps[host] || {}, found = V.discovered.includes(host) || S.services.some(x => x.name === host);
  const opts = S.groups.map((g, i) => `<option value="${i}"${i === gi ? " selected" : ""}>${esc(g.name)}</option>`).join("")
    + `<option value="-1"${gi < 0 ? " selected" : ""}>(no group)</option>`;
  const src = iconSrc(host), letter = esc(((a.label || host)[0] || "?").toUpperCase()), h = esc(host);
  return `<div class="srow" data-host="${h}">
    ${src ? `<img src="${esc(src)}" alt="" data-letter="${letter}">` : `<span class="lt">${letter}</span>`}
    <div class="host" title="${h}">${h}${found ? "" : "<small>not found any more</small>"}</div>
    <label class="sl"><span class="sl-t">Name<span class="vh"> on the dashboard for ${h}</span></span>
      <input type="text" placeholder="${esc(host.split(".")[0])}" value="${esc(a.label || "")}" data-f="label"></label>
    <label class="sl"><span class="sl-t">Icon<span class="vh"> for ${h}</span></span>
      <input type="text" placeholder="automatic" value="${esc(a.icon || "")}" data-f="icon"></label>
    <span class="acts">
      <label class="sl"><span class="sl-t">Group<span class="vh"> for ${h}</span></span><select data-a="move">${opts}</select></label>
      ${gi >= 0 ? `<button type="button" class="ibtn" data-a="up" ${idx === 0 ? "disabled" : ""} aria-label="Move ${h} up" title="move up">↑</button>`
                + `<button type="button" class="ibtn" data-a="down" ${idx === len - 1 ? "disabled" : ""} aria-label="Move ${h} down" title="move down">↓</button>` : ""}
      <button type="button" class="ibtn danger" data-a="hide" aria-label="Hide ${h} from the dashboard" title="hide from the dashboard">hide</button>
    </span></div>`;
}
const SROW_HEAD = '<div class="srow-h" aria-hidden="true"><span></span><span>Service</span><span>Name on the dashboard</span><span>Icon</span><span>Group</span></div>';

function renderServices() {
  const grouped = new Set(S.groups.flatMap(g => g.hosts)), hidden = new Set(S.skip_hosts);
  const all = [...new Set([...V.discovered, ...S.services.map(x => x.name)])].filter(h => !hidden.has(h));
  $("svc-count").textContent = `${all.length} found`;
  $("svc-empty").hidden = !!(all.length || S.skip_hosts.length);
  $("svc-empty").innerHTML = 'No services found yet. <a href="#setup">Setup</a> says how to connect Nginx Proxy Manager, '
    + 'or add services by hand under <a href="#h-extras">Services not in NPM</a> below.';
  $("groups").innerHTML = S.groups.map((g, gi) => `<div class="grp-ed" data-g="${gi}">
      <div class="gh"><input type="text" value="${esc(g.name)}" data-gname aria-label="Group name">
        <button type="button" class="ibtn" data-ga="up" ${gi === 0 ? "disabled" : ""} aria-label="move group ${esc(g.name)} up" title="move group up">↑</button>
        <button type="button" class="ibtn" data-ga="down" ${gi === S.groups.length - 1 ? "disabled" : ""} aria-label="move group ${esc(g.name)} down" title="move group down">↓</button>
        <button type="button" class="ibtn danger" data-ga="del" aria-label="Delete group ${esc(g.name)}" title="delete group (its services become ungrouped)">delete</button>
        <small class="mute" style="margin-left:auto">${g.hosts.length} service${g.hosts.length === 1 ? "" : "s"}</small></div>
      ${g.hosts.length ? SROW_HEAD + g.hosts.map((h, i) => srow(h, gi, i, g.hosts.length)).join("") : '<div class="hint">Empty. Put services here with their Group menu.</div>'}
    </div>`).join("");
  const loose = all.filter(h => !grouped.has(h));
  $("unassigned").innerHTML = loose.length ? `<div class="grp-ed" data-g="-1"><div class="gh"><b>Not in a group</b>
      <small class="mute">shown as "New / other" on the dashboard</small></div>${SROW_HEAD}${loose.map(h => srow(h, -1, 0, 0)).join("")}</div>` : "";
  $("hidden-wrap").innerHTML = S.skip_hosts.length ? `<div class="hint" style="margin-bottom:6px">Hidden from the dashboard (click to show again):</div>
      <div class="chiplist">${S.skip_hosts.map(h => `<button type="button" data-unhide="${esc(h)}" aria-label="Show ${esc(h)} on the dashboard again">↩ ${esc(h)}</button>`).join("")}</div>` : "";
  document.querySelectorAll("#groups input, #groups button, #groups select, #unassigned input, #unassigned button, #unassigned select, #hidden-wrap button, #add-group")
    .forEach(el => el.disabled = el.disabled || !V.ui_enabled);
  applySearch();
}

function refocus(...sels) {   // the first of these that exists and isn't disabled
  for (const sel of sels) { const el = sel && document.querySelector(sel); if (el && !el.disabled) return el.focus(); }
}
const rowSel = (host, what) => `.srow[data-host="${CSS.escape(host)}"] ${what}`;
function onServicesEvent(e) {
  const t = e.target, row = t.closest(".srow"), grp = t.closest(".grp-ed");
  const gi = grp ? +grp.dataset.g : null;
  if (t.dataset.unhide) {
    const h = t.dataset.unhide;
    S.skip_hosts = S.skip_hosts.filter(x => x !== h); renderServices(); setDirty();
    return refocus(rowSel(h, "[data-a=hide]"), "#hidden-wrap button", "#add-group");
  }
  if (t.hasAttribute("data-gname")) { if (e.type === "input") { S.groups[gi].name = t.value; setDirty(); } return; }
  if (t.dataset.ga && e.type === "click") {
    const g = S.groups, ga = t.dataset.ga;
    let to = gi;
    if (ga === "up") { [g[gi - 1], g[gi]] = [g[gi], g[gi - 1]]; to = gi - 1; }
    if (ga === "down") { [g[gi + 1], g[gi]] = [g[gi], g[gi + 1]]; to = gi + 1; }
    if (ga === "del") { if (!confirm(`Delete the group "${g[gi].name}"? Its services become ungrouped.`)) return; g.splice(gi, 1); to = Math.max(0, gi - 1); }
    renderServices(); setDirty();
    return ga === "del" ? refocus(`.grp-ed[data-g="${to}"] [data-gname]`, "#add-group")
                        : refocus(`.grp-ed[data-g="${to}"] [data-ga=${ga}]`, `.grp-ed[data-g="${to}"] [data-gname]`);
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
  if (a === "move" && e.type === "change") {   // the menu shows the row's group: picking one puts it there
    if (+t.value === gi) return;
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
  if (a === "hide") refocus(`[data-unhide="${CSS.escape(host)}"]`);
  else refocus(rowSel(host, `[data-a=${a}]`), rowSel(host, `[data-a=${a === "up" ? "down" : "up"}]`), rowSel(host, "[data-a=move]"));
}
["click", "input", "change"].forEach(ev => {
  $("groups").addEventListener(ev, onServicesEvent); $("unassigned").addEventListener(ev, onServicesEvent); $("hidden-wrap").addEventListener(ev, onServicesEvent);
});
$("add-group").onclick = () => {
  S.groups.push({name: "New group", hosts: []}); renderServices(); setDirty();
  const n = document.querySelector(`.grp-ed[data-g="${S.groups.length - 1}"] [data-gname]`); n.focus(); n.select();
};

// ---------- services not in NPM ----------
function renderExtras() {
  $("extras").innerHTML = S.services.map((x, i) => `<div class="xrow" data-i="${i}">
      <div class="field"><label for="x-name-${i}">Name</label><input type="text" id="x-name-${i}" value="${esc(x.name)}" data-x="name" data-was="${esc(x.name)}"${i ? "" : ` aria-describedby="x-d-0"`}>
        ${i ? "" : `<small id="x-d-${i}">A host name, like nas.lan. It's the tile's name.</small>`}</div>
      <div class="field"><label for="x-url-${i}">Address to check</label><input type="url" id="x-url-${i}" value="${esc(x.url)}" data-x="url"${i ? "" : ` aria-describedby="x-u-0"`}>
        ${i ? "" : `<small id="x-u-${i}">What the dashboard checks, like http://192.168.1.5:5000.</small>`}</div>
      <div class="field"><label for="x-link-${i}">Link on the tile (optional)</label><input type="url" id="x-link-${i}" value="${esc((S.apps[x.name] || {}).url || "")}" data-x="link" aria-describedby="x-l-${i}">
        <small id="x-l-${i}">Empty: https://${esc(x.name || "name")}</small></div>
      <div class="field xauth"><label><input type="checkbox" ${x.auth ? "checked" : ""} data-x="auth"${i ? "" : ` aria-describedby="x-a-0"`}> Behind your sign-in</label>
        ${i ? "" : `<small id="x-a-${i}">Only a label in the tile's tooltip.</small>`}</div>
      <button type="button" class="ibtn danger" data-xdel aria-label="Remove ${esc(x.name || "this service")}">Remove</button></div>`).join("") || '<div class="hint">None yet.</div>';
  $("extras").querySelectorAll("input,button").forEach(el => el.disabled = !V.ui_enabled);
  $("add-extra").disabled = !V.ui_enabled;
  applySearch();
}
$("extras").addEventListener("input", e => {
  const row = e.target.closest("[data-i]"); if (!row) return;
  const x = S.services[+row.dataset.i], k = e.target.dataset.x, v = e.target.value.trim();
  if (k === "auth") x.auth = e.target.checked;
  else if (k === "link") { app(x.name).url = v; cleanApp(x.name); }
  else if (k === "name") { x.name = v; $("x-l-" + row.dataset.i).textContent = "Empty: https://" + (v || "name"); }
  else x[k] = v;
  setDirty();
});
$("extras").addEventListener("change", e => {   // a finished rename takes the tile's label, icon and link along
  const el = e.target; if (el.dataset.x !== "name") return;
  const was = el.dataset.was, now = el.value.trim();
  const npm = V.discovered.filter(h => !V.settings.services.some(x => x.name === h));
  const shared = npm.includes(was) || S.services.some(x => x.name === was);
  if (was && now && was !== now && was !== "__proto__" && now !== "__proto__" && Object.hasOwn(S.apps, was)
      && !Object.hasOwn(S.apps, now) && !shared) {
    S.apps[now] = S.apps[was]; delete S.apps[was]; setDirty();
  }
  el.dataset.was = now;
});
$("extras").addEventListener("click", e => {
  if (!e.target.hasAttribute("data-xdel")) return;
  const i = +e.target.closest("[data-i]").dataset.i;
  S.services.splice(i, 1);
  renderExtras(); renderServices(); setDirty();
  ($("x-name-" + Math.min(i, S.services.length - 1)) || $("add-extra")).focus();   // focus stays nearby
});
$("add-extra").onclick = () => { S.services.push({name: "", url: "", auth: false}); renderExtras(); setDirty(); $("x-name-" + (S.services.length - 1)).focus(); };

// ---------- integrations ----------
const INTEG = [
  {k: "qbittorrent", title: "qBittorrent", what: "torrents + VPN-leak check", fields: [["qbit_user", "Username", "text"], ["qbit_pass", "Password", "password"]],
   extra: `<div class="field"><label for="i-home">Your home's public IP (optional)</label><input type="text" id="i-home" data-k="qbittorrent.home_ip" aria-describedby="i-home-d">`
        + `<small id="i-home-d">What a "what is my IP" site shows with the VPN off. If qBittorrent ever reports this address, the VPN leaked: big red banner.</small></div>`,
   url: "http://qbittorrent:8080 or http://192.168.1.10:8080"},
  {k: "seerr", title: "Seerr / Overseerr / Jellyseerr", what: "approve requests", fields: [["seerr_key", "API key", "password"]], url: "http://overseerr:5055",
   help: "In Seerr: Settings → General → API Key."},
  {k: "tautulli", title: "Tautulli", what: "Plex now playing", fields: [["tautulli_key", "API key", "password"]], url: "http://tautulli:8181",
   help: "In Tautulli: Settings → Web Interface → API Key."},
];
const LOCKED = "🔒 set in .env / docker-compose.yml";
function renderInteg() {
  $("integ").innerHTML = INTEG.map(it => {
    const urlLocked = V.locked_urls[it.k], on = S[it.k].url && it.fields.every(([f]) => V.secrets[f].set);
    return `<div class="integ" data-it="${it.k}" role="group" aria-labelledby="ih-${it.k}">
      <h3><span id="ih-${it.k}">${esc(it.title)} <small class="mute">${esc(it.what)}</small></span>
        <span class="${on ? "ok-t" : "mute"} integ-st">${on ? "● set up" : "○ not set up"}</span></h3>
      <div class="field"><label for="i-${it.k}-url">Address</label><input type="url" id="i-${it.k}-url" data-k="${it.k}.url" aria-describedby="i-${it.k}-ud" ${urlLocked ? "disabled" : ""}>
        <small id="i-${it.k}-ud">${urlLocked ? `<span class="lockt">${LOCKED}</span>` : `Like ${esc(it.url)}`}</small></div>
      ${it.fields.map(([f, label, type]) => { const st = V.secrets[f];
        return `<div class="field"><label for="i-${f}">${esc(label)} <span class="kst ${st.set ? "on" : ""}">${st.locked ? LOCKED : st.set ? "saved" : "not set"}</span></label>
          <input type="${type}" id="i-${f}" data-secret="${f}" autocomplete="${type === "password" ? "new-password" : "off"}" ${st.locked || !V.ui_enabled ? "disabled" : ""}
            placeholder="${st.set && !st.locked ? "leave empty to keep the saved one" : ""}"${it.help && type === "password" ? ` aria-describedby="i-${f}-d"` : ""}>
          ${it.help && type === "password" ? `<small id="i-${f}-d">${esc(it.help)}</small>` : ""}
          ${st.set && !st.locked ? `<label class="clr"><input type="checkbox" data-clear="${f}"${V.ui_enabled ? "" : " disabled"}> Remove the saved ${esc(label.toLowerCase())}</label>` : ""}</div>`; }).join("")}
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
  if (changedSections().includes("integrations")) { res.innerHTML = '<span class="warn-t">Save first: Test uses the saved settings.</span>'; return; }
  res.textContent = "testing…";
  try { const r = await post("/api/settings/test", {name: k}); res.innerHTML = `<span class="${r.ok ? "ok-t" : "crit-t"}">${r.ok ? "✓" : "✗"} ${esc(r.detail)}</span>`; }
  catch (err) { res.innerHTML = `<span class="crit-t">✗ ${esc(err.message)}</span>`; }
});

// ---------- the collector's settings (Drives & hardware, Security → what it watches, Backups) ----------
// The page keeps C = every collector setting it wants (what's in effect, plus what it asked for last). Save hands C to
// the root collector, which checks every value itself: switches and choices apply within a minute; files and patterns
// wait for `sudo homelab-dash-collector --apply` on the host (it shows the change and asks first).
let C = {}, C0 = "{}", FP = {}, pollTimer = 0;
const APPROVE = new Set(["security.authelia_db", "security.fail2ban_log", "security.immutable_files", "security.watcher",
                         "backup.log", "backup.success_regex", "backup.failure_regex", "backup.checks", "backup.artifacts", "backup.jobs"]);
const CKEYS = ["interval", "gpu", "usage", "net_if", "mounts", "mount_labels", "security.process_watch", "security.cron_watch",
               "security.firewall", "security.details", "security.fail2ban_jail", "security.ssh_unit", "security.authelia_container",
               "security.bad_process_names", "backup.max_age_hours", ...APPROVE];
const MINERS = ["xmrig", "minerd", "cryptonight", "kdevtmpfsi", "kinsing", "kworkerds", "xmr-stak"];   // the collector's default list
// what the collector uses for a setting that isn't set anywhere (kept in step with homelab-dash-collector.py)
const DEFAULTS = {interval: 5, gpu: true, usage: true, net_if: "", mounts: [], mount_labels: {}, "security.process_watch": true,
  "security.cron_watch": true, "security.firewall": true, "security.details": true, "security.fail2ban_jail": "sshd",
  "security.ssh_unit": "ssh", "security.authelia_container": "", "security.bad_process_names": MINERS, "security.authelia_db": "",
  "security.fail2ban_log": "/var/log/fail2ban.log", "security.immutable_files": [], "security.watcher": {}, "backup.max_age_hours": 26,
  "backup.log": "", "backup.success_regex": "", "backup.failure_regex": "(?i)\\berror|failed\\b", "backup.checks": [],
  "backup.artifacts": [], "backup.jobs": []};
const cv = () => V.collector || {};
const clabel = k => (V.collector_labels || {})[k] || k;
const csection = k => k.startsWith("backup.") ? "backups" : k.startsWith("security.") ? "security" : "drives";
// Each change goes to the collector as an explicit value ("" / [] / {} = off or automatic), with the fingerprint of the
// value the page was showing: the collector takes it only while that's still the value in effect (otherwise it was
// changed on the host meanwhile)
const cget = (k, def = DEFAULTS[k]) => { const v = getPath(C, k); return v === undefined || v === null ? def : v; };
const clist = (k, def = DEFAULTS[k] || []) => { const v = cget(k, def); return Array.isArray(v) ? v : def; };   // never trust a shape
const stable = v => JSON.stringify(v, (_, x) => x && typeof x === "object" && !Array.isArray(x)
  ? Object.fromEntries(Object.keys(x).sort().map(k => [k, x[k]])) : x);
const cnorm = (k, v) => stable(v === undefined || v === null ? DEFAULTS[k] : v);
const cchanged = k => cnorm(k, getPath(C, k)) !== cnorm(k, getPath(JSON.parse(C0), k));
const emptyOf = k => Array.isArray(DEFAULTS[k]) ? [] : k === "mount_labels" || k === "security.watcher" ? {} : "";
function cset(k, v) { setPath(C, k, v === undefined ? emptyOf(k) : v); setDirty(); refreshState(k); }

// what the page shows: what's in effect, then what waits for your OK, then what was saved but not taken yet
function collectorShown() {
  const c = cv(), nested = {};
  for (const src of [c.settings, c.pending_values, c.wanted])
    for (const [k, v] of Object.entries(src || {})) if (CKEYS.includes(k)) setPath(nested, k, structuredClone(v));
  return nested;
}
function collectorStart() { C = collectorShown(); C0 = JSON.stringify(C); FP = {...(cv().fp || {})}; }
// news from the collector. Where what the page showed is now what's in effect (its own save, taken late), that's the
// value the page saw: its fingerprint becomes the baseline. adopt (only when the fields are redrawn right after):
// settings nobody is editing show the new value too. A setting being edited keeps its baseline, so a change made on
// the host meanwhile is refused rather than overwritten.
function collectorRefresh(adopt) {
  const c = cv(), fresh = collectorShown(), c0 = JSON.parse(C0), eff = c.settings || {};
  for (const k of CKEYS) {
    if (cnorm(k, eff[k]) === cnorm(k, getPath(c0, k))) FP[k] = (c.fp || {})[k];
    if (!adopt || cchanged(k)) continue;
    const v = getPath(fresh, k);
    setPath(C, k, v === undefined ? undefined : structuredClone(v));
    setPath(c0, k, v === undefined ? undefined : structuredClone(v));
    FP[k] = (c.fp || {})[k];
  }
  C0 = JSON.stringify(c0);
}
const collectorChanged = () => CKEYS.some(cchanged);
function collectorChanges() {   // what Save hands to the collector
  return Object.fromEntries(CKEYS.filter(cchanged).map(k => {
    const v = getPath(C, k);
    return [k, {from: FP[k] || "", to: v === undefined || v === null ? structuredClone(DEFAULTS[k]) : v}];
  }));
}
const waitingKeys = () => { const c = cv();
  return [...new Set([...(c.pending || []), ...Object.keys(c.wanted || {}).filter(k => APPROVE.has(k))])]; };

// a field's state, next to it (read out through aria-describedby): refused (and why), waiting for your OK on the host,
// handed to the collector, will need your OK (changed, not saved yet), or quietly "needs your OK" where that applies
function cstate(k) {
  const c = cv(), rej = (c.rejected || []).find(([key]) => key === k);
  if (waitingKeys().includes(k)) return '<span class="cst wait">⏳ waiting for your OK on the host</span>';
  if (Object.hasOwn(c.wanted || {}, k)) return '<span class="cst wait">⏳ handed to the collector</span>';   // (newer than a refusal)
  if (rej) return `<span class="cst bad">✗ refused: ${esc(rej[1])}</span>`;
  if (!APPROVE.has(k)) return "";
  return cchanged(k) ? '<span class="okchip">will need your OK on the host</span>' : '<span class="hostchip">needs your OK on the host</span>';
}
function refreshState(k) {   // after an edit: the chip next to that field, without redrawing the field
  const box = $(cid(k) + "-st");
  if (box) box.innerHTML = cstate(k);
}
const cid = k => "c-" + k.replace(/[^a-z0-9]+/gi, "-");
const refused = k => !Object.hasOwn(cv().wanted || {}, k) && !waitingKeys().includes(k) && (cv().rejected || []).some(([key]) => key === k);
const aria = (k, help) => ` aria-describedby="${[help ? cid(k) + "-d" : "", cid(k) + "-st"].filter(Boolean).join(" ")}"`
  + (refused(k) ? ' aria-invalid="true"' : "");
const chip = k => `<span class="cstate" id="${cid(k)}-st">${cstate(k)}</span>`;
const cfield = (k, label, input, help, kw = "") => `<div class="field" data-ck="${esc(k)}"${kw ? ` data-kw="${esc(kw)}"` : ""}>
  <div class="lrow"><label for="${cid(k)}">${esc(label)}</label>${chip(k)}</div>${input}${help ? `<small id="${cid(k)}-d">${help}</small>` : ""}</div>`;
const dis = () => V.ui_enabled ? "" : " disabled";
function ccheck(k, label, help) {
  return `<div class="field" data-ck="${esc(k)}"><div class="lrow"><label class="cbox"><input type="checkbox" id="${cid(k)}" data-c="${esc(k)}" data-type="bool"${cget(k) ? " checked" : ""}${aria(k, help)}${dis()}>
    ${esc(label)}</label>${chip(k)}</div>${help ? `<small id="${cid(k)}-d">${help}</small>` : ""}</div>`;
}
function cselect(k, label, opts, help) {
  const v = cget(k), has = opts.some(([o]) => o === v);
  return cfield(k, label, `<select id="${cid(k)}" data-c="${esc(k)}"${aria(k, help)}${dis()}>
    ${has ? "" : `<option value="${esc(v)}" selected>${esc(v)} (not found on this machine)</option>`}
    ${opts.map(([o, t]) => `<option value="${esc(o)}"${o === v ? " selected" : ""}>${esc(t)}</option>`).join("")}</select>`, help);
}
function cnum(k, label, min, max, help) {
  return cfield(k, label, `<input type="number" id="${cid(k)}" data-c="${esc(k)}" data-type="int" min="${min}" max="${max}" step="1" value="${esc(cget(k))}"${aria(k, help)}${dis()}>`, help);
}
function ctext(k, label, help, kw = "") {
  return cfield(k, label, `<input type="text" id="${cid(k)}" data-c="${esc(k)}" value="${esc(cget(k))}" spellcheck="false"${aria(k, help)}${dis()}>`, help, kw);
}
function clines(k, label, help, kw = "") {
  return cfield(k, label, `<textarea id="${cid(k)}" data-c="${esc(k)}" data-type="lines" rows="3" spellcheck="false"${aria(k, help)}${dis()}>${esc(clist(k).filter(x => typeof x === "string").join("\n"))}</textarea>`, help, kw);
}

function renderCollector() {
  const c = cv(), det = c.detected || {}, ok = c.installed && c.supported;
  const why = !c.installed ? 'These come from the collector on the host, which isn\'t installed yet: <a href="#setup">Setup</a> says how.'
    : !c.supported ? 'Your collector is older than this page, so these are still set in <code>/etc/homelab-dash/collector.toml</code>. '
      + '<a href="#setup">Setup</a> says how to update it.' : "";
  const waiting = waitingKeys(), err = ok && c.error ? `<div class="chk chk-warn"><span class="chk-ic" aria-hidden="true">!</span>
    <div class="chk-b"><p><b>The collector can't use the page's settings right now:</b> ${esc(c.error)}</p></div></div>` : "";
  const open = [...document.querySelectorAll("#drives details[open], #security details[open], #backups details[open]")]
    .map(d => d.dataset.did).filter(Boolean);
  document.querySelectorAll("[data-col-msg]").forEach(el => {
    const sec = el.closest("section").id, mine = waiting.filter(k => csection(k) === sec);
    el.innerHTML = why ? `<div class="empty">${why}</div>` : err
      + (mine.length ? `<div class="chk chk-warn"><span class="chk-ic" aria-hidden="true">!</span><div class="chk-b">
          <p><b>Waiting for your OK on the host:</b> ${mine.map(k => `<a href="#${cid(k)}">${esc(clabel(k))}</a>`).join(", ")}.</p>
          <p>Run this on the host. It shows exactly what changes and asks: <b>y</b> applies them, <b>d</b> drops them from the
            waiting list (and <code>--apply --undo</code> drops every setting made here):</p>${codeBlock("sudo homelab-dash-collector --apply")}</div></div>` : "");
  });
  document.querySelectorAll(".col-hint").forEach(el => el.hidden = !ok);
  ["c-drives", "c-checks", "c-backups"].forEach(id => $(id).hidden = !ok);
  if (!ok) return;

  // Drives & hardware
  const m0 = cget("mounts"), mounts = Array.isArray(m0) && m0.length ? m0 : null;   // [] = every drive it finds
  const l0 = cget("mount_labels"), labels = l0 && typeof l0 === "object" && !Array.isArray(l0) ? l0 : {};
  // the drives offered: what's mounted now, plus ticked or named ones that are unplugged right now (their alarm and
  // name must stay, and a name must stay removable)
  const found = det.mounts || [];
  const offered = [...new Set([...found, ...[...(mounts || []), ...Object.keys(labels)].filter(m => typeof m === "string")])];
  $("c-drives").innerHTML = `
    <fieldset class="field" id="c-mounts" data-ck="mounts"><legend>Drives to show</legend>${chip("mounts")}
      <label class="cbox"><input type="radio" name="c-mauto" value="auto"${mounts ? "" : " checked"} aria-describedby="c-mounts-st"${dis()}> Every drive it finds (/, and what's under /mnt, /media and /srv)</label>
      <label class="cbox"><input type="radio" name="c-mauto" value="pick"${mounts ? " checked" : ""} aria-describedby="c-mounts-st"${dis()}> Only the ones I tick (a ticked drive that disappears shows as NOT MOUNTED)</label>
      <fieldset class="mpicks${mounts ? "" : " auto"}"><legend class="vh">Drives</legend>
        <div class="mrow mhead" aria-hidden="true"><span>Drive</span><span>Name on the dashboard (optional)</span></div>
        ${offered.map((m, i) => `<div class="mrow">
          <label class="cbox"><input type="checkbox" data-mount="${esc(m)}"${!mounts || mounts.includes(m) ? " checked" : ""}${dis()}> <code>${esc(m)}</code>${found.includes(m) ? "" : ' <small class="crit-t">not mounted right now</small>'}</label>
          <input type="text" data-mlabel="${esc(m)}" value="${esc(labels[m] || "")}" placeholder="Name (optional)" aria-label="Name on the dashboard for ${esc(m)}" aria-describedby="c-mount-labels-st" maxlength="40"${dis()}></div>`).join("")
          || '<p class="hint">No drives found.</p>'}</fieldset>
      <small>Only real mount points are offered.</small> ${chip("mount_labels")}</fieldset>
    ${cselect("net_if", "Network interface (the up/down rate)", [["", `Automatic (the one with the default route${det.default_iface ? ": " + det.default_iface : ""})`],
              ...(det.ifaces || []).map(i => [i, i])], "The one that carries your internet traffic.")}
    ${det.gpu ? ccheck("gpu", "Show the NVIDIA GPU") : '<p class="hint">No NVIDIA GPU found (nvidia-smi).</p>'}
    ${ccheck("usage", "CPU and memory breakdown (which apps use the most)")}
    ${cnum("interval", "Update every (seconds)", 2, 60, "2 to 60. Drives, disk health and security are checked less often (1 to 5 minutes).")}`;

  // Security → what the collector watches
  const jails = [["", "off"], ...(det.jails || []).map(j => [j, j])], units = [["", "off"], ...(det.ssh_units || []).map(u => [u, u])];
  const auth = [["", "off"], ...(det.authelia || []).map(a => [a, a])], dbHint = Object.values(det.authelia_db || {})[0];
  const w0 = cget("security.watcher"), watcher = w0 && typeof w0 === "object" && !Array.isArray(w0) && Object.keys(w0).length ? w0 : null;
  $("c-checks").innerHTML = `
    ${cselect("security.fail2ban_jail", "Blocked logins (fail2ban)", jails, (det.jails || []).length
              ? "fail2ban blocks addresses that keep failing to log in. Each set of its rules is a \"jail\": pick sshd for SSH."
              : "fail2ban isn't installed, or has no jails.")}
    ${cselect("security.ssh_unit", "Where SSH logins are logged", units, "The SSH service (usually ssh, on some systems sshd).")}
    ${cselect("security.authelia_container", "Authelia container (failed sign-ins)", auth, (det.authelia || []).length ? "" : "No running Authelia container found.")}
    ${ctext("security.authelia_db", "Authelia's database (sign-ins and lock-outs in the details)",
            dbHint ? `Found: <code>${esc(dbHint)}</code> <button type="button" class="ibtn" data-cfill="security.authelia_db" data-v="${esc(dbHint)}" aria-label="Use ${esc(dbHint)} as Authelia's database"${dis()}>Use it</button>`
                   : "Its db.sqlite3 on the host, like <code>/srv/authelia/config/db.sqlite3</code>.", "sqlite path file")}
    ${ccheck("security.process_watch", "Watch for crypto-miners and show the busiest programs")}
    ${clines("security.bad_process_names", "Process names to flag (one per line)", "Letters, digits, . _ and -.")}
    ${ccheck("security.cron_watch", "Flag any crontab change until you click \"Looks good\"")}
    ${ccheck("security.firewall", "Show the firewall" + (det.firewall ? ` (${esc(det.firewall)})` : ""))}
    ${clines("security.immutable_files", "Files that must stay locked (one per line)",
             "Files you made unchangeable with <code>sudo chattr +i &lt;file&gt;</code>. The card goes red if one gets unlocked.", "chattr immutable lock")}
    ${ccheck("security.details", "Detail views (click a box's title on the dashboard's Security & backups card)")}
    <details class="adv" data-did="watch"${watcher ? " open" : ""}><summary>Advanced: your own watch script, and fail2ban's log</summary>
      ${ctext("security.fail2ban_log", "fail2ban's log (for the recent bans list)", "Usually <code>/var/log/fail2ban.log</code>. Empty: off.")}
      <div class="field" id="c-security-watcher" data-ck="security.watcher"><div class="lrow"><span class="flabel">Your own watch script</span>${chip("security.watcher")}</div>
        <small id="c-security-watcher-d">A script that runs from cron, shown in the Process watch details: when it last ran and what it alerted on.</small></div>
      ${[["label", "Its name", ""], ["heartbeat", "A file it rewrites every run", "Its age is the time since the last run."],
         ["alerts", "A file with one line per problem it alerted on", "Lines look like kind:detail."]].map(([f, l, h]) =>
        `<div class="field"><label for="c-w-${f}">${esc(l)}</label><input type="text" id="c-w-${f}" data-cw="${f}" value="${esc((watcher || {})[f] || "")}" spellcheck="false" aria-describedby="${h ? `c-w-${f}-d ` : ""}c-security-watcher-st"${dis()}>${h ? `<small id="c-w-${f}-d">${h}</small>` : ""}</div>`).join("")}
      <div class="field"><label for="c-w-every">Runs every (minutes)</label><input type="number" id="c-w-every" data-cw="every_minutes" min="1" max="1440" step="1" value="${esc((watcher || {}).every_minutes || "")}" aria-describedby="c-security-watcher-st"${dis()}></div>
      <div class="field"><label for="c-w-checks">What it checks (one per line)</label><textarea id="c-w-checks" data-cw="checks" rows="3" aria-describedby="c-security-watcher-st"${dis()}>${esc((Array.isArray((watcher || {}).checks) ? watcher.checks : []).join("\n"))}</textarea></div>
      <div class="field"><label for="c-w-kinds">Labels for its alert kinds (one per line: kind = label)</label><textarea id="c-w-kinds" data-cw="kinds" rows="3" spellcheck="false" aria-describedby="c-security-watcher-st"${dis()}>${esc(Object.entries((watcher || {}).kinds && typeof watcher.kinds === "object" && !Array.isArray(watcher.kinds) ? watcher.kinds : {}).map(([k, v]) => `${k} = ${v}`).join("\n"))}</textarea></div>
    </details>`;

  // Backups
  const jobs = clist("backup.jobs").filter(j => j && typeof j === "object" && !Array.isArray(j));
  const pat = 'Text the line contains, like <code>backup ok</code>. (It\'s a regular expression: <code>.</code> <code>*</code> <code>(</code> and friends have special meanings.)';
  $("c-backups").innerHTML = `
    ${ctext("backup.log", "Your backup script's log", "Like <code>/var/log/backup.log</code>. Each run starts at a line beginning with a date, like 2026-10-29.")}
    ${ctext("backup.success_regex", "A line that means the run went fine (optional)", pat, "regex pattern regular expression")}
    ${ctext("backup.failure_regex", "A line that means it failed", "Like <code>error</code> or <code>failed</code>. Empty: off (no line counts as a failure).", "regex pattern regular expression")}
    ${cnum("backup.max_age_hours", "A backup is overdue after (hours)", 1, 8760, "This one applies by itself.")}
    <div class="field" id="c-backup-jobs" data-ck="backup.jobs" data-kw="regex pattern"><div class="lrow"><h3 class="jobs-h">Backup jobs</h3>${chip("backup.jobs")}</div>
      <small>One per backup: the log line that marks it done, the files it leaves, where it goes. Shown in the Backups view.</small></div>
    <div id="c-jobs">${jobs.map((j, i) => jobCard(j, i)).join("")}</div>
    <button type="button" id="c-add-job" class="ibtn"${dis()}>+ Add a backup job</button>
    <div class="field" id="c-backup-checks" data-ck="backup.checks" data-kw="regex pattern count offsite"><div class="lrow"><h3 class="jobs-h">Counted checks</h3>${chip("backup.checks")}</div>
      <small>Lines to count in the last run of the log, like "offsite copy ok" twice when you keep two copies. Fewer than that
        marks the run as failed.</small></div>
    <div>${tableCards("backup.checks")}</div>
    <div class="field" id="c-backup-artifacts" data-ck="backup.artifacts" data-kw="glob pattern files"><div class="lrow"><h3 class="jobs-h">Backup files</h3>${chip("backup.artifacts")}</div>
      <small>Files your backups leave behind. For each pattern, the newest file must be newer than the overdue time above.</small></div>
    <div>${tableCards("backup.artifacts")}</div>`;
  document.querySelectorAll("#drives details, #security details, #backups details").forEach(d => { if (open.includes(d.dataset.did)) d.open = true; });
  applySearch();
}
// counted checks and backup file patterns: small tables, edited like the jobs
const TABLES = {
  "backup.checks": {one: "check", fresh: "New check", fields: [
    ["label", "Name", ""],
    ["regex", "A line to count in the last run", "Text the line contains, like <code>offsite copy ok</code>."],
    ["min_count", "At least this many times (empty = once)", "", "number"]]},
  "backup.artifacts": {one: "file pattern", fresh: "New file pattern", fields: [
    ["label", "Name", ""],
    ["glob", "Its files (* and ? work as wildcards)", "Like <code>/srv/backups/db/*.sql.gz</code>."]]},
};
// a counted check needs its line and a file pattern its pattern: an empty one would mark every backup as failed
function incompleteTables() {
  let first = null;
  for (const [k, need] of [["backup.checks", "regex"], ["backup.artifacts", "glob"]]) {
    clist(k).forEach((x, i) => {
      const el = $(`${cid(k)}-${i}-${need}`);
      if (!el || !x || typeof x !== "object") return;
      if (!x[need]) { fieldErr(el, need === "regex" ? "Which line to count?" : "Which files?"); first = first || el; } else clearErr(el);
    });
  }
  return first;
}
function tableCards(k) {
  const t = TABLES[k], p = cid(k), items = clist(k).filter(x => x && typeof x === "object" && !Array.isArray(x));
  const bad = refused(k) ? ' aria-invalid="true"' : "";
  return items.map((x, i) => { const name = x.label || `${t.one[0].toUpperCase()}${t.one.slice(1)} ${i + 1} (no name)`;
    return `<div class="jobcard" role="group" aria-labelledby="${p}-${i}-h"><div class="gh"><h4 id="${p}-${i}-h">${esc(name)}</h4>
      <button type="button" class="ibtn danger" data-tdel="${esc(k)}" data-ti="${i}" aria-label="Remove the ${t.one} ${esc(name)}"${dis()}>Remove</button></div>
    ${t.fields.map(([f, label, help, type = "text"]) => `<div class="field"><label for="${p}-${i}-${f}">${esc(label)}</label>
      <input type="${type}" id="${p}-${i}-${f}" data-tk="${esc(k)}" data-ti="${i}" data-f="${f}" value="${esc(x[f] ?? "")}" spellcheck="false"${type === "number" ? ' min="1" max="1000" step="1"' : ""} aria-describedby="${help ? `${p}-${i}-${f}-d ` : ""}${p}-st"${bad}${dis()}>${help ? `<small id="${p}-${i}-${f}-d">${help}</small>` : ""}</div>`).join("")}
  </div>`; }).join("") + `<button type="button" class="ibtn" data-tadd="${esc(k)}"${dis()}>+ Add a ${t.one}</button>`;
}
function jobCard(j, i) {
  const f = (k, label, help = "", type = "text") => `<div class="field"><label for="c-j${i}-${k}">${esc(label)}</label>
    <input type="${type}" id="c-j${i}-${k}" data-cj="${i}" data-f="${k}" value="${esc(j[k] ?? "")}" spellcheck="false"${type === "number" ? ' min="1" max="8760" step="1"' : ""} aria-describedby="${help ? `c-j${i}-${k}-d ` : ""}c-backup-jobs-st"${refused("backup.jobs") ? ' aria-invalid="true"' : ""}${dis()}>${help ? `<small id="c-j${i}-${k}-d">${help}</small>` : ""}</div>`;
  const name = j.label || `Backup job ${i + 1} (no name)`;
  return `<div class="jobcard" data-job="${i}" role="group" aria-labelledby="c-j${i}-h"><div class="gh"><h4 id="c-j${i}-h">${esc(name)}</h4>
      <button type="button" class="ibtn danger" data-jdel="${i}" aria-label="Remove the backup job ${esc(name)}"${dis()}>Remove</button></div>
    ${f("label", "Name")}
    ${f("regex", "The log line that marks it done", "Text the line contains, like <code>snapshot done</code>.")}
    <details class="adv" data-did="job${i}"><summary>Show the job's name, size or time from that line</summary>
      <p class="hint">In the pattern above, <code>(?P&lt;name&gt;…)</code>, <code>(?P&lt;size&gt;…)</code> and <code>(?P&lt;took&gt;…)</code> pick out a
        part of the line to show next to the job, like <code>dumped (?P&lt;name&gt;\\S+)</code>.</p></details>
    ${f("fail_regex", "A log line that means it failed (optional)")}
    <div class="field"><label for="c-j${i}-files">Files it leaves behind (one per line; * and ? work as wildcards)</label>
      <textarea id="c-j${i}-files" data-cj="${i}" data-f="files" rows="2" spellcheck="false" aria-describedby="c-backup-jobs-st"${dis()}>${esc((Array.isArray(j.files) ? j.files : []).join("\n"))}</textarea></div>
    ${f("dest", "Its destination folder (optional)", "Shows that drive's free space.")}
    <label class="cbox"><input type="checkbox" data-cj="${i}" data-f="size"${j.size ? " checked" : ""}${dis()}> Also measure the destination's size (once a night, at low priority)</label>
    ${f("every", "When it runs (a note, like \"nightly at 03:00\")")}
    ${f("max_age_hours", "Overdue after (hours; empty = the setting above)", "", "number")}
  </div>`;
}

// after a save that changed collector settings: say what the collector did with them (the save waits a few seconds for
// it), then check back a few times (every 15 s, for 2 minutes) so the rest shows up without reloading the page
function collectorSaved(changed) {
  const c = cv(), waits = waitingKeys(), rej = new Set((c.rejected || []).map(([k]) => k).filter(k => !waits.includes(k) && !Object.hasOwn(c.wanted || {}, k)));
  const bad = changed.filter(k => rej.has(k)), wait = changed.filter(k => !rej.has(k) && waits.includes(k));
  const handed = changed.filter(k => !rej.has(k) && !waits.includes(k) && Object.hasOwn(c.wanted || {}, k));
  const done = changed.length - bad.length - wait.length - handed.length;
  clearInterval(pollTimer);
  let n = 0;
  pollTimer = setInterval(async () => {
    if (++n > 8) return clearInterval(pollTimer);
    try {
      const v = await api("/api/settings");
      V.collector = v.collector; V.setup = v.setup;
      // someone is typing there, or a field shows a typing error: only the chips change now; values on the next try
      const busy = document.activeElement?.closest("#drives, #security, #backups")
        || document.querySelector("#drives .ferr, #security .ferr, #backups .ferr");
      collectorRefresh(!busy); renderSetup();
      if (!busy) renderCollector(); else CKEYS.forEach(refreshState);
      setDirty(dirty);
    } catch { /* the next try */ }
  }, 15000);
  return (done ? ` ${done} collector setting${done > 1 ? "s" : ""} applied.` : "")
    + (handed.length ? ` ${handed.length} handed to the collector (it applies them within a minute).` : "")
    + (bad.length ? ` <span class="crit-t">${bad.length} refused by the collector (${esc(bad.map(clabel).join(", "))}): see why next to ${bad.length > 1 ? "them" : "it"}.</span>` : "")
    + (wait.length ? ` ${wait.length} wait${wait.length > 1 ? "" : "s"} for your OK on the host (${esc(wait.map(clabel).join(", "))}):`
      + ` run <code>sudo homelab-dash-collector --apply</code> there.` : "");
}

// edits
document.addEventListener("input", e => {
  const t = e.target;
  if (t.dataset.c) {
    const k = t.dataset.c, ty = t.dataset.type;
    if (ty === "int") {
      const n = +t.value, lo = +t.min, hi = +t.max;
      if (t.value === "" || !Number.isInteger(n) || n < lo || n > hi) return fieldErr(t, `A whole number from ${lo} to ${hi}.`);
      clearErr(t);
    }
    cset(k, ty === "int" ? +t.value : ty === "lines" ? lines(t.value) : ty === "bool" ? t.checked : t.value.trim());
  } else if (t.dataset.cw) {
    const w0 = cget("security.watcher", {}), w = structuredClone(w0 && typeof w0 === "object" && !Array.isArray(w0) ? w0 : {}), f = t.dataset.cw;
    if (f === "checks") w.checks = lines(t.value);
    else if (f === "kinds") w.kinds = Object.fromEntries(lines(t.value).map(l => l.split("=")).filter(p => p.length > 1).map(([k, ...v]) => [k.trim(), v.join("=").trim()]));
    else if (f === "every_minutes") {
      if (t.value === "") delete w.every_minutes;
      else if (!Number.isInteger(+t.value) || +t.value < 1 || +t.value > 1440) return fieldErr(t, "A whole number from 1 to 1440.");
      else { clearErr(t); w.every_minutes = +t.value; }
    }
    else w[f] = t.value.trim();
    Object.keys(w).forEach(k => (w[k] === "" || (Array.isArray(w[k]) && !w[k].length) || (k === "kinds" && !Object.keys(w[k]).length)) && delete w[k]);
    cset("security.watcher", Object.keys(w).length ? w : undefined);
  } else if (t.dataset.cj !== undefined) {
    const jobs = structuredClone(clist("backup.jobs")), j = jobs[+t.dataset.cj], f = t.dataset.f;
    if (!j || typeof j !== "object") return;
    if (f === "files") j.files = lines(t.value);
    else if (f === "size") j.size = t.checked;
    else if (f === "max_age_hours") {
      if (t.value === "") delete j.max_age_hours;
      else if (!Number.isInteger(+t.value) || +t.value < 1 || +t.value > 8760) return fieldErr(t, "A whole number from 1 to 8760.");
      else { clearErr(t); j.max_age_hours = +t.value; }
    }
    else j[f] = t.value.trim();
    Object.keys(j).forEach(k => (j[k] === "" || j[k] === false || (Array.isArray(j[k]) && !j[k].length)) && delete j[k]);
    cset("backup.jobs", jobs);
    if (f === "label") t.closest(".jobcard").querySelector(".gh h4").textContent = j.label || `Backup job ${+t.dataset.cj + 1} (no name)`;
  } else if (t.dataset.tk) {
    const k = t.dataset.tk, list = structuredClone(clist(k)), x = list[+t.dataset.ti], f = t.dataset.f;
    if (!TABLES[k] || !x || typeof x !== "object") return;
    if (f === "min_count") {
      if (t.value === "") delete x.min_count;
      else if (!Number.isInteger(+t.value) || +t.value < 1 || +t.value > 1000) return fieldErr(t, "A whole number from 1 to 1000.");
      else { clearErr(t); x.min_count = +t.value; }
    } else x[f] = t.value.trim();
    Object.keys(x).forEach(kk => x[kk] === "" && delete x[kk]);
    cset(k, list);
    if (f === "label") t.closest(".jobcard").querySelector(".gh h4").textContent = x.label || `${TABLES[k].one[0].toUpperCase()}${TABLES[k].one.slice(1)} ${+t.dataset.ti + 1} (no name)`;
  } else if (t.dataset.mlabel) {
    const l0 = cget("mount_labels", {}), l = {...(l0 && typeof l0 === "object" && !Array.isArray(l0) ? l0 : {})};
    if (t.value.trim()) l[t.dataset.mlabel] = t.value.trim(); else delete l[t.dataset.mlabel];
    cset("mount_labels", Object.keys(l).length ? l : undefined);
  }
});
document.addEventListener("change", e => {
  const t = e.target;
  if (t.name === "c-mauto" || t.dataset.mount) {
    if (t.dataset.mount && !document.querySelector("[data-mount]:checked"))   // none ticked = every drive it finds
      document.querySelector('[name="c-mauto"][value="auto"]').checked = true;
    const auto = document.querySelector('[name="c-mauto"][value="auto"]').checked;
    document.querySelector(".mpicks").classList.toggle("auto", auto);
    cset("mounts", auto ? undefined : [...document.querySelectorAll("[data-mount]:checked")].map(b => b.dataset.mount));
  } else if (t.dataset.c && t.tagName === "SELECT") cset(t.dataset.c, t.value);
  else if (t.dataset.c && t.dataset.type === "bool") cset(t.dataset.c, t.checked);
});
document.addEventListener("click", e => {
  const t = e.target;
  if (t.dataset.cfill) { cset(t.dataset.cfill, t.dataset.v); renderCollector(); $(cid(t.dataset.cfill)).focus(); }
  else if (t.id === "c-add-job") {
    const have = new Set(clist("backup.jobs").map(j => j && j.label));   // each job needs its own name
    let name = "New backup job";
    for (let i = 2; have.has(name); i++) name = `New backup job ${i}`;
    cset("backup.jobs", [...structuredClone(clist("backup.jobs")), {label: name}]);
    renderCollector(); const n = clist("backup.jobs").length - 1; $(`c-j${n}-label`).focus(); $(`c-j${n}-label`).select();
  } else if (t.dataset.tadd && TABLES[t.dataset.tadd]) {
    const k = t.dataset.tadd, have = new Set(clist(k).map(x => x && x.label));   // each needs its own name
    let name = TABLES[k].fresh;
    for (let i = 2; have.has(name); i++) name = `${TABLES[k].fresh} ${i}`;
    cset(k, [...structuredClone(clist(k)), {label: name}]);
    renderCollector(); const el = $(`${cid(k)}-${clist(k).length - 1}-label`); el.focus(); el.select();
  } else if (t.dataset.tdel && TABLES[t.dataset.tdel]) {
    const k = t.dataset.tdel, list = structuredClone(clist(k)), i = +t.dataset.ti;
    if (!confirm(`Remove the ${TABLES[k].one} "${(list[i] || {}).label || ""}"?`)) return;
    list.splice(i, 1); cset(k, list); renderCollector();
    ($(`${cid(k)}-${Math.min(i, list.length - 1)}-label`) || document.querySelector(`[data-tadd="${k}"]`)).focus();
  } else if (t.dataset.jdel !== undefined) {
    const jobs = structuredClone(clist("backup.jobs")), i = +t.dataset.jdel;
    if (!confirm(`Remove the backup job "${jobs[i].label || ""}"?`)) return;
    jobs.splice(i, 1); cset("backup.jobs", jobs); renderCollector();
    ($(`c-j${Math.min(i, jobs.length - 1)}-label`) || $("c-add-job")).focus();
  }
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
  if (performance.now() < spyPausedUntil) return;
  const nav = document.querySelector(".set-nav"), barOnTop = getComputedStyle(nav).position === "sticky" && nav.offsetWidth > innerWidth * .8;
  const line = document.querySelector("header").offsetHeight + (barOnTop ? nav.offsetHeight : 0) + 40;
  const shown = sections.filter(s => !s.classList.contains("q-out") && !s.hidden);   // (Account is hidden without the own sign-in)
  let cur = shown.filter(s => s.getBoundingClientRect().top <= line).pop() || shown[0] || sections[0];
  if (innerHeight + scrollY >= document.documentElement.scrollHeight - 4) cur = shown.pop() || cur;
  if (pinned) {   // the section a menu click went to stays current while it's on screen (a short one can't reach the top)
    const r = $(pinned).getBoundingClientRect();
    if (r.bottom > line && r.top < innerHeight - 60) cur = $(pinned); else pinned = "";
  }
  if (cur.id === curId) return;
  curId = cur.id;
  navLinks.forEach(a => a.hash === "#" + curId ? a.setAttribute("aria-current", "true") : a.removeAttribute("aria-current"));
  const a = navLinks.find(a => a.hash === "#" + curId), ul = a.closest("ul");
  if (ul.scrollWidth > ul.clientWidth) ul.scrollTo({left: a.offsetLeft - ul.offsetLeft - 16});   // phone: keep it in view
}
let spyQueued = false, spyPausedUntil = 0, spyCatchUp = 0, pinned = "", pinFrom = 0;
addEventListener("scroll", () => {
  if (performance.now() < spyPausedUntil) {   // until a menu jump settles; then catch up with wherever scrolling ended
    spyPausedUntil = performance.now() + 150;
    clearTimeout(spyCatchUp); spyCatchUp = setTimeout(markCurrent, 200);
    return;
  }
  if (pinned && performance.now() > pinFrom) pinned = "";   // you scrolled yourself: back to following the page
  if (!spyQueued) { spyQueued = true; requestAnimationFrame(() => { spyQueued = false; markCurrent(); }); }
}, {passive: true});
document.querySelector(".set-nav ul").addEventListener("click", e => {
  const a = e.target.closest("a"); if (!a) return;
  curId = ""; spyPausedUntil = performance.now() + 600; pinned = a.hash.slice(1); pinFrom = spyPausedUntil;
  navLinks.forEach(l => l === a ? l.setAttribute("aria-current", "true") : l.removeAttribute("aria-current"));
  curId = a.hash.slice(1);
});
addEventListener("resize", markCurrent);

// Search: shows the sections and settings whose text (label, help, current value) contains what you typed.
// A match keeps everything inside it (a matching card keeps all its fields); a service group stays when its name or
// one of its services matches; Advanced boxes with a match open up.
const UNITS = ".field, .integ, .xrow, .chk, .grp-ed";
function applySearch() {
  const q = $("set-q").value.trim().toLowerCase(), words = q.split(/\s+/);
  const hasAll = t => words.every(w => t.includes(w));      // every word you typed, in any order
  document.querySelectorAll(".q-out").forEach(el => el.classList.remove("q-out"));
  if (!q) { $("q-none").hidden = true; $("q-count").textContent = ""; return; }
  const has = el => hasAll((el.textContent + " " + (el.dataset.kw || "") + " " + [...el.querySelectorAll("input:not([type=checkbox]):not([type=file]),textarea,select")]
    .map(i => `${i.value} ${i.placeholder || ""} ${i.getAttribute("aria-label") || ""}`).join(" ")).toLowerCase());
  const rowHas = r => hasAll([r.dataset.host, ...[...r.querySelectorAll("input[data-f]")].map(i => i.value)].join(" ").toLowerCase());
  const reveal = el => { for (let d = el.closest("details"); d; d = d.parentElement.closest("details")) d.open = true; };
  let any = false, n = 0;
  document.querySelectorAll(".set-main > section").forEach(sec => {
    if (sec.hidden) return;
    let hit = [sec.querySelector("h2"), ...sec.querySelectorAll("#theme-tabs button")].some(el => hasAll((el.textContent + " " + (el.dataset.kw || "")).toLowerCase()));
    if (!hit) {
      const hits = [];
      sec.querySelectorAll(UNITS).forEach(u => {
        if (hits.some(h => h.contains(u))) return;            // inside a unit that already matched: keep it
        if (u.matches(".grp-ed")) {
          const gname = (u.querySelector("[data-gname]")?.value ?? u.querySelector(".gh b")?.textContent ?? "").toLowerCase();
          if (hasAll(gname)) { hits.push(u); return; }
          u.querySelectorAll(".srow").forEach(r => r.classList.toggle("q-out", !rowHas(r)));
          if (u.querySelector(".srow:not(.q-out)")) hits.push(u); else u.classList.add("q-out");
          return;
        }
        if (has(u)) {
          hits.push(u); reveal(u);
          u.querySelectorAll("details").forEach(d => { if (hasAll(d.textContent.toLowerCase())) d.open = true; });
        } else u.classList.add("q-out");
      });
      hit = hits.length > 0;
    }
    sec.classList.toggle("q-out", !hit);
    navLinks.find(a => a.hash === "#" + sec.id)?.parentElement.classList.toggle("q-out", !hit);
    any = any || hit; n += hit;
  });
  $("q-none").hidden = any;
  $("q-none").textContent = `No settings match "${$("set-q").value.trim()}".`;
  $("q-count").textContent = n ? `${n} section${n > 1 ? "s" : ""} match` : "Nothing matches";
  markCurrent();
}
$("set-q").addEventListener("input", applySearch);
$("set-q").addEventListener("keydown", e => { if (e.key === "Escape") { $("set-q").value = ""; applySearch(); } });

// ---------- load / save ----------
// ---------- Account (Fishbowl's own sign-in, AUTH=login) ----------
// Its buttons act right away; nothing here goes through the Save bar. Every change sends the "Confirm it's you" box
// (the current password, and a code when two-step is on); the server checks them, this only shows forms and results.
let acctShown = false, A = null;
const ago = t => { const s = Math.max(0, Date.now() / 1000 - t);
  return s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 129600 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} days ago`; };
const browserName = ua => {   // most specific first: Edge and Opera say "Chrome" too, Chrome on iPhone says "Safari"
  ua = ua || "";
  const b = /Edg(?:A|iOS)?\//.test(ua) ? "Edge" : /OPR\/|OPT\//.test(ua) ? "Opera" : /Firefox\/|FxiOS\//.test(ua) ? "Firefox"
    : /Chrome\/|CriOS\//.test(ua) ? "Chrome" : /Safari\//.test(ua) ? "Safari" : "A browser";
  const o = /Android/.test(ua) ? "Android" : /iPhone/.test(ua) ? "iPhone" : /iPad/.test(ua) ? "iPad" : /Windows/.test(ua) ? "Windows"
    : /CrOS/.test(ua) ? "ChromeOS" : /Mac OS X/.test(ua) ? "Mac" : /Linux/.test(ua) ? "Linux" : "";
  return b + (o ? " on " + o : "");
};
const when = t => new Date(t * 1000).toLocaleString();
function acctMsg(id, text, bad) { const el = $(id); if (!el) return; el.textContent = text || ""; el.className = "acct-msg " + (bad ? "crit-t" : "ok-t"); }
// A change. confirm: send the "Confirm it's you" box along (cleared after a success: a code works once anyway). Its
// button is off while the request runs (a double click can't send it twice). On failure: {error} with the message
// shown and the field the server named marked and focused.
async function acctPost(url, body, msgId, btn = null, confirm = true) {
  document.querySelectorAll("#account [aria-invalid]").forEach(el => el.removeAttribute("aria-invalid"));
  if (confirm) body = {...body, password: $("acct-cur").value, code: A && A.totp ? $("acct-code").value.trim() : ""};
  acctMsg("acct-confirm-msg", "");
  if (btn) btn.disabled = true;
  try {
    const r = await post(url, body);
    acctMsg(msgId, "");
    if (confirm) $("acct-cur").value = $("acct-code").value = "";
    return r;
  } catch (e) {
    acctMsg(msgId, e.message, true);
    if (confirm && ["password", "code"].includes(e.field)) acctMsg("acct-confirm-msg", e.message, true);   // (said at the box too)
    const id = {password: "acct-cur", code: confirm ? "acct-code" : "acct-tc-code", new: "acct-pw-new"}[e.field];
    if (id && $(id)) { $(id).setAttribute("aria-invalid", "true"); $(id).focus(); }
    return {error: e};
  } finally { if (btn) btn.disabled = false; }
}
// before a yes/no question: is the box filled in? (else say so at the box, and don't ask the question twice)
function confirmFilled() {
  const empty = !$("acct-cur").value ? "acct-cur" : A && A.totp && !$("acct-code").value.trim() ? "acct-code" : "";
  if (!empty) return true;
  acctMsg("acct-confirm-msg", empty === "acct-cur" ? "Type your current password here first." : "Type a code from your app here first.", true);
  $(empty).setAttribute("aria-invalid", "true");
  $(empty).focus();
  return false;
}
async function refreshSetup() {   // the Setup checklist after an account change (two-step on or off, password changed)
  try { V.setup = (await api("/api/settings")).setup; renderSetup(); } catch (e) { /* it updates on the next load */ }
}

// part "lists" redraws only the browsers and the sign-in history, so one-time codes or a QR code on screen stay
async function renderAccount(focusId, part = "all") {
  const on = V.auth === "login" && !V.demo;
  $("account").hidden = $("nav-account").hidden = !on;
  if (!on) return;
  try { A = await api("/api/account"); }
  catch (e) { $("acct-who").textContent = "Couldn't load the account: " + e.message; return; }
  renderLists();
  $("acct-code-f").hidden = !A.totp;
  const busy = $("acct-codes")?.innerHTML.trim() || $("acct-setup")?.innerHTML.trim();
  if (part === "lists" || busy) { if (focusId && $(focusId)) $(focusId).focus(); return; }
  $("acct-who").innerHTML = `Signed in as <b>${esc(A.user)}</b>.`;
  document.querySelectorAll(".acct-user").forEach(el => el.value = A.user);   // (so a password manager knows whose it is)
  document.querySelectorAll(".pw-min").forEach(el => el.textContent = A.min_length);
  const n = A.notice || {};
  $("acct-alerts").innerHTML = (A.locked ? `<div class="chk chk-problem"><span class="chk-ic" aria-hidden="true">!</span><div class="chk-b">
      <p><b>Sign-ins from new browsers are locked</b> after ${esc(A.failures)} wrong tries in a row. Browsers that signed in here
        before (like this one) still can. Unlock it on the host when you're ready:</p>
      <div class="code"><pre><code>docker exec -it &lt;container&gt; python /app/auth.py unlock</code></pre></div></div></div>` : "")
    + (n.code ? `<div class="chk chk-problem"><span class="chk-ic" aria-hidden="true">!</span><div class="chk-b">
      <p><b>Someone may know your password.</b> Someone typed it correctly but the code wrong (${esc(n.code)}
        time${n.code > 1 ? "s" : ""}, last at ${esc(when(n.t))}). If that wasn't you, change the password below. If it was,
        confirm it's you below, then press "That was me".</p>
      <div class="acct-btns"><button type="button" class="ibtn" id="acct-notice-ok">That was me</button></div></div></div>` : "");
  $("acct-totp").innerHTML = A.totp
    ? `<p class="ok-t">On. <span class="mute">${A.recovery_left} recovery code${A.recovery_left === 1 ? "" : "s"} left.</span></p>
       <div class="acct-btns"><button type="button" class="ibtn" data-totp="recovery">New recovery codes</button>
         <button type="button" class="ibtn danger" data-totp="off">Turn it off</button></div><div id="acct-codes"></div>`
    : `<p class="warn-t">Off. With it on, signing in also needs a code from an app on your phone (Aegis, Google Authenticator,
         1Password, …), so a stolen or guessed password isn't enough.</p>
       <div class="acct-btns"><button type="button" class="ibtn" id="acct-totp-start">Set up an authenticator app</button></div>
       <div id="acct-setup"></div><div id="acct-codes"></div>`;
  if (focusId && $(focusId)) $(focusId).focus();
}

function renderLists() {
  $("acct-sessions").innerHTML = `<ul class="acct-list">${A.sessions.map(s => `<li><span><b>${esc(browserName(s.ua))}</b>${s.current ? ' <span class="hostchip">this browser</span>' : ""}<br>
        <small class="mute">${esc(s.ip)} · last seen ${esc(ago(s.seen))} · signed in ${esc(new Date(s.created * 1000).toLocaleDateString())}${s.keep ? " · kept signed in" : ""}</small></span>
      ${s.current ? "" : `<button type="button" class="ibtn" data-end="${esc(s.id)}" aria-label="Sign out ${esc(browserName(s.ua))} (${esc(s.ip)})">Sign out</button>`}</li>`).join("")}</ul>
    ${A.sessions.length > 1 ? '<button type="button" class="ibtn" id="acct-end-all">Sign out every other browser</button>' : ""}`;
  const rows = list => `<ul class="acct-list acct-events">${list.map(e => `<li><span>${esc(e.kind)}<br><small class="mute">${esc(when(e.t))} · ${esc(e.ip)}</small></span></li>`).join("")}</ul>`;
  $("acct-events").innerHTML = A.events.length ? rows(A.events) : '<p class="mute">None yet.</p>';
  $("acct-fails").innerHTML = A.fails.length ? rows(A.fails) : '<p class="mute">None.</p>';
}

function showCodes(codes) {
  const txt = "Fishbowl recovery codes. Each one works once, instead of an authenticator code.\n\n" + codes.join("\n") + "\n";
  $("acct-codes").innerHTML = `<div class="chk chk-warn"><span class="chk-ic" aria-hidden="true">!</span><div class="chk-b">
    <p id="acct-rc-title" tabindex="-1"><b>Your recovery codes.</b> Each one signs you in once if you lose your phone. Save
      them somewhere safe now (your password manager, or printed): they aren't shown again.</p>
    <pre class="acct-rc" id="acct-rc">${codes.map(esc).join("\n")}</pre>
    <div class="acct-btns"><button type="button" class="ibtn" id="acct-rc-copy">Copy</button>
      <a class="ibtn" id="acct-rc-dl" download="fishbowl-recovery-codes.txt">Download</a>
      <button type="button" class="ibtn" id="acct-rc-done">I saved them</button></div>
    <p class="acct-msg" id="acct-rc-msg" role="status" aria-live="polite"></p></div></div>`;
  $("acct-rc-dl").href = URL.createObjectURL(new Blob([txt], {type: "text/plain"}));
  $("acct-rc-copy").onclick = async () => {
    try { await navigator.clipboard.writeText(txt); acctMsg("acct-rc-msg", "✓ Copied."); }
    catch (e) { getSelection().selectAllChildren($("acct-rc")); acctMsg("acct-rc-msg", "Selected: press Ctrl+C (⌘C) to copy."); }   // no clipboard over plain HTTP
  };
  $("acct-rc-done").onclick = () => { URL.revokeObjectURL($("acct-rc-dl").href); $("acct-codes").innerHTML = ""; renderAccount("h-acct-totp"); };
  $("acct-rc-title").focus();
}

document.addEventListener("submit", async e => {
  if (e.target.id === "acct-confirm") e.preventDefault();   // (Enter in the box: nothing to send on its own)
  if (e.target.id === "acct-pw") {
    e.preventDefault();
    acctMsg("acct-pw-msg", "");
    if ($("acct-pw-new").value !== $("acct-pw-new2").value) {
      acctMsg("acct-pw-msg", "The two new passwords aren't the same.", true);
      $("acct-pw-new2").setAttribute("aria-invalid", "true");
      return $("acct-pw-new2").focus();
    }
    const r = await acctPost("/api/account/password", {new: $("acct-pw-new").value}, "acct-pw-msg", e.submitter);
    if (r.error) return;
    $("acct-pw-new").value = $("acct-pw-new2").value = "";
    acctMsg("acct-pw-msg", "✓ Changed. Every other browser is signed out.");
    await renderAccount("h-acct-pw");
    refreshSetup();
  } else if (e.target.id === "acct-totp-confirm") {
    e.preventDefault();
    acctMsg("acct-totp-msg", "");
    const r = await acctPost("/api/account/totp/confirm", {code: $("acct-tc-code").value.trim()}, "acct-tc-msg", e.submitter, false);
    if (r.error) {
      if (r.error.restart) {   // the setup expired (or the server restarted): start over from the button
        $("acct-setup").innerHTML = "";
        await renderAccount("h-acct-totp");
        acctMsg("acct-totp-msg", r.error.message + " Remove the Fishbowl entry you just added in your app, then set it up again.", true);
      }
      return;
    }
    $("acct-totp").innerHTML = '<p class="ok-t">✓ On. Every other browser was signed out. From now on, changes here also ask '
      + 'for a code from the app.</p><div id="acct-codes"></div>';
    $("acct-code-f").hidden = false;
    if (A) A.totp = true;
    showCodes(r.recovery);
    renderAccount(null, "lists");
    refreshSetup();
  }
});
document.addEventListener("click", async e => {
  const t = e.target;
  if (t.id === "acct-totp-start") {
    acctMsg("acct-totp-msg", "");
    const r = await acctPost("/api/account/totp/start", {}, "acct-totp-msg", t);
    if (r.error) return;
    t.closest(".acct-btns").hidden = true;
    $("acct-setup").innerHTML = `<ol class="acct-steps">
      <li>In your authenticator app, add an account and scan this code:<div class="acct-qr" id="acct-qr"></div>
        Can't scan? Type this key instead: <code class="acct-key">${esc(r.secret.match(/.{1,4}/g).join(" "))}</code>
        (or <a href="${esc(r.uri)}">open it in an authenticator app on this device</a>).</li>
      <li><form class="form acct-form" id="acct-totp-confirm" novalidate>
        <div class="field"><label for="acct-tc-code">The 6-digit code the app shows now</label>
          <input type="text" id="acct-tc-code" autocomplete="one-time-code" inputmode="numeric" maxlength="7" spellcheck="false"></div>
        <button type="submit" class="ibtn">Turn it on</button>
        <p class="acct-msg" id="acct-tc-msg" role="status" aria-live="polite"></p></form></li></ol>`;
    const svg = new DOMParser().parseFromString(r.qr, "image/svg+xml").documentElement;   // drawn by the server, no script in it
    if (svg && svg.nodeName === "svg") $("acct-qr").appendChild(document.importNode(svg, true));
    $("acct-tc-code").focus();
  } else if (t.dataset.totp) {
    acctMsg("acct-totp-msg", "");
    if (t.dataset.totp === "off" && (!confirmFilled() || !confirm("Turn the authenticator app off? Signing in will need only the password."))) return;
    const r = await acctPost(t.dataset.totp === "off" ? "/api/account/totp/off" : "/api/account/recovery", {}, "acct-totp-msg", t);
    if (r.error) return;
    if (r.recovery) showCodes(r.recovery);
    else {   // (recovery codes still on screen are dead now: they go too)
      $("acct-codes").innerHTML = "";
      acctMsg("acct-totp-msg", "✓ Turned off. Every other browser was signed out.");
      await renderAccount("h-acct-totp");
      refreshSetup();
    }
  } else if (t.dataset.end) {
    if ((await acctPost("/api/account/sessions/end", {id: t.dataset.end}, "acct-s-msg", t)).error) return;
    acctMsg("acct-s-msg", "✓ Signed out.");
    await renderAccount("h-acct-sessions", "lists");
  } else if (t.id === "acct-end-all") {
    if (!confirmFilled() || !confirm("Sign out every other browser (including wall screens)? If someone else may know the password, change it instead: that also forgets every other browser.")) return;
    if ((await acctPost("/api/account/sessions/end", {all: true}, "acct-s-msg", t)).error) return;
    acctMsg("acct-s-msg", "✓ Every other browser is signed out.");
    await renderAccount("h-acct-sessions", "lists");
  } else if (t.id === "acct-notice-ok") {
    if ((await acctPost("/api/account/notice/dismiss", {seen: (A.notice || {}).code || 0}, "acct-alert-msg", t)).error) return;
    $("acct-alerts").innerHTML = "";
    acctMsg("acct-alert-msg", "✓ Warning dismissed. It comes back if it happens again.");
    $("h-acct-pw").focus();
    refreshSetup();
  } else if (t.id === "acct-signout") {
    try { await post("/api/logout", {}); } catch (err) { /* signed out either way */ }
    location.replace("/login");
  }
});

function render() {
  S = structuredClone(V.settings); SEC = {};
  if (V.settings.site.favicon) document.querySelector("link[rel=icon]").href = V.settings.site.favicon;
  document.title = "Settings · " + S.site.title; $("site-title").textContent = S.site.title; $("demo-tag").hidden = !V.demo;
  document.querySelectorAll("[aria-invalid]").forEach(clearErr);
  collectorStart();
  renderInteg(); bindFields(); renderPanels(); renderThemes(); renderServices(); renderExtras(); renderCollector(); renderInfra(); renderSetup();
  if (!acctShown) {   // once: Save / Discard must not wipe codes or a QR code on screen
    acctShown = true;
    renderAccount().then(() => { if (location.hash === "#account") $("account").scrollIntoView(); });   // (it was hidden at load)
  }
  document.querySelectorAll(".ferr").forEach(m => m.remove());
  hideErr(); setDirty(false); applySearch(); markCurrent(); bubbles();
}
async function load() { V = await api("/api/settings"); render(); }
$("reload").onclick = () => { if (!dirty || confirm("Discard your unsaved changes?")) render(); };
$("save").onclick = async () => {
  if (document.activeElement && document.activeElement.matches("input, textarea")) document.activeElement.blur();
  $("save").disabled = true;
  const cch = collectorChanged() ? CKEYS.filter(cchanged) : [], keepC = JSON.stringify(C), keepFP = {...FP};
  $("msg").textContent = cch.length ? "Saving… (and handing the collector its settings)" : "Saving…";
  const form = document.querySelector(".set-main");   // nothing can be edited until the answer is in (it may take seconds)
  const unlock = () => { form.inert = false; form.removeAttribute("aria-busy"); };
  form.inert = true; form.setAttribute("aria-busy", "true");
  try {
    const incomplete = incompleteTables();
    if (incomplete) { unlock(); setDirty(true); incomplete.focus(); return setMsg('<span class="crit-t">✗ Not saved: a card is missing its line or pattern (marked).</span>'); }
    if (cch.length) {   // a save of ours the collector took after the last check: the fingerprints it shows now
      try { V.collector = (await api("/api/settings")).collector; collectorRefresh(false); } catch (e) { /* send what we have */ }
    }
    // empty "not in NPM" rows aren't sent; S keeps them, so the rows on the page still match S if the save is refused
    V = await post("/api/settings", {settings: {...S, services: S.services.filter(x => x.name || x.url)}, secrets: SEC,
                                     ...(cch.length ? {collector: collectorChanges()} : {})});
    render();
    if (V.collector_warning) {   // the dashboard's part is saved; the collector's isn't: keep those edits on the page
      unlock();
      C = JSON.parse(keepC); FP = keepFP; renderCollector(); setDirty(true);
      showErr(V.collector_warning, csection(cch[0]), "Saved, except the collector's settings");
      return setMsg('<span class="warn-t">● Saved, except the collector\'s settings: see the message at the top.</span>');
    }
    const note = cch.length && !V.demo_not_saved ? collectorSaved(cch) : "";
    setMsg(V.demo_not_saved ? '<span class="ok-t">✓ Looks valid! (Demo: nothing is really saved, so the next visitor sees the original.)</span>'
      : `<span class="ok-t">✓ Saved.</span> ${cch.length ? "Dashboard settings apply right away." + note : "The dashboard uses the new settings right away."}`);
  } catch (e) {
    unlock();
    setDirty(true);
    // a collector setting's message names it: open its section
    const key = e.section === "collector" && CKEYS.find(k => e.message.includes(clabel(k)));
    showErr(e.message, e.section === "collector" ? (key ? csection(key) : "drives") : e.section);
    setMsg('<span class="crit-t">✗ Not saved: see the message at the top.</span>');
  } finally {
    unlock();
  }
};
function bubbles() {   // the dashboard's rule: this browser's 🫧 choice, else the saved default ("auto" = the device's)
  const box = $("bubbles");
  let mine = "";
  try { mine = localStorage.getItem("motion") || ""; } catch (e) { /* storage blocked: the default */ }
  const mode = ["on", "off"].includes(mine) ? mine : V.settings.site.motion || "auto";
  const still = mode === "off" || (mode === "auto" && matchMedia("(prefers-reduced-motion: reduce)").matches);
  document.documentElement.dataset.motion = still ? "still" : "move";
  if (still || box.childElementCount) return;
  for (let i = 0; i < 12; i++) { const b = document.createElement("i"), s = 14 + Math.random() * 40;
    Object.assign(b.style, {width: s + "px", height: s + "px", left: Math.random() * 100 + "%",
                            animationDuration: 16 + Math.random() * 20 + "s", animationDelay: -Math.random() * 30 + "s"});
    box.appendChild(b); }
}
load().catch(e => showErr(e.message, "", "Couldn't load the settings"));
