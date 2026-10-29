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

