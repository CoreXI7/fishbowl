// ---------- qBittorrent ----------
const TOR_STATE = {downloading: "downloading", stalledDL: "stalled", metaDL: "fetching metadata", forcedDL: "forced",
                   queuedDL: "queued", checkingDL: "checking", pausedDL: "paused", stoppedDL: "stopped"};

function torTab(which) {
  ["dl", "seed", "nzb"].forEach(w => { $(w).hidden = w !== which; $("t-" + w).classList.toggle("on", w === which); });
}
$("t-dl").addEventListener("click", () => torTab("dl"));
$("t-seed").addEventListener("click", () => torTab("seed"));
$("t-nzb").addEventListener("click", () => torTab("nzb"));

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
  if (!CFG.features.qbit) { document.body.classList.add("no-tor-client"); return; }   // (wall mode: no empty torrent lists)
  try {
    const q = await getJSON("/api/qbit");
    setHTML("dl", q.downloading.length ? q.downloading.map(t => torRow(t, true)).join("") : `<div class="empty">Nothing downloading.</div>`);
    setHTML("seed", q.seeding.length ? q.seeding.map(t => torRow(t, false)).join("") : `<div class="empty">No active uploads right now.</div>`);
    setText("t-dl", `Downloading (${q.downloading.length})`);
    setText("t-seed", `Seeding (${q.seeding_active})`);
    // all-time totals when qBittorrent reports them (this session's in the tooltip), else this session's, labelled so
    const all = q.up_alltime != null && q.dl_alltime != null;
    setText("tor-n", all ? `· ${q.total} torrents · all time ↑ ${size(q.up_alltime)} ↓ ${size(q.dl_alltime)}`
                         : `· ${q.total} torrents · this session ↑ ${size(q.up_total)} ↓ ${size(q.dl_total)}`);
    $("tor-n").title = all ? `this session: ↑ ${size(q.up_total)} ↓ ${size(q.dl_total)}` : "";
    // VPN check: qbit's external IP equal to your home IP means it's NOT going through the VPN
    const connOk = q.connection !== "disconnected" && !q.vpn_leak && q.external_ip;
    const mbs = b => (b / 1048576).toFixed(b >= 10485760 ? 0 : 1);
    const speeds = `↓ ${mbs(q.dl_speed)} ↑ ${mbs(q.up_speed)} <small style="font-size:.55em">MB/s</small>`;
    if (q.client === "transmission") {
      // no VPN check for Transmission
      kpi("k-net", "ok", speeds, `Transmission · ${esc(q.downloading.length)} downloading`);
    } else {
      kpi("k-net", q.vpn_leak ? "bad" : connOk ? "ok" : "warn", speeds,
          q.vpn_leak ? "<b>VPN LEAK: home IP!</b>" : `${q.vpn_check ? "VPN" : "IP"} ${esc(q.external_ip || "?")} · ${esc(q.connection)}`);
    }
    $("banner").hidden = !q.vpn_leak;
    if (q.vpn_leak) setText("banner", "qBittorrent is using your home IP. The VPN is not protecting it!");
  } catch (e) {
    setHTML("dl", `<div class="err">Torrents: ${esc(e.message)}</div>`);
    kpi("k-net", "warn", "torrents ?", "unreachable");
  }
}

// ---------- SABnzbd ----------
let sabTabPicked = false;

function sabRow(j) {
  const pct = (+j.progress * 100 || 0).toFixed(1);
  return `<div class="tor"><div class="t" title="${esc(j.name)}">${esc(j.name)}</div>
    <div class="bar"><i style="width:${pct}%"></i></div>
    <div class="s"><span>${pct}% of ${size(j.size)}</span><span>${size(j.size_left)} left</span><span>${esc(j.time_left)} left</span><span>${esc(j.status)}</span>${j.category ? `<span>${esc(j.category)}</span>` : ""}</div></div>`;
}

function sabHistRow(h) {
  return `<div class="tor"><div class="t" title="${esc(h.name)}">${esc(h.name)}</div>
    <div class="s"><span>${size(h.size)}</span><span>${esc(h.status)}</span>${h.fail ? `<span>${esc(h.fail)}</span>` : ""}<span>${esc(new Date(h.completed * 1000).toLocaleString())}</span></div></div>`;
}

async function loadSab() {
  if (!CFG.features.sab) return;
  // only SABnzbd set up: show the Usenet tab instead of the (hidden) torrents tabs
  if (!CFG.features.qbit && !sabTabPicked) { sabTabPicked = true; torTab("nzb"); }
  document.body.classList.add("has-sab");   // (wall mode shows the Usenet list when there's no torrent client)
  try {
    const s = await getJSON("/api/sab");
    const queue = s.queue.length ? s.queue.map(sabRow).join("") : `<div class="empty">Usenet queue is empty.</div>`;
    const hist = s.history.length ? s.history.map(sabHistRow).join("") : `<div class="empty">No recent jobs.</div>`;
    setHTML("nzb", queue + `<div class="nzb-h">Recent</div>` + hist);
    setText("t-nzb", `Usenet (${s.queue_total})`);
  } catch (e) {
    setHTML("nzb", `<div class="err">SABnzbd: ${esc(e.message)}</div>`);
  }
}
