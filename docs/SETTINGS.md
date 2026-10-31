# Settings reference

Three places, each for a different kind of setting:

| Where | What goes there | Who can change it |
|---|---|---|
| The **/settings page** (saved to `data/settings.json`, keys to `data/secrets.json`) | everything about the look, the services, the integrations. Its **Setup** section checks the other two places and gives the exact fix for anything missing | anyone who can open the dashboard (unless `SETTINGS_UI=0`) |
| **Environment** (`docker-compose.yml`, `.env`) | anything security-critical: who may talk to the dash, where it listens, the CSRF origin; optionally the integration keys (then the page can't change them) | whoever runs the container |
| **`/etc/homelab-dash/collector.toml`** | what the root collector checks | root (see `collector/collector.example.toml`) |

`config.toml` in the data folder (see `config.example.toml`) is an optional starting point: it's read until the first
save from the settings page. Every field is validated on the server; a bad value is refused with a message saying which.

## Dashboard settings (`settings.json` / the settings page)

### `site`: look and feel

| Field | Type, limit | What it does |
|---|---|---|
| `title` | text, 60 | Header and browser tab title |
| `tagline`, `tagline_down` | text, 120 | The line next to the title when all is well / when something is down |
| `click_emoji` | text, 16 | What rains down when you click the title or mascot (several emoji: a random one each) |
| `greeting`, `quotes` | text 200 / up to 50 lines | What the mascot says first, then at random |
| `mascot`, `mascot_credit` | `/static/…` or `/uploads/…` path / text 80 | The picture in the left column and its credit line |
| `favicon` | `/static/…` or `/uploads/…` path | Browser tab icon |
| `theme` | `bubbles`, `miku` or `gura` | The default theme (each browser can switch with 🎨 or T and remembers its pick) |
| `themes.<id>` | the personality fields above | Per-theme mascot, quotes, taglines, click emoji, tab icon (Bubbles uses the top-level fields) |
| `motion` | `auto`, `on` or `off` | Background motion (bubbles and drifting effects) for browsers that haven't chosen with 🫧 / M. `auto` (default) follows the device's reduce-motion setting |
| `hidden_panels` | list of `services`, `system`, `temps`, `gpu`, `mascot`, `plex`, `requests`, `torrents`, `security`, `drives` | Panels to leave off the page. Their summary tile at the top stays (a VPN leak still shows with Torrents hidden). Unknown names are ignored. |

### Services

| Field | What it does |
|---|---|
| `groups` | `[{name, hosts: [...]}]`, up to 30 groups: how tiles are grouped, in this order. Hosts in no group land in "New / other", so a new proxy host is never hidden. |
| `apps.<host>` | `{label, icon, url}`: a tile's name, its icon (a [dashboard-icons](https://github.com/homarr-labs/dashboard-icons) name, an uploaded file or a URL) and where it links (default `https://<host>`) |
| `services` | `[{name, url, auth}]`: services that aren't proxy hosts; `url` is what gets health-checked |
| `skip_hosts` | proxy hosts to leave off the dashboard |
| `via_proxy` | up to 100 `host` or `host/path` entries (Services → Advanced): health-checked through the reverse proxy instead of directly, for services on a network the dashboard isn't on. A path is checked instead of `/` (use one that skips the sign-in). `CHECK_VIA_PROXY` in the environment wins and locks the field. In `config.toml`: `[proxy] check_via_proxy` |

A service counts as **up** when it answers below HTTP 500 (a login page or a redirect counts: redirects aren't
followed). Health checks run at most every 25 s whatever the number of open browsers.

### Integrations

| Field | Keys (settings page or env) | What it adds |
|---|---|---|
| `qbittorrent.url`, `qbittorrent.home_ip` | `QBIT_USER`, `QBIT_PASS` | Torrents panel, speeds, VPN check: qBittorrent reporting `home_ip` as its external IP means the VPN leaked (red banner) |
| `seerr.url` | `SEERR_KEY` | Pending requests with Approve / Decline (Overseerr, Jellyseerr, Seerr) |
| `tautulli.url` | `TAUTULLI_KEY` | Plex now playing |

An integration turns on when its URL and keys are set. Changing an integration's URL on the page requires typing its
keys again, so a changed address can't receive the saved ones. Keys never go back to the browser (the page only sees
"set" / "not set"). The integrations never follow redirects; the settings page's **Test** says so if one redirects.

### `known_ips` ("Known IPs" on the page)

Up to 200 entries: full addresses (`198.51.100.7`, `2001:db8::1`) or prefixes ending in `.` or `:` (`203.0.113.`,
`2001:db8:`). SSH and SSO logins from anywhere else are flagged. The SSH detail view can add an IP that really logged in.

## Environment (`docker-compose.yml`, `.env`)

| Variable | Default | What it does |
|---|---|---|
| `AUTH` | `login` | `login`: Fishbowl's own sign-in (one account, optional authenticator app). `proxy`: off, because your reverse proxy's sign-in (Authelia, Authentik, an access list) protects the dashboard. Anything else counts as `login`. The demo has no sign-in |
| `TRUSTED_PROXY` | (empty = anyone) | Your reverse proxy's container name(s) or IP(s), space-separated: the only clients allowed. Others are dropped before a thread is started for them. A name that more than one container answers to (another compose service called like your proxy gets it as an alias) is trusted for none of them, and the Setup checklist says so: use a name nothing else has, or the IP. When the dashboard is on more than one Docker network and `LISTEN` isn't set, names aren't trusted at all (Docker answers a name from one network only, so a container on another network with the proxy's name would win): give the proxy's IP, pinned with `ipv4_address` in the proxy's compose file (else Docker may give it to another container after a restart) |
| `LISTEN` | `0.0.0.0` | Addresses to answer on, space-separated (IPv4 or IPv6). With `"127.0.0.1 <address on a network only the proxy shares>"` other containers can't reach the dash at all. Don't mix a wildcard (`0.0.0.0`, `::`) with specific addresses |
| `ORIGIN` | (the address it's opened on) | The dashboard's public URL(s), space-separated: every POST must come from one (CSRF guard) |
| `SETTINGS_UI` | `1` | `0` makes the settings page read-only |
| `USER_HEADER` | (off) | A header naming the SSO user (e.g. `Remote-User`), logged with each change. Only if your proxy sets it itself, and only trusted on requests from `TRUSTED_PROXY` |
| `NPM_DIR` | `/npm` | Nginx Proxy Manager's `proxy_host` folder, mounted read-only: the service list |
| `CHECK_VIA_PROXY` | (none) | `host[/path] …`: the same as `via_proxy` on the settings page. When set, it wins and the field is locked |
| `QBIT_URL`, `SEERR_URL`, `TAUTULLI_URL` and the keys above | (none) | Integration settings from the environment; the settings page then shows them locked |
| `HOME_IP` | (none) | Same as `qbittorrent.home_ip` |
| `DATA_DIR` | `/data` | Settings, keys, acknowledgements, uploads (writable by uid 1000) |
| `HW_FILE`, `SEC_DETAIL_FILE`, `REQ_DIR` | `/hw/hw.json`, next to it, `/req` | Where the root collector's files are mounted |
| `PORT` | `8000` | |
| `DEMO` | (off) | `1` = fake data, nothing saved, no uploads (the public demo) |
| `REPO_URL` | (none) | Link for the demo's "demo · fake data" pill (`https://…` only) |

Limits that aren't settings: uploads are raster images (PNG, JPEG, GIF, WebP) of at most 2 MB, 200 files / 100 MB in
all; answers from qBittorrent are read up to 32 MB, from Seerr and Tautulli up to 8 MB.

## Lock the dash to the proxy (`LISTEN`)

`TRUSTED_PROXY` checks the source address of each connection. On a Docker network shared with other containers, one
that can forge the proxy's address (ARP spoofing needs only the default `NET_RAW` capability) gets past that check. To
close it, give the dash a network that only the proxy shares, and answer only there:

```sh
docker network create --internal --subnet 10.89.250.0/29 dash_front
docker network connect --ip 10.89.250.3 dash_front <proxy container>
```

```yaml
services:
  dash:
    environment:
      LISTEN: "127.0.0.1 10.89.250.2"       # 127.0.0.1 keeps the healthcheck working
      TRUSTED_PROXY: "nginx 10.89.250.3"    # Docker's DNS gives only one of the proxy's addresses: list the new one
    networks:
      reverse_proxy: {}                    # still needed to reach the services it checks
      dash_front: {ipv4_address: 10.89.250.2}
networks:
  dash_front: {external: true}
```

Then point the proxy host at `10.89.250.2:8000` (and add the network to the proxy's own compose file so it survives a
recreate).

## Root collector (`/etc/homelab-dash/collector.toml`)

Every option is in `collector/collector.example.toml` with a comment: drives and mount labels, the network interface,
the GPU, the security checks (fail2ban jail, SSH unit, Authelia container and database, process watch names, cron
watch, immutable files, firewall), the backup jobs, and how often it writes.

Files the collector reads (logs, backup files, Authelia's database, `authorized_keys`) must be regular files, and a
symlink on the way is followed only where it sits in a folder only root can write (like `/var/run`): one in a folder
a container or user can write is refused, so point those settings at the real file.

Most of them can also be changed on the settings page (Drives & hardware, Security, Backups), so you rarely need to
open that file:

- **Switches and choices** (what's shown, the interval, drives, the jail, the network interface, ...) apply within a
  minute. The collector checks each one itself, against what it found on the machine.
- **Files and patterns** (log files, the Authelia database, locked files, backup jobs, counted checks, backup file
  patterns, regexes, your watch script) make
  root read a file or run a search, so they wait until you approve them on the host:
  `sudo homelab-dash-collector --apply` shows each change, marked and whole, and asks (y applies, d drops them
  from the waiting list). Approved values are kept in
  `/etc/homelab-dash/collector.page.toml`.
- **The last change wins**: a change on the page applies only if the setting still has the value the page showed, and
  editing a setting in `collector.toml` later (then restarting the collector) wins over what the page set for it.
- `sudo homelab-dash-collector --apply --undo` drops everything the page set: back to `collector.toml` alone.
- `out_dir` and `dash_uid` can only be set in `collector.toml`.

## Signing in (`AUTH: login`, the default)

- **First run**: the container's log shows a one-time **setup code**
  (`docker logs <container> 2>&1 | grep "^SETUP CODE" | tail -n 1`). Opening the dashboard asks for it, a name and a
  password. A new code appears at every start until the account exists: only the newest works.
  `python /app/auth.py setup-code` (on the host, see below) makes a new one without a restart.
- **Passwords**: 15 to 1024 characters, checked against a list of common passwords, your name and the words
  fishbowl, homelab and dashboard, and refused when they're one short piece repeated. No other rules.
  Stored with scrypt (N=2^15, r=8, p=3) in `data/auth.json` (0600).
- **Two-step sign-in**: Settings → Account → set up an authenticator app (scan the QR code, type a code), which also
  gives 10 one-time recovery codes. Changing the password, turning it off or making new recovery codes needs the
  password and a code.
- **Sessions**: kept in `data/sessions.json` (0600, only hashes of the cookies). "Keep me signed in" = 30 days at most
  (7 days without a visit); otherwise 12 hours (1 hour without a visit). Over HTTPS the cookie is `__Host-fishbowl`
  (Secure, HttpOnly, SameSite=Strict); over plain HTTP `fishbowl` (the Setup checklist warns).
- **Your reverse proxy** must send `X-Real-IP` (the visitor's address) and `X-Forwarded-Proto` (http or https), and be
  named in `TRUSTED_PROXY`. It must **set** both itself, never pass on what the visitor sent: otherwise a visitor
  picks the address the dashboard sees (and can pass for one on your network) and can claim HTTPS. Out of the box
  only Traefik gets this right; the others need these lines (each tested with that proxy):
  - **Nginx Proxy Manager**: it sends both, but its stock config believes a visitor's own `X-Real-IP` when the visitor
    comes from a private address (Docker's userland proxy makes every IPv6 visitor one) and passes on a visitor's
    `X-Forwarded-Proto`. In the dashboard's proxy host, add `set_real_ip_from 127.0.0.2/32;` under **Advanced** (it
    replaces that trust for this host), and turn on **Force SSL** under **SSL**.
  - **Caddy**: `header_up X-Real-IP {remote_host}` in the `reverse_proxy` block (required: without it a visitor's own
    X-Real-IP goes through). Caddy sets X-Forwarded-Proto itself.
  - **HAProxy**: `http-request set-header X-Real-IP %[src]`, `http-request set-header X-Forwarded-Proto https if { ssl_fc }`
    and `http-request set-header X-Forwarded-Proto http if !{ ssl_fc }` (without the last one a visitor's own value
    stays on plain HTTP).
  - **Traefik**: nothing to add (it replaces both unless you trust forwarded headers from the visitor's side).
  - **A CDN or tunnel in front of the proxy** (Cloudflare and the like): the proxy must take the visitor's address
    from that service's header (e.g. `CF-Connecting-IP`) and only for that service's own addresses.

  Without X-Real-IP every visitor shares one count of wrong tries and plain-HTTP sign-in is refused; without
  X-Forwarded-Proto sign-in from outside your network is refused (the sign-in page and the Setup checklist say which
  header is missing). If your proxy passes request bodies on as they arrive (Caddy, Traefik and HAProxy do by
  default), let it buffer them: Caddy `request_buffers 64KiB` in the reverse_proxy block, Traefik the Buffering
  middleware, HAProxy `option http-buffer-request`. Nginx Proxy Manager buffers already.
- **Changes end other access**: a new password, or turning two-step on or off, signs every other browser out and
  forgets every trusted browser. Signing one browser out (or every other one) needs the password too.
- **Plain HTTP stays at home**: over plain HTTP, passwords are only accepted from your own network (private addresses),
  and a sign-in made that way only works there, from the very same address (IPv6 too): the same cookie is refused
  from any other address and over HTTPS, and so is a half-finished sign-in (password typed, code due). Browsers send a
  site's cookies to every port of its address, so over plain HTTP every other web app on that address receives them;
  this keeps a hostile one from reusing them. A trusted browser stays trusted on a new address (so someone guessing
  can't keep a laptop that changed networks out), but its wrong tries from another address are never forgiven as your
  typos. When every visitor reaches the dashboard from Docker's gateway or its own address (Docker's userland
  proxy, rootless Docker, or rootless Podman 4's `podman run -p`), they can't be told apart, and plain-HTTP sign-in is refused: use your proxy's https address.
- **Over HTTPS, give the dashboard its own host name**: `__Host-` and `Secure` cookies still go to every other HTTPS
  port of that name, so don't serve other apps on other ports of the same name (with a shared wildcard certificate).
- **Wrong tries**: after 5 (per address, and for the account), each try waits twice as long as the last, up to 15
  minutes. 100 wrong tries in a row lock the account for browsers that never signed in here. A browser that did
  (it keeps a `fishbowl-device` cookie for a year) still signs in, and its wrong tries count on their own, so someone
  guessing can't lock it out (after 10 wrong tries of its own it's just another browser). A new browser of yours does
  wait while someone keeps guessing (one try every few minutes is enough, and about a day of it reaches the lock): use
  a browser you've signed in with before, or `auth.py unlock` on the host. That's on purpose: the account-wide waits
  are what stop guessing from many addresses at once. The next sign-in shows what
  failed since the last one (not your own typos just before it). The right password with a wrong code becomes a
  Setup problem until you change the password, or press "That was me" in Settings → Account.
- **Settings → Account** has one "Confirm it's you" box: the current password (and a code when two-step is on, a new
  one each time) before every change there, including signing browsers out. A code typed twice is refused as "just
  used", not counted as wrong.
- **`AUTH: proxy`** needs `TRUSTED_PROXY`; without it the dashboard keeps its own sign-in on (and says so). With it,
  everything your proxy lets through is the admin: don't give the dashboard's host any SSO bypass rule (an Authelia
  `bypass` for `^/api`, say, which phone apps need on other hosts), or anyone could read and change its settings.
- **On the host** (`docker exec -it <container> python /app/auth.py ...`): `status`; `unlock` (after the lock);
  `setup-code` (a new setup code, while there's no account); `signout` (signs every browser out and forgets trusted browsers, the account stays);
  `reset` (deletes the account and every session, keeps your settings and keys, prints a new setup code).
