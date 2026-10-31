# Fishbowl

A self-hosted status page for a home server. One page shows whether your services are up, who is watching Plex,
pending Seerr requests, qBittorrent downloads (with a VPN-leak check), temperatures, drives, security events and
backups.

- Python standard library only: no pip packages, no Node, no database. Uses about 25–40 MB of RAM.
- Runs in Docker behind your reverse proxy. Has its own sign-in, or can use your existing SSO.
- Optional host helper (the "collector") for drives, GPU, power and security checks.

Live demo with fake data: https://demo.vicy.us

![Fishbowl in wall mode](docs/screenshot-wall-dark.png)

## Contents

1. [Features](#features)
2. [Requirements](#requirements)
3. [Install](#install)
4. [Sign-in](#sign-in)
5. [Configuration](#configuration)
6. [Updating](#updating)
7. [Keys and links](#keys-and-links)
8. [Screenshots](#screenshots)
9. [Security](#security)
10. [Demo, tests and other docs](#demo-tests-and-other-docs)

## Features

| Panel | Shows | Needs |
|---|---|---|
| Services | Each Nginx Proxy Manager host (or ones you add), checked every 30 s | NPM's config folder, or a list on the settings page |
| Plex | Current streams: who, what, direct play or transcode | Tautulli |
| Requests | Pending requests, with Approve / Decline | Seerr, Overseerr or Jellyseerr |
| Torrents | Downloads, speeds, and an alert if your real IP shows | qBittorrent |
| System | CPU per thread, RAM, network, temperatures, NVIDIA GPU | nothing (GPU: the collector) |
| Drives | Space, SMART health and temperature | the collector |
| Security & backups | fail2ban, SSH logins from unknown places, Authelia failures, miner watch, cron changes, locked files, last backup | the collector |

Also: a wall mode for an always-on screen, a live view of CPU/temps/power/disk I/O, a breakdown of what uses CPU and
memory, three themes with light and dark, and a settings page with a Setup checklist that finds problems in your
install. Panels you don't configure stay hidden.

## Requirements

- Docker with Compose
- A reverse proxy in front of it with HTTPS (Nginx Proxy Manager, Caddy, Traefik, HAProxy, nginx)
- For the optional collector: Linux with systemd, Python 3.11+, `smartctl`

## Install

**1. Get the code and create the data folder**

```sh
git clone https://github.com/CoreXI7/fishbowl && cd fishbowl
mkdir data && sudo chown 1000:1000 data
cp .env.example .env && chmod 600 .env    # can stay empty
```

**2. Edit `docker-compose.yml`**

| Setting | Set it to |
|---|---|
| `ORIGIN` | The URL you'll open it at, e.g. `https://dash.example.com` |
| `TRUSTED_PROXY` | Your reverse proxy's container name, e.g. `npm` |
| NPM folder volume | Path to NPM's `data/nginx/proxy_host` (or remove the line if you don't use NPM) |
| `networks` | Your reverse proxy's Docker network |

Not sure of the network name? `docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' <proxy>`

**3. Start it**

```sh
docker compose up -d
```

**4. Add it to your reverse proxy**

Forward your host name to `dash:8000` over HTTPS. Your proxy must set `X-Real-IP` and `X-Forwarded-Proto` itself:

- Nginx Proxy Manager: turn on **Force SSL**, and under **Advanced** add `set_real_ip_from 127.0.0.2/32;`
- Traefik: works as is.
- Caddy, HAProxy, nginx: see [docs/SETTINGS.md](docs/SETTINGS.md#signing-in-auth-login-the-default).

**5. Create your account**

Open the dashboard. It asks for a setup code, which is in the container's log:

```sh
docker logs dash 2>&1 | grep "^SETUP CODE" | tail -n 1
```

Already use SSO (Authelia, Authentik)? Skip this: add `AUTH: proxy` under `environment:` in docker-compose.yml and run
`docker compose up -d` again.

**6. Finish in the browser**

Click **⚙** → **Setup**. It lists anything still missing (proxy headers, integrations, the collector) with the exact fix.
Add qBittorrent, Seerr and Tautulli under **Integrations**.

**7. Optional: install the collector**

```sh
sudo bash collector/install.sh
sudo nano /etc/homelab-dash/collector.toml    # drives, fail2ban jail, backup log, ...
sudo systemctl restart homelab-dash-collector
```

It runs as root in a systemd sandbox, never uses the network, and writes a file the dashboard reads. Remove it with
`sudo bash collector/install.sh --uninstall`. Settings that make it read a file or run a pattern only apply after you
approve them on the host with `sudo homelab-dash-collector --apply`.

**Troubleshooting:** an empty page or 502 usually means `TRUSTED_PROXY` doesn't match your proxy. `docker logs dash`
says why.

## Sign-in

- One account. Password of at least 15 characters (up to 1,024), checked against common passwords. Four random words
  work well. *Why 15?* Length is what stops someone with a copy of the password's hash from guessing it offline; it's
  what NIST SP 800-63B-4 asks for when a password is the only factor.
- Two-step sign-in (optional, recommended): **⚙ → Account** → authenticator app. Save the 10 recovery codes.
- "Keep me signed in": 30 days. Otherwise 12 hours.
- Wrong guesses: waits grow after 5 tries; after 100 in a row, new browsers are locked out until you run `unlock`.
  If someone enters your right password without the right code, you are warned at your next sign-in.
- Plain HTTP (no HTTPS): sign-in only from your local network. Use HTTPS if you can.

Recovery commands, run on the host:

| `docker exec -it dash python /app/auth.py ...` | Does |
|---|---|
| `status` | Show the account, lock, two-step, signed-in browsers |
| `unlock` | Clear the lock after 100 wrong tries |
| `signout` | Sign out every browser (lost device) |
| `reset` | Delete the account (settings stay) and print a new setup code |

With `AUTH: proxy`, Fishbowl's sign-in is off and your proxy's sign-in protects it. Then `TRUSTED_PROXY` is required,
and your SSO must not have bypass rules covering the dashboard's paths.

## Configuration

Most settings are on the settings page (**⚙** or `/settings`) and apply without a restart:

| Section | Contains |
|---|---|
| Setup | Checks your install and shows fixes |
| Look & feel, Themes | Title, visible panels, theme, mascot, taglines |
| Services | Groups, names, icons, hidden hosts, services not in NPM |
| Integrations | qBittorrent, Seerr, Tautulli, each with a Test button |
| Drives & hardware, Security, Backups | What the collector shows and checks |
| Account | Password, two-step, recovery codes, signed-in browsers |

They are saved in `data/settings.json`; keys in `data/secrets.json` (both mode 600). A `data/config.toml` can be used
instead ([example](config.example.toml)).

Security settings live only in `docker-compose.yml`: `ORIGIN`, `TRUSTED_PROXY`, `LISTEN`, `AUTH`, `SETTINGS_UI`.
Recommended: set `LISTEN` so only your proxy can reach the dashboard. Every setting is described in
[docs/SETTINGS.md](docs/SETTINGS.md).

## Updating

```sh
git pull
docker compose up -d --force-recreate
sudo bash collector/install.sh        # only if you use the collector
```

If `git pull` refuses because you edited `docker-compose.yml`: run `git stash`, `git pull`, `git stash pop`. On a
conflict, merge the file by hand, then `git add docker-compose.yml`, `git reset -q`, `git stash drop`. To avoid this,
put your changes in `docker-compose.override.yml` instead.

Upgrading from homelab-dash (before 1.0): the built-in sign-in is now on by default. Either create an account (step 5)
or set `AUTH: proxy`. Also re-run the collector installer.

## Keys and links

| Key | Action |
|---|---|
| `W` | Wall mode (fits 1920×1080, hides the cursor) |
| `T` | Change theme |
| `M` | Background animation on/off |
| `Esc` | Close a window |

Direct links: `/?wall`, `/?live`, `/?breakdown`, `/?security=ssh` (also `fw`, `auth`, `procs`, `cron`, `locks`,
`backups`), `/settings`. Click a box title on the Security & backups card to see its details.

## Screenshots

All screenshots use demo mode (fake data).

| | |
|---|---|
| ![Wall mode, light](docs/screenshot-wall-light.png) Wall mode, light | ![Full page](docs/screenshot-full.png) Full page |
| ![Setup checklist](docs/screenshot-setup.png) Setup checklist | ![Settings](docs/screenshot-settings.png) Settings page |
| ![Sign-in](docs/screenshot-signin.png) Sign-in | ![Account](docs/screenshot-account.png) Account, with a password warning |
| ![SSH details](docs/screenshot-security.png) Security details: SSH | ![Backups](docs/screenshot-backups.png) Security details: backups |
| ![Live window](docs/screenshot-live.png) Live window | ![Breakdown](docs/screenshot-breakdown.png) CPU / memory breakdown |
| ![Gura theme](docs/screenshot-gura.png) Gura theme | ![Miku theme](docs/screenshot-miku.png) Miku theme |

## Security

How it is built:

- The web part runs as a normal user in a read-only container with no capabilities, capped at 256 MB.
- The collector, which runs as root, never uses the network. The dashboard cannot make it read files or run patterns
  without your approval on the host.
- API keys never reach the browser. Changing an integration's address requires entering its key again.
- Only your reverse proxy may connect (`TRUSTED_PROXY`, `LISTEN`), and changes must come from the dashboard's own page.
- Data from qBittorrent, Seerr and Tautulli is treated as untrusted: escaped, size-limited and time-limited.

Anyone with root or Docker access on the host can read the container's environment, as with any self-hosted app.

### The 1.0 security audit

Before 1.0, Fishbowl went through a four-round security audit (40 reviews by AI agents across ten areas):

| Item | Result |
|---|---|
| Areas | Sign-in and sessions, HTTP handling, browser attacks, outbound requests, the root collector, cryptography, black-box testing, fuzzing, research, release hygiene |
| Method | Throwaway copies only; the real Docker image; real proxies (NPM, nginx, Caddy, HAProxy, Traefik) and browsers (Chrome, Firefox); every round re-ran earlier rounds' attacks |
| Fuzzing | Over 15 million executions |
| Findings | 2 high and about 50 medium, all fixed, each with a test |
| Last three rounds | No way in found |
| Tests | 357 automated tests |

Limits: this was not a paid third-party audit, and Safari, Podman, Cloudflare Tunnel and Tailscale were not tested.

- Results and known limits: [TESTING.md](TESTING.md), [SECURITY-TESTING.md](SECURITY-TESTING.md)
- Design and remaining risks: [THREAT-MODEL.md](THREAT-MODEL.md)
- Report a problem privately: [SECURITY.md](SECURITY.md)

## Demo, tests and other docs

Try it without setup: `DEMO=1 python3 app/app.py`, then open http://localhost:8000.

Host a public demo (fake data, nothing saved):

```sh
docker compose -p fishbowl-demo -f docker-compose.demo.yml up -d
docker network connect homelab_demo <your-proxy-container>
```

Then point a proxy host at `homelab-demo:8000`, without SSO.

Run the tests: `python3 -m unittest discover -s tests` (standard library only; page tests use Chrome if installed).

Other docs: [docs/SETTINGS.md](docs/SETTINGS.md) (all settings), [ARCHITECTURE.md](ARCHITECTURE.md) (code layout),
[PERF-BASELINE.md](PERF-BASELINE.md) (measured resource use).

## License

MIT
