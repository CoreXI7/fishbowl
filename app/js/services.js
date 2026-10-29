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

