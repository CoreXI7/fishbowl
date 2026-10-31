# Testing

How Fishbowl 1.0 was tested, what came out of it, and what wasn't covered. Every attack and its result, in detail:
[SECURITY-TESTING.md](SECURITY-TESTING.md). How it's built to be safe: [THREAT-MODEL.md](THREAT-MODEL.md).

## Run the tests yourself

```sh
python3 -m unittest discover -s tests       # 357 tests, stdlib only, ~5 minutes; page tests use headless Chrome if installed
```

Nothing real is contacted: fake qBittorrent, Seerr, Tautulli and web apps (`tests/helpers.py`) stand in for the real
ones, and test servers bind `127.0.0.1` only. Result for 1.0: **357 tests, all passing**, on Python 3.12.

## What's tested, and how

| Kind | What | How |
|---|---|---|
| **Unit and integration tests** (`tests/`) | The sign-in (RFC 6238 vectors, hashing, sessions, lockouts, the "someone knows your password" alarm), the gate and every API, settings validation and saving, the Setup checklist, upstream answers (sizes, chunking, deadlines, redirects), the NPM parser, uploads, `LISTEN`/`TRUSTED_PROXY`, log forging, the page's escaping and Content-Security-Policy, and the root collector (its file reading, journal, crontabs, redaction, the dashboard's requests, the self-test) | Each test runs the real app code against throwaway data folders and fake services; page tests drive headless Chrome |
| **Regression tests for every finding** | Every security finding of medium or above (and most lows) from the reviews | Each fix came with a test that **failed before the fix** and passes after, checked by running it against the previous commit |
| **Browser sign-in test** | The whole sign-in by hand, in a real browser: setup code, account creation, two-step with QR code and recovery codes, sign-out, wrong codes, the alarm, wall mode, phone width, no JavaScript errors | Headless Chrome against a local copy: 44 checks, all passing |
| **Screenshots before and after** | The wall view, dashboard, settings, sign-in page and phone views, in all three themes, light and dark | Compared pixel by pixel before and after the audit fixes: no visual changes beyond live demo data |
| **Live checks after deploying** | The live install's pages and APIs, the wall view, the public demo, and the logs | Every page and API answered; 54 of 54 services checked as up; no errors in the log |

## The 1.0 security audit

Four rounds, 40 independent reviews by ten roles (sign-in, HTTP layer, browser side, outbound requests, root
collector, cryptography, a black-box red team, fuzzing, research, release hygiene), each round with fresh reviewers.

| Round | Reviewers | Found (medium or above) | Result |
|---|---|---|---|
| 1 | 10 | 1 high, 18 medium | all fixed, each with a test |
| 2 | 10 | 1 high, 11 medium (several in round-1 fixes) | all fixed |
| 3 | 10 | 11 medium | all fixed |
| 4 | 9 + a final re-check of the round-4 fixes | 9 medium: a hostile service using up memory, rare setups (rootless Podman 4, IPv6-only Docker networks), the root collector's memory budget | all fixed |

- **Never against a real install:** throwaway copies on `127.0.0.1` or internal Docker networks, on the host's Python
  3.12.3 and the exact pinned image (Python 3.12.16).
- **Real proxies and browsers:** Nginx Proxy Manager, nginx 1.30, Caddy, HAProxy and Traefik in front; Chrome and
  Firefox for the browser attacks.
- **Fuzzing:** more than 15 million executions over the HTTP API in every sign-in mode (with hostile fake services
  answering), the settings validation, and the collector's parsers; 660,000 differential runs of the NPM-config reader
  against real `nginx -t`. Line coverage during fuzzing: app 87–89%, sign-in 81%, settings 96%, upstream 81–93%.
- **Regression every round:** each round re-ran every earlier round's attack scripts and crashers; none of the fixed
  findings came back.
- **No way in** was found in rounds 2, 3 or 4: no session or account change without the password and code, no
  protected data, no code execution, nothing that steers the root collector.

## Known issues and limits

- **AI review, not a paid audit.** The reviewers were AI agents. Nothing is formally proven; reports are welcome
  ([SECURITY.md](SECURITY.md)).
- **Not run for real:** Safari, Podman (its behaviour was reproduced from its documentation), Cloudflare Tunnel,
  Tailscale.
- **Accepted trade-offs**, written down in [THREAT-MODEL.md](THREAT-MODEL.md#still-open): someone who keeps guessing can
  make a *new* browser of yours wait or lock it out (browsers you've used before still get in); over plain HTTP, other
  web apps on the same address receive the dashboard's cookies; a page user (a stolen session, or anyone past your SSO)
  can make the dashboard fetch addresses it chooses; an owner-approved backup pattern can be slow for root's
  collector.
- **Very large qBittorrent libraries:** an answer is refused once its text would take more than twice its 32 MB cap in
  memory, and one emoji anywhere makes every character count four times. So a library of roughly 7,500+ torrents with
  an emoji in any name shows an error on the torrents panel (the rest of the dashboard is unaffected).
- **Lows left open:** a few low-severity items (mostly wrong "all good" readings of unusual hand-written NPM configs,
  and long-standing robustness issues on inputs only root writes) are listed in the audit reports and don't let anyone
  in.
