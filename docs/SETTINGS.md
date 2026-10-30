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
| `TRUSTED_PROXY` | (empty = anyone) | Your reverse proxy's container name(s) or IP(s), space-separated: the only clients allowed. Others are dropped before a thread is started for them |
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
docker network create --internal --subnet 10.250.81.0/29 dash_front
docker network connect --ip 10.250.81.3 dash_front <proxy container>
```

```yaml
services:
  dash:
    environment:
      LISTEN: "127.0.0.1 10.250.81.2"       # 127.0.0.1 keeps the healthcheck working
      TRUSTED_PROXY: "nginx 10.250.81.3"    # Docker's DNS gives only one of the proxy's addresses: list the new one
    networks:
      reverse_proxy: {}                    # still needed to reach the services it checks
      dash_front: {ipv4_address: 10.250.81.2}
networks:
  dash_front: {external: true}
```

Then point the proxy host at `10.250.81.2:8000` (and add the network to the proxy's own compose file so it survives a
recreate).

## Root collector (`/etc/homelab-dash/collector.toml`)

Every option is in `collector/collector.example.toml` with a comment: drives and mount labels, the network interface,
the GPU, the security checks (fail2ban jail, SSH unit, Authelia container and database, process watch names, cron
watch, immutable files, firewall), the backup jobs, and how often it writes.

Most of them can also be changed on the settings page (Drives & hardware, Security, Backups), so you rarely need to
open that file:

- **Switches and choices** (what's shown, the interval, drives, the jail, the network interface, ...) apply within a
  minute. The collector checks each one itself, against what it found on the machine.
- **Files and patterns** (log files, the Authelia database, locked files, backup jobs, regexes, your watch script) make
  root read a file or run a search, so they wait until you approve them on the host:
  `sudo homelab-dash-collector --apply` shows each change, marked and whole, and asks (y applies, d drops them
  from the waiting list). Approved values are kept in
  `/etc/homelab-dash/collector.page.toml`.
- **The last change wins**: a change on the page applies only if the setting still has the value the page showed, and
  editing a setting in `collector.toml` later (then restarting the collector) wins over what the page set for it.
- `sudo homelab-dash-collector --apply --undo` drops everything the page set: back to `collector.toml` alone.
- `out_dir` and `dash_uid` can only be set in `collector.toml`.
