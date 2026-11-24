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

// "N stopped" on the Containers row opens the list of names (kept open across the few-second refresh)
let sysStoppedOpen = false, sysLast = null;
document.addEventListener("click", (e) => {
  if (!e.target.closest || !e.target.closest("#sys-stopped-btn") || !sysLast) return;
  sysStoppedOpen = !sysStoppedOpen;
  renderSystem(sysLast[0], sysLast[1]);
  const again = $("sys-stopped-btn");
  if (again) again.focus();
});
// Remove button: only on leftovers the collector marked removable (containers left by `docker compose run`).
// First click arms it ("Sure?"), a second within 4 s sends the id; the root collector checks it again before removing.
let sysArmed = "", sysArmedAt = 0, sysArmTimer = 0, sysRemoving = {};
document.addEventListener("click", async (e) => {
  const b = e.target.closest && e.target.closest("[data-rm-id]");
  if (!b || !sysLast) return;
  const id = b.dataset.rmId;
  if (!/^[0-9a-f]{64}$/.test(id) || sysRemoving[id]) return;
  const refocus = () => { const again = document.querySelector(`[data-rm-id="${id}"]`); if (again) again.focus(); };
  if (sysArmed !== id || Date.now() - sysArmedAt < 400) {   // (a fast double-click is not a confirm)
    if (sysArmed === id) return;
    sysArmed = id; sysArmedAt = Date.now(); clearTimeout(sysArmTimer);
    sysArmTimer = setTimeout(() => { sysArmed = ""; if (sysLast) { renderSystem(sysLast[0], sysLast[1]); refocus(); } }, 4000);
    renderSystem(sysLast[0], sysLast[1]); refocus();   // (the re-render replaces the button: keep the keyboard on it)
    return;
  }
  sysArmed = ""; clearTimeout(sysArmTimer); sysRemoving[id] = Date.now(); sysRmError = "";
  renderSystem(sysLast[0], sysLast[1]);
  try {
    const r = await postJSON("/api/containers/remove", {id});
    if (r && r.demo) { delete sysRemoving[id]; sysRmError = "this is the demo, nothing is removed"; sysRmErrorAt = Date.now(); renderSystem(sysLast[0], sysLast[1]); }
  } catch (err) { delete sysRemoving[id]; sysRmError = String(err.message || err).slice(0, 120); sysRmErrorAt = Date.now(); if (sysLast) renderSystem(sysLast[0], sysLast[1]); }
});
let sysRmError = "", sysRmErrorAt = 0;
function stoppedList(c) {
  const names = Array.isArray(c.stopped) ? c.stopped.filter((x) => x && typeof x.name === "string") : null;
  if (!names) return `<div class="stopped-list mute">Names show up once the collector is updated.</div>`;
  const now = Date.now();
  if (sysRmError && now - sysRmErrorAt > 15000) sysRmError = "";   // an error note doesn't hide later results for long
  for (const id of Object.keys(sysRemoving)) if (now - sysRemoving[id] > 90000 || !names.some((x) => x.id === id)) delete sysRemoving[id];
  const last = c.last_remove && typeof c.last_remove === "object" && now / 1000 - Number(c.last_remove.at || 0) < 600 ? c.last_remove : null;
  // the collector answered for this click (not an older answer about the same id): no more "removing…"
  if (last && typeof last.id === "string" && sysRemoving[last.id] && Number(last.at) * 1000 > sysRemoving[last.id] - 5000) delete sysRemoving[last.id];
  const note = sysRmError ? `<li class="crit-t">Couldn't remove: ${esc(sysRmError)}</li>`
    : last ? `<li class="${last.ok ? "mute" : "crit-t"}">${last.ok ? "Removed" : "Not removed"}: ${esc(String(last.name || ""))}${last.ok ? "" : " (" + esc(String(last.why || "")) + ")"}</li>` : "";
  if (!names.length) return note ? `<ul class="stopped-list">${note}</ul>` : "";
  return `<ul class="stopped-list">` + names.map((x) => {
    const id = typeof x.id === "string" && /^[0-9a-f]{64}$/.test(x.id) ? x.id : "";
    const btn = !(x.removable === true && id) ? ""
      : sysRemoving[id] ? `<span class="mute">removing…</span>`
      : `<button type="button" class="rm-btn${sysArmed === id ? " armed" : ""}" data-rm-id="${id}" title="Remove this leftover container (left by docker compose run). Its volumes are kept.">${sysArmed === id ? "Sure?" : "Remove"}</button>`;
    return `<li><span class="mono">${esc(x.name)}</span><span class="rm-cell"><span class="mute">${esc(String(x.state || ""))}</span>${btn}</span></li>`;
  }).join("") + note + `</ul>`;
}

function renderSystem(s, hw) {
  sysLast = [s, hw];
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
    hw && hw.containers ? kvRow("Containers", `${hw.containers.running} running${stopped > 0 ? ` · <button type="button" id="sys-stopped-btn" class="linkbtn warn-t" aria-expanded="${sysStoppedOpen}" title="Show which ones">${stopped} stopped</button>` : ""}`)
      + (stopped > 0 && sysStoppedOpen ? stoppedList(hw.containers) : "") : "",
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

function zfsBlock(p, smart) {
  const n = v => Number.isFinite(+v) ? +v : 0;   // counters come from zpool output: only ever numbers in the HTML
  const scls = s => !s || s === "ONLINE" || s === "AVAIL" ? "" : s === "DEGRADED" ? "warn-t" : "crit-t";
  const pct = p.size ? 100 * (p.alloc || 0) / p.size : 0;
  const members = (p.vdevs || []).map(v => {
    const x = v.dev ? smart[v.dev] : null;
    const temp = x ? `<span class="chip ${tcls(x.temp, ...(/nvme/.test(v.dev) ? [65, 75] : [45, 50]))}">${x.temp != null ? x.temp + "°C" : "–"}</span>` : "";
    const rwc = n(v.read) > 0 || n(v.write) > 0 || n(v.cksum) > 0
      ? `<span class="chip warn-t" title="read / write / cksum errors">R ${n(v.read)} · W ${n(v.write)} · C ${n(v.cksum)}</span>` : "";
    // healthy members stay quiet (just a temperature); only a problem state gets a badge
    const st = v.state && scls(v.state) ? `<span class="chip ${scls(v.state)}">${esc(v.state)}</span>` : "";
    const pad = 12 * Math.min(4, Math.max(0, n(v.depth)));
    const label = v.kind === "class" ? `<span class="md">${esc(v.name || "")}</span>` : esc(v.name || "");
    // long by-id names end in "…" (full name in the tooltip) so the chips always stay in view (issue #12)
    const tip = [v.name, v.note].filter(Boolean).map(String).join(" · ");
    return `<div class="s zm" style="padding-left:${pad}px"${tip ? ` title="${esc(tip)}"` : ""}><span class="zn">${label}</span><span class="zc">${rwc} ${st} ${temp}</span></div>`;
  }).join("");
  const vd = p.vdevs || [], top = vd.find(v => v.kind === "group" && n(v.depth) === 0);
  const ndisk = vd.filter(v => v.kind === "disk" && !vd.slice(0, vd.indexOf(v)).some(c => c.kind === "class" && n(c.depth) === 0)).length;
  const extra = vd.filter(v => v.kind === "class").map(v => esc(v.name || "")).join(" + ");
  const kind = ["ZFS pool", top ? esc(String(top.name || "").replace(/-\d+$/, "")) : "", ndisk ? `${ndisk} disk${ndisk > 1 ? "s" : ""}` : ""].filter(Boolean).join(" · ") + (extra ? ` + ${extra}` : "");
  const stateChip = p.state ? `<span class="chip ${scls(p.state) || "ok-t"}">${p.state === "ONLINE" ? "✓ " : ""}${esc(p.state)}</span>` : "";
  return `<div class="drv"><div class="h"><b>${esc(p.name || "")}</b>${stateChip}</div><div class="md">${kind}</div>`
    + (p.size ? `${meter(pct, 90, 95)}<div class="s"><span>${size(p.alloc || 0)} / ${size(p.size)}</span><span>${p.frag != null ? `frag ${n(p.frag)}%` : ""}</span></div>` : "")
    + (p.scan ? `<div class="md" style="white-space:normal">${esc(p.scan)}</div>` : "")
    + (p.stuck ? `<div class="s crit-t">zpool status not responding (pool suspended?)</div>`
       : p.failed ? `<div class="s warn-t">zpool status failed: state from the kernel only</div>` : "")
    + (n(p.data_errors) > 0 ? `<div class="s crit-t">${n(p.data_errors)} data errors (zpool status -v)</div>` : "")
    + members + `</div>`;
}

function renderDrives(hw) {
  const anyPool = !!(hw && hw.zfs && Array.isArray(hw.zfs.pools) && hw.zfs.pools.length);
  if (!(hw && hw.disks && hw.disks.length) && !anyPool) {
    setHTML("drv", `<div class="empty">Drive stats need the collector service.</div>`);
    kpi("k-disk", "", "–", "needs the collector");
    return;
  }
  const smart = hw.smart || {}, disks = hw.disks || [];
  // the totals count data drives only (the system disk would skew "free space"), unless that's all there is
  const data = disks.filter(d => !d.system && d.total);
  const pool = data.length ? data : disks.filter(d => d.total);
  const isZfs = d => d.zfs && !d.dev;
  let tot, used, free;
  if (pool.some(isZfs)) {
    // datasets of one pool share its free space: count that once (largest free), each dataset's used once
    const shared = {};
    pool.filter(isZfs).forEach(d => { const k = d.pool || d.zfs; shared[k] = Math.max(shared[k] || 0, d.free); });
    used = pool.reduce((a, d) => a + d.used, 0);
    free = pool.reduce((a, d) => a + (isZfs(d) ? 0 : d.free || 0), 0) + Object.values(shared).reduce((a, v) => a + v, 0);
    tot = used + free;
  } else {
    tot = pool.reduce((a, d) => a + d.total, 0);
    used = pool.reduce((a, d) => a + d.used, 0);
    free = pool.reduce((a, d) => a + (d.free || 0), 0);   // what df calls available (leaves out ext4's reserved blocks)
  }
  setText("drv-n", `${size(used)} / ${size(tot)}`);

  const z = hw.zfs && !hw.zfs.error && hw.zfs.pools ? hw.zfs.pools : [];
  const zerr = hw.zfs && hw.zfs.error ? `<div class="empty">ZFS: could not read pools</div>` : "";
  const smartBad = disks.some(d => d.missing || (smart[d.dev] || {}).passed === false);
  const full = disks.some(d => d.total && d.used / (d.used + d.free) > .95);
  const sectors = disks.filter(d => { const x = smart[d.dev] || {}; return x.realloc > 0 || x.pending > 0; }).length;
  const stuck = !!(hw.zfs && hw.zfs.stuck);
  const poolBad = stuck || z.some(p => p.stuck || (p.state !== "ONLINE" && p.state !== "DEGRADED") || p.data_errors > 0);
  const poolWarn = z.some(p => p.state === "DEGRADED" || (p.vdevs || []).some(v => v.read > 0 || v.write > 0 || v.cksum > 0));
  const znote = (stuck ? ["<b>ZFS not responding</b>"] : []).concat(
    z.filter(p => p.state && p.state !== "ONLINE").map(p => `<b>pool ${esc(p.name || "")} ${esc(p.state)}</b>`)).join(" · ");
  kpi("k-disk", smartBad || poolBad ? "bad" : full || sectors || poolWarn ? "warn" : "ok", `${size(free)} free`,
      `${znote ? znote + " · " : ""}${(tot ? 100 * used / tot : 0).toFixed(0)}% of ${size(tot)}${smartBad ? " · <b>drive problem!</b>" : sectors ? ` · ${sectors} drive${sectors > 1 ? "s" : ""} with bad sectors` : ""}`);

  const drive = d => {
    if (d.missing) return `<div class="drv"><div class="h"><b class="crit-t">${esc(d.name)}</b><span class="chip crit-t">NOT MOUNTED</span></div></div>`;
    if (isZfs(d)) {
      const pct = 100 * d.used / (d.used + d.free);   // same % as df
      return `<div class="drv" title="${esc(d.mount || d.zfs)}"><div class="h"><b>${esc(d.name)}</b></div>
      <div class="md">ZFS · ${esc(d.zfs)}</div>${meter(pct, 90, 95)}
      <div class="s"><span>${size(d.used)} / ${size(d.total)}</span><span>${pct.toFixed(0)}% · ${size(d.free)} free</span></div></div>`;
    }
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
  setHTML("drv", kvRow(data.length ? "All data drives" : "Total", `${size(free)} free`) + meter(tot ? 100 * used / tot : 0, 90, 95)
    + disks.map(drive).join("") + z.map(p => zfsBlock(p, smart)).join("") + zerr);
}

