# homelab-dash 🫧

A glanceable dashboard for a self-hosted server: service status, Plex streams, media requests,
torrents + VPN-leak check, a live **"what's eating my CPU" breakdown**, temps/GPU, drive health, and a security & backups panel.
One small stdlib-Python app (no pip, no Node, no database), ~25 MB RAM.

**▶ Live demo: [demo.vicy.us](https://demo.vicy.us)** (fake data; try **⚡ breakdown**, the ⚙ settings page and the wall-mode button)

![wall mode, dark](docs/screenshot-wall-dark.png)

| Light | Full page |
|---|---|
| ![](docs/screenshot-wall-light.png) | ![](docs/screenshot-full.png) |

*(Screenshots are demo mode: fake data, Blender open-movie titles.)*

## ⚡ Spotlight: what's eating my CPU?

![CPU breakdown](docs/screenshot-breakdown.png)

Ever wondered why your fans spun up? Click **⚡ breakdown** (or the CPU tile) and get a **live, animated treemap of
everything using your machine**, grouped the way you think about it:

- **One tile per container** (Plex, qBittorrent, Immich…) and per program on the host (dockerd, your desktop, that
  rogue script). Bigger tile = more CPU. Tiles glide and resize as the load moves, every few seconds.
- **Flip to Memory** to see who's hoarding RAM instead.
- **Click a tile** for its CPU in cores, memory, process count, a live sparkline, and the **busiest processes inside it**:
  watch "Plex Transcoder" light up the moment someone starts a transcode.
- Counts *every* process on the box, not just containers, so nothing hides. Costs about 15 ms every 5 seconds.
- Deep-link it: `https://your-dash/?breakdown`. [Try it on the demo](https://demo.vicy.us/?breakdown).

Needs the (optional) root collector; see Install.

## Features

- **Services, automatically.** Reads Nginx Proxy Manager's proxy host files, so every host you add in NPM
  shows up on its own, health-checked every 30 s, with icons from [dashboard-icons](https://github.com/homarr-labs/dashboard-icons).
  Hosts behind Authelia/Authentik get a 🔒. Not using NPM? List services in `config.toml`.
- **Plex Now Playing** (via Tautulli): who's watching what, direct play vs transcode, HW vs CPU, home vs remote.
  Poster art is proxied, so your Plex token never reaches the browser. More viewers than fit? **"+N more"** gives
  Now Playing the whole row (Requests steps aside until you click "show requests"), and on the wall the list scrolls.
- **Requests** (Seerr / Overseerr / Jellyseerr): approve or decline right from the dashboard.
- **qBittorrent**: downloads, seeding, speeds, and a big red banner if it ever shows your real home IP (VPN leak).
- **System**: CPU per thread, RAM, network, temperatures (AMD, Intel, DDR5, NVMe), NVIDIA GPU incl. NVENC/NVDEC.
- **⚡ CPU / memory breakdown**: a live treemap of every container and host program, with per-process detail (see above).
- **Drives**: usage + SMART health/temps for every disk, without waking sleeping drives.
- **Security & backups**: fail2ban, SSH logins from unknown IPs, Authelia failures, crypto-miner watch,
  crontab change detection with a one-click "Looks good" approval, `chattr +i` lock checks, backup freshness.
- **Wall mode**: `https://your-dash/?wall` fits a 1920×1080 screen with no scrolling and hides the cursor.
  Click the Plex tile there to expand or collapse Now Playing; the stream list scrolls inside its own box.
- Light/dark automatic, floating bubbles, optional mascot. Every panel is optional and hides itself if not configured.

## How it fits together

```
browser ──► reverse proxy + SSO ──► dash container (uid 1000, read-only, no capabilities)
                                       │  reads (read-only):  NPM proxy_host/*.conf, /proc, hwmon, hw.json
                                       │  calls with API keys: qBittorrent, Seerr, Tautulli
                                       ▼
                     /var/lib/homelab-dash/hw.json  ◄── homelab-dash-collector (root systemd service, no network)
                                                        smartctl · nvidia-smi · fail2ban · journal · cron · lsattr
```

The part exposed to the network has no power; the part with root never talks to the network.
API keys live only in `.env` on the server; the browser only ever talks to the dashboard's own backend.
The one write path from the dash to root is the cron "Looks good" button: it drops a hash into a folder, and the
collector approves only if that hash matches the crontabs as they are right now.

## Try it (demo mode, fake data)

```sh
git clone https://github.com/CoreXI7/homelab-dash && cd homelab-dash
DEMO=1 python3 app/app.py          # Python 3.11+, then open http://localhost:8000
```

## Install

1. **Dashboard**
   ```sh
   mkdir data && sudo chown 1000:1000 data     # settings, saved keys and uploads live here
   cp .env.example .env && chmod 600 .env      # optional: keys can be typed on the settings page instead
   # edit docker-compose.yml: ORIGIN, TRUSTED_PROXY, your NPM folder and your proxy's Docker network
   docker compose up -d
   ```
   Add a proxy host for it in NPM (forward to `dash:8000`) and put it behind your SSO.
   Then open the dashboard, click ⚙, and set everything else there.

2. **Collector** (optional, for drives, GPU, disk temps, security and backups)
   ```sh
   sudo bash collector/install.sh
   sudo nano /etc/homelab-dash/collector.toml   # mounts, fail2ban jail, backup log, ...
   sudo systemctl restart homelab-dash-collector
   ```
   Needs Python 3.11+ on the host. Uses `smartctl` (smartmontools), and `nvidia-smi`, `fail2ban-client`,
   `docker` if present. Costs about 0.02 % CPU on a modern box. Uninstall: `sudo bash collector/install.sh --uninstall`.
   It's configured by file on purpose: it runs as root, so a web page shouldn't be able to change what it does.

## Settings page

![settings](docs/screenshot-settings.png)

Click ⚙ on the dashboard (or open `/settings`):

- **Look & feel**: title, taglines, click emoji, mascot (upload an image), what the mascot says.
- **Services**: everything found in NPM, with groups you can create, rename and reorder, labels, icons
  (any [dashboard-icons](https://dashboard-icons.homarr.dev) name), and hiding. New NPM hosts appear on their own.
- **Extra services** that aren't in NPM (NAS, router, ...), health-checked too.
- **Integrations**: qBittorrent, Seerr/Overseerr/Jellyseerr, Tautulli, with a Test button.
- **Trusted IPs** for the SSH/SSO login check.

Changes apply immediately, no restart. They're saved to `data/settings.json`; keys to `data/secrets.json` (both `chmod 600`).
Prefer files? Put a `config.toml` (see [`config.example.toml`](config.example.toml)) in `data/`; it's used until the first save.

## Host a public demo

[`docker-compose.demo.yml`](docker-compose.demo.yml) runs a demo with fake data that's safe to leave open to the internet:
no secrets, no host access, no outbound connections, a settings page that never saves, uploads off, and tight
CPU/RAM/process/log limits. It sits on an internal network that only your reverse proxy joins:

```sh
docker compose -f docker-compose.demo.yml up -d
docker network connect homelab_demo <your-proxy-container>   # e.g. nginx for Nginx Proxy Manager
```

Then add a proxy host for it (forward to `homelab-demo:8000`), with no SSO.

## Security notes

**Your API keys never leave the server.**
- Keys live only on the server: in `data/secrets.json` (`chmod 600`) or `.env`. The settings page is write-only for keys:
  you can type a new one, but it only ever shows "saved", never the value.
- Changing an integration's address requires typing its key again in the same save, so someone who got into the
  settings page can't redirect your stored key to their own server. Keys set in `.env` lock their address too.
- **Test** only uses the saved address, never one typed into the form. The browser only ever talks to the dashboard's own
  backend, which calls qBittorrent, Seerr and Tautulli for it. No endpoint returns a key, a password, or an upstream URL.
- Every error message is scrubbed before it reaches the browser or the container log (Python errors can echo
  request URLs, and Tautulli's key travels in its URL). Unexpected errors return a generic message.
- Plex posters are proxied through Tautulli, so your Plex token isn't exposed either.
- Your `known_ips` list stays on the server; the page only gets a "known / unknown" flag per login.
- The collector redacts passwords, tokens, webhook URLs and `user:pass@` from cron and log lines before writing
  `hw.json`, and that file is readable only by root and the dash container's group.

**Who can use it.**
- Run it **behind SSO** (Authelia, Authentik, ...). The app has no login of its own, so anyone who gets past your SSO
  can change settings. Set `SETTINGS_UI=0` to make the settings page read-only.
- What protects the dashboard itself (allowed origin, trusted proxy, NPM folder) is set in `docker-compose.yml`,
  never on the settings page. So is everything about the root collector.
- Everything typed into the settings page is validated on the server (lengths, URL schemes, icon names, IPs).
  Uploads must really be PNG/JPEG/GIF/WebP (checked by content, not name); SVG is refused because it can carry scripts.
- `trusted_proxy` (default `nginx`) makes the app refuse any connection that isn't from your reverse proxy,
  so another container on the same Docker network can't skip the SSO.
- Every POST (saving settings, uploads, approve/decline, cron approval) requires the browser's `Origin` to be the
  dashboard itself (`ORIGIN`, or its own Host if unset). That blocks other websites from acting for you (CSRF).
- Everything from outside (torrent names, Plex titles, Seerr usernames, hostnames) is HTML-escaped before it's shown,
  under a strict Content-Security-Policy.
- Icons are fetched only from hosts the dashboard already knows (no open proxy), and static files can't escape `app/static/`.
- The container runs as uid 1000 with a read-only filesystem, all capabilities dropped, and 256 MB / 128 process caps.
- The root collector never listens on the network. The only thing the dash can ask of it is "approve the cron jobs
  as they are right now", checked by hash.

What it can't protect against: anyone with Docker or root access on the host can read the container's environment
(`docker inspect`), like with any self-hosted app. Keep `.env` at `chmod 600` and don't commit it (`.gitignore` covers it).

## License

MIT
