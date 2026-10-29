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

