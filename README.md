# Fishbowl

Your whole home server in one glanceable page.

| Light | Dark |
|---|---|
| ![Fishbowl, light](docs/screenshot-wall-light.png) | ![Fishbowl, dark](docs/screenshot-wall-dark.png) |

Fishbowl is a dashboard for a home server. Open it in a browser (or leave it on a spare screen) and you can see at a
glance whether everything is running: your services, who is watching Plex, new media requests, downloads, temperatures,
drives, and recent security events.

It's written in plain Python with no extra packages, and runs in Docker.

**Try it first:** there's a [live demo](https://demo.vicy.us) with made-up data.

## Contents

- [What it shows](#what-it-shows)
- [What you need](#what-you-need)
- [Install](#install)
- [Using it](#using-it)
- [Updating](#updating)
- [Screenshots](#screenshots)
- [Security](#security)
- [Contributing](#contributing)
- [License](#license)

## What it shows

- **Services:** whether each of your web services is up. If you use Nginx Proxy Manager, Fishbowl finds them on its
  own; otherwise you add them on the settings page.
- **Plex:** who is watching what right now (needs Tautulli).
- **Requests:** new movie and TV requests from Seerr, with Approve and Decline buttons.
- **Downloads:** qBittorrent's progress and speeds, plus a warning if your real IP address is showing instead of your
  VPN's.
- **System:** CPU, memory, network and temperatures, with a live view that updates every second.
- **GPU:** usage and video encoding for NVIDIA cards. Other GPUs aren't supported yet.
- **Drives, security and backups:** disk space and health, blocked login attempts, SSH logins from new places, changed
  scheduled jobs, and whether your last backup ran. These need the optional [collector](#optional-install-the-collector).

It also has a wall mode for an always-on screen, three themes in light and dark, and its own sign-in.

## What you need

- A Linux server with Docker and Docker Compose
- A reverse proxy that gives the dashboard an HTTPS address (any one you already use is fine)
- Optional, for the collector: Python 3.11 or newer, systemd and `smartctl` on the server

## Install

### 1. Download Fishbowl

```sh
git clone https://github.com/CoreXI7/fishbowl
cd fishbowl
```

### 2. Create the data folder and the `.env` file

Fishbowl saves its settings in `data/`. The `.env` file can hold your API keys, but it can stay empty, since you can
also enter keys on the settings page later.

```sh
mkdir data
sudo chown 1000:1000 data
cp .env.example .env
chmod 600 .env
```

### 3. Edit `docker-compose.yml`

Open `docker-compose.yml` and change these lines to match your server:

- `ORIGIN`: the address you'll open the dashboard at, for example `https://dash.example.com`
- `TRUSTED_PROXY`: the container name of your reverse proxy (it's set to `nginx` by default)
- `networks`: the Docker network your reverse proxy is on (`reverse_proxy` by default)
- The `proxy_host` volume: the path to Nginx Proxy Manager's `data/nginx/proxy_host` folder. Delete this line if you
  don't use Nginx Proxy Manager.

### 4. Start it

```sh
docker compose up -d
```

### 5. Connect your reverse proxy

In your reverse proxy, send your chosen address (for example `dash.example.com`) to `dash`, port `8000`.

Your proxy also needs to pass on each visitor's real IP address, so Fishbowl can tell visitors apart. The exact lines
for each proxy are in [docs/SETTINGS.md](docs/SETTINGS.md#signing-in-auth-login-the-default). The settings page will
remind you if this is missing.

### 6. Create your account

Open your dashboard's address. The first time, it asks for a one-time setup code. Get it from the container's log:

```sh
docker logs dash 2>&1 | grep "^SETUP CODE" | tail -n 1
```

Then choose a name and a password of at least 15 characters (a few random words work well).

Already have a login page in front of your services, like Authelia or Authentik? Then you can turn Fishbowl's own
sign-in off: add `AUTH: proxy` under `environment:` in `docker-compose.yml` and run `docker compose up -d` again.

### 7. Finish setup in the browser

Click the **⚙** button and open **Setup**. It checks your install and tells you how to fix anything that's missing.
Then connect your apps under **Integrations**: qBittorrent, Seerr and Tautulli each have a **Test** button.

### Optional: install the collector

The collector is a small helper that runs directly on your server, outside Docker. It reads things the dashboard
can't see from inside its container: drives, the GPU, power use, and security logs.

```sh
sudo bash collector/install.sh
sudo nano /etc/homelab-dash/collector.toml
sudo systemctl restart homelab-dash-collector
```

In `collector.toml`, list your drives and, if you use them, your fail2ban jail and backup log. To remove the
collector: `sudo bash collector/install.sh --uninstall`.

## Using it

- **Settings:** click ⚙ (or go to `/settings`). Everything from your services to themes to your account is set there,
  and changes apply right away.
- **Two-step sign-in:** go to ⚙ → **Account** to add an authenticator app. Keep the recovery codes it gives you.
- **Wall mode:** press `W` for a full-screen view made for a TV or spare monitor. `T` changes the theme.
- **Locked out or forgot your password:** run `docker exec -it dash python /app/auth.py reset` on the server. It
  removes the account (your settings stay) and prints a new setup code.

Every setting is explained in [docs/SETTINGS.md](docs/SETTINGS.md).

## Updating

```sh
cd fishbowl
git pull
docker compose up -d --force-recreate
```

If you use the collector, update it too: `sudo bash collector/install.sh`.

Tip: if you changed `docker-compose.yml`, `git pull` may refuse to update it. Putting your changes in a file called
`docker-compose.override.yml` instead avoids that.

## Screenshots

All screenshots use the demo's made-up data.

| | |
|---|---|
| ![Full page](docs/screenshot-full.png) Full page | ![Settings](docs/screenshot-settings.png) Settings |
| ![Setup checklist](docs/screenshot-setup.png) Setup checklist | ![Sign-in](docs/screenshot-signin.png) Sign-in |
| ![SSH logins](docs/screenshot-security.png) Security: SSH logins | ![Backups](docs/screenshot-backups.png) Security: backups |
| ![Live view](docs/screenshot-live.png) Live view | ![Breakdown](docs/screenshot-breakdown.png) What's using the CPU |
| ![Gura theme](docs/screenshot-gura.png) Gura theme | ![Gura theme, dark](docs/screenshot-gura-dark.png) Gura theme, dark |
| ![Miku theme](docs/screenshot-miku.png) Miku theme | ![Account](docs/screenshot-account.png) Account settings |

## Security

- **Sign-in required.** Nobody can see or change anything without signing in, either with Fishbowl's own sign-in or
  your existing login page.
- **Your API keys stay on the server.** They are never sent to the browser.
- **Only your reverse proxy can connect** to the dashboard, set with `TRUSTED_PROXY`.
- **The parts are kept apart.** The dashboard, which is reachable from the network, runs with no special permissions.
  The collector, which runs as root, can't be reached from the network at all. So someone who breaks into the
  dashboard still can't control the server.

More detail: [THREAT-MODEL.md](THREAT-MODEL.md) explains the design and its known limits, and
[TESTING.md](TESTING.md) describes how it was tested.

Found a security problem? Please report it privately, as described in [SECURITY.md](SECURITY.md).

## Contributing

Bug reports and pull requests are welcome. Before sending a change, run the tests:

```sh
python3 -m unittest discover -s tests
```

[ARCHITECTURE.md](ARCHITECTURE.md) explains how the code is laid out.

## License

MIT
