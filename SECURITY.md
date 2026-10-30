# Security

Fishbowl runs on your home server and shows sensitive things (who logs in, your firewall, your backups), so it's
built to be hard to misuse. This page says how, what you have to do yourself, and how to report a problem.

## Reporting a vulnerability

Please **don't open a public issue**. Use GitHub's private report instead: the repository's **Security** tab →
**Report a vulnerability**. Include what you found, how to reproduce it, and which version (commit) you ran.
This is a one-person hobby project: expect an answer within about a week, and a fix as soon as it's understood.
Thank you for looking.

Supported: the latest `main`. There are no older release branches.

## What you have to do

Nobody gets in without signing in: Fishbowl's own sign-in is on by default (or your SSO protects it, with
`AUTH: proxy`). So:

1. **Create the account** the first time you open it, with the setup code from the container's log
   (`docker logs <container> 2>&1 | grep "^SETUP CODE" | tail -n 1`), and **turn on the authenticator app** (Settings → Account).
   Or, if your reverse proxy's sign-in (Authelia, Authentik, an access list) already protects it: `AUTH: proxy`.
2. **Reach it over HTTPS** through your reverse proxy, so the password and the session cookie are encrypted.
3. **Set `TRUSTED_PROXY`** to your proxy's container name, so nothing else can connect.
4. **Set `ORIGIN`** to the address you open it at.
5. Optional, recommended: **`LISTEN`** on a network only the proxy shares ([how](docs/SETTINGS.md#lock-the-dash-to-the-proxy-listen)).

The settings page's **Setup** checklist checks all of these and says how to fix what's missing.

## How it's built

Two parts, with only files between them:

- **The dashboard** (a container). It's what your proxy forwards to. It runs as an ordinary user with no Linux
  capabilities and a read-only filesystem, answers only your proxy, and can't run anything on the host. Everything it
  shows from outside (torrent names, Plex titles, log lines) is escaped; the Content-Security-Policy allows scripts only
  from its own files. API keys stay on the server and never go back to the browser.
- **The collector** (root, on the host). It never listens on a port and runs a fixed list of read-only commands
  (`smartctl`, `journalctl`, `fail2ban-client status`, `ufw status`, `docker ps/logs`, ...). Its systemd sandbox
  blocks all IP traffic, makes the system read-only except its output folder, and drops the root powers it doesn't use.
  `install.sh` picks the strictest sandbox that still works on your machine by running the collector's self-test
  under each.

The only way the dashboard can ask root for anything is a file in one folder, and the collector reads it as if an
attacker wrote it (no symlinks, no FIFOs, size caps, strict checks). The cron "Looks good" button sends a hash that
must match the crontabs exactly. Settings from the page that only change what's shown apply by themselves; anything
that would make root read a file or run a pattern waits until you approve it on the host with
`sudo homelab-dash-collector --apply`, which shows exactly what changes.

The full threat model, what's been fixed and what's still open: [THREAT-MODEL.md](THREAT-MODEL.md).
Every security fix has a test that failed before it (`python3 -m unittest discover -s tests`).
