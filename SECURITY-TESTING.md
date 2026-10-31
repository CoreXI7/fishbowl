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
python3 -m unittest discover -s tests       # 357 tests, stdlib only; page tests use headless Chrome if installed
```

**Why the attack scripts aren't in the repo.** The reviewers wrote about 1,500 scripts. They aren't published because:

- They were written for throwaway copies (fixed ports, temporary folders, the reviewer's own machine) and most no
  longer run against the current code, so they couldn't be re-run as they are.
- Several are working attacks against versions before 1.0; there's no reason to hand those out.

What each one found is pinned by a regression test instead: the test reproduces the problem, failed before the fix
and passes after. Those tests are the reproducible part, and anyone can run them (above). A test's comment says which
review round found the problem it checks.

## Contents

- [How the testing was done](#how-the-testing-was-done)
- [The sign-in: five review rounds](#the-sign-in-five-review-rounds-2026-10-30)
- [What was attacked, and the result](#what-was-attacked-and-the-result)
- [The 1.0 pre-release audit](#the-10-pre-release-audit-2026-10-30)
- [Every finding of the 1.0 audit](#every-finding-of-the-10-audit)
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

## The 1.0 pre-release audit (2026-10-30)

Before 1.0, the whole app (the sign-in, the HTTP layer, the browser side, outbound requests, the root collector)
was audited again, in four rounds of fresh reviewers: 40 reviews by ten roles (sign-in and sessions, HTTP layer,
browser and web, SSRF and upstreams, root collector, cryptography, a black-box red team with only the README, fuzzing,
research, release hygiene). Every round re-ran all earlier rounds' attack scripts.

| Round | Medium or above found | Examples |
|---|---|---|
| 1 | 1 high, 18 medium | **High:** over plain HTTP by IP, every other web app on the host received the session cookie and could replay it (sessions are now bound to the exact address). Medium: slow answers bypassing the outbound deadline, a ReDoS in the collector's redaction, the collector following links in folders others can write. |
| 2 | 1 high, 11 medium | **High:** the outbound deadline was dropped for answers that "will close" (fixed in the deadline watcher). Medium: several bugs in round-1 fixes (an off-by-one in the code step's tries, device binding locking the owner out, IPv6 /64 binding). |
| 3 | 11 medium | A hostile service's failed answer kept ~50 MB alive per request (OOM), a password step left to run out unseen, a forwarding header sent twice slipping past the check for an unnamed proxy, the collector's log tail reading unbounded. |
| 4 | 9 medium | A password step ended without a code ("Start over") unseen; rootless Podman 4 making every visitor the container itself; an IPv6-only network not counted; a chunked or wide-character answer using up memory; connecting outside the deadline; three ways to exhaust the root collector's memory or make it write through a link. |

**How:** throwaway copies only, on the host's Python and the exact pinned image (3.12.16); real Nginx Proxy Manager,
nginx, Caddy, HAProxy and Traefik in front; Chrome and Firefox for the browser attacks; more than 15 million fuzzing
executions plus 660,000 differential runs against real `nginx -t`.

**Result:** every finding of medium or above fixed, each with a test that failed before the fix. No way in was found
in rounds 2 to 4. What remains is listed in [THREAT-MODEL.md](THREAT-MODEL.md#still-open); the summary for users is in
[TESTING.md](TESTING.md).

## Every finding of the 1.0 audit

Each finding of medium or above, what happened to it, the commit that fixed it and the test that checks it. The ID's
letter is the reviewer's area (A sign-in, B HTTP layer, C browser, D outbound requests, E root collector, F
cryptography, G black-box red team, H fuzzing, I research, J release hygiene) and its digit the round. To check a
fix yourself, read its commit and run its test, for example:

```sh
python3 -m unittest discover -s tests -p test_upstream_slow.py -k test_connecting_has_an_overall_deadline
```

| ID | Severity | What it was | Status | Fix | Test |
|---|---|---|---|---|---|
| I1-1 | High | Over plain HTTP by IP, other web apps on the host received the session cookie and could replay it | Fixed | [`5b95736`](https://github.com/CoreXI7/fishbowl/commit/5b95736) | `test_auth.py`: `test_a_plain_http_session_counts_only_from_the_address_it_was_made_on` |
| A1-1 | Medium | The code step's try limits didn't hold under parallel requests | Fixed | [`eb743ce`](https://github.com/CoreXI7/fishbowl/commit/eb743ce) | `test_auth.py`: `test_parallel_codes_on_one_step_are_five_at_most` |
| A1-2 | Medium | Plain HTTP: other apps on the host receive the dashboard's cookies (same as I1-1) | Fixed; cookies reaching other ports documented | [`5b95736`](https://github.com/CoreXI7/fishbowl/commit/5b95736) | `test_auth.py`: `test_a_plain_http_session_counts_only_from_the_address_it_was_made_on` |
| C1-1 | Medium | Plain-HTTP sessions usable by other apps on the host (same as I1-1) | Fixed | [`5b95736`](https://github.com/CoreXI7/fishbowl/commit/5b95736) | `test_auth.py`: `test_a_plain_http_session_counts_only_from_the_address_it_was_made_on` |
| C1-2 | Medium | A hostile service's icon could be an SVG page on the dashboard's origin | Fixed | [`90f3184`](https://github.com/CoreXI7/fishbowl/commit/90f3184), [`95af9ce`](https://github.com/CoreXI7/fishbowl/commit/95af9ce) | `test_icons.py`: `test_an_svg_never_gets_through` |
| B1-1 | Medium | Real proxies' defaults let visitors choose X-Real-IP / X-Forwarded-Proto | Fixed (tested proxy lines, Setup warning) | [`0e963fd`](https://github.com/CoreXI7/fishbowl/commit/0e963fd) | `test_auth.py`: `test_npm_that_lets_visitors_choose_their_address_is_a_setup_item` |
| J1-1 | Medium | With Nginx Proxy Manager as documented, visitors choose the address the dashboard sees | Fixed (same as B1-1) | [`0e963fd`](https://github.com/CoreXI7/fishbowl/commit/0e963fd) | `test_auth.py`: `test_npm_that_lets_visitors_choose_their_address_is_a_setup_item` |
| B1-2 | Medium | TRUSTED_PROXY by name trusted every container Docker's DNS returned for it | Fixed | [`ac73241`](https://github.com/CoreXI7/fishbowl/commit/ac73241) | `test_listen.py`: `test_a_name_two_containers_answer_to_is_trusted_for_neither` |
| B1-3 | Medium | ~32 slow requests from one client kept the public demo down | Fixed | [`8b970b1`](https://github.com/CoreXI7/fishbowl/commit/8b970b1) | `test_auth.py`: `test_connection_caps_and_deadline` |
| G1-1 | Medium | An outsider's guessing can make new browsers wait or lock them out | Accepted and documented (browsers used before still get in) | [`0132a29`](https://github.com/CoreXI7/fishbowl/commit/0132a29) | `test_auth.py`: `test_a_new_browser_kept_waiting_is_told_why` |
| I1-2 | Medium | An upstream that trickles its answer held request threads forever | Fixed | [`00b460f`](https://github.com/CoreXI7/fishbowl/commit/00b460f) | `test_upstream_slow.py`: `test_a_health_check_ends_on_a_trickling_header` |
| D1-1 | Medium | One slow upstream filled every connection place (same as I1-2) | Fixed | [`00b460f`](https://github.com/CoreXI7/fishbowl/commit/00b460f) | `test_upstream_slow.py`: `test_an_icon_ends_on_a_trickling_body` |
| D1-2 | Medium | A hostile qBittorrent grew the dashboard's memory without bound | Fixed | [`38cb0ca`](https://github.com/CoreXI7/fishbowl/commit/38cb0ca) | `test_upstream_memory.py`: `test_torrent_ids_stay_short` |
| D1-3 | Medium | A hostile Tautulli or Seerr crashed the dashboard with one small answer (uncapped lists) | Fixed | [`38cb0ca`](https://github.com/CoreXI7/fishbowl/commit/38cb0ca) | `test_upstream_memory.py`: `test_cookie_jar_stays_small` |
| D1-4 | Medium | A signed-in user could use extra services to fetch LAN addresses and read image bodies back | Fixed (icons from the service's own origin only); status-only checks documented | [`90f3184`](https://github.com/CoreXI7/fishbowl/commit/90f3184) | `test_icons.py`: `test_only_the_services_own_origin` |
| E1-1 | Medium | Root's unredacted crontab snapshot was readable while being written | Fixed | [`d4751a1`](https://github.com/CoreXI7/fishbowl/commit/d4751a1) | `test_collector_files.py`: `test_files_are_private_while_written` |
| E1-2 | Medium | Root opened device nodes planted in container-writable folders | Fixed | [`cbf17d5`](https://github.com/CoreXI7/fishbowl/commit/cbf17d5) | `test_collector_files.py`: `test_a_device_or_fifo_is_never_opened_for_reading` |
| E1-3 | Medium | Anyone could OOM-kill the collector through Authelia's log | Fixed | [`0ae64e0`](https://github.com/CoreXI7/fishbowl/commit/0ae64e0) | `test_collector_files.py`: `test_a_huge_log_is_streamed` |
| E1-4 | Medium | Configured paths were read through links planted by a container | Fixed | [`cbf17d5`](https://github.com/CoreXI7/fishbowl/commit/cbf17d5) | `test_collector_files.py`: `test_a_link_in_a_folder_others_can_write_is_not_followed` |
| F1-1 | Medium | redact() let common password forms through to the browser | Fixed; plain-argument passwords documented | [`4b8447c`](https://github.com/CoreXI7/fishbowl/commit/4b8447c), [`d1783ab`](https://github.com/CoreXI7/fishbowl/commit/d1783ab) | `test_collector_redact.py`: `test_common_passwords_on_command_lines_are_hidden` |
| H1-1 | Medium | One long crontab line froze the collector (redact() was cubic) | Fixed | [`7043360`](https://github.com/CoreXI7/fishbowl/commit/7043360), [`cf14d5e`](https://github.com/CoreXI7/fishbowl/commit/cf14d5e) | `test_collector_redact.py`: `test_hostile_lines_stay_quick` |
| H1-2 | Medium | A lone surrogate in settings text gave a 502 and left a temp file | Fixed | [`c90976b`](https://github.com/CoreXI7/fishbowl/commit/c90976b) | `test_settings_page.py`: `test_a_lone_surrogate_is_refused` |
| I1-3 | Medium | The collector ran SQL from Authelia's database file (generated columns, virtual tables) | Fixed | [`6dd3b70`](https://github.com/CoreXI7/fishbowl/commit/6dd3b70) | `test_collector_files.py`: `test_authelia_tables_must_be_plain_tables` |
| D2-0 | High | The deadline was dropped for answers that close the connection | Fixed | [`ee8f499`](https://github.com/CoreXI7/fishbowl/commit/ee8f499) | `test_upstream_slow.py`: `test_a_closing_answer_ends_too` |
| A2-1 | Medium | The owner's own sign-in hid an attacker's wrong code | Fixed | [`9f23c85`](https://github.com/CoreXI7/fishbowl/commit/9f23c85) | `test_auth.py`: `test_the_owners_right_code_forgives_nobody_elses_wrong_one` |
| A2-2 | Medium | A trusted browser became "new" when its address changed, so a guesser could lock it out | Fixed | [`8d37152`](https://github.com/CoreXI7/fishbowl/commit/8d37152) | `test_auth.py`: `test_a_plain_http_trusted_device_survives_a_new_address_but_its_tries_there_are_no_typos` |
| F2-1 | Medium | Plain-HTTP binding covered a whole IPv6 /64 | Fixed (exact address) | [`8d37152`](https://github.com/CoreXI7/fishbowl/commit/8d37152) | `test_auth.py`: `test_a_plain_http_session_counts_only_from_the_address_it_was_made_on` |
| F2-2 | Medium | The half-finished sign-in cookie wasn't tied to an address | Fixed | [`8d37152`](https://github.com/CoreXI7/fishbowl/commit/8d37152) | `test_auth.py`: `test_a_half_finished_sign_in_counts_only_where_it_started` |
| C2-2 | Medium | Plain HTTP: an app on the same host could end the owner's session | Fixed in part; the rest documented | [`8d37152`](https://github.com/CoreXI7/fishbowl/commit/8d37152) | `test_auth.py`: `test_signing_out_from_a_new_address_ends_the_session` |
| I2-3 | Medium | The address binding meant nothing when every visitor came from Docker's gateway | Fixed (plain-HTTP sign-in refused there, Setup warns) | [`8d37152`](https://github.com/CoreXI7/fishbowl/commit/8d37152), [`717e83d`](https://github.com/CoreXI7/fishbowl/commit/717e83d) | `test_auth.py`: `test_every_visitor_looking_like_the_gateway_is_flagged` |
| E2-1 | Medium | Links in container-written folders were still followed | Fixed | [`246ace4`](https://github.com/CoreXI7/fishbowl/commit/246ace4) | `test_collector_files.py`: `test_a_link_in_a_root_owned_container_folder_is_not_followed` |
| E2-2 | Medium | A carriage return could hide a cron line from the watch | Fixed | [`ca47c72`](https://github.com/CoreXI7/fishbowl/commit/ca47c72) | `test_collector_files.py`: `test_a_carriage_return_cant_hide_a_cron_line` |
| E2-3 | Medium | redact() was quadratic: one backup-log line froze the collector | Fixed | [`7043360`](https://github.com/CoreXI7/fishbowl/commit/7043360), [`cf14d5e`](https://github.com/CoreXI7/fishbowl/commit/cf14d5e) | `test_collector_files.py`: `test_a_long_line_is_redacted_quickly` |
| H2-1 | Medium | An empty forwarding header sent first lifted the sign-in refusal | Fixed | [`89f1fef`](https://github.com/CoreXI7/fishbowl/commit/89f1fef) | `test_auth.py`: `test_an_empty_forwarding_header_first_hides_nothing` |
| B2-1 | Medium | A proxy-named container on another network became the trusted proxy | Fixed | [`779c0b6`](https://github.com/CoreXI7/fishbowl/commit/779c0b6) | `test_listen.py`: `test_a_name_isnt_trusted_on_more_than_one_network` |
| J2-1 | Medium | Fixes didn't reach existing installs, and collector updates were never prompted | Fixed | [`f1103be`](https://github.com/CoreXI7/fishbowl/commit/f1103be) | `test_collector_build.py`: `test_an_old_collector_is_a_setup_item` |
| G2-1 | Medium | LISTEN (lock the dashboard to the proxy) was off by default | Setup warning with AUTH: proxy; documented | [`fc4e3fa`](https://github.com/CoreXI7/fishbowl/commit/fc4e3fa) | `test_listen.py`: `test_sso_mode_without_listen_is_a_warning` |
| G2-2 | Medium | Guessing temporarily locks the owner's new browsers (same as G1-1) | Accepted and documented | [`0132a29`](https://github.com/CoreXI7/fishbowl/commit/0132a29) | `test_auth.py`: `test_a_new_browser_kept_waiting_is_told_why` |
| I2-1 | Medium | One icon lookup had no overall time limit | Fixed | [`0d2bff1`](https://github.com/CoreXI7/fishbowl/commit/0d2bff1) | `test_upstream_slow.py`: `test_an_icon_lookup_has_an_overall_budget` |
| I2-2 | Medium | An HTTPS session reaches other HTTPS apps on the same host name | Documented (give the dashboard its own host name) | – | (documentation) |
| D2-1 | Medium | Failures weren't shared, so waiting requests repeated a slow failing fetch | Fixed | [`2c37a14`](https://github.com/CoreXI7/fishbowl/commit/2c37a14) | `test_upstream_slow.py`: `test_waiters_share_a_failure_instead_of_queueing_to_repeat_it` |
| D2-2 | Medium | A signed-in user could read back what the dashboard fetched (URL parts, banners) | Fixed; DNS rebinding of icon lookups documented | [`dac3e33`](https://github.com/CoreXI7/fishbowl/commit/dac3e33) | `test_settings_page.py`: `test_integration_urls_have_no_query_fragment_or_user` |
| D2-3 | Medium | /api/icon kept every name it was asked about (memory) | Fixed | [`b7ef03e`](https://github.com/CoreXI7/fishbowl/commit/b7ef03e) | `test_icons.py`: `test_unknown_names_leave_nothing_behind` |
| C2-1 | Medium | /api/icon kept a lock for every host name ever asked for | Fixed | [`b7ef03e`](https://github.com/CoreXI7/fishbowl/commit/b7ef03e) | `test_icons.py`: `test_unknown_names_leave_nothing_behind` |
| E3-1 | Medium | lsattr could be made to open a device node between its check and its open | Fixed (flags read through the safe walk) | [`4c781c2`](https://github.com/CoreXI7/fishbowl/commit/4c781c2) | `test_collector_files.py`: `test_a_device_or_fifo_is_never_opened` |
| E3-2 | Medium | A log tail read to the end of a still-growing file | Fixed | [`da6057c`](https://github.com/CoreXI7/fishbowl/commit/da6057c) | `test_collector_files.py`: `test_a_tail_reads_a_bounded_amount` |
| E3-3 | Medium | A log of tiny lines took ~370 MB | Fixed | [`da6057c`](https://github.com/CoreXI7/fishbowl/commit/da6057c), [`2c1477c`](https://github.com/CoreXI7/fishbowl/commit/2c1477c) | `test_collector_files.py`: `test_a_tail_reads_a_bounded_amount` |
| H3-1 | Medium | A cached failure kept a hostile service's parsed answer in memory | Fixed | [`6c4a6f8`](https://github.com/CoreXI7/fishbowl/commit/6c4a6f8) | `test_upstream_slow.py`: `test_a_shared_failure_holds_nothing_of_the_failed_fetch` |
| B3-1 | Medium | Hostile answer headers were kept for 30 s per fetch | Fixed | [`4f879c6`](https://github.com/CoreXI7/fishbowl/commit/4f879c6) | `test_upstream_size.py`: `test_huge_headers_are_refused_and_nothing_big_is_kept` |
| D3-1 | Medium | Every hostile answer stayed in memory until its deadline | Fixed | [`4f879c6`](https://github.com/CoreXI7/fishbowl/commit/4f879c6) | `test_upstream_size.py`: `test_huge_headers_are_refused_and_nothing_big_is_kept` |
| D3-2 | Medium | A shared failure's traceback grew with every request | Fixed | [`ad2ac12`](https://github.com/CoreXI7/fishbowl/commit/ad2ac12), [`6c4a6f8`](https://github.com/CoreXI7/fishbowl/commit/6c4a6f8) | `test_upstream_slow.py`: `test_a_shared_failure_doesnt_grow` |
| D3-3 | Medium | A burst of integration Tests filled the connection table | Fixed | [`c6d3ef4`](https://github.com/CoreXI7/fishbowl/commit/c6d3ef4) | `test_settings_page.py`: `test_one_integration_test_at_a_time` |
| I3-1 | Medium | Chunk size "-1" got past every byte cap (CVE-2026-103995) | Fixed | [`99d41f5`](https://github.com/CoreXI7/fishbowl/commit/99d41f5) | `test_upstream_size.py`: `test_a_negative_chunk_size_is_refused` |
| J3-1 | Medium | "git pull, then docker compose up -d" left the old code running | Fixed (docs + Setup check) | [`80a217c`](https://github.com/CoreXI7/fishbowl/commit/80a217c) | `test_settings_page.py`: `test_code_changed_on_disk_asks_for_a_restart` |
| C3-1 | Medium | A sibling subdomain can flood out the trusted-browser cookie | Documented (give the dashboard a host name whose siblings you trust) | [`9f401df`](https://github.com/CoreXI7/fishbowl/commit/9f401df) | (documentation) |
| A4-1 | Medium | A password step could be abandoned ("Start over") without the owner being told | Fixed | [`c4ea3c1`](https://github.com/CoreXI7/fishbowl/commit/c4ea3c1), [`47d22c4`](https://github.com/CoreXI7/fishbowl/commit/47d22c4) | `test_auth.py`: `test_a_password_step_ended_without_a_code_is_told` |
| F4-1 | Medium | Same as A4-1 | Fixed | [`c4ea3c1`](https://github.com/CoreXI7/fishbowl/commit/c4ea3c1) | `test_auth.py`: `test_a_password_step_ended_without_a_code_is_told` |
| A4-2 | Medium | Rootless Podman 4 made every visitor look like the container itself | Fixed | [`48d8b6b`](https://github.com/CoreXI7/fishbowl/commit/48d8b6b) | `test_auth.py`: `test_every_visitor_looking_like_the_container_itself_is_flagged` |
| B4-1 | Medium | An IPv6-only network wasn't counted, so a proxy name stayed trusted | Fixed | [`aa81de9`](https://github.com/CoreXI7/fishbowl/commit/aa81de9) | `test_listen.py`: `test_an_ipv6_only_network_counts` |
| D4-1 | Medium | One answer in tiny chunks used up the memory | Fixed | [`a20ce66`](https://github.com/CoreXI7/fishbowl/commit/a20ce66) | `test_upstream_slow.py`: `test_an_answer_in_tiny_chunks_takes_about_its_size` |
| D4-2 | Medium | One wide character in a large JSON answer used up the memory | Fixed | [`a20ce66`](https://github.com/CoreXI7/fishbowl/commit/a20ce66) | `test_upstream_slow.py`: `test_wide_characters_count_against_the_cap` |
| D4-3 | Medium | Connecting wasn't covered by the deadline (a name with many addresses) | Fixed | [`a20ce66`](https://github.com/CoreXI7/fishbowl/commit/a20ce66) | `test_upstream_slow.py`: `test_connecting_has_an_overall_deadline` |
| E4-1 | Medium | A folder full of names made the collector's backup check use up its memory | Fixed | [`48aca56`](https://github.com/CoreXI7/fishbowl/commit/48aca56) | `test_collector_files.py`: `test_a_backup_glob_looks_at_a_bounded_number_of_names` |
| E4-2 | Medium | A hostile log plus a flooded SSH journal got the collector killed every round | Fixed | [`2c1477c`](https://github.com/CoreXI7/fishbowl/commit/2c1477c) | `test_collector_files.py`: `test_a_tail_never_holds_more_than_it_keeps` |
| E4-3 | Medium | The installer's self-test could be tricked into writing through a link as root | Fixed | [`13fee7c`](https://github.com/CoreXI7/fishbowl/commit/13fee7c) | `test_collector_page.py`: `test_the_write_probe_never_writes_through_a_planted_link` |

Low and info findings aren't listed here; many were fixed alongside these, and the rest are in [THREAT-MODEL.md](THREAT-MODEL.md#still-open) where they matter.

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
- **Proxies:** the sign-in rounds played the proxy themselves; the 1.0 audit ran real Nginx Proxy Manager, nginx,
  Caddy, HAProxy and Traefik in front. Cloudflare Tunnel and Tailscale weren't run.
- **Choices that differ from the standards** (longer "keep me signed in" sessions for wall screens, optional
  two-step, CSRF without a token, and others) are deliberate and listed in
  [THREAT-MODEL.md](THREAT-MODEL.md#the-sign-in-authlogin) under "Deliberate deviations".
- **Known open items** are listed in [THREAT-MODEL.md](THREAT-MODEL.md#still-open). The main one for the sign-in:
  someone who can reach port 8000 directly from many addresses can still keep it busy. Keep it behind your proxy
  (`TRUSTED_PROXY` and `LISTEN`).
