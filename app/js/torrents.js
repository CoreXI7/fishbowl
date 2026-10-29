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

