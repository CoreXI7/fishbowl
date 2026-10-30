# Threat model

What Fishbowl protects, who might attack it, where the trust boundaries are, and which controls hold each one.
Written for the reference setup (a home server behind a reverse proxy with SSO), but the reasoning applies to any install.

## What's worth protecting

| Asset | Where it lives | Why it matters |
|---|---|---|
| Integration secrets (qBittorrent login, Seerr and Tautulli API keys) | `data/secrets.json` (0600) or the container environment | Control over downloads, requests, Plex activity |
| Security state: known IPs, acknowledged logins, approved crontabs | `data/settings.json`, `data/acks.json`, the collector's `cron-approved.json` (root) | Changing it silences the alerts the dashboard exists for |
| The root collector | systemd service, runs as root | Anything that steers it runs with root's power |
| Information about the host | `hw.json`, `sec-detail.json`, the API | Firewall rules, cron lines, who logs in from where, backup paths: reconnaissance for a next step |
| Availability of monitoring | collector main loop, the dash server | A frozen collector means every tile goes quietly stale |

## Components and data flow

```
 internet ─► reverse proxy (only 443) ─► SSO (2FA; a LAN bypass is a deployment choice)
                         │
                         ▼
                dash container  (uid 1000, read-only rootfs, cap_drop ALL, 256 MB, answers only the proxy's IP)
                 │   reads: NPM proxy_host/*.conf (ro), /proc + hwmon (ro), hw.json + sec-detail.json (ro)
                 │   writes: data/ (settings, secrets, acks, uploads), req/cron-approve (a 64-hex hash)
                 │   calls out: qBittorrent, Seerr, Tautulli (API keys); every service upstream (health check,
                 │              favicon); the proxy itself (checks via the proxy: which hosts and paths is a
                 │              setting on the page since Oct 29, so a page user can make a down service look up)
                 ▼
 /var/lib/homelab-dash/  hw.json, sec-detail.json (root:dash-group 0640)  ◄── homelab-dash-collector (root)
                                         req/ (owned by the dash uid)    ──► read by the collector each round
     collector reads: /proc, /sys, journald (sshd), `docker logs` (Authelia), Authelia's SQLite file (as bytes),
                      fail2ban, ufw, crontabs, lsattr, smartctl, nvidia-smi, backup logs, `du` of backup folders
     collector never listens on the network and makes no outbound connections.

 public demo container: same code, DEMO=1, fake data, internal network shared only with the proxy, no secrets,
                        no host mounts, nothing saved, no outbound connections.
```

## Trust boundaries

1. **Internet → proxy → SSO → dash.** The dash has no login of its own; the SSO is the gate. Anyone past it
   (or inside the SSO's LAN bypass) is an admin.
2. **Other containers → dash.** Many containers share the proxy's Docker network. They can open a connection
   to the dash's port; the app answers only the proxy's IP (`TRUSTED_PROXY`). The check is by source IP.
3. **Upstream data → page.** Text from qBittorrent (torrent names, numbers), Seerr (titles, user names, ids),
   Tautulli/Plex (titles, players), NPM host names, and the collector (log lines, process and container names,
   cron lines, user names typed at a login prompt) is **attacker-controlled**. It reaches the page through the
   JSON API and is rendered with `innerHTML`.
4. **Upstream services ← dash.** The dash fetches from every service upstream (health check, favicon) and
   from the integrations. Any of those services may be hostile and may answer with redirects or huge bodies.
5. **Dash → root collector.** The only intended channel is `req/cron-approve` (a hash the collector verifies).
   The folder is writable by the dash's uid, so anything the dash (or another process with that uid) puts there
   is untrusted input to root.
6. **Containers → collector inputs.** Containers can write to their own volumes (Authelia's database folder,
   files the backup checks look at), produce log lines, and name their processes. All of it is untrusted.
7. **Public demo visitors.** Anyone on the internet. They can send any request to the demo.

## Attackers and what they can do

| Attacker | Starting point | Realistic goal |
|---|---|---|
| A1 Internet, unauthenticated | the proxy | get past the SSO, abuse the public demo |
| A2 Hostile container | code execution in a container (one was taken over by a cryptominer in this setup's past) | feed HTML/JS into the page, steer the dash's outbound requests, freeze or mislead the collector, reach the dash directly |
| A3 Log writer | can make a log line exist (SSH user name, failed SSO login, process name, torrent name) | inject markup, slow the parsers, fake a known IP |
| A4 Public demo visitor | the demo | leak real data, run code, deny service to the host |
| A5 Compromised dash container | code execution as uid 1000 inside the dash | write into `req/`, read `hw.json` |
| (out of scope) host user with Docker or root | already root-equivalent | — |

## Where attacker-controlled text goes

| Source | Sink | Control |
|---|---|---|
| qBittorrent / Seerr / Tautulli JSON | page `innerHTML` | the server passes on only typed, clipped fields (`num()`, `text()`; ids must be ints); the page escapes every integration value, numbers included (the collector's own numbers are typed by the collector); CSP `script-src 'self'` with no inline script or handlers |
| Tautulli poster art | `/api/plexart` on the dash's origin | raster image types only, `Content-Security-Policy: default-src 'none'` |
| NPM `server_name`, settings text | page `innerHTML`, attributes | `esc()`; settings validated server-side |
| Log lines, process names, cron lines | `hw.json` / `sec-detail.json` → page | `clip()` (length, control and bidi characters), `redact()` (secrets), `esc()` in the page |
| Request path, headers | dash container log | `log_safe()`: control characters shown as `\xNN`, clipped; `X-Real-IP` and the user header count only from the proxy |
| Authelia SQLite file | root collector's SQLite | bytes read with `O_NOFOLLOW`/`O_NONBLOCK`, size cap, in-memory copy, defensive mode, real tables only, parameter-free fixed queries, row + time limits |
| `req/cron-approve` | root collector | read with `O_NOFOLLOW`/`O_NONBLOCK`, regular files only, 100 bytes; must be 64 hex chars and equal the live crontab hash |
| Upstream redirects | dash outbound requests | never followed off-host (health checks count a 3xx as up; icons follow at most 3 on the same host; integrations none) |
| Upstream answer size | dash memory | `read_json()`: 32 MB qBittorrent, 8 MB Seerr/Tautulli, at most 1,000,000 JSON values; the torrent state is capped at 20,000 |
| SSH journal (log lines an attacker writes) | root collector | one bounded read a minute: lines over 1,100 bytes skipped without being held, ASCII only, newest 100,000 kept, process group killed on timeout; the card and the details use the same anchored parser |
| Shell | none | every command is an argument list, never a shell string |

## What runs as root

The collector, as `python3 -I`, with `ProtectSystem=full`, `ProtectHome=read-only`, `PrivateTmp`, `NoNewPrivileges`,
a memory and task cap. On top of that, `install.sh` runs `collector/sandbox.sh`, which adds the strictest of three
sandbox levels the collector still works under on that machine (it runs `--selftest` twice under the plain unit and
once under each level, and keeps the first that answers like the plain runs; the self-test reports facts only, uses
`collector.toml` and approved settings but never the page's automatic ones, and the collector is restarted and checked
afterwards, the level removed again if it stops updating): no IP traffic at all (`IPAddressDeny=any`), the whole filesystem read-only
except the output folder (`ProtectSystem=strict`), kernel tunables/modules/logs/cgroups/clock protected, no new
namespaces, SUID or realtime, and, at the strictest level, only the capabilities it uses (`CAP_DAC_READ_SEARCH`,
`CAP_CHOWN`, `CAP_FOWNER`, `CAP_SYS_RAWIO`, `CAP_NET_ADMIN`, `CAP_NET_RAW`), `MemoryDenyWriteExecute` and the
`@system-service` syscall set (an unexpected call fails with EPERM instead of killing the collector). Commands: `journalctl`, `docker ps/logs`, `ps`, `top`, `fail2ban-client`, `ufw`, `lsattr`,
`smartctl`, `nvidia-smi`, `findmnt`, `systemctl list-timers`, `du` (idle priority). Files: `/proc`, `/sys`, crontabs,
`authorized_keys` (no symlink at the name), Authelia's database (as above), configured log files (tails, regular files only).
It writes only to its state folder (and, with `--apply`, `collector.page.toml`).

## Collector settings from the settings page (the second channel from the dash to root)

The page writes only what was changed, each setting with a fingerprint of the value the page showed (a hash of what
the collector reported) and the new value, into
`req/collector-settings.json` (a root-owned folder the dash's group can write to). The collector treats that file as
written by an attacker who controls the dash container:

- It's read when it changes (and once a minute) without following a symlink, without waiting on a FIFO, only as a
  regular file, ≤ 256 KB; JSON that's too deep, not UTF-8 or another version is refused as a whole, and what the page
  set before stays as it is. Each request is handled once (by its hash), at most one every 10 s.
- Only keys the collector knows are read; `out_dir` and `dash_uid` never come from it; unknown keys are reported back,
  escaped and at most 20 of them.
- A change is taken only while the setting still has the value the page showed; otherwise someone changed it on the
  host meanwhile, and the change is refused. Both kinds of page values remember the `collector.toml` value they
  replaced: editing that setting in `collector.toml` later wins over the page.
- **Applied by itself** (a hijacked dash already controls what's shown, so these give it nothing new): on/off switches,
  the update interval (2–60 s), mount points (each a real mount point the collector found itself, or one it already
  shows), labels, the network interface (must exist), the fail2ban jail, ssh unit and Authelia container (each must be
  one the collector detected itself), process names (`[A-Za-z0-9._-]{1,32}`), backups' overdue hours. They're kept
  root-only (`page-state.json`, 0600; rewritten only when something in it changed).
- **Wait for a person** (they'd make root read a file or run a pattern the dash picked): every path, every regex, the
  watch-script and backup tables, and turning any of them off. Nothing happens until someone runs
  `sudo homelab-dash-collector --apply` on the host, which needs a terminal and shows only what changes, marked
  (`+` added, `-` removed, `~` old -> new), whole (never cut), table entries by their name (two entries can't share
  one), unchanged ones counted. Every string is shown as printable ASCII only (anything else as `\uXXXX`), so a value
  can't repaint the terminal, hide itself or pass for other letters. It warns when a path's folder belongs to the
  dash's user or anyone can write to it, or when a path is a link. It asks y (apply), N (not now) or d (drop them from
  the waiting list), and applies only if the page didn't change the list meanwhile. Approved values go to
  `/etc/homelab-dash/collector.page.toml` (root; ignored unless only root can write it; a damaged one is ignored, not a
  crash); `collector.toml` is never rewritten. `--apply --undo` drops every page setting and the page's request.
- Paths must be absolute and normalized (no `..`, no `//`), wildcards only after the first folder, and never in `/proc`,
  `/sys`, `/dev` or the collector's own folder (checked part by part, so a wildcard can't reach them either); patterns
  must compile and are capped at 300 characters, paths at 512. A path the page set is never read through a link,
  anywhere on the way (opened folder by folder with `O_NOFOLLOW`), and FIFOs never block. Approving a path still means
  root reads that file: read what it shows.

## Controls that hold today

- Secrets never reach the browser; errors are scrubbed; changing an integration URL requires re-entering its key.
- Every POST needs `Origin` = the dashboard (CSRF). Every GET is read-only.
- `TRUSTED_PROXY`: direct connections from other containers get 403 (every address of the proxy counts).
- `LISTEN` (optional): the dash answers only on given addresses, e.g. a network that only the proxy shares with it,
  so other containers can't reach it at all. This is the control for trust boundary 2.
- CSP `script-src 'self'`, no inline script; fonts and styles from the dashboard only.
- Uploads: raster images only, 2 MB each, 200 files / 100 MB in all.
- The demo has no secrets, no mounts, no network beyond the proxy, and never saves.
- The collector's log parsers cap sizes, skip giant lines, take IPs from the end of lines and validate them.

## Fixed in the 2026-10-29 hardening

Each with a test that failed before the fix (`tests/`):

| Finding | Fix |
|---|---|
| Script injection from qBittorrent/Seerr fields; CSP allowed inline script | typed fields on the server, escaping on the page, strict CSP |
| Redirect-following in outbound requests (SSRF, API key sent along) | no off-host redirects |
| Collector trusted the file type in `req/` (FIFO stalled it, symlinks followed) | no-follow, non-blocking, regular files only |
| Unbounded upstream reads | size caps |
| Forgeable user/IP and raw control characters in the log; malformed request crashed the logger | `log_safe()`, proxy-only headers, `USER_HEADER` opt-in |
| IPv6 known-IP prefixes never matched | canonical addresses + prefix matching |
| Uploads unbounded in number | 200 files / 100 MB |
| Source-IP-only proxy check on a shared Docker network | `LISTEN` + a network shared only with the proxy (opt-in: needs a compose and proxy change); clients that aren't the proxy are dropped before a thread starts |

Found by the reviewers of the fixes, also fixed and tested: poster art from a hostile Tautulli could serve script on the
dash's origin; a crafted SSH user name could fake a login on the card (and "Add all" could then trust a whole /8);
deeply nested JSON could exhaust memory under the byte caps; several ways a misbehaving journalctl could stall or
bloat the collector.

## Still open

- A person can approve a bad path or pattern with `--apply` without reading the diff: approval is only as good as the
  reading. (The dash alone can't apply them.)

- Without `LISTEN`, a container on the proxy's network that can forge the proxy's address (ARP spoofing needs only the
  default `NET_RAW` capability) can still reach the dash as the proxy, and read what the proxy sends it.
- The page loads service icons from a public CDN (`cdn.jsdelivr.net`) and posters from TMDB when those are configured.
- `style-src` still allows inline styles (the page sets widths and colours inline); styles can't run script.
- A malicious `journalctl` (that is, root already compromised) whose child calls `setsid()` can keep the detail
  thread's journal read open; the main loop isn't affected (it shows the last good SSH numbers, marked as old).
