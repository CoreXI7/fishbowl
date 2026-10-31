# Threat model

What Fishbowl protects, who might attack it, where the trust boundaries are, and which controls hold each one.
Written for the reference setup (a home server behind a reverse proxy, signed in with Fishbowl's own sign-in or the
proxy's SSO), but the reasoning applies to any install.

## What's worth protecting

| Asset | Where it lives | Why it matters |
|---|---|---|
| The dashboard account (password hash, authenticator secret, recovery-code hashes, sessions) | `data/auth.json`, `data/sessions.json` (0600) | Whoever signs in is the admin |
| Integration secrets (qBittorrent login, Seerr and Tautulli API keys) | `data/secrets.json` (0600) or the container environment | Control over downloads, requests, Plex activity |
| Security state: known IPs, acknowledged logins, approved crontabs | `data/settings.json`, `data/acks.json`, the collector's `cron-approved.json` (root) | Changing it silences the alerts the dashboard exists for |
| The root collector | systemd service, runs as root | Anything that steers it runs with root's power |
| Information about the host | `hw.json`, `sec-detail.json`, the API | Firewall rules, cron lines, who logs in from where, backup paths: reconnaissance for a next step |
| Availability of monitoring | collector main loop, the dash server | A frozen collector means every tile goes quietly stale |

## Components and data flow

```
 internet ─► reverse proxy (only 443) ─► Fishbowl's own sign-in (AUTH=login), or the proxy's SSO (AUTH=proxy)
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
                      fail2ban, ufw, crontabs, file attributes, smartctl, nvidia-smi, backup logs, `du` of backup folders
     collector never listens on the network and makes no outbound connections.

 public demo container: same code, DEMO=1, fake data, internal network shared only with the proxy, no secrets,
                        no host mounts, nothing saved, no outbound connections.
```

## Trust boundaries

1. **Internet → proxy → sign-in → dash.** With `AUTH=login` (the default) the dash's own sign-in is the gate (below);
   with `AUTH=proxy` the proxy's SSO is, and anyone past it (or inside its LAN bypass) is an admin.
2. **Other containers → dash.** Many containers share the proxy's Docker network. They can open a connection
   to the dash's port; the app answers only the proxy's IP (`TRUSTED_PROXY`). The check is by source IP.
3. **Upstream data → page.** Text from qBittorrent (torrent names, numbers), Seerr (titles, user names, ids),
   Tautulli/Plex (titles, players), NPM host names, and the collector (log lines, process and container names,
   cron lines, user names typed at a login prompt) is **attacker-controlled**. It reaches the page through the
   JSON API and is rendered with `innerHTML`.
4. **Upstream services ← dash.** The dash fetches from every service upstream (health check, favicon) and
   from the integrations. Any of those services may be hostile and may answer with redirects or huge bodies.
5. **Dash → root collector.** The intended channels are `req/cron-approve` (a hash the collector verifies) and
   `req/collector-settings.json` (see "Collector settings from the settings page").
   The folder is writable by the dash's uid, so anything the dash (or another process with that uid) puts there
   is untrusted input to root.
6. **Containers → collector inputs.** Containers can write to their own volumes (Authelia's database folder,
   files the backup checks look at), produce log lines, and name their processes. All of it is untrusted.
7. **Public demo visitors.** Anyone on the internet. They can send any request to the demo.

## Attackers and what they can do

| Attacker | Starting point | Realistic goal |
|---|---|---|
| A1 Internet, unauthenticated | the proxy | get past the sign-in (or the SSO, with AUTH=proxy), abuse the public demo |
| A2 Hostile container | code execution in a container (one was taken over by a cryptominer in this setup's past) | feed HTML/JS into the page, steer the dash's outbound requests, freeze or mislead the collector, reach the dash directly |
| A3 Log writer | can make a log line exist (SSH user name, failed SSO login, process name, torrent name) | inject markup, slow the parsers, fake a known IP |
| A4 Public demo visitor | the demo | leak real data, run code, deny service to the host |
| A5 Compromised dash container | code execution as uid 1000 inside the dash | write into `req/`, read `hw.json` |
| (out of scope) host user with Docker or root | already root-equivalent | — |

## Where attacker-controlled text goes

| Source | Sink | Control |
|---|---|---|
| qBittorrent / Seerr / Tautulli JSON | page `innerHTML` | the server passes on only typed, clipped fields (`num()`, `text()`; ids must be ints); the page escapes every integration value, numbers included (the collector's own numbers are typed by the collector); CSP `script-src 'self'` with no inline script or handlers |
| Tautulli poster art, service icons | `/api/plexart`, `/api/icon` on the dash's origin | raster image types only (the type read case-blind, trimmed); icons only from the service's own origin (`/` and `/favicon.ico`, never its URL's path or query); `Content-Security-Policy: default-src 'none'; sandbox` and `Cross-Origin-Resource-Policy: same-origin` (as on every answer) |
| NPM `server_name`, settings text | page `innerHTML`, attributes | `esc()`; settings validated server-side |
| Log lines, process names, cron lines | `hw.json` / `sec-detail.json` → page | crontabs split on `\n` only, as cron does (a `\r` can't hide the rest of a line); lines over 8 KB cut when read, over 1,000 characters before `redact()`, the card says what was cut and how many lines it didn't list; `clip()` (length, control and bidi characters), `redact()` (common ways of putting a secret on a command line; a bare argument can't be told apart), `esc()` in the page |
| Request path, headers | dash container log | `log_safe()`: control characters shown as `\xNN`, clipped; `X-Real-IP` and the user header count only from the proxy |
| Authelia SQLite file | root collector's SQLite | bytes read with `O_NOFOLLOW`/`O_NONBLOCK`, size cap, in-memory copy, defensive mode, plain tables only (no virtual tables, no generated columns: either runs code the file chose), cell size checks, 256 KB values, parameter-free fixed queries, row + time limits |
| `req/cron-approve` | root collector | read with `O_NOFOLLOW`/`O_NONBLOCK`, regular files only, 100 bytes; must be 64 hex chars and equal the live crontab hash |
| Upstream redirects | dash outbound requests | never followed off-host (health checks count a 3xx as up; icons follow at most 3 on the same host; integrations none) |
| Upstream answer speed (one byte every few seconds) | dash threads and connection slots | every outbound request has an overall deadline (3x its timeout: 15 s for checks and icons, 30 s for integrations); at the deadline its socket is shut down; an icon cut short isn't kept; one icon lookup tries at most 5 URLs in 20 s, one at a time per service, and a Seerr refresh looks titles up for 20 s at most |
| Upstream answer size | dash memory | headers at most 64 KB an answer (http.client alone takes ~6 MB), chunk sizes must be plain hex (http.client reads "-1" as "to the end": CVE-2026-103995, still in 3.12.16); `read_json()`: 32 MB qBittorrent, 8 MB Seerr/Tautulli, at most 1,000,000 JSON values; the torrent state is capped at 20,000 (ids at 64 characters), lists at 100 Plex sessions and 50 requests, qBittorrent's cookies at 50 |
| SSH journal (log lines an attacker writes) | root collector | one bounded read a minute: lines over 1,100 bytes skipped without being held, ASCII only, newest 100,000 kept, process group killed on timeout; the card and the details use the same anchored parser |
| Authelia's log (failed sign-ins anyone can cause, user names they choose) | root collector | streamed line by line, lines over 2,000 bytes skipped without being held, only the count and the newest 6 kept; docker's process group killed after 30 s |
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
`@system-service` syscall set (an unexpected call fails with EPERM instead of killing the collector). Commands: `journalctl`, `docker ps/logs`, `ps`, `top`, `fail2ban-client`, `ufw`,
`smartctl`, `nvidia-smi`, `findmnt`, `systemctl list-timers`, `du` (idle priority). Files: `/proc`, `/sys`, crontabs,
`authorized_keys`, Authelia's database (as above), configured log files (tails), the attributes of the files you
listed as immutable (an ioctl, not `lsattr`). Every one of these is reached part
by part as a path only (`O_PATH`, which runs no driver code), checked to be a regular file, and only then opened (so
a device node planted in a container's folder is never opened: `/dev/watchdog` would reset the host); a link is
followed only where it sits in one of the system's own folders (`/`, `/usr`, `/var`, root's alone: `/var/run`, `/bin`),
never anywhere else (a folder Docker made `root:root 0755` for a container is the container's to write).
Backup files' sizes and dates come from regular files only, never a link's target.
It writes only to its state folder (and, with `--apply`, `collector.page.toml`), every file created readable by root
alone (umask 077, `UMask=0077`); `hw.json` and `sec-detail.json` are opened up to the dash's group only once complete.

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
  anywhere on the way (opened folder by folder with `O_NOFOLLOW`), and FIFOs and device nodes are never opened. Approving a path still means
  root reads that file: read what it shows.

## The sign-in (`AUTH=login`)

Built to NIST SP 800-63B-4 and the OWASP Authentication, Password Storage and Session Management cheat sheets.

- **Every request** except the sign-in page, the stylesheets, the app's scripts under `/js/` and files under `/static/`
  (all open source: the same as on GitHub, no data), the state the sign-in page needs (`/api/auth/state`) and
  `/healthz` needs a session. Pages redirect to `/login`, the API answers 401. The request target must be a plain
  path (`//`, `\`, absolute URIs are refused), and the gate and the router read it the same way (`urlsplit`), so no
  spelling of a path reaches a page the gate didn't check. `next=` only ever leads to `/` or `/settings` (with a plain
  query such as `?wall`).
- **First run**: the account can only be created with a one-time setup code (125 bits) from the container's log (only
  its SHA-256 is kept in `data/setup-code`), compared in constant time, throttled; creating it is atomic (one account,
  ever, also against a reset on the host).
- **Passwords**: 15–1024 characters, NFKC-normalised, checked against 5,531 common ones of that length and the
  account's own words; scrypt N=2^15 r=8 p=3 (32 MiB) with a 16-byte salt, one hash at a time (no memory DoS); a
  wrong name costs a hash too; constant-time comparisons; generic "wrong name or password".
- **Guessing**: per address (the proxy's X-Real-IP only from `TRUSTED_PROXY`; IPv6 per /64) and for the account: after 5 failures
  each try waits twice as long (≤15 min); each try is counted (and saved) before it is checked, under one lock, so
  parallel tries can't slip under a limit; 100 consecutive failures lock the account for new browsers until
  `auth.py unlock` on the host. The account-wide waits are on purpose (they stop guessing from many addresses at once), so
  someone who keeps guessing keeps every *new* browser waiting, and a trickle of about a day reaches the lock; browsers
  that signed in before aren't affected, and the sign-in page says so when it makes a new one wait. Password, code
  and setup-code failures all count. The code step counts too but never waits (its own cap is 5 codes per password
  step): otherwise someone else's guessing could stretch the waits past the 5 minutes the step lasts, and no new browser
  could finish signing in.
- **Trusted devices** (OWASP "device cookies"): a browser that signed in before carries a 1-year `fishbowl-device`
  token (stored as SHA-256, 10 kept). Its tries count on their own (10 wrong ones and it's just another browser), so
  someone guessing can't lock the owner out; its successes never reset the account's count, so the lock still trips.
- **Telling the owner**: failures are kept apart from sign-ins (so they can't push them out of view) and counted until
  the next sign-in, which shows them once. Only the owner's own typos are left out, and only per browser, never per
  address (an attacker can share the owner's address): wrong codes of that very sign-in, and wrong passwords and codes
  typed in that same trusted browser in the 10 minutes before. A right code typed a second time (used already) is
  refused but isn't counted as a wrong one (it still uses up one of its step's 5 tries). A half-finished sign-in that runs out without the right code
  counts as a wrong code (someone with the password who never types a code is shown too). "The right password with a wrong code" adds up into a lasting Setup
  checklist problem until the password changes or the owner says "That was me" (which needs the password, and the
  code when two-step is on).
- **Sessions**: 256-bit tokens, stored as SHA-256; a new one at every sign-in (no fixation); HttpOnly,
  SameSite=Strict, `__Host-` + Secure over HTTPS (only that name is read over HTTPS, so a sibling subdomain can't set
  one); every value of the cookie is tried, so junk cookies sent first can't push the real one out; 30 days max
  (7 idle) when kept, else 12 h (1 h idle). This is longer than NIST's AAL2 (24 h, 1 h idle) on purpose: wall screens
  stay signed in; untick "keep me signed in" for the AAL2 limits on a browser. An open dashboard polls, so it counts as
  activity. At most 50 sessions; a new one pushes out the one seen longest ago. A session counts only on the kind of
  connection it was made on: one made over plain HTTP (exposed there) works only over plain HTTP, from the very address
  it was made from (IPv6 too: a /64 is a whole network, the host included), never from another address or over HTTPS;
  half-finished sign-ins are bound the same way. Trusted-device tokens are bound to the kind of connection only (one
  bound to an address made a laptop that changed networks "new", so someone guessing could keep its owner out); a
  copied one gets only that device's own 10 tries, and its wrong tries from another address are never forgiven as the
  owner's typos. When every visitor arrives from the container's gateway or its own address (Docker's userland
  proxy, rootless Docker/Podman) nothing can be bound to a browser, so plain-HTTP sign-in is refused. (Browsers send cookies to every
  port of a host, so over plain HTTP every other web app on the dashboard's address receives them: a hostile one on your
  own network must not be able to replay them. Over HTTPS `__Host-` cookies go to every HTTPS port of the same host
  name too: the dashboard needs a host name of its own.) Sign-out ends the session whatever address it comes from. Sign-out ends it on the
  server and sends `Clear-Site-Data: "cache"` (not `"cookies"`, which would also wipe your SSO's cookies on the same
  site); pages behind the sign-in are `no-store`, and a page brought back by Back reloads, so the back/forward cache
  never shows them (or recovery codes) after signing out. Cookies are `Priority=High`, but that doesn't protect them: a
  sibling subdomain can flood the cookie jar with its own high-priority cookies (Chrome and Firefox), pushing out the
  session and the trusted-browser cookie. Then that browser is a new one, and if someone's guessing has tripped the
  account lock it gets the lock until `auth.py unlock` (see "Still open").
- **Revocation**: a password change, or turning two-step on or off, saves the change together with a new random
  account *epoch* (62 bits, new at every change and for every new account, so nothing made for an account that was
  reset can match the next one). Sessions, half-finished sign-ins and trusted devices carry the epoch they were made
  in, so everything from before is worthless at once, including a sign-in that was checked a moment before the change
  and finishes after it. Each change is saved only if the account is still at the epoch it was approved under and the
  browser asking still has a live session (checked inside the same locked update that saves the change, so a sign-out
  that has answered can't be overtaken), so of two changes approved at the same moment only one lands.
  "Sign out every other browser" ends sessions only: forgetting trusted browsers needs the password (a password change).
- **Two-step**: TOTP (RFC 6238, 160-bit secret, ±1 step, each step accepted once), a 5-minute pre-sign-in cookie
  (SameSite=Strict) with 5 tries; 10 recovery codes (120 bits each, stored as SHA-256, single use). The QR code is
  drawn on the server. Every change in Settings → Account (password, two-step, recovery codes, signing browsers out,
  "That was me") needs the current password, and a code when two-step is on: a stolen session plus the password
  isn't enough. These tries count per session (10, then that browser is signed out), not toward the account lock,
  so a locked account never keeps a signed-in owner out of Settings.
- **Files**: `auth.json` and `sessions.json` are 0600, written atomically (flushed, folder too), read without
  following links, checked field by field (a wrong type or an empty authenticator secret means "damaged", never
  "anyone"); a damaged `auth.json` blocks every sign-in (fail closed) until reset on the host. The server and the
  `auth.py` command line take the same lock (`data/.auth.lock`), so a reset can't be undone by a sign-in in flight.
- **Plain HTTP**: a password is only accepted on positive evidence: the trusted proxy says the browser used HTTPS
  (`X-Forwarded-Proto: https`), or the browser's address is known (through the proxy: its X-Real-IP, IPv4-mapped
  addresses mapped, scoped ones refused) and private, loopback or link-local. `ORIGIN` alone doesn't count. A proxy
  nobody named in `TRUSTED_PROXY` that sends forwarding headers makes the address unknown (the sign-in page says to set
  `TRUSTED_PROXY`). The proxy must set both headers itself (stock Nginx Proxy Manager believes a visitor's own X-Real-IP from private
  addresses and passes on their X-Forwarded-Proto: docs/SETTINGS.md has the tested line for each proxy, and the Setup
  checklist warns when the dashboard's NPM host lacks it). The Setup checklist says when the proxy leaves out either header, or when every request comes from
  the container's gateway or its own address (Docker's userland proxy, rootless Docker/Podman: visitors can't be told apart). A repeated X-Real-IP or
  X-Forwarded-Proto counts as missing. "Your own network" is spelled out: 10/8, 172.16/12, 192.168/16, 127/8,
  169.254/16, fc00::/7, fe80::/10, ::1. With `ORIGIN` unset, changes over plain
  HTTP are only accepted for a Host no website can own (an IP address, a name without dots, `.lan`, `.local`,
  `.home.arpa`, `.internal`, `.localhost`): this stops DNS rebinding.
- **Headers**: `Cache-Control: no-store` on the sign-in API and on pages; CSP on every answer
  (`frame-ancestors 'none'`, `object-src 'none'`, `base-uri 'none'`), `X-Frame-Options: DENY`, `Cross-Origin-Opener-Policy: same-origin` on pages; API reads the browser marks
  same-site or cross-site are refused (a sibling subdomain's page can't make the owner's browser load or time them); every POST (sign-in
  included) must carry the dashboard's Origin, be JSON, and not be marked cross-site by the browser.
- **Connections**: at most 80 at once for the whole process (every `LISTEN` address), 32 per address (IPv6 per /64;
  through the proxy, per browser address when the proxy sends X-Real-IP); when all 80 are taken, or an address's 32 are, the
  oldest one still sending its request (that address's own, in the second case) is cut to make room. The request (line, headers, body) must arrive within 20 s, every connection
  is cut after 120 s, request headers are at most 64 KB, and cookie parsing is linear and tries at most 300 values.
  Proxies that pass request bodies on as they arrive (Caddy, Traefik, HAProxy by default) should buffer them (see
  docs/SETTINGS.md). Someone who can reach port 8000 directly from many addresses can still keep it busy (keep it
  behind the proxy: `TRUSTED_PROXY` and `LISTEN`).
- **Deliberate deviations** (ASVS 5 / NIST 800-63B-4): CSRF is stopped by Origin + JSON-only bodies + Sec-Fetch-Site +
  SameSite=Strict instead of a token; two-step is optional (strongly suggested by the Setup checklist); the TOTP window
  is ±1 step (about 90 s) for clock drift; the setup code has 125 bits and recovery codes 120 (NIST asks 64; both are
  single-use, throttled and stored hashed); no replacement recovery code is issued automatically (the notice says how
  many are left, new ones any time in Account); HSTS is your proxy's job; settings changes need a session, not the
  password again; session lifetimes as above; the owner is told in the app only (the notice after signing in, the
  Account section, the Setup checklist), not over a second channel such as email; wrong tries in Settings are shown at
  the next sign-in but don't raise the lasting "someone may know your password" warning (a signed-in browser making
  them is far more likely the owner mistyping).

## Controls that hold today

- Secrets never reach the browser; errors are scrubbed; changing an integration URL requires re-entering its key.
- Every POST needs `Origin` = the dashboard (CSRF). Every GET is read-only.
- `TRUSTED_PROXY`: direct connections from other containers get 403 (every address of the proxy counts). A name that
  answers with two addresses of one kind is shared by another container (a compose service called like the proxy
  gets its name as an alias), so it's trusted for neither; a name that stops existing stops counting (Docker gives its
  old address to the next container). On more than one network, without `LISTEN`, names aren't trusted at all: Docker
  answers a name from one network only, so a container on another network with the proxy's name would be the only
  "proxy". (On one network, a container with the proxy's name is still trusted while the real proxy is stopped.)
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

- A hostile page on a sibling subdomain the owner opens can push the dashboard's cookies out of the browser (a cookie
  flood): signed out, and no longer a trusted browser. Combined with the account lock (a day of someone's guessing),
  that browser needs `auth.py unlock`. Give the dashboard a host name whose siblings you trust.
- On plain HTTP by a LAN name or IP, browsers ignore `Cross-Origin-Opener-Policy` and `Clear-Site-Data` (they're for
  secure origins): use HTTPS for both.

- Over plain HTTP on a LAN name or IP, browsers send no `Sec-Fetch-Site`, so the rule that refuses API reads from
  other sites can't tell them apart there; `Cross-Origin-Resource-Policy: same-origin` still keeps other pages from
  reading the answers.

- Plain-HTTP sign-in is refused when a visitor arrives straight from the container's IPv4 gateway (every visitor
  looks alike there). An IPv6 gateway, or a gateway address the proxy passes on as X-Real-IP (Docker's userland
  proxy in front of the proxy), isn't recognised: behind such a setup, use HTTPS.

- Over plain HTTP, another web app on the dashboard's address receives its cookies (browsers send them to every
  port). Sessions and half-finished sign-ins only work from the address they were made on, but a trusted-device cookie
  isn't bound to an address (so a laptop that changes networks stays trusted): a hostile app next to the dashboard can
  use up that browser's own 10 tries, after which someone guessing can keep it waiting like a new one. Use HTTPS, or
  only run apps you trust on the same address.

- A signed-in user (with `AUTH=proxy`, anyone past the SSO) can add services, so the dashboard GETs any URL it can
  reach and shows the status code and timing: a stolen session is also a way to probe your LAN and Docker networks
  (but not to read what comes back: icons come only from a service's own origin, raster images only).

- A person can approve a bad path or pattern with `--apply` without reading the diff: approval is only as good as the
  reading. (The dash alone can't apply them.)

- Without `LISTEN`, a container on the proxy's network that can forge the proxy's address (ARP spoofing needs only the
  default `NET_RAW` capability) can still reach the dash as the proxy, and read what the proxy sends it.
- The page loads service icons from a public CDN (`cdn.jsdelivr.net`) and posters from TMDB when those are configured.
- `style-src` still allows inline styles (the page sets widths and colours inline); styles can't run script.
- A malicious `journalctl` (that is, root already compromised) whose child calls `setsid()` can keep the detail
  thread's journal read open; the main loop isn't affected (it shows the last good SSH numbers, marked as old).
