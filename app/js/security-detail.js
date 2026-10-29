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
// a long path wraps only after a slash (each folder name is one unbreakable piece, unless it alone is too wide)
const sdPath = p => String(p).split(/(?<=\/)/).map(s => `<span class="seg">${esc(s)}</span>`).join("");
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
      (j.files ? kvRow("Files", `${esc(j.files.count)} · ${size(j.files.bytes)}`) +
        kvRow("Age", `newest ${sdAgo(j.files.newest)}${j.files.count > 1 ? ` · oldest ${sdAgo(j.files.oldest)}` : ""}`) : "") +
      paths("Missing", j.missing) + paths(`Older than ${esc(j.max_age_hours)} h`, j.old_files) +
      (d && d.missing ? kvRow("Destination", `<span class="crit-t">${sdPath(d.path)} missing</span>`) : "") +
      (d && !d.missing ? `<div class="kv sd-dest"><span>Destination</span><span class="mono">${sdPath(d.path)}</span></div>` +
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

