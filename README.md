# Fishbowl

Your whole home server in one glanceable page.

| Light | Dark |
|---|---|
| ![Fishbowl, light](docs/screenshot-wall-light.png) | ![Fishbowl, dark](docs/screenshot-wall-dark.png) |

Fishbowl shows your services, Plex streams, media requests, torrents, temperatures, drives, security events and backups
on one page. It is a single Python app with no dependencies, made to run in Docker behind a reverse proxy.

[Live demo](https://demo.vicy.us) (fake data)

## Contents

- [Features](#features)
- [Install](#install)
- [Usage](#usage)
- [Updating](#updating)
- [Screenshots](#screenshots)
- [Security](#security)
- [Contributing](#contributing)
- [License](#license)

## Features

- Service status for every Nginx Proxy Manager host, or a list you enter
- Plex now playing (Tautulli), requests with approve/decline (Seerr, Overseerr, Jellyseerr)
- qBittorrent downloads with a VPN-leak alert
- CPU, memory, network, temperatures and NVIDIA GPU, with a live per-second view
- Drive space and SMART health, fail2ban, SSH logins, Authelia failures, cron changes and backup status (with the
  optional collector)
- Wall mode for an always-on screen, three themes, light and dark
- Built-in sign-in with optional two-step, or your existing SSO
- About 25–40 MB of RAM, no database

## Install

Requirements: Docker with Compose, and a reverse proxy with HTTPS.

```sh
git clone https://github.com/CoreXI7/fishbowl && cd fishbowl
mkdir data && sudo chown 1000:1000 data
cp .env.example .env && chmod 600 .env
```

In `docker-compose.yml`, set `ORIGIN` (the dashboard's URL), `TRUSTED_PROXY` (your proxy's container name), the path
to Nginx Proxy Manager's `proxy_host` folder (if you use it) and your proxy's network. Then:

```sh
docker compose up -d
```

Point your reverse proxy at `dash:8000`. It must set the `X-Real-IP` and `X-Forwarded-Proto` headers;
[docs/SETTINGS.md](docs/SETTINGS.md#signing-in-auth-login-the-default) has the lines for common proxies.

Open the dashboard and create your account with the setup code from the log:

```sh
docker logs dash 2>&1 | grep "^SETUP CODE" | tail -n 1
```

Then open **⚙ → Setup**: it checks the rest of your install and shows how to fix anything missing.

### Optional: the collector

Drives, GPU, power and the security panel come from a small helper that runs on the host as root, in a systemd sandbox
and without network access:

```sh
sudo bash collector/install.sh
sudo nano /etc/homelab-dash/collector.toml
sudo systemctl restart homelab-dash-collector
```

## Usage

- **Settings:** click ⚙ or open `/settings`. Services, integrations, themes, drives, security checks and your account
  are all set there.
- **Sign-in:** one account, password of 15+ characters (long passwords are what resist offline guessing), optional
  authenticator app with recovery codes. Already using SSO? Set `AUTH: proxy` in `docker-compose.yml`.
- **Locked out:** `docker exec -it dash python /app/auth.py reset` (also `status`, `unlock`, `signout`).
- **Keys:** `W` wall mode, `T` theme, `M` background motion, `Esc` close a window.

All settings and environment variables: [docs/SETTINGS.md](docs/SETTINGS.md).

## Updating

```sh
git pull
docker compose up -d --force-recreate
sudo bash collector/install.sh    # if you use the collector
```

Keep your own compose changes in `docker-compose.override.yml` so `git pull` doesn't conflict.

## Screenshots

All screenshots use demo mode (fake data).

| | |
|---|---|
| ![Full page](docs/screenshot-full.png) Full page | ![Settings](docs/screenshot-settings.png) Settings |
| ![Setup checklist](docs/screenshot-setup.png) Setup checklist | ![Sign-in](docs/screenshot-signin.png) Sign-in |
| ![SSH logins](docs/screenshot-security.png) Security: SSH logins | ![Backups](docs/screenshot-backups.png) Security: backups |
| ![Live view](docs/screenshot-live.png) Live view | ![Breakdown](docs/screenshot-breakdown.png) CPU and memory breakdown |
| ![Gura theme](docs/screenshot-gura.png) Gura theme | ![Gura theme, dark](docs/screenshot-gura-dark.png) Gura theme, dark |
| ![Miku theme](docs/screenshot-miku.png) Miku theme | ![Account](docs/screenshot-account.png) Account, with a password warning |

## Security

The web app runs unprivileged in a read-only container and accepts connections only from your proxy. API keys never
reach the browser. The collector runs as root but never uses the network, and nothing the web app sends can change
what it reads without your approval on the host.

Version 1.0 was audited before release: four rounds of independent review (40 reviews by AI agents, covering
sign-in, HTTP handling, browser attacks, outbound requests, the root collector, cryptography and fuzzing). They found
2 high and about 50 medium issues, all fixed with regression tests; the last three rounds found no way in. It was not a
paid third-party audit.

- [TESTING.md](TESTING.md): what was tested and the results
- [THREAT-MODEL.md](THREAT-MODEL.md): design and known limits
- [SECURITY.md](SECURITY.md): report a vulnerability privately

## Contributing

Issues and pull requests are welcome. Run the tests before sending a change:

```sh
python3 -m unittest discover -s tests
```

[ARCHITECTURE.md](ARCHITECTURE.md) describes the code layout. Please report security problems privately
([SECURITY.md](SECURITY.md)), not as public issues.

## License

MIT
