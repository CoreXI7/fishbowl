"""Fishbowl's own sign-in (app/auth.py + the gate in app.py): the door to everything else, so every rule has a test.
Sources for the numbers: NIST SP 800-63B-4, OWASP Password Storage + Session Management cheat sheets, RFC 6238/4226."""
import http.client
import json
import os
import re
import subprocess
import sys
import threading
import time
import unittest
import urllib.parse

from helpers import ROOT, load_app, serve

PW = "plinth-marble-otter-cascade"


def call(url, method="GET", body=None, cookie=None, origin=True, headers=None, raw_path=None):
    """One request, no redirects followed. Returns (status, headers dict (lower-case, Set-Cookie as a list), body)."""
    u = urllib.parse.urlsplit(url)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=30)
    h = {"Content-Type": "application/json"} if body is not None else {}
    if origin and method == "POST":
        h["Origin"] = origin if isinstance(origin, str) else f"http://{u.hostname}:{u.port}"
    if cookie:
        h["Cookie"] = cookie
    h.update(headers or {})
    conn.request(method, raw_path or (u.path + (f"?{u.query}" if u.query else "")),
                 body=json.dumps(body).encode() if body is not None else None, headers=h)
    r = conn.getresponse()
    data = r.read()
    hdrs = {}
    for k, v in r.getheaders():
        k = k.lower()
        if k == "set-cookie":
            hdrs.setdefault(k, []).append(v)
        else:
            hdrs[k] = v
    conn.close()
    try:
        data = json.loads(data)
    except ValueError:
        pass
    return r.status, hdrs, data


def cookie_of(hdrs, name="fishbowl"):
    for c in hdrs.get("set-cookie", []):
        k, _, rest = c.partition("=")
        if k == name:
            return f"{k}={rest.split(';')[0]}"
    return None


class Base(unittest.TestCase):
    env = {"AUTH": "login"}

    def setUp(self):
        self.app = load_app(**self.env)
        self.auth = self.app.auth
        self.srv, self.url = serve(self.app.Handler)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def setup_code(self):
        return self.auth.new_setup_code()

    def create(self, user="admin", pw=PW, keep=False):
        st, h, d = call(self.url + "/api/setup-account", "POST",
                        {"code": self.setup_code(), "user": user, "password": pw, "keep": keep})
        self.assertEqual(st, 200, d)
        return cookie_of(h)

    def login(self, user="admin", pw=PW, cookie=None, **kw):
        return call(self.url + "/api/login", "POST", {"user": user, "password": pw, **kw}, cookie=cookie)


class Primitives(unittest.TestCase):
    def setUp(self):
        self.auth = load_app(AUTH="login").auth

    def test_rfc_vectors(self):
        a = self.auth
        hotp = ["755224", "287082", "359152", "969429", "338314", "254676", "287922", "162583", "399871", "520489"]
        self.assertEqual([a.totp_code(b"12345678901234567890", i) for i in range(10)], hotp, "RFC 4226 appendix D")
        for t, code in [(59, "94287082"), (1111111109, "07081804"), (1111111111, "14050471"), (1234567890, "89005924"),
                        (2000000000, "69279037"), (20000000000, "65353130")]:
            self.assertEqual(a.totp_code(b"12345678901234567890", t // 30, 8), code, "RFC 6238 appendix B")

    def test_totp_window_and_replay(self):
        a, s, now = self.auth, self.auth.new_totp_secret(), 1_800_000_015
        step = now // 30
        for d in (-1, 0, 1):
            self.assertEqual(a.totp_match(s, a.totp_code(s, step + d), 0, now), step + d)
        for d in (-2, 2):
            self.assertIsNone(a.totp_match(s, a.totp_code(s, step + d), 0, now))
        self.assertIsNone(a.totp_match(s, a.totp_code(s, step), step, now), "a used code doesn't work again")
        self.assertIsNone(a.totp_match(s, a.totp_code(s, step - 1), step, now), "nor an older one")
        for junk in ("", "12345", "1234567", "abcdef", None, "12 34 5x"):
            self.assertIsNone(a.totp_match(s, junk, 0, now))
        self.assertEqual(len(a._b32(s)), 20, "160-bit secret")

    def test_hashing(self):
        a = self.auth
        h = a.hash_password(PW)
        self.assertRegex(h, r"^scrypt\$15\$8\$3\$")
        self.assertEqual(a.verify_password(PW, h), (True, False))
        self.assertEqual(a.verify_password(PW + "x", h), (False, False))
        self.assertEqual(a.verify_password("ＰＷ" + PW, a.hash_password("PW" + PW)), (True, False), "NFKC: the same password typed differently")
        old = a.hash_password.__wrapped__(PW) if hasattr(a.hash_password, "__wrapped__") else None
        self.assertIsNone(old)
        weaker = "scrypt$14$8$1$" + h.split("$", 5)[4] + "$" + h.split("$", 5)[5]
        self.assertEqual(a.verify_password(PW, weaker)[0], False, "params are part of the hash")
        for junk in ("", "scrypt$30$8$3$AAAA$BBBB", "md5$x", None, "scrypt$15$99$3$" + "A" * 24 + "$" + "B" * 44):
            self.assertEqual(a.verify_password(PW, junk), (False, False), junk)
        self.assertEqual(a.verify_password("x" * 5000, h), (False, False), "too long: refused, still costs a hash")

    def test_rehash_on_old_parameters(self):
        a = self.auth
        salt = b"s" * 16
        dk = __import__("hashlib").scrypt(PW.encode(), salt=salt, n=2 ** 14, r=8, p=1, maxmem=64 << 20, dklen=32)
        import base64
        old = f"scrypt$14$8$1${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"
        self.assertEqual(a.verify_password(PW, old), (True, True))

    def test_password_rules(self):
        a = self.auth
        good = ["Tr0ub4dor&3-xylophone-q", "plinth marble otter cascade", "ünïcödé-pässwörd-łong", "x" * 14 + "y" + "zqw"]
        for pw in good:
            self.assertIsNone(a.password_problem(pw, "admin"), pw)
        bad = ["short", "fourteen-chars", "passwordpassword", "1q2w3e4r5t6y7u8i", "aaaaaaaaaaaaaaaaaaaa",
               "abcabcabcabcabcabc", "abcdefghijklmnopq", "0123456789012345678", "adminadminadmin1234", "fishbowl-fishbowl-1",
               "x" * 1025, 12345678901234567, None, "correct horse battery staple", "with\x00control-character"]
        for pw in bad:
            self.assertIsNotNone(a.password_problem(pw, "admin"), repr(pw)[:40])
        self.assertIsNotNone(a.name_problem(""))
        self.assertIsNotNone(a.name_problem("x" * 65))
        self.assertIsNotNone(a.name_problem("a\nb"))
        self.assertIsNone(a.name_problem("Vic"))

    def test_recovery_codes(self):
        codes, hashes = self.auth.new_recovery_codes()
        self.assertEqual(len(codes), 10)
        self.assertEqual(len(set(codes)), 10)
        for c, h in zip(codes, hashes):
            self.assertRegex(c, r"^[A-Z2-9]{4}(-[A-Z2-9]{4}){5}$")   # 24 characters from 32: 120 bits
            self.assertEqual(self.auth.rc_hash(c.lower().replace("-", " ")), h)
            self.assertNotIn(c, json.dumps(hashes))

    def test_sessions(self):
        a = self.auth
        s = a.Sessions()
        tok, max_age = s.create(True, "1.2.3.4", "UA", 1)
        self.assertEqual(max_age, 30 * 86400)
        self.assertGreaterEqual(len(tok), 43)
        with open(a.SESSIONS_FILE) as f:
            raw = f.read()
        self.assertNotIn(tok, raw, "only the token's hash is stored")
        self.assertEqual(oct(os.stat(a.SESSIONS_FILE).st_mode & 0o777), "0o600")
        self.assertIsNotNone(s.check(tok, 1))
        self.assertIsNone(s.check(tok, 2), "a session from another epoch (before a password change) doesn't count")
        tok = s.create(True, "1.2.3.4", "UA", 1)[0]
        for junk in ("", "x", tok + "x", tok[:-1], "../" * 20, None, "A" * 43):
            self.assertIsNone(s.check(junk, 1), junk)
        raw = open(a.SESSIONS_FILE).read()
        d = json.loads(raw)
        k = next(iter(d))
        d[k]["seen"] -= 8 * 86400   # idle longer than 7 days
        with open(a.SESSIONS_FILE, "w") as f:
            json.dump(d, f)
        self.assertIsNone(s.check(tok, 1), "idle too long")
        tok, _ = s.create(False, "ip", "ua", 1)
        d = json.loads(open(a.SESSIONS_FILE).read())
        d[a.Sessions._key(tok)]["until"] = int(time.time()) - 1
        open(a.SESSIONS_FILE, "w").write(json.dumps(d))
        self.assertIsNone(s.check(tok, 1), "past its absolute lifetime")
        t1, _ = s.create(False, "ip", "ua", 1)
        t2, _ = s.create(False, "ip", "ua", 1)
        s.end_all(but_token=t2)
        self.assertIsNone(s.check(t1, 1))
        self.assertIsNotNone(s.check(t2, 1))
        sid = s.listing(t2, 1)[0]["id"]
        self.assertTrue(s.end_id(sid))
        self.assertIsNone(s.check(t2, 1))
        open(a.SESSIONS_FILE, "w").write("{broken")
        self.assertEqual(s.listing(), [], "a damaged file signs everyone out, never in")
        for _ in range(60):
            s.create(False, "ip", "ua", 1)
        self.assertLessEqual(len(s.listing()), s.MAX)

    def test_throttle(self):
        t = self.auth.Throttle()
        for i in range(5):
            self.assertEqual(t.ip_wait("1.1.1.1", 1000.0), 0, i)
            t.ip_failed("1.1.1.1", 1000.0)
        self.assertGreater(t.ip_wait("1.1.1.1", 1000.0), 0)
        self.assertEqual(t.ip_wait("2.2.2.2", 1000.0), 0, "per address")
        self.assertEqual(t.wait_for(105, 0, 0), 901, "capped at 15 minutes")
        self.assertEqual(t.ip_wait("1.1.1.1", 1000.0 + 901), 0, "old failures age out of the 15-minute window")
        for _ in range(20000):
            t.ip_failed(str(_), 0)
        self.assertLessEqual(len(t.by_ip), 10001, "a flood of addresses can't grow it without bound")


class Gate(Base):
    def test_everything_is_locked_until_signed_in(self):
        for p in ("/", "/settings"):
            st, h, _ = call(self.url + p)
            self.assertEqual((st, h.get("location")), (303, "/login?next=" + p))
        for p in ("/api/settings", "/api/system", "/api/services", "/api/account", "/api/security/detail", "/uploads/x.png",
                  "/api/icon?host=x", "/api/plexart?img=x", "/nope"):
            self.assertIn(call(self.url + p)[0], (401,), p)
        for p in ("/api/settings", "/api/upload", "/api/cron/approve", "/api/requests/1/approve", "/api/account/password",
                  "/api/security/known", "/api/settings/test"):
            self.assertIn(call(self.url + p, "POST", {})[0], (401, 403), p)   # (an upload with a JSON type: 403 first)
        for p in ("/login", "/api/auth/state", "/style.css", "/js/login.js", "/js/settings.js", "/static/favicon.svg", "/healthz",
                  "/favicon.ico"):   # (the CSS/JS are the open-source app, on GitHub anyway)
            self.assertEqual(call(self.url + p)[0], 200, p)

    def test_path_tricks(self):
        for raw in ("http://127.0.0.1/api/settings", "/\\api/settings", "\\api/settings", "*", "api/settings"):
            self.assertIn(call(self.url, raw_path=raw)[0], (400, 404), raw)
        for raw in ("//api/settings", "///settings", "/api/settings;x", "/api/settings/", "/API/settings", "/api/./settings",
                    "/api/../api/settings", "/%61pi/settings", "/api/settings%00", "/api/settings?x=/login", "/login/../api/settings",
                    "/static/../api/settings", "/js/login.js/../settings.js"):
            st = call(self.url, raw_path=raw)[0]
            self.assertIn(st, (303, 400, 401, 404), raw)
            self.assertNotEqual(st, 200, raw)
        for method in ("HEAD", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE"):
            self.assertNotEqual(call(self.url + "/api/settings", method)[0], 200, method)
        st = call(self.url + "/api/settings", headers={"X-HTTP-Method-Override": "GET", "X-Original-URL": "/login",
                                                         "X-Rewrite-URL": "/login", "X-Forwarded-For": "127.0.0.1"})[0]
        self.assertEqual(st, 401)

    def test_no_open_redirect(self):
        self.create()
        st, h, d = self.login()
        c = cookie_of(h)
        for nxt, want in (("//evil.example", "/"), ("https://evil.example", "/"), ("/settings", "/settings"),
                          ("/\\evil.example", "/"), ("javascript:alert(1)", "/"), ("/api/settings", "/")):
            st, h, _ = call(self.url + "/login?next=" + urllib.parse.quote(nxt), cookie=c)
            self.assertEqual((st, h.get("location")), (303, want), nxt)

    def test_proxy_mode_and_demo_have_no_gate(self):
        for env in ({"AUTH": "proxy", "TRUSTED_PROXY": "127.0.0.1"}, {"DEMO": "1", "AUTH": "login"}):
            app = load_app(**env)
            srv, url = serve(app.Handler)
            try:
                self.assertEqual(call(url + "/api/settings")[0], 200, env)
                self.assertEqual(call(url + "/login")[0], 303, env)
            finally:
                srv.shutdown()
                srv.server_close()

    def test_unknown_auth_value_means_login(self):
        self.assertEqual(load_app(AUTH="nope").AUTH, "login")
        self.assertEqual(load_app(AUTH="PROXY", TRUSTED_PROXY="127.0.0.1").AUTH, "proxy")
        self.assertEqual(load_app(AUTH="proxy").AUTH, "login", "no TRUSTED_PROXY: nothing stands in front, so never open")
        self.assertEqual(load_app(AUTH="").AUTH, "login")


class Setup(Base):
    def test_first_run(self):
        st, _, d = call(self.url + "/api/auth/state")
        self.assertTrue(d["setup"])
        self.assertEqual(self.login()[0], 409, "no account yet")
        code = self.setup_code()
        st, _, d = call(self.url + "/api/setup-account", "POST", {"code": "WRONG-CODE", "user": "a", "password": PW})
        self.assertEqual((st, d.get("field")), (403, "code"))
        st, _, d = call(self.url + "/api/setup-account", "POST", {"code": code, "user": "admin", "password": "short"})
        self.assertEqual((st, d.get("field")), (400, "password"))
        st, _, d = call(self.url + "/api/setup-account", "POST", {"code": code, "user": "", "password": PW})
        self.assertEqual((st, d.get("field")), (400, "user"))
        st, h, d = call(self.url + "/api/setup-account", "POST", {"code": code.lower().replace("-", ""), "user": "admin", "password": PW})
        self.assertEqual(st, 200, d)
        self.assertFalse(os.path.exists(self.auth.SETUP_FILE), "the code is gone once used")
        self.assertEqual(call(self.url + "/api/settings", cookie=cookie_of(h))[0], 200)
        st, _, d = call(self.url + "/api/setup-account", "POST", {"code": code, "user": "x", "password": PW + "z"})
        self.assertEqual(st, 409, "only one account, ever")
        acct = json.load(open(self.auth.AUTH_FILE))
        self.assertNotIn(PW, json.dumps(acct))
        self.assertEqual(oct(os.stat(self.auth.AUTH_FILE).st_mode & 0o777), "0o600")

    def test_setup_code_guessing_is_throttled(self):
        self.setup_code()
        codes = [call(self.url + "/api/setup-account", "POST", {"code": f"AAAAA-AAAAA-AAAAA-AAAA{i}", "user": "a", "password": PW})[0]
                 for i in range(7)]
        self.assertEqual(codes[:5], [403] * 5)
        self.assertEqual(codes[5], 429)

    def test_racing_setups_make_one_account(self):
        code = self.setup_code()
        results = []

        def go(i):
            results.append(call(self.url + "/api/setup-account", "POST", {"code": code, "user": f"u{i}", "password": PW + str(i)})[0])
        ts = [threading.Thread(target=go, args=(i,)) for i in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(results).count(200), 1, results)

    def test_no_setup_without_the_code_file(self):
        st, _, _ = call(self.url + "/api/setup-account", "POST", {"code": "", "user": "a", "password": PW})
        self.assertEqual(st, 400, "an empty code isn't a guess: not counted")
        st, _, _ = call(self.url + "/api/setup-account", "POST", {"code": "AAAAA-AAAAA-AAAAA-AAAAA-AAAAA", "user": "a", "password": PW})
        self.assertEqual(st, 403)


class SignIn(Base):
    def test_login_logout(self):
        self.create()
        st, h, d = self.login()
        self.assertEqual(st, 200)
        c = h["set-cookie"][0]
        for part in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(part, c)
        self.assertNotIn("Max-Age", c, "not kept: a browser-session cookie")
        self.assertNotIn("Secure", c, "plain http here")
        self.assertEqual(h.get("cache-control"), "no-store")
        cook = cookie_of(h)
        self.assertEqual(call(self.url + "/api/settings", cookie=cook)[0], 200)
        st, h2, _ = call(self.url + "/api/logout", "POST", {}, cookie=cook)
        self.assertEqual(h2.get("clear-site-data"), '"cache"', "not cookies: that would sign you out of your SSO too")
        self.assertEqual(call(self.url + "/api/settings", cookie=cook)[0], 401, "ended on the server, not just in the browser")

    def test_wrong_answers_are_the_same(self):
        self.create()
        a = self.login("admin", "wrong-password-123456")
        b = self.login("nobody", PW)
        self.assertEqual((a[0], a[2]), (b[0], b[2]))
        self.assertEqual(a[2], {"error": "Wrong name or password.", "field": "password"})

    def test_unknown_name_costs_a_hash(self):
        self.create()
        times = {}
        for who, pw in (("admin", "wrong-password-123456"), ("nobody-at-all", "wrong-password-123456")):
            t0 = time.perf_counter()
            for _ in range(3):
                self.app.THROTTLE.ip_ok("127.0.0.1")
                self.app.ACCOUNT.update(lambda d: d.update(failures=0))
                self.login(who, pw)
            times[who] = time.perf_counter() - t0
        ratio = times["admin"] / times["nobody-at-all"]
        self.assertTrue(0.6 < ratio < 1.6, times)

    def test_fixation_and_new_token(self):
        self.create()
        planted = "fishbowl=" + "A" * 43
        st, h, _ = call(self.url + "/api/login", "POST", {"user": "admin", "password": PW}, cookie=planted)
        self.assertNotEqual(cookie_of(h), planted)
        self.assertEqual(call(self.url + "/api/settings", cookie=planted)[0], 401)

    def test_login_needs_the_dashboards_origin(self):
        self.create()
        for origin in (False, "https://evil.example", "null"):
            self.assertEqual(call(self.url + "/api/login", "POST", {"user": "admin", "password": PW}, origin=origin)[0], 403, origin)

    def test_throttling_and_lock(self):
        self.create()
        sts = [self.login("admin", f"wrong-password-{i:04}x")[0] for i in range(6)]
        self.assertEqual(sts[:5], [401] * 5)
        self.assertEqual(sts[5], 429)
        st, h, d = self.login()
        self.assertEqual(st, 429, "even the right password waits (no oracle)")
        self.assertIn("retry-after", h)
        self.app.THROTTLE.ip_ok("127.0.0.1")
        self.app.ACCOUNT.update(lambda d: d.update(failures=99, last_fail=0))
        self.assertEqual(self.login("admin", "wrong-password-xxxxx")[0], 401)
        self.app.THROTTLE.ip_ok("127.0.0.1")
        self.app.ACCOUNT.update(lambda d: d.update(last_fail=0))
        self.assertEqual(self.login()[0], 423, "100 in a row: locked, even with the right password")
        out = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "app", "auth.py"), "unlock"], capture_output=True, text=True,
                             env={**os.environ, "DATA_DIR": self.auth.DATA_DIR})
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.app.THROTTLE.ip_ok("127.0.0.1")
        self.assertEqual(self.login()[0], 200, "unlock on the host works without a restart")

    def test_https_cookie(self):
        app = load_app(AUTH="login", ORIGIN="https://dash.example.com")
        srv, url = serve(app.Handler)
        try:
            code = app.auth.new_setup_code()
            st, h, d = call(url + "/api/setup-account", "POST", {"code": code, "user": "admin", "password": PW, "keep": True},
                            origin="https://dash.example.com")
            self.assertEqual(st, 200, d)
            c = h["set-cookie"][0]
            self.assertTrue(c.startswith("__Host-fishbowl="), c)
            for part in ("Secure", "HttpOnly", "Path=/", "SameSite=Strict", "Max-Age=2592000"):
                self.assertIn(part, c)
            self.assertNotIn("Domain", c)
            tok = cookie_of(h, "__Host-fishbowl").split("=", 1)[1]
            self.assertEqual(call(url + "/api/settings", cookie=f"fishbowl={tok}")[0], 401, "over https only the __Host- cookie counts")
            self.assertEqual(call(url + "/api/settings", cookie=f"fishbowl=zzz; __Host-fishbowl={tok}")[0], 200)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_pages_are_private(self):
        c = self.create()
        st, h, _ = call(self.url + "/", cookie=c)
        self.assertEqual(st, 200)
        self.assertEqual(h.get("cache-control"), "no-store", "not even the back/forward cache keeps it after signing out")
        st, h, _ = call(self.url + "/login")
        self.assertIn("frame-ancestors 'none'", h.get("content-security-policy", ""))
        self.assertEqual(h.get("x-frame-options"), "DENY")

    def test_damaged_account_file_fails_closed(self):
        self.create()
        with open(self.auth.AUTH_FILE, "w") as f:
            f.write('{"v": 1, "user": "admin", "hash": "nope"}')
        st, _, d = self.login()
        self.assertEqual(st, 503)
        self.assertIn("reset", d["error"])
        self.assertTrue(call(self.url + "/api/auth/state")[2]["broken"])


class TwoStep(Base):
    def turn_on(self, cookie):
        st, _, d = call(self.url + "/api/account/totp/start", "POST", {"password": "wrong-password-123"}, cookie=cookie)
        self.assertEqual(st, 403, "needs the current password")
        st, _, d = call(self.url + "/api/account/totp/start", "POST", {"password": PW}, cookie=cookie)
        self.assertEqual(st, 200, d)
        self.assertIn("<svg", d["qr"])
        self.assertTrue(d["uri"].startswith("otpauth://totp/Fishbowl%3Aadmin?secret="))
        secret = d["secret"]
        code = self.auth.totp_code(secret, int(time.time() // 30))
        st, h, d = call(self.url + "/api/account/totp/confirm", "POST", {"code": code}, cookie=cookie)
        self.assertEqual(st, 200, d)
        self.assertEqual(call(self.url + "/api/settings", cookie=cookie)[0], 401, "every session ends, this one too...")
        self.cookie = cookie_of(h)
        self.assertEqual(call(self.url + "/api/settings", cookie=self.cookie)[0], 200, "...and this browser gets a new one")
        self.assertTrue(cookie_of(h, "fishbowl-device"), "and a new trusted-device token")
        return secret, d["recovery"]

    def test_full_flow(self):
        c = self.create()
        other = cookie_of(self.login()[1])
        secret, recovery = self.turn_on(c)
        self.assertEqual(len(recovery), 10)
        self.assertEqual(call(self.url + "/api/settings", cookie=other)[0], 401, "turning it on signs other browsers out")
        acct = json.load(open(self.auth.AUTH_FILE))
        self.assertNotIn(recovery[0], json.dumps(acct))
        _, _, view = call(self.url + "/api/account", cookie=self.cookie)
        self.assertNotIn("secret", json.dumps(view))
        self.assertNotIn("hash", view)
        # sign in: password, then a code
        st, h, d = self.login()
        self.assertEqual((st, d), (200, {"code_due": True}))
        self.assertIsNone(cookie_of(h), "no session before the code")
        pre = cookie_of(h, "fishbowl-pre")
        self.assertIn("SameSite=Strict", h["set-cookie"][0])
        self.assertTrue(cookie_of(h, "fishbowl-pre"))
        self.assertEqual(call(self.url + "/api/settings", cookie=pre)[0], 401)
        step = int(time.time() // 30) + 1   # the confirm used the current step
        code = self.auth.totp_code(secret, step)
        st, h, d = call(self.url + "/api/login/code", "POST", {"code": code}, cookie=pre)
        if st != 200:   # the clock may not have moved: wait for the next step
            time.sleep(31 - time.time() % 30)
            st, h, d = self.login()
            pre = cookie_of(h, "fishbowl-pre")
            code = self.auth.totp_code(secret, int(time.time() // 30))
            st, h, d = call(self.url + "/api/login/code", "POST", {"code": code}, cookie=pre)
        self.assertEqual(st, 200, d)
        sess = cookie_of(h)
        self.assertEqual(call(self.url + "/api/settings", cookie=sess)[0], 200)
        # the same code again: refused (replay), and the pre-sign-in is used up
        st, h, _ = self.login()
        pre2 = cookie_of(h, "fishbowl-pre")
        self.assertEqual(call(self.url + "/api/login/code", "POST", {"code": code}, cookie=pre2)[0], 401)
        self.assertEqual(call(self.url + "/api/login/code", "POST", {"code": code}, cookie=pre)[0], 401, "pre-sign-in used")
        # a recovery code works once
        st, h, d = call(self.url + "/api/login/code", "POST", {"code": recovery[0].lower()}, cookie=pre2)
        self.assertEqual(st, 200, d)
        st, h, _ = self.login()
        self.assertEqual(call(self.url + "/api/login/code", "POST", {"code": recovery[0]}, cookie=cookie_of(h, "fishbowl-pre"))[0], 401)
        self.assertEqual(json.load(open(self.auth.AUTH_FILE))["recovery"].__len__(), 9)

    def test_five_wrong_codes_end_the_attempt(self):
        c = self.create()
        secret, _ = self.turn_on(c)
        st, h, _ = self.login()
        pre = cookie_of(h, "fishbowl-pre")
        out = []
        for _ in range(5):   # (the general limits reset each time: this checks the 5-tries rule on its own)
            self.app.THROTTLE.ip_ok("127.0.0.1")
            self.app.ACCOUNT.update(lambda d: d.update(failures=0, last_fail=0))
            out.append(call(self.url + "/api/login/code", "POST", {"code": "000000"}, cookie=pre))
        self.assertTrue(out[4][2].get("restart"), out[4])
        good = self.auth.totp_code(secret, int(time.time() // 30) + 1)
        st, _, d = call(self.url + "/api/login/code", "POST", {"code": good}, cookie=pre)
        self.assertEqual((st, d.get("restart")), (401, True), "that attempt is over, even with a right code")
        self.app.THROTTLE.ip_ok("127.0.0.1")
        self.app.ACCOUNT.update(lambda d: d.update(failures=0, last_fail=0))
        st, _, d = call(self.url + "/api/login/code", "POST", {"code": "000000"}, cookie=pre)
        self.assertNotEqual(st, 200, "a used-up attempt never becomes a session")

    def test_turning_it_off_needs_a_code(self):
        secret, recovery = self.turn_on(self.create())
        c = self.cookie
        self.assertEqual(call(self.url + "/api/account/totp/off", "POST", {"password": PW, "code": "123456"}, cookie=c)[0], 403)
        st, _, d = call(self.url + "/api/account/totp/off", "POST", {"password": PW, "code": recovery[1]}, cookie=c)
        self.assertEqual(st, 200, d)
        self.assertFalse(json.load(open(self.auth.AUTH_FILE))["totp"])


class AccountChanges(Base):
    def test_password_change_signs_everyone_else_out(self):
        c = self.create()
        other = cookie_of(self.login()[1])
        st, _, d = call(self.url + "/api/account/password", "POST", {"password": "not-it-at-all-123", "new": PW + "-2"}, cookie=c)
        self.assertEqual(st, 403)
        st, _, d = call(self.url + "/api/account/password", "POST", {"password": PW, "new": "short"}, cookie=c)
        self.assertEqual((st, d.get("field")), (400, "new"))
        st, h, d = call(self.url + "/api/account/password", "POST", {"password": PW, "new": PW + "-2"}, cookie=c)
        self.assertEqual(st, 200, d)
        self.assertEqual(call(self.url + "/api/settings", cookie=other)[0], 401)
        self.assertEqual(call(self.url + "/api/settings", cookie=c)[0], 401, "even this browser's old token")
        self.assertEqual(call(self.url + "/api/settings", cookie=cookie_of(h))[0], 200, "it got a new one")
        self.assertEqual(self.login(pw=PW)[0], 401)
        self.assertEqual(self.login(pw=PW + "-2")[0], 200)

    def test_sessions_list_and_end(self):
        c = self.create()
        other = cookie_of(self.login()[1])
        _, _, view = call(self.url + "/api/account", cookie=c)
        self.assertEqual(len(view["sessions"]), 2)
        self.assertEqual(sum(s["current"] for s in view["sessions"]), 1)
        sid = next(s["id"] for s in view["sessions"] if not s["current"])
        self.assertEqual(call(self.url + "/api/account/sessions/end", "POST", {"id": sid, "password": PW}, cookie=c)[0], 200)
        self.assertEqual(call(self.url + "/api/settings", cookie=other)[0], 401)
        self.assertEqual(call(self.url + "/api/account/sessions/end", "POST", {"id": "nope", "password": PW}, cookie=c)[0], 404)
        self.assertNotIn("events", json.dumps(view["sessions"]))

    def test_cli_reset(self):
        self.create()
        out = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "app", "auth.py"), "reset", "--yes"], capture_output=True,
                             text=True, env={**os.environ, "DATA_DIR": self.auth.DATA_DIR})
        self.assertEqual(out.returncode, 0, out.stderr)
        code = re.search(r"setup code: (\S+)", out.stdout).group(1)
        self.assertFalse(os.path.exists(self.auth.AUTH_FILE))
        self.assertTrue(call(self.url + "/api/auth/state")[2]["setup"], "the running server notices")
        st, _, d = call(self.url + "/api/setup-account", "POST", {"code": code, "user": "new", "password": PW})
        self.assertEqual(st, 200, d)
        out = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "app", "auth.py"), "reset"], capture_output=True, text=True,
                             env={**os.environ, "DATA_DIR": self.auth.DATA_DIR}, stdin=subprocess.DEVNULL)
        self.assertNotEqual(out.returncode, 0, "without a terminal or --yes it does nothing")
        self.assertTrue(os.path.exists(self.auth.AUTH_FILE))


class Hardening(Base):
    """What the research turned up (peer apps' CVEs, NIST/OWASP): each one tried here."""

    def test_trusted_device_gets_past_an_attackers_lockout(self):
        self.create()
        st, h, _ = self.login()
        device = cookie_of(h, "fishbowl-device")
        self.assertTrue(device)
        self.app.ACCOUNT.update(lambda d: d.update(failures=150, last_fail=int(time.time())))   # someone hammered it
        self.app.THROTTLE.ip_ok("127.0.0.1")
        self.assertEqual(self.login()[0], 423, "a new browser: locked")
        self.app.THROTTLE.ip_ok("127.0.0.1")
        st, h, d = call(self.url + "/api/login", "POST", {"user": "admin", "password": PW}, cookie=device)
        self.assertEqual(st, 200, d)
        # a device's own wrong tries count on their own, and wear it out
        for i in range(12):
            call(self.url + "/api/login", "POST", {"user": "admin", "password": f"wrong-password-{i:03}xx"}, cookie=device)
            dev = json.load(open(self.auth.AUTH_FILE))["devices"]
            for v in dev.values():
                v["last"] = 0   # (skip the waits)
            acct = json.load(open(self.auth.AUTH_FILE))
            acct["devices"] = dev
            self.app.ACCOUNT.save(acct)
        self.app.THROTTLE.ip_ok("127.0.0.1")
        self.app.ACCOUNT.update(lambda d: d.update(failures=150))   # (the owner's sign-in above reset the count, as it should)
        self.assertEqual(call(self.url + "/api/login", "POST", {"user": "admin", "password": PW}, cookie=device)[0], 423,
                         "a worn-out device token is just a browser again")

    def test_parallel_guesses_cant_slip_under_the_limit(self):
        self.create()
        out = []
        ts = [threading.Thread(target=lambda i=i: out.append(self.login("admin", f"wrong-password-{i:03}x")[0])) for i in range(20)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertLessEqual(out.count(401), 6, out)
        self.assertGreaterEqual(out.count(429), 14, out)

    def test_odd_path_spellings_are_refused(self):
        for raw in ("/api/settings%2f", "/%2e%2e/api/settings", "/static/..%2fapi", "/api/settings;.css", "/static/.%2e/x",
                    "/.env", "/api/./settings", "/api/settings%09", "/js/%6cogin.js", "/login/.", "/api//settings"):
            self.assertEqual(call(self.url, raw_path=raw)[0], 400, raw)

    def test_totp_takes_ascii_digits_only(self):
        a, s_ = self.auth, self.auth.new_totp_secret()
        code = a.totp_code(s_, int(time.time() // 30))
        wide = "".join(chr(0xFF10 + int(c)) for c in code)
        arabic = "".join(chr(0x0660 + int(c)) for c in code)
        for c in (wide, arabic, f"+{code[1:]}", f"{code[:3]}_{code[3:]}", f"\t{code}", f"{code}\n"):
            self.assertIsNone(a.totp_match(s_, c, 0), repr(c))
        self.assertIsNotNone(a.totp_match(s_, f"{code[:3]} {code[3:]}", 0), "a space in the middle, as apps show it, is fine")

    def test_plain_http_sign_in_only_from_your_own_network(self):
        app = load_app(AUTH="login", TRUSTED_PROXY="127.0.0.1")
        srv, url = serve(app.Handler)
        try:
            code = app.auth.new_setup_code()
            for ip, want in (("1.1.1.1", 403), ("8.8.8.8", 403), ("192.168.1.20", 200)):
                st, _, d = call(url + "/api/setup-account", "POST", {"code": code, "user": "admin", "password": PW},
                                headers={"X-Real-IP": ip, "X-Forwarded-Proto": "http"})
                self.assertEqual(st, want, (ip, d))
            st, _, d = call(url + "/api/login", "POST", {"user": "admin", "password": PW},
                            headers={"X-Real-IP": "1.1.1.1", "X-Forwarded-Proto": "https"})
            self.assertEqual(st, 200, "over HTTPS (said by the proxy) from anywhere: fine")
            self.assertFalse(call(url + "/api/auth/state", headers={"X-Real-IP": "1.1.1.1", "X-Forwarded-Proto": "http"})[2]["http_ok"])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_csrf_rules(self):
        self.create()
        body = {"user": "admin", "password": PW}
        for hdrs in ({"Content-Type": "text/plain"}, {"Content-Type": "application/x-www-form-urlencoded"},
                     {"Content-Type": "multipart/form-data"}, {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
                     {"Sec-Fetch-Site": "none"}):
            self.assertEqual(call(self.url + "/api/login", "POST", body, headers=hdrs)[0], 403, hdrs)
        self.assertEqual(call(self.url + "/api/login", "POST", body, headers={"Sec-Fetch-Site": "same-origin"})[0], 200)

    def test_a_recovery_code_used_twice_at_once_works_once(self):
        c = self.create()
        TwoStep.turn_on(self, c)
        rec = None
        for f in ("recovery",):
            pass
        st, _, d = call(self.url + "/api/account/recovery", "POST", {"password": PW, "code": "000000"}, cookie=self.cookie)
        self.assertEqual(st, 403)
        # fresh codes to race with (the turn_on ones were shown once; make new ones with an authenticator code)
        secret = json.load(open(self.auth.AUTH_FILE))["totp"]["secret"]
        time.sleep(max(0, 31 - time.time() % 30))
        st, _, d = call(self.url + "/api/account/recovery", "POST",
                        {"password": PW, "code": self.auth.totp_code(secret, int(time.time() // 30))}, cookie=self.cookie)
        self.assertEqual(st, 200, d)
        rec = d["recovery"][0]
        pres = []
        for _ in range(4):
            self.app.THROTTLE.ip_ok("127.0.0.1")
            self.app.ACCOUNT.update(lambda x: x.update(failures=0, last_fail=0))
            pres.append(cookie_of(self.login()[1], "fishbowl-pre"))
        out = []
        ts = [threading.Thread(target=lambda pc=pc: out.append(call(self.url + "/api/login/code", "POST", {"code": rec}, cookie=pc)[0]))
              for pc in pres]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(out.count(200), 1, out)

    def test_password_change_ends_trusted_devices_too(self):
        c = self.create()
        st, h, _ = self.login()
        device = cookie_of(h, "fishbowl-device")
        st, h, _ = call(self.url + "/api/account/password", "POST", {"password": PW, "new": PW + "-new"}, cookie=c)
        self.assertEqual(st, 200)
        self.app.ACCOUNT.update(lambda d: d.update(failures=150, last_fail=int(time.time())))
        self.app.THROTTLE.ip_ok("127.0.0.1")
        st, _, _ = call(self.url + "/api/login", "POST", {"user": "admin", "password": PW + "-new"}, cookie=device)
        self.assertEqual(st, 423, "the old device token no longer counts")


class RoundOne(Base):
    """The first review round's findings (security review, pen test, UX review), one test each."""

    def code_now(self, secret, after=0):
        """A code newer than step `after` (each code works once): waits for the next step when needed."""
        step = int(time.time() // 30)
        if step <= after:
            time.sleep(31 - time.time() % 30)
            step = int(time.time() // 30)
        return self.auth.totp_code(secret, step), step

    def turn_on(self, cookie):
        st, _, d = call(self.url + "/api/account/totp/start", "POST", {"password": PW}, cookie=cookie)
        secret = d["secret"]
        code, step = self.code_now(secret)
        st, h, d = call(self.url + "/api/account/totp/confirm", "POST", {"code": code}, cookie=cookie)
        self.assertEqual(st, 200, d)
        return secret, step, cookie_of(h), d["recovery"]

    def test_half_finished_sign_in_dies_with_a_password_change(self):
        secret, step, c, recovery = self.turn_on(self.create())
        st, h, _ = self.login()              # someone with the password, waiting for a code
        pre = cookie_of(h, "fishbowl-pre")
        st, h, d = call(self.url + "/api/account/password", "POST", {"password": PW, "new": PW + "-2", "code": recovery[0]}, cookie=c)
        self.assertEqual(st, 200, d)
        st, _, d = call(self.url + "/api/login/code", "POST", {"code": recovery[1]}, cookie=pre)
        self.assertEqual((st, d.get("restart")), (401, True), "the code step from before the change is over")
        self.assertEqual(len(json.load(open(self.auth.AUTH_FILE))["recovery"]), 9, "it used up no second recovery code")

    def test_a_session_from_before_a_change_is_worthless(self):
        c = self.create()
        epoch = self.app.ACCOUNT.get()["epoch"]
        tok = self.app.SESSIONS.create(False, "1.2.3.4", "ua", epoch)[0]   # as if created by a sign-in still in flight
        st, h, _ = call(self.url + "/api/account/password", "POST", {"password": PW, "new": PW + "-2"}, cookie=c)
        self.assertEqual(st, 200)
        self.assertEqual(call(self.url + "/api/settings", cookie=f"fishbowl={tok}")[0], 401)
        self.assertNotEqual(self.app.ACCOUNT.get()["epoch"], epoch)

    def test_password_change_needs_the_code_when_two_step_is_on(self):
        secret, step, c, recovery = self.turn_on(self.create())
        st, _, d = call(self.url + "/api/account/password", "POST", {"password": PW, "new": PW + "-2"}, cookie=c)
        self.assertEqual((st, d.get("field")), (400, "code"), "no code: asked for one")
        st, _, d = call(self.url + "/api/account/password", "POST", {"password": PW, "new": PW + "-2", "code": "123456"}, cookie=c)
        self.assertEqual((st, d.get("field")), (403, "code"))
        st, _, d = call(self.url + "/api/account/password", "POST", {"password": PW, "new": PW + "-2", "code": recovery[0]}, cookie=c)
        self.assertEqual(st, 200, d)

    def test_weak_new_password_uses_up_no_code(self):
        secret, step, c, recovery = self.turn_on(self.create())
        st, _, d = call(self.url + "/api/account/password", "POST", {"password": PW, "new": "short", "code": recovery[0]}, cookie=c)
        self.assertEqual((st, d.get("field")), (400, "new"))
        self.assertEqual(len(json.load(open(self.auth.AUTH_FILE))["recovery"]), 10)

    def test_trusted_device_success_keeps_the_account_count(self):
        self.create()
        device = cookie_of(self.login()[1], "fishbowl-device")
        self.app.ACCOUNT.update(lambda d: d.update(failures=99, last_fail=0))
        st, _, _ = call(self.url + "/api/login", "POST", {"user": "admin", "password": PW}, cookie=device)
        self.assertEqual(st, 200)
        self.assertEqual(self.app.ACCOUNT.get()["failures"], 99, "someone else's guessing keeps counting toward the lock")
        self.app.THROTTLE.ip_ok("127.0.0.1")
        self.assertEqual(self.login(pw="wrong-guess-number-100")[0], 401)
        self.app.THROTTLE.ip_ok("127.0.0.1")
        self.assertEqual(self.login()[0], 423, "100 in a row: locked for new browsers")

    def test_cookie_tossing_cant_push_the_session_out(self):
        c = self.create()
        tok = c.split("=", 1)[1]
        junk = "; ".join(f"fishbowl={'A' * 42}{chr(65 + i % 26)}" for i in range(60)) + "; " + \
               "; ".join(f"pad{i}={'x' * 200}" for i in range(60))
        self.assertEqual(call(self.url + "/api/settings", cookie=f"{junk}; fishbowl={tok}")[0], 200)
        self.assertEqual(call(self.url + "/api/settings", cookie=f" fishbowl={tok}".encode().decode("latin-1"))[0], 401,
                         "a name with other whitespace isn't ours")

    def test_cli_waits_for_the_server_lock(self):
        self.create()
        with self.auth.file_lock():
            p = subprocess.Popen([sys.executable, "-I", os.path.join(ROOT, "app", "auth.py"), "reset", "--yes"],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env={**os.environ, "DATA_DIR": self.auth.DATA_DIR})
            time.sleep(1)
            self.assertIsNone(p.poll(), "reset waits while the server is writing")
            self.assertTrue(os.path.exists(self.auth.AUTH_FILE))
        out = p.communicate(timeout=30)[0].decode()
        self.assertEqual(p.returncode, 0, out)
        self.assertFalse(os.path.exists(self.auth.AUTH_FILE))
        with self.assertRaises(self.auth.Broken):
            self.app.ACCOUNT.update(lambda d: d.update(failures=1))   # an in-flight write can't bring it back
        self.assertFalse(os.path.exists(self.auth.AUTH_FILE))
        code = re.search(r"setup code: (\S+)", out).group(1)
        self.assertNotIn(code.replace("-", ""), open(self.auth.SETUP_FILE).read(), "only the code's hash is kept")
        self.assertEqual(len(code.replace("-", "")), 25, "125 bits")

    def test_double_confirm_shows_codes_that_work(self):
        c = self.create()
        st, _, d = call(self.url + "/api/account/totp/start", "POST", {"password": PW}, cookie=c)
        code, _ = self.code_now(d["secret"])
        out = []
        ts = [threading.Thread(target=lambda: out.append(call(self.url + "/api/account/totp/confirm", "POST", {"code": code}, cookie=c)))
              for _ in range(3)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        ok = [r for r in out if r[0] == 200]
        self.assertEqual(len(ok), 1, [r[0] for r in out])
        stored = json.load(open(self.auth.AUTH_FILE))["recovery"]
        self.assertEqual(sorted(self.auth.rc_hash(x) for x in ok[0][2]["recovery"]), sorted(stored))

    def test_one_character_that_normalizes_long_isnt_a_password(self):
        self.assertIsNotNone(self.auth.password_problem("ﷺ"))
        self.assertIsNotNone(self.auth.password_problem("ﷺ" * 3))

    def test_odd_text_is_just_wrong(self):
        self.create()
        for body in ({"user": "admin", "password": "\ud800" * 20}, {"user": "\udfff", "password": PW}):
            raw = json.dumps(body)   # (json escapes the lone surrogates as \udXXX)
            u = urllib.parse.urlsplit(self.url)
            conn = http.client.HTTPConnection(u.hostname, u.port, timeout=30)
            conn.request("POST", "/api/login", body=raw.encode(), headers={"Content-Type": "application/json", "Origin": self.url})
            self.assertIn(conn.getresponse().status, (400, 401))
            conn.close()
            self.app.THROTTLE.ip_ok("127.0.0.1")
        c = cookie_of(self.login()[1])
        st, _, _ = call(self.url + "/api/account/sessions/end", "POST", {"id": "é" * 16, "password": PW}, cookie=c)
        self.assertEqual(st, 404)

    def test_dns_rebinding_name_cant_post(self):
        self.create()
        for host, want in (("rebind.attacker.example:8000", 403), ("192.168.1.5:8000", 401), ("dash.lan:8000", 401),
                           ("dash:8000", 401), ("[fd00::1]:8000", 401)):
            st, _, _ = call(self.url + "/api/login", "POST", {"user": "admin", "password": "wrong-password-xyz"},
                            origin=f"http://{host}", headers={"Host": host})
            self.assertEqual(st, want, host)
            self.app.THROTTLE.ip_ok("127.0.0.1")
            self.app.ACCOUNT.update(lambda d: d.update(failures=0, last_fail=0))
        st, _, _ = call(self.url + "/api/login", "POST", {"user": "admin", "password": PW}, origin="null", headers={"Host": ""})
        self.assertEqual(st, 403, "no Host and Origin: null")

    def test_failures_shown_once_at_the_next_sign_in(self):
        secret, step, c, _ = self.turn_on(self.create())
        self.assertEqual(self.login(pw="wrong-guess-number-1")[0], 401)
        self.app.THROTTLE.ip_ok("127.0.0.1")
        st, h, _ = self.login()
        st, _, _ = call(self.url + "/api/login/code", "POST", {"code": "000000"}, cookie=cookie_of(h, "fishbowl-pre"))
        self.assertEqual(st, 401)
        self.app.THROTTLE.ip_ok("127.0.0.1")
        st, h, _ = self.login()
        code, _ = self.code_now(secret, step)
        self.app._recent_fails.clear()   # as if those failures came from another address (this one's own are forgiven)
        st, h, d = call(self.url + "/api/login/code", "POST", {"code": code}, cookie=cookie_of(h, "fishbowl-pre"))
        self.assertEqual(st, 200, d)
        self.assertEqual({k: d["notice"][k] for k in ("password", "code")}, {"password": 1, "code": 1})
        sess = cookie_of(h)
        view = call(self.url + "/api/account", cookie=sess)[2]
        self.assertEqual(view["notice"]["code"], 1)
        self.assertEqual([f["kind"] for f in view["fails"]], ["right password, wrong code", "wrong name or password"])
        self.assertTrue(all("wrong" not in e["kind"] for e in view["events"]), "failures can't push sign-ins out of view")
        self.assertEqual({i["id"]: i["state"] for i in self.app.setup_checks(host="x", https=True)}["password-out"], "problem")
        self.app.THROTTLE.ip_ok("127.0.0.1")
        st, h, _ = self.login()
        code, _ = self.code_now(secret, int(time.time() // 30))
        st, _, d = call(self.url + "/api/login/code", "POST", {"code": code}, cookie=cookie_of(h, "fishbowl-pre"))
        self.assertNotIn("notice", d, "shown once")

    def test_two_step_with_an_empty_secret_is_broken_not_open(self):
        self.create()
        d = json.load(open(self.auth.AUTH_FILE))
        d["totp"] = {"on": True, "secret": ""}
        self.app.ACCOUNT.save(d)
        self.assertEqual(self.login()[0], 503)
        d["totp"] = []
        self.app.ACCOUNT.save(d)
        self.assertEqual(self.login()[0], 503)

    def test_sign_out_everywhere_else_keeps_trusted_devices(self):
        c = self.create()
        h1 = self.login()[1]
        dev1, other = cookie_of(h1, "fishbowl-device"), cookie_of(h1)
        st, _, _ = call(self.url + "/api/account/sessions/end", "POST", {"all": True, "password": PW}, cookie=c)
        self.assertEqual(st, 200)
        self.assertEqual(call(self.url + "/api/settings", cookie=other)[0], 401, "the other session ended")
        h = self.auth.hashlib.sha256(dev1.split("=", 1)[1].encode()).hexdigest()
        self.assertIn(h, self.app.ACCOUNT.get()["devices"], "a session alone can't strip the owner's browsers of trust")

    def test_uploads_are_private_behind_the_sign_in(self):
        c = self.create()
        os.makedirs(self.app.cfgstore.UPLOAD_DIR, exist_ok=True)
        with open(os.path.join(self.app.cfgstore.UPLOAD_DIR, "x.png"), "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n")
        st, h, _ = call(self.url + "/uploads/x.png", cookie=c)
        self.assertEqual(st, 200)
        self.assertTrue(h["cache-control"].startswith("private"), h["cache-control"])
        self.assertTrue(call(self.url + "/static/favicon.svg")[1]["cache-control"].startswith("public"))

    def test_back_to_the_same_page_after_signing_in(self):
        st, h, _ = call(self.url + "/?wall")
        self.assertEqual(h["location"], "/login?next=/%3Fwall")
        for n, want in (("/?wall", "/?wall"), ("/settings?security=fw", "/settings?security=fw"), ("//evil.example", "/"),
                        ("/\\evil", "/"), ("/?x=<y>", "/"), ("https://evil.example/", "/"), ("/login", "/")):
            self.assertEqual(self.app.safe_next(n), want, n)

    def test_proxy_without_x_real_ip_is_not_your_network(self):
        app = load_app(AUTH="login", TRUSTED_PROXY="127.0.0.1")
        srv, url = serve(app.Handler)
        try:
            code = app.auth.new_setup_code()
            st, _, d = call(url + "/api/setup-account", "POST", {"code": code, "user": "admin", "password": PW},
                            headers={"X-Forwarded-Proto": "http"})
            self.assertEqual(st, 403, d)
            self.assertIn("X-Real-IP", d["error"], "the refusal names what the proxy leaves out")
            st, _, d = call(url + "/api/setup-account", "POST", {"code": code, "user": "admin", "password": PW},
                            headers={"X-Forwarded-Proto": "http", "X-Real-IP": "192.168.1.9"})
            self.assertEqual(st, 200, d)
            view = app.settings_view("x", False, ["X-Real-IP"])
            self.assertIn("realip", {i["id"] for i in view["setup"]})
        finally:
            srv.shutdown()
            srv.server_close()


class RoundTwo(Base):
    """The second review round's findings (security review, pen test, UX review), one test each."""

    def proxied(self, **env):
        app = load_app(AUTH="login", TRUSTED_PROXY="127.0.0.1", **env)
        srv, url = serve(app.Handler)
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        return app, url

    def test_a_new_account_gets_a_new_random_epoch(self):
        self.create()
        old = self.app.ACCOUNT.get()["epoch"]
        out = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "app", "auth.py"), "reset", "--yes"], capture_output=True,
                             text=True, env={**os.environ, "DATA_DIR": self.auth.DATA_DIR})
        self.assertEqual(out.returncode, 0, out.stdout)
        late = self.app.SESSIONS.create(False, "1.2.3.4", "ua", old)[0]   # a sign-in checked against the old account
        self.create(user="owner2", pw=PW + "-new")
        self.assertNotEqual(self.app.ACCOUNT.get()["epoch"], old)
        self.assertEqual(call(self.url + "/api/settings", cookie=f"fishbowl={late}")[0], 401, "nothing from the old account works")

    def test_of_two_changes_at_once_only_one_lands(self):
        c = self.create()
        out = []
        go = lambda pw: out.append((pw, call(self.url + "/api/account/password", "POST", {"password": PW, "new": pw}, cookie=c)[0]))
        ts = [threading.Thread(target=go, args=(PW + f"-{i}",)) for i in range(3)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        won = [pw for pw, st in out if st == 200]
        self.assertEqual(len(won), 1, out)
        self.assertTrue(self.auth.verify_password(won[0], self.app.ACCOUNT.get()["hash"])[0], "the one that said OK is the one saved")

    def test_two_browsers_confirming_two_step_at_once(self):
        c1 = self.create()
        c2 = cookie_of(self.login()[1])
        secrets_ = [call(self.url + "/api/account/totp/start", "POST", {"password": PW}, cookie=c)[2]["secret"] for c in (c1, c2)]
        step = int(time.time() // 30)
        out = []
        ts = [threading.Thread(target=lambda c=c, s=s: out.append((s, call(self.url + "/api/account/totp/confirm", "POST",
                                                                             {"code": self.auth.totp_code(s, step)}, cookie=c))))
              for c, s in zip((c1, c2), secrets_)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        ok = [(s, r) for s, r in out if r[0] == 200]
        self.assertEqual(len(ok), 1, [r[0] for _, r in out])
        d = self.app.ACCOUNT.get()
        self.assertEqual(d["totp"]["secret"], ok[0][0], "the QR code that was confirmed is the one stored")
        self.assertEqual(sorted(self.auth.rc_hash(x) for x in ok[0][1][2]["recovery"]), sorted(d["recovery"]))

    def test_a_malformed_origin_is_refused_not_an_error(self):
        for o in ("http://[", "http://[zz]", "http://[::1].evil.com"):
            st, _, _ = call(self.url + "/api/login", "POST", {"user": "a", "password": "b"}, origin=o)
            self.assertEqual(st, 403, o)

    def test_the_refusal_names_the_check(self):
        u = urllib.parse.urlsplit(self.url)
        conn = http.client.HTTPConnection(u.hostname, u.port, timeout=30)
        conn.request("POST", "/api/login", body=b"{}", headers={"Content-Type": "text/plain", "Origin": self.url})
        r = conn.getresponse()
        self.assertEqual(r.status, 403)
        self.assertIn("application/json", json.loads(r.read())["error"])
        conn.close()

    def test_x_real_ip_scope_ids_and_mapped_addresses(self):
        app, url = self.proxied()
        code = app.auth.new_setup_code()
        body = {"code": code, "user": "admin", "password": PW}
        st, _, _ = call(url + "/api/setup-account", "POST", body, headers={"X-Real-IP": "fe80::1%evil-text", "X-Forwarded-Proto": "http"})
        self.assertEqual(st, 403, "a scoped address isn't a known address")
        st, _, d = call(url + "/api/setup-account", "POST", body, headers={"X-Real-IP": "::ffff:192.168.1.9", "X-Forwarded-Proto": "http"})
        self.assertEqual(st, 200, d)
        self.assertEqual(app.ACCOUNT.get()["events"][-1]["ip"], "192.168.1.9")

    def test_throttle_keys_and_rounding(self):
        T = self.auth.Throttle
        self.assertEqual(T._key("2001:db8::1"), T._key("2001:db8::ffff:1"), "IPv6 counts per /64")
        self.assertNotEqual(T._key("2001:db8::1"), T._key("2001:db8:0:1::1"))
        self.assertEqual(T._key("::ffff:10.0.0.1"), "10.0.0.1")
        self.assertEqual(T.wait_for(6, 100, 101.5), 2, "0.5 s left: rounded up, plus a second")
        self.assertLessEqual(T.wait_for(30, 2_000_000_000, 1_000_000_000), T.MAX_WAIT + 1, "a clock stepped back")

    def test_one_wide_character_repeated_isnt_a_password(self):
        self.assertIsNotNone(self.auth.password_problem("ﷺ" * 15))
        self.assertIsNotNone(self.auth.password_problem("ﷺﷺﷺabcabcabcabc"))

    def test_the_documented_setup_code_command_finds_the_code(self):
        tmp = os.path.join(self.app.TEST_TMP, "fresh")
        for d in ("data", "npm", "hw", "req"):
            os.makedirs(os.path.join(tmp, d))
        env = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", ""), "DATA_DIR": os.path.join(tmp, "data"),
               "NPM_DIR": os.path.join(tmp, "npm"), "HW_FILE": os.path.join(tmp, "hw", "hw.json"), "REQ_DIR": os.path.join(tmp, "req"),
               "LISTEN": "127.0.0.1", "PORT": "0", "TRUSTED_PROXY": "", "PYTHONUNBUFFERED": "1"}
        p = subprocess.Popen([sys.executable, "-I", os.path.join(ROOT, "app", "app.py")], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, env=env)
        log = []
        try:
            for line in p.stdout:
                log.append(line)
                if line.startswith("=" * 20) and any(x.startswith("SETUP CODE") for x in log):
                    break
        finally:
            p.kill()
            p.wait()
        out = subprocess.run(["bash", "-c", "grep '^SETUP CODE' | tail -n 1"], input="".join(log), capture_output=True, text=True).stdout
        code = out.strip().split(": ", 1)[1]
        self.assertRegex(code, r"^[A-Z2-9]{5}(-[A-Z2-9]{5}){4}$", out)
        self.auth.configure(os.path.join(tmp, "data"))
        try:
            self.assertTrue(self.auth.setup_code_matches(code))
        finally:
            self.auth.configure(self.app.cfgstore.DATA_DIR)

    def test_your_own_typos_arent_news_others_are(self):
        app, url = self.proxied()
        st, h, _ = call(url + "/api/setup-account", "POST", {"code": app.auth.new_setup_code(), "user": "admin", "password": PW},
                        headers={"X-Real-IP": "192.168.1.9", "X-Forwarded-Proto": "https"})
        self.assertEqual(st, 200)
        mine = cookie_of(h, "__Host-fishbowl-device")   # (the proxy says https)
        login = lambda pw, cookie=None, ip="192.168.1.9": call(url + "/api/login", "POST", {"user": "admin", "password": pw}, cookie=cookie,
                                                               headers={"X-Real-IP": ip, "X-Forwarded-Proto": "https"})
        self.assertEqual(login("someone-elses-guess", ip="192.168.1.9")[0], 401, "same address as the owner, other browser")
        self.assertEqual(login("my-own-typo-here", mine)[0], 401)
        st, _, d = login(PW, mine)
        self.assertEqual((st, d.get("notice")), (200, {"password": 1}), "only the other browser's try is news, even from the same address")

    def test_stale_typos_dont_hide_later_failures(self):
        c = self.create()
        dev = cookie_of(self.login()[1], "fishbowl-device")
        self.assertEqual(self.login(pw="phone-typo-xxxxx", cookie=dev)[0], 401)      # the owner's typo in one browser...
        self.app.THROTTLE.ip_ok("127.0.0.1")
        self.assertEqual(self.login()[0], 200)                                        # ...then a sign-in in another
        self.assertEqual(self.login(pw="attacker-guess-xx")[0], 401)                  # someone else, later
        self.app.THROTTLE.ip_ok("127.0.0.1")
        st, _, d = self.login(cookie=dev)
        self.assertEqual(d.get("notice"), {"password": 1}, "the old typo can't cancel the new failure")

    def test_wrong_codes_forgiven_only_for_the_sign_in_that_got_it_right(self):
        c = self.create()
        st, _, d = call(self.url + "/api/account/totp/start", "POST", {"password": PW}, cookie=c)
        secret, step = d["secret"], int(time.time() // 30)
        st, h, _ = call(self.url + "/api/account/totp/confirm", "POST", {"code": self.auth.totp_code(secret, step)}, cookie=c)
        self.assertEqual(st, 200)
        pre_other = cookie_of(self.login()[1], "fishbowl-pre")   # someone with the password...
        call(self.url + "/api/login/code", "POST", {"code": "000000"}, cookie=pre_other)   # ...gets the code wrong and gives up
        self.app.THROTTLE.ip_ok("127.0.0.1")
        pre = cookie_of(self.login()[1], "fishbowl-pre")         # the owner: one typo, then right
        call(self.url + "/api/login/code", "POST", {"code": "111111"}, cookie=pre)
        self.app.THROTTLE.ip_ok("127.0.0.1")
        while int(time.time() // 30) <= step:
            time.sleep(1)
        st, _, d = call(self.url + "/api/login/code", "POST", {"code": self.auth.totp_code(secret, int(time.time() // 30))}, cookie=pre)
        self.assertEqual(st, 200, d)
        self.assertEqual(d["notice"].get("code"), 2, "forgiven only in the owner's trusted browser: this one isn't one")

    def test_the_alarm_adds_up_until_dismissed(self):
        c = self.create()
        self.app.ACCOUNT.update(lambda d: d.update(notice={"code": 2, "t": 1}, since={"password": 1}))
        st, _, d = self.login()
        self.assertEqual(self.app.ACCOUNT.get()["notice"]["code"], 2, "a later wrong password doesn't clear the alarm")
        self.app.ACCOUNT.update(lambda d: d.update(since={"code": 1}))
        st, _, d = self.login()
        self.assertEqual(self.app.ACCOUNT.get()["notice"]["code"], 3, "it adds up")
        self.assertIn("password-out", {i["id"] for i in self.app.setup_checks(host="x", https=True)})
        st, _, _ = call(self.url + "/api/account/notice/dismiss", "POST", {"password": PW}, cookie=c)
        self.assertEqual(st, 200)
        self.assertEqual(self.app.ACCOUNT.get()["notice"], {})
        self.assertNotIn("password-out", {i["id"] for i in self.app.setup_checks(host="x", https=True)})

    def test_an_empty_code_isnt_a_try(self):
        c = self.create()
        st, _, d = call(self.url + "/api/account/totp/start", "POST", {"password": PW}, cookie=c)
        st, h, _ = call(self.url + "/api/account/totp/confirm", "POST",
                        {"code": self.auth.totp_code(d["secret"], int(time.time() // 30))}, cookie=c)
        c = cookie_of(h)
        st, _, d = call(self.url + "/api/account/password", "POST", {"password": PW, "new": PW + "-2", "code": ""}, cookie=c)
        self.assertEqual((st, d.get("field")), (400, "code"))
        self.assertEqual(self.app.ACCOUNT.get()["failures"], 0)

    def test_a_password_needs_positive_evidence_of_https(self):
        app, url = self.proxied(ORIGIN="https://dash.example.com")
        body = {"code": app.auth.new_setup_code(), "user": "admin", "password": PW}
        st, _, d = call(url + "/api/setup-account", "POST", body, origin="https://dash.example.com", headers={"X-Real-IP": "8.8.4.4"})
        self.assertEqual(st, 403, "ORIGIN alone isn't evidence: the browser may be on http://")
        self.assertIn("X-Forwarded-Proto", d["error"])
        st, _, d = call(url + "/api/setup-account", "POST", body, origin="https://dash.example.com",
                        headers={"X-Real-IP": "8.8.4.4", "X-Forwarded-Proto": "https"})
        self.assertEqual(st, 200, d)

    def test_every_visitor_looking_like_the_gateway_is_flagged(self):
        self.create()
        self.app.default_gateway = lambda: "127.0.0.1"
        self.assertIn("gateway", {i["id"] for i in self.app.setup_checks(proxy_gaps=["gateway"])})
        # every visitor looking alike: a plain-HTTP sign-in couldn't be tied to the browser (audit round 2), so the
        # sign-in page says why it's refused
        state = call(self.url + "/api/auth/state")[2]
        self.assertFalse(state["http_ok"])
        self.assertIn("gateway", state["gap_hint"])
        self.assertEqual(self.login()[0], 403)


class RoundThree(Base):
    """The third review round's findings, one test each."""

    def test_cookie_parsing_stays_cheap(self):
        c = self.create()
        junk = "; ".join(f"fishbowl={i:043d}" for i in range(1100))   # ~55 KB of token-shaped junk, the real one last
        t = time.monotonic()
        st = call(self.url + "/api/settings", cookie=f"{junk}; {c}")[0]
        self.assertLess(time.monotonic() - t, 2.0, "linear, and at most 300 values tried")
        self.assertIn(st, (200, 401))
        u = urllib.parse.urlsplit(self.url)
        conn = http.client.HTTPConnection(u.hostname, u.port, timeout=60)
        conn.putrequest("GET", "/api/settings")
        try:
            for _ in range(60):
                conn.putheader("Cookie", junk)
            conn.endheaders()
            st = conn.getresponse().status
        except (BrokenPipeError, ConnectionResetError):
            st = 431   # (the server answered and closed before all of it was sent)
        self.assertEqual(st, 431, "more than 64 KB of headers in all: refused")
        conn.close()

    def test_a_repeated_proxy_header_is_unknown(self):
        app = load_app(AUTH="login", TRUSTED_PROXY="127.0.0.1")
        srv, url = serve(app.Handler)
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        u = urllib.parse.urlsplit(url)
        conn = http.client.HTTPConnection(u.hostname, u.port, timeout=30)
        body = json.dumps({"code": app.auth.new_setup_code(), "user": "admin", "password": PW}).encode()
        conn.putrequest("POST", "/api/setup-account")
        for k, v in (("Content-Type", "application/json"), ("Origin", url), ("X-Real-IP", "192.168.1.9"), ("X-Real-IP", "8.8.8.8"),
                     ("X-Forwarded-Proto", "http"), ("Content-Length", str(len(body)))):
            conn.putheader(k, v)
        conn.endheaders(body)
        self.assertEqual(conn.getresponse().status, 403, "two X-Real-IP headers: the address is unknown")
        conn.close()

    def test_own_network_is_spelled_out(self):
        own = lambda ip: any(self.app.ipaddress.ip_address(ip) in n for n in self.app.OWN_NETWORKS)
        for ip in ("192.168.1.9", "10.1.2.3", "172.20.0.5", "127.0.0.1", "fe80::1", "fd00::5", "::1"):
            self.assertTrue(own(ip), ip)
        for ip in ("192.0.2.1", "198.18.0.1", "0.0.0.0", "::", "2002:c0a8:101::1", "100.64.0.1", "240.0.0.1"):
            self.assertFalse(own(ip), ip)

    def test_a_blank_looking_name_is_refused(self):
        for n in ("ㅤ", "​", " ⠀ ", "́"):
            self.assertIsNotNone(self.auth.name_problem(n), repr(n))
        self.assertIsNone(self.auth.name_problem("Vic"))

    def test_a_session_on_disk(self):
        tok = self.app.SESSIONS.create(False, "ip", "ua", 7)[0]
        with self.auth.file_lock():
            self.assertTrue(self.app.SESSIONS.on_disk(tok, 7))
            self.assertFalse(self.app.SESSIONS.on_disk(tok, 8))
        self.app.SESSIONS.end(tok)
        with self.auth.file_lock():
            self.assertFalse(self.app.SESSIONS.on_disk(tok, 7), "after a sign-out returned, a change can't use that session")

    def test_connection_caps_and_deadline(self):
        import socket as so
        srv = self.app.Server(("127.0.0.1", 0), self.app.Handler)
        srv.MAX_PER_IP, srv.DEADLINE = 3, 1.5
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        port = srv.server_address[1]
        socks = [so.create_connection(("127.0.0.1", port)) for _ in range(3)]
        for x in socks:
            x.sendall(b"GET / HTTP/1.1\r\n")   # trickling: never finishes its headers
        time.sleep(0.5)
        # full for this address: its own oldest connection still sending its request makes room (audit 2026-10-30:
        # refusing instead let one address, e.g. everyone behind a proxy nobody named, lock the others out)
        extra = so.create_connection(("127.0.0.1", port))
        extra.settimeout(5)
        extra.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertTrue(extra.recv(12).startswith(b"HTTP/1."), "the 4th connection from one address is answered")
        socks[0].settimeout(5)
        self.assertEqual(socks[0].recv(10), b"", "the oldest slow sender from that address is cut")
        socks[1].settimeout(10)
        t = time.monotonic()
        try:
            while socks[1].recv(4096):
                pass
        except OSError:
            pass
        self.assertLess(time.monotonic() - t, 6, "a stuck connection is cut after the deadline")
        for x in socks + [extra]:
            x.close()


    def test_a_plain_http_session_only_counts_there(self):
        app = load_app(AUTH="login", TRUSTED_PROXY="127.0.0.1")
        srv, url = serve(app.Handler)
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        lan = {"X-Real-IP": "192.168.1.9", "X-Forwarded-Proto": "http"}
        st, h, _ = call(url + "/api/setup-account", "POST", {"code": app.auth.new_setup_code(), "user": "admin", "password": PW}, headers=lan)
        self.assertEqual(st, 200)
        tok = cookie_of(h).split("=", 1)[1]
        self.assertEqual(call(url + "/api/settings", cookie=f"fishbowl={tok}", headers=lan)[0], 200)
        self.assertEqual(call(url + "/api/settings", cookie=f"fishbowl={tok}", headers={**lan, "X-Real-IP": "8.8.8.8"})[0], 401,
                         "a token seen on the LAN over plain HTTP is no good from outside")
        self.assertEqual(call(url + "/api/settings", cookie=f"__Host-fishbowl={tok}",
                              headers={"X-Real-IP": "8.8.8.8", "X-Forwarded-Proto": "https"})[0], 401, "nor over HTTPS")

    def test_a_proxy_nobody_mentioned_hides_the_address(self):
        st, _, d = call(self.url + "/api/setup-account", "POST", {"code": self.setup_code(), "user": "admin", "password": PW},
                        headers={"X-Forwarded-For": "8.8.8.8"})
        self.assertEqual(st, 403, "forwarding headers from a peer that isn't TRUSTED_PROXY: the browser's address is unknown")
        self.assertIn("TRUSTED_PROXY", d["error"])
        state = call(self.url + "/api/auth/state", headers={"X-Forwarded-For": "8.8.8.8"})[2]
        self.assertIn("TRUSTED_PROXY", state["gap_hint"], "the sign-in page names the fix")

    def test_ending_sessions_and_dismissing_need_the_password(self):
        c = self.create()
        other = cookie_of(self.login()[1])
        for body in ({"all": True}, {"all": True, "password": "not-the-password-x"}):
            st, _, _ = call(self.url + "/api/account/sessions/end", "POST", body, cookie=c)
            self.assertIn(st, (400, 403), body)
        self.assertEqual(call(self.url + "/api/settings", cookie=other)[0], 200, "nothing ended without the password")
        self.app.ACCOUNT.update(lambda d: d.update(notice={"code": 1, "t": 1}))
        self.app.THROTTLE.ip_ok("127.0.0.1")
        self.assertIn(call(self.url + "/api/account/notice/dismiss", "POST", {}, cookie=c)[0], (400, 403))
        self.assertEqual(self.app.ACCOUNT.get()["notice"]["code"], 1)

    def test_every_answer_refuses_framing(self):
        st, h, _ = call(self.url + "/api/auth/state")
        self.assertIn("frame-ancestors 'none'", h["content-security-policy"])
        self.assertIn("object-src 'none'", call(self.url + "/login")[1]["content-security-policy"])

    def test_a_repeated_word_with_digits_isnt_a_password(self):
        for pw in ("monkeymonkey123", "dragondragon2024!", "qwertzqwertz1234"):
            self.assertIsNotNone(self.auth.password_problem(pw), pw)

    def test_a_password_change_clears_the_unreported_wrong_codes(self):
        c = self.create()
        self.app.ACCOUNT.update(lambda d: d.update(since={"code": 2}, notice={"code": 1, "t": 1}))
        st, _, _ = call(self.url + "/api/account/password", "POST", {"password": PW, "new": PW + "-2"}, cookie=c)
        self.assertEqual(st, 200)
        d = self.app.ACCOUNT.get()
        self.assertNotIn("code", d["since"])
        self.assertNotIn("code", d["notice"])

    def test_switching_to_auth_proxy_forgets_the_sign_ins(self):
        self.create()
        self.assertTrue(os.path.exists(self.auth.SESSIONS_FILE))
        env = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", ""), "DATA_DIR": self.auth.DATA_DIR,
               "NPM_DIR": os.path.join(self.app.TEST_TMP, "npm"), "HW_FILE": os.path.join(self.app.TEST_TMP, "hw", "hw.json"),
               "REQ_DIR": os.path.join(self.app.TEST_TMP, "req"), "LISTEN": "127.0.0.1", "PORT": "0", "AUTH": "proxy",
               "TRUSTED_PROXY": "127.0.0.1", "PYTHONUNBUFFERED": "1"}
        p = subprocess.Popen([sys.executable, "-I", os.path.join(ROOT, "app", "app.py")], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, env=env)
        try:
            for line in p.stdout:
                if line.startswith("Sign-in:"):
                    break
        finally:
            p.kill()
            p.wait()
        self.assertFalse(os.path.exists(self.auth.SESSIONS_FILE), "switching back to login later can't revive them")


class RoundFour(Base):
    """The fourth review round's findings, one test each."""

    def two_step(self):
        c = self.create()
        st, _, d = call(self.url + "/api/account/totp/start", "POST", {"password": PW}, cookie=c)
        step = int(time.time() // 30)
        st, h, r = call(self.url + "/api/account/totp/confirm", "POST", {"code": self.auth.totp_code(d["secret"], step)}, cookie=c)
        self.assertEqual(st, 200)
        return cookie_of(h), d["secret"], r["recovery"], cookie_of(h, "fishbowl-device")

    def test_sign_outs_and_that_was_me_need_the_code_too(self):
        c, secret, recovery, _ = self.two_step()
        self.assertEqual(call(self.url + "/api/account/sessions/end", "POST", {"all": True, "password": PW}, cookie=c)[0], 400)
        self.app.ACCOUNT.update(lambda d: d.update(notice={"code": 1, "t": 1}))
        self.assertEqual(call(self.url + "/api/account/notice/dismiss", "POST", {"password": PW}, cookie=c)[0], 400)
        st, _, d = call(self.url + "/api/account/sessions/end", "POST", {"all": True, "password": PW, "code": recovery[0]}, cookie=c)
        self.assertEqual(st, 200, d)

    def test_signing_others_out_leaves_half_finished_sign_ins(self):
        c, secret, recovery, dev = self.two_step()
        pre = cookie_of(self.login(cookie=dev)[1], "fishbowl-pre")   # the owner, between password and code
        st, _, _ = call(self.url + "/api/account/sessions/end", "POST", {"all": True, "password": PW, "code": recovery[0]}, cookie=c)
        self.assertEqual(st, 200)
        st, _, d = call(self.url + "/api/login/code", "POST", {"code": recovery[1]}, cookie=f"{pre}; {dev}")
        self.assertEqual(st, 200, d)

    def test_that_was_me_clears_only_what_was_shown(self):
        c = self.create()
        self.app.ACCOUNT.update(lambda d: d.update(notice={"code": 2, "t": 1}, since={"code": 3}))
        self.assertEqual(call(self.url + "/api/account/notice/dismiss", "POST", {"password": PW}, cookie=c)[0], 200)
        d = self.app.ACCOUNT.get()
        self.assertEqual((d["notice"], d["since"]), ({}, {"code": 3}), "newer wrong codes still reach the next sign-in")

    def test_a_signed_in_owner_isnt_locked_out_by_the_lock(self):
        c = self.create()
        self.app.ACCOUNT.update(lambda d: d.update(failures=150, devices={}))   # locked, and this browser not trusted
        st, h, d = call(self.url + "/api/account/password", "POST", {"password": PW, "new": PW + "-2"}, cookie=c)
        self.assertEqual(st, 200, d)

    def test_a_session_gets_ten_wrong_tries(self):
        c = self.create()
        out = []
        for i in range(12):
            self.app.THROTTLE.ip_ok("127.0.0.1")
            self.app._session_tries.update({k: (v[0], 0) for k, v in self.app._session_tries.items()})   # (skip the waits)
            out.append(call(self.url + "/api/account/password", "POST", {"password": f"wrong-password-{i:02d}", "new": PW + "-2"}, cookie=c)[0])
        self.assertEqual(out[:10], [403] * 10)
        self.assertEqual(out[10], 401, "then the browser is signed out")
        self.assertEqual(call(self.url + "/api/settings", cookie=c)[0], 401)
        self.assertEqual(self.app.ACCOUNT.get()["failures"], 0, "none of it counts toward the account lock")

    def test_a_trusted_device_counts_only_on_its_kind_of_connection(self):
        d = {"epoch": 5, "devices": {}}
        tok = self.auth.new_device(d, tls=False)
        self.assertIsNotNone(self.auth.device_of(d, tok, False))
        self.assertIsNone(self.auth.device_of(d, tok, True), "seen over plain HTTP: no good over HTTPS")

    def test_own_wrong_code_then_start_over_isnt_an_alarm(self):
        c, secret, recovery, dev = self.two_step()
        pre = cookie_of(self.login(cookie=dev)[1], "fishbowl-pre")
        call(self.url + "/api/login/code", "POST", {"code": "000000"}, cookie=f"{pre}; {dev}")
        call(self.url + "/api/logout", "POST", {}, cookie=f"{pre}; {dev}")            # "Start over"
        self.app.THROTTLE.ip_ok("127.0.0.1")
        pre = cookie_of(self.login(cookie=dev)[1], "fishbowl-pre")
        st, _, d = call(self.url + "/api/login/code", "POST", {"code": recovery[0]}, cookie=f"{pre}; {dev}")
        self.assertEqual(st, 200, d)
        self.assertNotIn("code", d.get("notice", {}), "the owner's own typo, in their own trusted browser")

    def test_cli_signout(self):
        c = self.create()
        out = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "app", "auth.py"), "signout"], capture_output=True, text=True,
                             env={**os.environ, "DATA_DIR": self.auth.DATA_DIR})
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertEqual(call(self.url + "/api/settings", cookie=c)[0], 401)
        self.assertIsNotNone(self.app.ACCOUNT.get(), "the account stays")

    def test_reading_the_request_has_a_deadline(self):
        import socket as so
        srv = self.app.Server(("127.0.0.1", 0), self.app.Handler)
        srv.READ_DEADLINE = 1.5
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        x = so.create_connection(("127.0.0.1", srv.server_address[1]))
        x.sendall(b"POST /api/login HTTP/1.1\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{")   # body never ends
        x.settimeout(10)
        t = time.monotonic()
        try:
            while x.recv(4096):
                pass
        except OSError:
            pass
        self.assertLess(time.monotonic() - t, 5, "a request still arriving after READ_DEADLINE is cut")
        x.close()

    def test_through_the_proxy_the_cap_counts_per_browser(self):
        srv = self.app.Server(("127.0.0.1", 0), self.app.Handler)
        self.addCleanup(srv.server_close)
        srv.MAX_PER_IP, srv._conn_lock = 2, threading.Lock()
        srv._conns = {object(): [0, "via 1.2.3.4", True], object(): [0, "via 1.2.3.4", True]}
        me = object()
        srv._conns[me] = [0, "127.0.0.1", True]
        self.assertFalse(srv.proxied(me, "1.2.3.4"))
        self.assertTrue(srv.proxied(me, "5.6.7.8"))

    def test_odd_requests_get_our_headers_and_bodies_are_utf8(self):
        import socket as so
        with so.create_connection(("127.0.0.1", int(self.url.rsplit(":", 1)[1]))) as x:
            x.sendall(b"GET /a b c d\r\n\r\n")
            reply = b""
            x.settimeout(5)
            while chunk := x.recv(4096):
                reply += chunk
        self.assertIn(b" 400 ", reply.split(b"\r\n")[0])
        self.assertIn(b"frame-ancestors 'none'", reply)
        u = urllib.parse.urlsplit(self.url)
        conn = http.client.HTTPConnection(u.hostname, u.port, timeout=30)
        conn.request("POST", "/api/login", body='{"user": "a"}'.encode("utf-16"), headers={"Content-Type": "application/json", "Origin": self.url})
        self.assertEqual(conn.getresponse().status, 400)
        conn.close()

    def test_cookies_forms_and_passwords(self):
        st, hd, _ = call(self.url + "/api/setup-account", "POST", {"code": self.setup_code(), "user": "admin", "password": PW})
        self.assertIn("Priority=High", hd["set-cookie"][0])
        page = open(os.path.join(ROOT, "app", "login.html")).read()
        self.assertEqual(page.count('method="post"'), 3, "a native submit never puts the password in the address")
        self.assertIsNotNone(self.auth.password_problem("plinth-marble-otter-͸"), "an unassigned character")
        self.assertFalse(self.app.SESSIONS.end_all(but_token="x" * 43), "this browser's own session is gone: nothing ended")


class RoundFive(Base):
    """The fifth review round's findings, one test each."""

    def test_a_code_typed_twice_is_used_not_wrong(self):
        c = self.create()
        st, _, d = call(self.url + "/api/account/totp/start", "POST", {"password": PW}, cookie=c)
        secret, step = d["secret"], int(time.time() // 30)
        st, h, _ = call(self.url + "/api/account/totp/confirm", "POST", {"code": self.auth.totp_code(secret, step)}, cookie=c)
        c = cookie_of(h)
        st, _, d = call(self.url + "/api/account/recovery", "POST", {"password": PW, "code": self.auth.totp_code(secret, step)}, cookie=c)
        self.assertEqual((st, d.get("field")), (403, "code"))
        self.assertIn("just used", d["error"])
        self.assertEqual(self.app.ACCOUNT.get()["fails"], [], "not a wrong code: nothing in Recent failed tries")
        pre = cookie_of(self.login()[1], "fishbowl-pre")
        st, _, d = call(self.url + "/api/login/code", "POST", {"code": self.auth.totp_code(secret, step)}, cookie=pre)
        self.assertEqual(st, 401)
        self.assertIn("just used", d["error"])
        self.assertNotIn("code", self.app.ACCOUNT.get()["since"], "nothing for the alarm")
        self.assertEqual(self.auth.totp_used(secret, "000000", step), False)

    def test_the_notice_link_is_readable(self):
        css = open(os.path.join(ROOT, "app", "style.css")).read()
        self.assertIn(".login-card a{", css.replace(".set-main a:not(.pill),.login-card a{", ".login-card a{"))


    def test_settings_failures_are_news_but_not_the_alarm(self):
        c = self.create()
        self.assertEqual(call(self.url + "/api/account/password", "POST", {"password": "not-the-password-x", "new": PW + "-2"}, cookie=c)[0], 403)
        self.app.THROTTLE.ip_ok("127.0.0.1")
        st, _, d = self.login()
        self.assertEqual(d.get("notice"), {"settings": 1})
        self.assertNotIn("code", self.app.ACCOUNT.get()["notice"])

    def test_that_was_me_keeps_what_came_after(self):
        c = self.create()
        self.app.ACCOUNT.update(lambda d: d.update(notice={"code": 3, "t": 1}))
        st, _, _ = call(self.url + "/api/account/notice/dismiss", "POST", {"password": PW, "seen": 2}, cookie=c)
        self.assertEqual(st, 200)
        self.assertEqual(self.app.ACCOUNT.get()["notice"]["code"], 1, "the one added after the page loaded stays")

    def test_signing_one_browser_out_from_a_browser_signed_out_meanwhile(self):
        t1 = self.app.SESSIONS.create(False, "ip", "ua", 1)[0]
        t2 = self.app.SESSIONS.create(False, "ip", "ua", 1)[0]
        sid = self.app.SESSIONS.listing(t2, 1)[0]["id"]
        self.app.SESSIONS.end(t1)
        self.assertIsNone(self.app.SESSIONS.end_id(sid, t1))
        self.assertIsNotNone(self.app.SESSIONS.check(t2, 1), "nothing ended")

    def test_account_answers_are_not_compressed(self):
        c = self.create()
        st, h, d = call(self.url + "/api/account/totp/start", "POST", {"password": PW}, cookie=c, headers={"Accept-Encoding": "gzip"})
        self.assertEqual(st, 200)
        self.assertNotIn("content-encoding", h, "a secret next to request-controlled text: never compressed")

    def test_a_full_table_cuts_the_oldest_slow_sender(self):
        import socket as so
        srv = self.app.Server(("127.0.0.1", 0), self.app.Handler)
        srv.MAX_CONN, srv.MAX_PER_IP = 3, 10
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        port = srv.server_address[1]
        slow = [so.create_connection(("127.0.0.1", port)) for _ in range(3)]
        for x in slow:
            x.sendall(b"POST /api/login HTTP/1.1\r\nContent-Length: 50\r\n\r\n{")
            time.sleep(0.2)
        st, _, _ = call(f"http://127.0.0.1:{port}/healthz")
        self.assertEqual(st, 200, "the owner still gets in")
        slow[0].settimeout(5)
        self.assertIn(slow[0].recv(10)[:10], (b"", b"HTTP/1.0 4"), "the oldest slow sender was cut to make room")
        for x in slow:
            x.close()


    def test_someone_elses_guessing_cant_outlast_the_code_step(self):
        c = self.create()
        st, _, d = call(self.url + "/api/account/totp/start", "POST", {"password": PW}, cookie=c)
        st, h, r = call(self.url + "/api/account/totp/confirm", "POST",
                        {"code": self.auth.totp_code(d["secret"], int(time.time() // 30))}, cookie=c)
        self.app.ACCOUNT.update(lambda x: x.update(failures=30, last_fail=int(time.time())))   # an outsider's guessing
        self.app.THROTTLE.ip_ok("127.0.0.1")
        self.app.ACCOUNT.update(lambda x: x.update(last_fail=0))   # (the owner waited out the password step's wait)
        pre = cookie_of(self.login()[1], "fishbowl-pre")
        self.app.ACCOUNT.update(lambda x: x.update(last_fail=int(time.time())))   # ...and the guessing goes on
        st, _, d2 = call(self.url + "/api/login/code", "POST", {"code": r["recovery"][0]}, cookie=pre)
        self.assertEqual(st, 200, d2)

    def test_without_x_real_ip_nobody_shares_a_small_bucket(self):
        app = load_app(AUTH="login", TRUSTED_PROXY="127.0.0.1")
        srv = app.Server(("127.0.0.1", 0), app.Handler)
        self.addCleanup(srv.server_close)
        srv.MAX_PER_IP, srv._conn_lock = 1, threading.Lock()
        srv._conns = {object(): [0, "via 127.0.0.1", True]}
        self.assertTrue(app.gap_hint(["X-Real-IP"]).startswith("Your reverse proxy doesn't send X-Real-IP"))
        self.assertNotIn("from outside your network is refused", app.gap_hint(["X-Real-IP"]))

    def test_http_09_gets_a_real_answer(self):
        import socket as so
        with so.create_connection(("127.0.0.1", int(self.url.rsplit(":", 1)[1]))) as x:
            x.sendall(b"GET /\r\n\r\n")
            x.settimeout(5)
            reply = b""
            while chunk := x.recv(4096):
                reply += chunk
        self.assertTrue(reply.startswith(b"HTTP/1.0 400"), reply[:40])


class SetupChecks(Base):
    def test_sign_in_items(self):
        ids = lambda **kw: {i["id"]: i for i in self.app.setup_checks(**kw)}
        self.create()
        st = ids(host="127.0.0.1", https=False)
        self.assertEqual(st["signin"]["state"], "ok")
        self.assertEqual(st["totp"]["state"], "warn", "two-step off: worth turning on")
        self.assertEqual(st["https"]["state"], "warn")
        self.assertNotIn("https", ids(host="127.0.0.1", https=True))
        with open(os.path.join(self.app.NPM_DIR, "1.conf"), "w") as f:
            f.write("server {\n  set $forward_scheme http;\n  set $server \"dash\";\n  set $port 8000;\n  server_name dash.example.com;\n"
                    "  include conf.d/include/assets.conf;\n  location / { include conf.d/include/proxy.conf; }\n}\n")
        self.app._npm["sig"] = None
        self.app.read_npm_hosts()
        self.assertEqual(ids(host="dash.example.com", https=True).get("cache", {}).get("state"), "warn")
        with open(self.auth.AUTH_FILE, "w") as f:
            f.write("{}")
        st, _, _ = call(self.url + "/api/settings")
        self.assertEqual(st, 401, "a damaged account file signs everyone out (the sign-in page says what to do)")


class AuditPlainHttp(Base):
    """The pre-release audit (2026-10-30), plain HTTP through a proxy: each finding's attack, refused."""
    env = {"AUTH": "login", "TRUSTED_PROXY": "127.0.0.1"}

    def test_a_plain_http_session_counts_only_from_the_address_it_was_made_on(self):
        # Browsers send cookies to every port of a host: over plain HTTP, any other web app on that address gets the
        # session cookie, and a hostile one could replay it from its own address on the same network.
        lan = {"X-Real-IP": "192.168.1.9", "X-Forwarded-Proto": "http"}
        st, h, _ = call(self.url + "/api/setup-account", "POST", {"code": self.setup_code(), "user": "admin", "password": PW},
                        headers=lan)
        self.assertEqual(st, 200)
        c = cookie_of(h)
        self.assertEqual(call(self.url + "/api/settings", cookie=c, headers=lan)[0], 200)
        self.assertEqual(call(self.url + "/api/settings", cookie=c, headers={**lan, "X-Real-IP": "192.168.1.66"})[0], 401,
                         "another address on the same network: no good")
        v6 = {"X-Real-IP": "fd00:1:2:3:aaaa::1", "X-Forwarded-Proto": "http"}
        st, h, _ = call(self.url + "/api/login", "POST", {"user": "admin", "password": PW}, headers=v6)
        self.assertEqual(st, 200)
        c6 = cookie_of(h)
        self.assertEqual(call(self.url + "/api/settings", cookie=c6, headers=v6)[0], 200)
        self.assertEqual(call(self.url + "/api/settings", cookie=c6, headers={**v6, "X-Real-IP": "fd00:1:2:3:bbbb::7"})[0], 401,
                         "the same /64 is a whole network, the host itself included (audit round 2): exact address only")

    def test_a_plain_http_trusted_device_survives_a_new_address_but_its_tries_there_are_no_typos(self):
        # A trusted browser stays trusted on another address (a laptop changing networks must not be locked out by
        # someone guessing: audit round 2); but wrong tries made with it from another address aren't forgiven as the
        # owner's typos at the owner's next sign-in (a copied cookie used elsewhere isn't the owner typing).
        lan = {"X-Real-IP": "192.168.1.9", "X-Forwarded-Proto": "http"}
        st, h, _ = call(self.url + "/api/setup-account", "POST", {"code": self.setup_code(), "user": "admin", "password": PW},
                        headers=lan)
        device = cookie_of(h, "fishbowl-device")
        self.app.ACCOUNT.update(lambda d: d.update(failures=150, last_fail=int(time.time())))   # someone hammered it
        login = lambda ip, pw=PW: call(self.url + "/api/login", "POST", {"user": "admin", "password": pw}, cookie=device,
                                       headers={**lan, "X-Real-IP": ip})
        self.assertEqual(login("192.168.1.66")[0], 200, "the owner's browser on a new address is still trusted")
        login("192.168.1.66", "wrong-password-from-the-copy")                 # the copied cookie, elsewhere
        self.app.ACCOUNT.update(lambda d: [v.update(last=0) for v in d["devices"].values()])   # (skip the device's wait)
        st, _, d = login("192.168.1.9")
        self.assertEqual(st, 200, d)
        self.assertEqual(d.get("notice", {}).get("password"), 1, "a wrong try from another address isn't a typo")

    def test_a_half_finished_sign_in_counts_only_where_it_started(self):
        # The code step's cookie goes to every port too: another app on the host could finish it, or use up its tries
        # and the owner's trusted browser's (audit round 2)
        lan = {"X-Real-IP": "192.168.1.9", "X-Forwarded-Proto": "http"}
        st, h, _ = call(self.url + "/api/setup-account", "POST", {"code": self.setup_code(), "user": "admin", "password": PW},
                        headers=lan)
        c = cookie_of(h)
        st, _, d = call(self.url + "/api/account/totp/start", "POST", {"password": PW}, cookie=c, headers=lan)
        st, _, _ = call(self.url + "/api/account/totp/confirm", "POST",
                        {"code": self.auth.totp_code(d["secret"], int(time.time() // 30))}, cookie=c, headers=lan)
        secret = d["secret"]
        pre = cookie_of(call(self.url + "/api/login", "POST", {"user": "admin", "password": PW}, headers=lan)[1], "fishbowl-pre")
        for _ in range(6):
            st, _, _ = call(self.url + "/api/login/code", "POST", {"code": "000000"}, cookie=pre,
                            headers={**lan, "X-Real-IP": "192.168.1.66"})
            self.assertEqual(st, 401)
        st, _, d = call(self.url + "/api/login/code", "POST", {"code": self.auth.totp_code(secret, int(time.time() // 30) + 1)},
                        cookie=pre, headers=lan)
        self.assertEqual(st, 200, "the owner's step wasn't used up from elsewhere")

    def test_signing_out_from_a_new_address_ends_the_session(self):
        lan = {"X-Real-IP": "192.168.1.9", "X-Forwarded-Proto": "http"}
        st, h, _ = call(self.url + "/api/setup-account", "POST", {"code": self.setup_code(), "user": "admin", "password": PW},
                        headers=lan)
        c = cookie_of(h)
        call(self.url + "/api/logout", "POST", {}, cookie=c, headers={**lan, "X-Real-IP": "192.168.1.66"})
        self.assertEqual(call(self.url + "/api/settings", cookie=c, headers=lan)[0], 401, "ended on the server, wherever from")


class Audit(Base):
    """The pre-release audit (2026-10-30): each finding's attack, refused."""

    def two_step(self):
        c = self.create()
        st, _, d = call(self.url + "/api/account/totp/start", "POST", {"password": PW}, cookie=c)
        st, h, r = call(self.url + "/api/account/totp/confirm", "POST",
                        {"code": self.auth.totp_code(d["secret"], int(time.time() // 30))}, cookie=c)
        self.assertEqual(st, 200)
        return cookie_of(h), d["secret"], r["recovery"], cookie_of(h, "fishbowl-device")

    def test_parallel_codes_on_one_step_are_five_at_most(self):
        self.two_step()
        pre = cookie_of(self.login()[1], "fishbowl-pre")
        before = self.app.ACCOUNT.get()["failures"]
        out = []
        ts = [threading.Thread(target=lambda i=i: out.append(call(self.url + "/api/login/code", "POST", {"code": f"{i:06d}"},
                                                                  cookie=pre)[0])) for i in range(30)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertLessEqual(self.app.ACCOUNT.get()["failures"] - before, 5, "five codes per password step, even all at once")

    def test_a_trusted_browsers_code_tries_stop_at_its_own_limit(self):
        _, _, _, dev = self.two_step()
        self.app.ACCOUNT.update(lambda d: [v.update(fails=self.auth.DEVICE_FAILS - 1) for v in d["devices"].values()])
        pre = cookie_of(self.login(cookie=dev)[1], "fishbowl-pre")
        self.assertTrue(pre)
        for i in range(4):
            call(self.url + "/api/login/code", "POST", {"code": f"{i:06d}"}, cookie=f"{pre}; {dev}")
        d = self.app.ACCOUNT.get()
        self.assertLessEqual(max(v.get("fails", 0) for v in d["devices"].values()), self.auth.DEVICE_FAILS)
        self.assertGreaterEqual(d["failures"], 3, "past its own 10, the device's tries count toward the account lock")

    def test_npm_that_lets_visitors_choose_their_address_is_a_setup_item(self):
        # Stock Nginx Proxy Manager believes a visitor's own X-Real-IP from any private address and passes on their
        # X-Forwarded-Proto (audit 2026-10-30): the Setup checklist says how to stop it on the dashboard's host.
        ids = lambda: {i["id"]: i for i in self.app.setup_checks(host="dash.example.com", https=True)}
        self.create()
        conf = os.path.join(self.app.NPM_DIR, "1.conf")

        def host(extra):
            with open(conf, "w") as f:
                f.write("server {\n  set $forward_scheme http;\n  set $server \"dash\";\n  set $port 8000;\n"
                        f"  server_name dash.example.com;\n{extra}  location / {{ include conf.d/include/proxy.conf; }}\n}}\n")
            self.app._npm["sig"] = None
            self.app.read_npm_hosts()
        host("")
        self.assertEqual(ids().get("npm-headers", {}).get("state"), "warn")
        host("  include conf.d/include/force-ssl.conf;\n  set_real_ip_from 127.0.0.2/32;\n")
        self.assertNotIn("npm-headers", ids())

    def test_a_new_browser_kept_waiting_is_told_why(self):
        # Someone else's guessing makes every new browser wait (on purpose: it stops guessing from many addresses);
        # the owner is told there that a browser they've used before isn't kept waiting (audit 2026-10-30).
        self.create()
        self.app.ACCOUNT.update(lambda d: d.update(failures=6, last_fail=int(time.time())))
        self.app.THROTTLE.ip_ok("127.0.0.1")
        st, _, d = self.login()
        self.assertEqual(st, 429)
        self.assertIn("signed in with before", d["error"])
        self.assertTrue(d.get("hint"), "the sign-in page shows this text instead of its own")

    def test_one_code_works_once_across_two_processes(self):
        # Two dashboards on one data folder: each checked "used before?" under its own lock only, so both could accept
        # the same code in the moment between one's check and the other's save (audit 2026-10-30).
        _, secret, _, _ = self.two_step()
        other = load_app(**self.env, DATA_DIR=os.path.dirname(self.auth.AUTH_FILE))
        epoch, before = self.app.ACCOUNT.get()["epoch"], other.ACCOUNT.get()   # (the other one read the account...)
        code = self.auth.totp_code(secret, int(time.time() // 30) + 1)
        self.assertEqual(self.app.Handler.code_ok(None, code, epoch), "ok")    # (...then this one used the code)
        real = other.ACCOUNT.get
        other.ACCOUNT.get = lambda: before
        try:
            self.assertEqual(other.Handler.code_ok(None, code, epoch), "used")
        finally:
            other.ACCOUNT.get = real

    def test_cli_signout_for_a_lost_device(self):
        # A lost laptop: the README says `auth.py signout`. It also has to forget that browser as trusted (its device
        # cookie skips the account's waits) and end a sign-in it had half finished (audit 2026-10-30).
        _, secret, _, dev = self.two_step()
        pre = cookie_of(self.login(cookie=dev)[1], "fishbowl-pre")      # the thief, password typed, code due
        self.assertTrue(pre)
        out = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "app", "auth.py"), "signout"], capture_output=True,
                             text=True, env={**os.environ, "DATA_DIR": self.auth.DATA_DIR})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.app.ACCOUNT.get().get("devices"), {}, "no browser is trusted any more")
        st, _, _ = call(self.url + "/api/login/code", "POST", {"code": self.auth.totp_code(secret, int(time.time() // 30) + 1)},
                        cookie=f"{pre}; {dev}")
        self.assertEqual(st, 401, "the half-finished sign-in can't be completed")

    def test_the_owners_right_code_forgives_nobody_elses_wrong_one(self):
        # The code step reserves its try before checking it (round 1): the right code's own try isn't a wrong code, or
        # the owner's next sign-in quietly forgave one wrong code someone else typed (audit round 2).
        _, secret, _, dev = self.two_step()
        self.app.THROTTLE.ip_ok("127.0.0.1")
        pre = cookie_of(self.login()[1], "fishbowl-pre")                          # someone with the password...
        call(self.url + "/api/login/code", "POST", {"code": "000000"}, cookie=pre)  # ...and one wrong code
        self.app.THROTTLE.ip_ok("127.0.0.1")
        pre = cookie_of(self.login(cookie=dev)[1], "fishbowl-pre")                # the owner, on their own browser
        st, _, d = call(self.url + "/api/login/code", "POST", {"code": self.auth.totp_code(secret, int(time.time() // 30) + 1)},
                        cookie=f"{pre}; {dev}")
        self.assertEqual(st, 200, d)
        self.assertEqual(d.get("notice", {}).get("code"), 1, "the wrong code is shown")


if __name__ == "__main__":
    unittest.main()
