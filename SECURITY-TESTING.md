# Security testing

What has been tested, how, and what came out of it, so you can judge Fishbowl's security for yourself. How it's
built and what it defends against is in [THREAT-MODEL.md](THREAT-MODEL.md); how to report a problem is in
[SECURITY.md](SECURITY.md).

**Who tested it.** The reviews below were done by AI review agents (Claude). Each round used fresh agents that hadn't
seen the earlier rounds' reasoning, each with one job: security code review, attacking a running copy, standards
research, or usability. This is **not** a paid outside audit. If you find something they missed, please report it
([SECURITY.md](SECURITY.md)).

**What you can re-run.** Every finding below was fixed together with a test that failed before the fix:

```sh
python3 -m unittest discover -s tests       # 253 tests, stdlib only; page tests use headless Chrome if installed
```

The attackers' own scripts aren't in the repo: they were written against throwaway copies (fixed ports, temporary
folders) and many target code that has since changed. What each one found is pinned by a test instead.

## Contents

- [How the testing was done](#how-the-testing-was-done)
- [The sign-in: five review rounds](#the-sign-in-five-review-rounds-2026-10-30)
- [What was attacked, and the result](#what-was-attacked-and-the-result)
- [The rest of the app](#the-rest-of-the-app-2026-10-29)
- [Where the tests are](#where-the-tests-are)
- [Limits of this testing](#limits-of-this-testing)

## How the testing was done

- **Only against copies.** Every attack ran against a throwaway copy of the app on `127.0.0.1` with its own data
  folder, never against a real install. The attacker played the reverse proxy too, both one that buffers request
  bodies (like Nginx Proxy Manager) and one that streams them (the default in Caddy, Traefik and HAProxy).
- **Four kinds of reviewer**, fresh each round:
  - **Security code review**: a line-by-line read, checked against OWASP ASVS 5.0 level 2 and NIST SP 800-63B-4.
  - **Penetration test / red team**: attacked the running copy with its own scripts. Goals: get a session or change
    the account without the password and code, read anything protected, lock the owner out, crash or exhaust the
    server, hide evidence from the owner.
  - **Research**: the standards (NIST 800-63B-4, ASVS 5, the OWASP cheat sheets, the RFC 6238 test vectors) and the
    published vulnerabilities of 16 similar self-hosted apps, turned into 94 attack scenarios to try. Round 4's
    researcher looked for attack classes specific to Python's standard library and to browsers.
  - **Usability and accessibility**, in Chrome and Firefox, including keyboard use and phone widths: a security feature
    that people can't use gets turned off, or leaves them locked out.
- **Regression every round.** Each round re-ran the earlier rounds' attacks against the new code. By the last round
  that was 41 attack scripts, and none of the earlier findings had come back.
- **Fix, then test, then review again.** Every finding of medium or above was fixed before the next round, with a
  test that failed before the fix. The next round's reviewers checked the fixes as well as the rest.

## The sign-in: five review rounds (2026-10-30)

Fishbowl's own sign-in (`AUTH=login`, the default) was reviewed in five rounds of three reviewers each (15 reviews in
all) before it was released.

| Round | Reviewers | What they found (highest first) |
|---|---|---|
| 1 | security, pen test, usability | **Security:** 1 high (a sign-in checked just before a password change could finish after it and stay signed in), 4 medium (recovery codes cut short; a reset on the host could be undone by a sign-in in flight; a password change without a code when two-step was on; a trusted browser's success reset the 100-try lock). **Pen test:** junk cookies sent first could push the real one out (forced sign-out). **Usability:** 2 high (recovery codes unusable and hard to read). |
| 2 | security, pen test, usability | **Security:** 1 medium (a new account started at the same epoch as the deleted one, so a sign-in checked just before a reset on the host could land in the new account), 3 low. **Pen test:** 1 medium (of two account changes approved at the same moment, both could land). A malformed `Origin` header gave a 502 instead of a refusal. **Usability:** 1 high (the documented command to find the setup code printed the hint line, not the code), 5 medium (your own typo raised a lasting "someone knows your password" alarm, among others). |
| 3 | ASVS 5 + NIST audit, pen test, usability | **No high.** Medium: a session cookie captured over plain HTTP also worked from outside and over HTTPS; forgiving the owner's typos "by address" could hide an attacker on the same address; stuck connections could use up the server's threads (only when port 8000 is reachable directly). Low: cookie parsing had become slow on huge headers; a sign-out wasn't final against a change already in flight. |
| 4 | researcher, pen test, code quality | **No way in.** Medium: after signing out, Back showed Settings from the browser's back/forward cache, fresh recovery codes included (Chrome and Firefox); a stolen session **plus** the password (without the code) could keep the owner out by signing them out again and again; stalled connections could exhaust memory despite the connection caps (again only when port 8000 is reachable directly). |
| 5 | final red team, final security review, final usability | **Security review: nothing medium or above, no regressions.** Red team: no way in; 2 medium availability issues: an outsider's guessing could make the wait at the code step outlast the step's 5-minute life, so no new browser could finish signing in to a two-step account; and behind a proxy that doesn't send `X-Real-IP` and streams bodies, one client with 16 slow requests could keep everyone out. |

Everything of medium or above was fixed, and so were most low findings. Each fix came with a test (in `tests/test_auth.py`, the class named after the round).

## What was attacked, and the result

**Held** means the attack failed. **Fixed** means it worked at first; the round in brackets is when it was fixed.

### Getting in without the password

| Attack | Result |
|---|---|
| Guessing passwords with a fresh `X-Real-IP` on every try; IPv6 from a whole /64 | **Held**: counted per address (IPv6 per /64) **and** for the account; 5 wrong, then 429 before the password is even hashed |
| Many guesses in parallel to slip under the limit | **Held**: each try is counted and saved before it's checked, under one lock |
| Timing: wrong name vs wrong password vs both | **Held**: same status and the same 57-byte answer; a wrong name costs a password hash too, so the times overlap (medians 137–152 ms across runs) |
| Racing to create the first account: 50 requests at once with the right setup code; replay | **Held**: exactly 1 account; replay refused |
| Guessing the setup code, from rotating addresses | **Held**: 125 bits; about 3,300 guesses a second would take ~10²⁶ years |
| Session fixation (choosing the cookie value) | **Held**: a new random token at every sign-in |
| Authenticator code: the same code in 8 parallel sign-ins; replay; an older code after a newer one | **Held**: exactly one works, once |
| 12 lookalike codes (fullwidth and Arabic-Indic digits, `+`, NUL, zero-width space, 7 digits, ...) | **Held**: all "wrong code" |
| The same recovery code from 8 browsers at once | **Held**: exactly one works |
| A sign-in that was half done (password right, code not yet typed) when the password changed | **Fixed** (round 1): it dies with the change |
| Hundreds of sign-ins racing four password changes and two-step on/off | **Held** (after round 1's fix): 137 sessions and 29 trusted browsers made around the changes, 0 work afterwards |
| A reset on the host racing sign-ins in flight | **Fixed** (round 1): the host command and the server share one lock |
| An account deleted and made again: does an old sign-in fit the new one? | **Fixed** (round 2): every account gets a new random epoch |

### Acting as you from another website (CSRF and friends)

| Attack | Result |
|---|---|
| 19 combinations of `Origin`, `Sec-Fetch-Site` and `Content-Type` on a password change | **Held**: only the dashboard's exact Origin with a JSON body passes |
| Cross-site forms (`text/plain`), `Origin: null`, a sibling site on the same domain (also with a forged right Origin) | **Held**: all refused before anything is counted |
| DNS rebinding: 57 Host/Origin pairs over plain HTTP with `ORIGIN` unset (lookalikes of `.local`, `.lan`, `.internal`, punycode, IP literals, ...) | **Fixed** (round 1): changes only for a Host no website can own |
| Framing the page (clickjacking) | **Held**: `frame-ancestors 'none'` and `X-Frame-Options: DENY`; the standard library's own error pages got them too in round 4 |
| Open redirect through `next=` (24 values: `//`, `javascript:`, CRLF, nested `next`, long queries) | **Held**: only ever `/` or `/settings` with a plain query |

### Faking where you are (proxy headers)

| Attack | Result |
|---|---|
| `X-Real-IP` / `X-Forwarded-Proto` sent by something that isn't the trusted proxy | **Held**: ignored (with `TRUSTED_PROXY` set, anything else is refused before a thread starts) |
| A proxy in front that nobody named in `TRUSTED_PROXY` | **Fixed** (round 3): it used to make every visitor look local; now the address counts as unknown and the sign-in page says to set `TRUSTED_PROXY` |
| Repeated headers, lists, `Forwarded`, `X-Forwarded-For` | **Fixed** (round 3): a repeated header counts as missing; lists are ignored |
| Plain-HTTP sign-in claimed from 6to4, NAT64, CGNAT, Teredo, `0.0.0.0`, scoped or mapped IPv6 | **Fixed** (round 3): "your own network" is now an explicit list; round 4 re-tested every range and it held |
| A session cookie captured over plain HTTP, replayed from outside or over HTTPS | **Fixed** (round 3): a session works only on the kind of connection it was made on; the same for trusted browsers (round 4) |

### Reading what you shouldn't

| Attack | Result |
|---|---|
| 55 path spellings (`//`, `%2e`, `%2f`, `;`, `\`, `%00`, `/static/../`, case) and 19 odd request lines (absolute URI, `*`, HTTP/0.9, two Hosts, `X-Original-URL`) | **Held**: refused or sent to the sign-in |
| Uploaded files (mascot, icons) without signing in | **Fixed** (round 1): private behind the sign-in |
| Back button after signing out (back/forward cache) | **Fixed** (round 4): pages behind the sign-in are `no-store` and reload when brought back |
| Session tokens in the log | **Held**: 0 tokens and 0 token-shaped strings in the log |
| Compression of Account answers (BREACH-style guessing) | **Fixed** (round 5): Account answers are never compressed |

### With a stolen session

| Attack | Result |
|---|---|
| Stolen session: change the password, turn two-step off, make new recovery codes, sign the owner out | **Fixed** (rounds 1, 3 and 4): every Account change, signing browsers out included, needs the password, and a code when two-step is on |
| Stolen session **plus** the password (no code): sign the owner out again and again | **Fixed** (round 4): 0 of about 123,000 attacker calls landed; the owner signs in and changes the password as normal |
| Pushing the owner's own "wrong tries" count from outside to sign them out | **Held**: only the session's own requests count on it |
| A sign-out racing an attacker's change already in flight | **Fixed** (round 3): the change re-checks the session inside the same locked update |

### Locking you out or knocking it over

| Attack | Result |
|---|---|
| An outsider guessing until the account locks | **Held**: browsers you signed in with before still get in; the outsider can't reset the lock |
| An outsider's guessing making the code step wait longer than the step lasts | **Fixed** (round 5): the code step counts tries but never waits |
| 500 slow-header and slow-body connections (slowloris) | **Fixed** (rounds 3–5): 80 connections at most, 32 per address, 20 s to send a request, the oldest slow sender is cut when full |
| 6.4 MB of headers on stalled connections (memory) | **Fixed** (round 4): 64 KB of headers at most (431); memory flat at 34 MB |
| 6 MB of junk cookies, 16 clients in parallel (CPU) | **Fixed** (round 3): linear parsing, at most 300 values; about 10 ms per request |
| Junk cookies sent first to push the real one out | **Fixed** (round 1): every value is tried |
| Request smuggling: `Content-Length` + `Transfer-Encoding`, two lengths, `-1`, `+31`, Arabic digits, `Expect: 100-continue` | **Held**: one answer per connection, never a second request |
| Keep-alive, pipelining, `h2c` upgrade | **Held**: one answer per connection, nothing carried over |
| 34 threads for 90 s doing sign-ins, reads, sign-outs, password changes and host commands at once | **Held**: no deadlock, no hang, no errors |

### Hiding evidence from the owner

| Attack | Result |
|---|---|
| Guessing from the owner's own address so it passes as the owner's typos | **Fixed** (round 3): typos are forgiven per trusted browser, never per address |
| A trusted-browser token captured over plain HTTP, to hide guesses as the owner's typos | **Fixed** (round 4) |
| Pressing "That was me" to clear warnings the owner hasn't seen | **Fixed** (round 4): it needs the password and code, and clears only what was shown |

### Hostile input

| Attack | Result |
|---|---|
| Names, browser strings and addresses carrying script (`"><img onerror>`, `<svg onload>`, template syntax) in every list, the notice and the QR page, in headless Chrome | **Held**: escaped everywhere; 0 handlers fired, 0 CSP errors |
| 37 hostile JSON values in every field of 9 sign-in endpoints, plus 17 odd bodies (UTF-16, BOM, broken UTF-8, duplicate keys, 33 levels deep, 5,000-digit numbers, NaN) | **Held**: 0 server errors; a malformed `Origin` returned 502 at first, **fixed** in round 2 |
| Damaged or planted files: `auth.json` as a symlink or FIFO, garbage or 5 MB `sessions.json`, a dangling lock file | **Held**: fails closed (nobody gets in, never "anyone"), never hangs |

## The rest of the app (2026-10-29)

Before the sign-in existed, the whole app had a security audit and hardening pass: a threat model, then reviews by
five agents (security, correctness, page/UI, and two for the root collector, one of them adversarial). The collector
was reviewed twice more after its fixes. The findings and fixes (script injection from qBittorrent and Seerr fields,
SSRF through redirects, the collector's handling of files the dashboard writes, size caps, log forging, upload caps,
IPv6 matching, and more) are listed in [THREAT-MODEL.md](THREAT-MODEL.md#fixed-in-the-2026-10-29-hardening).

The settings page (four rounds, four reviewers: security, correctness, usability and accessibility, a first-time
user) and the collector's settings on the page (three rounds: adversarial, reliability, the page) were reviewed the
same way. Each fix has a test in `tests/`.

## Where the tests are

| File | What it checks |
|---|---|
| `tests/test_auth.py` | The sign-in. `Primitives`: RFC 6238 test vectors, hashing, password rules, recovery codes. `Gate`, `Setup`, `SignIn`, `TwoStep`, `AccountChanges`: the normal flows and their refusals. `Hardening`: attacks from the research. `RoundOne` … `RoundFive`: one test per review finding, named after what it checks. `SetupChecks`: the Setup checklist's sign-in items. |
| `tests/test_csp.py`, `test_page_escaping.py` | Content-Security-Policy and escaping of everything from outside |
| `tests/test_redirects.py`, `test_upstream_*.py`, `test_plexart.py` | No off-host redirects (SSRF), size caps, hostile upstream data |
| `tests/test_uploads.py`, `test_known_ips.py`, `test_listen.py`, `test_logging.py` | Upload limits, IP matching, `LISTEN`, log forging |
| `tests/test_collector_*.py` | The root collector: requests from the dashboard, its settings, journal reading |
| `tests/test_settings_page.py` | The settings page: validation, fields locked by the environment, no secrets in the page data, the Setup checklist (including whether a sign-in sits in front) |

## Limits of this testing

- **Not a third-party audit**, and nothing is formally proven. Reviewers can miss things; that's why every round
  re-ran the earlier attacks, and why reports are welcome.
- **Proxies were played by the tester.** The buffering and streaming proxy behaviours were tested; specific proxy
  products and versions weren't.
- **Choices that differ from the standards** (longer "keep me signed in" sessions for wall screens, optional
  two-step, CSRF without a token, and others) are deliberate and listed in
  [THREAT-MODEL.md](THREAT-MODEL.md#the-sign-in-authlogin) under "Deliberate deviations".
- **Known open items** are listed in [THREAT-MODEL.md](THREAT-MODEL.md#still-open). The main one for the sign-in:
  someone who can reach port 8000 directly from many addresses can still keep it busy. Keep it behind your proxy
  (`TRUSTED_PROXY` and `LISTEN`).
