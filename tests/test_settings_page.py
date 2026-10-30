"""The settings page holds everything a new install needs: a Setup checklist that says what's missing and how to fix
it, the "check through the proxy" list (was env-only), and a refused save that points at the section it's about."""
import json
import os
import time
import unittest
import urllib.error
import urllib.request

from browser import CHROME, Browser
from helpers import load_app, serve


def post(url, body):
    """POST JSON the way the page does (same origin). Returns (status, json)."""
    base = url.split("/api/")[0]
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Origin": base})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def npm_conf(folder, name, extra=""):
    with open(os.path.join(folder, f"{name}.conf"), "w") as f:
        f.write(f'server {{\n  server_name {name};\n  set $forward_scheme http;\n  set $server "127.0.0.1";\n  set $port 9;\n{extra}}}\n')


def collector_files(app, age=0):
    with open(app.HW_FILE, "w") as f:
        json.dump({"updated": int(time.time()) - age}, f)
    with open(app.SEC_DETAIL_FILE, "w") as f:
        json.dump({"updated": int(time.time()) - age}, f)
    os.utime(app.HW_FILE, (time.time() - age,) * 2)
    os.utime(app.SEC_DETAIL_FILE, (time.time() - age,) * 2)


class ViaProxy(unittest.TestCase):
    def test_validation(self):
        app = load_app()
        ok = app.cfgstore.validate({"via_proxy": ["a.example.com", "b.example.com/alive", "c.example.com/x/y-z_1.html",
                                                  "a.example.com"]}, {})
        self.assertEqual(ok["via_proxy"], ["a.example.com", "b.example.com/alive", "c.example.com/x/y-z_1.html"])
        for bad in ["bad host", "a.example.com/x?y=1", "a.example.com/x#y", "a.example.com/../admin", "a.example.com/%2E%2e/admin",
                    "a.example.com/x\r\nHost: evil", "https://a.example.com", "a.example.com/x y"]:
            with self.assertRaises(app.cfgstore.Invalid, msg=bad):
                app.cfgstore.validate({"via_proxy": [bad]}, {})
        with self.assertRaises(app.cfgstore.Invalid):
            app.cfgstore.validate({"via_proxy": [f"h{i}.example.com" for i in range(101)]}, {})

    def test_used_by_the_checks_and_env_wins(self):
        app = load_app()
        app.cfgstore.save(app.cfgstore.validate({**app.ED, "via_proxy": ["x.example.com/alive"]}, {}), app.STORE)
        app.apply_settings()
        self.assertEqual(app.VIA_PROXY, {"x.example.com": "/alive"})
        seen = []
        app.check_via_proxy = lambda host, path, timeout: seen.append((host["name"], path)) or {"up": True, "code": 200}
        app.check_once({"name": "x.example.com", "auth": False, "upstream": "http://127.0.0.1:9"})
        self.assertEqual(seen, [("x.example.com", "/alive")])
        app2 = load_app(CHECK_VIA_PROXY="y.example.com z.example.com/health")
        app2.cfgstore.save(app2.cfgstore.validate({**app2.ED, "via_proxy": ["x.example.com"]}, {}), app2.STORE)
        app2.apply_settings()
        self.assertEqual(app2.VIA_PROXY, {"y.example.com": "/", "z.example.com": "/health"}, "the environment wins")
        self.assertEqual(app2.settings_view()["via_proxy_env"], ["y.example.com", "z.example.com/health"])

    def test_env_lock_holds_on_save(self):
        """With CHECK_VIA_PROXY set, a save from the page can't store a list of its own (it would silently take over
        once the variable is removed)."""
        app = load_app(CHECK_VIA_PROXY="y.example.com")
        srv, url = serve(app.Handler)
        try:
            base = app.settings_view()["settings"]
            code, _ = post(url + "/api/settings", {"settings": {**base, "via_proxy": ["attacker.example.com/x"]}, "secrets": {}})
            self.assertEqual(code, 200)
            with open(os.path.join(app.cfgstore.DATA_DIR, "settings.json")) as f:
                self.assertEqual(json.load(f)["via_proxy"], [])
        finally:
            srv.shutdown()


class ErrorSection(unittest.TestCase):
    """A refused save says which part of the page it's about, so the page can take you there."""
    def test_sections(self):
        app = load_app()
        srv, url = serve(app.Handler)
        try:
            base = app.settings_view()["settings"]
            cases = {"security": {"known_ips": ["10.0.0"]},
                     "services": {"via_proxy": ["bad host"]},
                     "integrations": {"qbittorrent": {"url": "ftp://x", "home_ip": ""}},
                     "themes": {"site": {**base["site"], "tagline": "x" * 500}},
                     "look": {"site": {**base["site"], "title": "x" * 500}}}
            for section, change in cases.items():
                code, body = post(url + "/api/settings", {"settings": {**base, **change}, "secrets": {}})
                self.assertEqual(code, 400, section)
                self.assertEqual(body.get("section"), section, body)
            code, body = post(url + "/api/settings", {"settings": base, "secrets": {"qbit_user": 7}})
            self.assertEqual((code, body.get("section")), (400, "integrations"), body)
            code, body = post(url + "/api/settings", {"settings": base, "secrets": "x"})
            self.assertEqual(code, 400, "a malformed secrets object is refused clearly, not a 502")
        finally:
            srv.shutdown()


class Setup(unittest.TestCase):
    def states(self, app):
        return {i["id"]: i["state"] for i in app.setup_checks()}

    def test_fresh_install(self):
        app = load_app()
        st = self.states(app)
        self.assertEqual(st["proxy"], "warn", "no TRUSTED_PROXY: reachable without the proxy (its own sign-in still guards it)")
        self.assertEqual(st["origin"], "warn")
        self.assertEqual(st["saving"], "ok")
        self.assertEqual(st["services"], "warn", "NPM folder mounted but empty")
        self.assertEqual(st["collector"], "todo")
        self.assertEqual(st["integrations"], "todo")
        self.assertNotIn("listen", st, "locking to the proxy only makes sense once TRUSTED_PROXY is set")
        steps = [s["code"] for i in app.setup_checks() for s in i["steps"]]
        self.assertIn("TRUSTED_PROXY: <your proxy's container name>", steps, "a placeholder, not a name to copy blindly")
        self.assertIn("ORIGIN: %ORIGIN%", steps)
        self.assertIn("sudo bash collector/install.sh", steps)

    def test_no_npm_folder(self):
        app = load_app()
        app.NPM_DIR = os.path.join(app.TEST_TMP, "not-there")
        st = {i["id"]: i for i in app.setup_checks()}
        self.assertEqual(st["services"]["state"], "problem")
        self.assertTrue(st["services"]["alt"].startswith("Or, without NPM"), "an alternative, not step 2")
        self.assertIn(":" + app.NPM_DIR + ":ro", " ".join(s["code"] for s in st["services"]["steps"]))

    def test_all_set(self):
        app = load_app(TRUSTED_PROXY="127.0.0.1", ORIGIN="https://dash.example.com", LISTEN="127.0.0.1 10.89.250.2")
        npm_conf(app.NPM_DIR, "plex.example.com")
        collector_files(app)
        st = self.states(app)
        for k in ("proxy", "origin", "saving", "services", "collector", "listen"):
            self.assertEqual(st[k], "ok", k)
        os.remove(app.SEC_DETAIL_FILE)
        self.assertEqual(self.states(app)["collector"], "warn", "no security details: worth fixing")
        collector_files(app, age=3600)
        self.assertEqual(self.states(app)["collector"], "problem", "a collector that stopped writing is a problem")

    def test_collector_file_unreadable(self):
        app = load_app()
        collector_files(app)
        os.chmod(app.HW_FILE, 0)
        try:
            if os.access(app.HW_FILE, os.R_OK):
                self.skipTest("running as root: every file is readable")
            item = {i["id"]: i for i in app.setup_checks()}["collector"]
            self.assertEqual(item["state"], "problem", "installed but unreadable isn't 'not installed'")
            self.assertIn("dash_uid", " ".join(s["code"] for s in item["steps"]))
        finally:
            os.chmod(app.HW_FILE, 0o644)

    def test_services_count(self):
        """Hand-added services aren't counted twice, and hidden NPM hosts aren't counted as shown."""
        app = load_app()
        for n in ("a.example.com", "b.example.com", "c.example.com"):
            npm_conf(app.NPM_DIR, n)
        app.cfgstore.save(app.cfgstore.validate({**app.ED, "skip_hosts": ["c.example.com"],
                                                 "services": [{"name": "nas.lan", "url": "http://10.0.0.5:5000"}]}, {}), app.STORE)
        app.apply_settings()
        item = {i["id"]: i for i in app.setup_checks()}["services"]
        self.assertEqual(item["detail"], "2 from Nginx Proxy Manager (1 hidden), 1 added by hand.")
        app.cfgstore.save(app.cfgstore.validate({**app.ED, "skip_hosts": ["a.example.com", "b.example.com", "c.example.com"],
                                                 "services": []}, {}), app.STORE)
        app.apply_settings()
        item = {i["id"]: i for i in app.setup_checks()}["services"]
        self.assertEqual(item["state"], "ok")
        self.assertEqual(item["detail"], "0 from Nginx Proxy Manager (3 hidden).")
        # a hand-added service with the same name as an NPM host is still counted as hand-added, once
        app.cfgstore.save(app.cfgstore.validate({**app.ED, "skip_hosts": ["c.example.com"],
                                                 "services": [{"name": "a.example.com", "url": "http://10.0.0.5"}]}, {}), app.STORE)
        app.apply_settings()
        item = {i["id"]: i for i in app.setup_checks()}["services"]
        self.assertEqual(item["detail"], "2 from Nginx Proxy Manager (1 hidden), 1 added by hand.", "counted once, as hand-added")

    def test_js_built_in_names_are_refused(self):
        app = load_app()
        for bad in ("__proto__", "constructor", "prototype"):
            with self.assertRaises(app.cfgstore.Invalid, msg=bad):
                app.cfgstore.validate({"groups": [{"name": "g", "hosts": [bad]}]}, {})

    def test_npm_names_with_underscores_and_wildcards_can_be_grouped(self):
        app = load_app()
        v = app.cfgstore.validate({"groups": [{"name": "g", "hosts": ["my_app.example.com", "*.example.com"]}],
                                   "skip_hosts": ["*.example.com"]}, {})
        self.assertEqual(v["groups"][0]["hosts"], ["my_app.example.com", "*.example.com"])

    def test_sign_in(self):
        """The dashboard has no login of its own: its proxy host must have one."""
        app = load_app(TRUSTED_PROXY="127.0.0.1")
        npm_conf(app.NPM_DIR, "dash.example.com")
        sso = lambda host: {i["id"]: i for i in app.setup_checks(host)}.get("sso", {}).get("state")
        self.assertEqual(sso("dash.example.com"), "warn")
        self.assertEqual(sso("203.0.113.5:8000"), "todo", "opened directly: it says it couldn't check")
        npm_conf(app.NPM_DIR, "dash.example.com", "  location / {\n    include /snippets/authelia-authrequest.conf;\n  }\n")
        self.assertEqual(sso("dash.example.com:443"), "ok", "Authelia's NPM include snippet counts")
        npm_conf(app.NPM_DIR, "dash.example.com", '  auth_basic "Authorization required";\n')
        self.assertEqual(sso("DASH.example.com"), "ok", "an NPM access list counts too")
        npm_conf(app.NPM_DIR, "dash.example.com", "  auth_request /authelia;\n")
        self.assertEqual(sso("dash.example.com"), "ok")
        self.assertEqual(sso("other.example.com"), "todo", "not one of NPM's hosts: say it couldn't check")

    def test_sign_in_detection_isnt_fooled(self):
        """A commented-out or switched-off sign-in, one only on another path, or Authelia's endpoint snippet alone
        (it defines /internal/authelia but asks nothing) is no sign-in in front of the dashboard."""
        app = load_app()
        L = app.has_login
        self.assertTrue(L("server {\n  auth_request /authelia;\n  location = /authelia { internal; }\n  location / { proxy_pass x; }\n}"))
        self.assertTrue(L("server { location / { include /snippets/authelia-authrequest.conf; } }"))
        self.assertTrue(L('server { location / { auth_basic "Authorization required"; auth_basic_user_file /data/access/1; } }'))
        self.assertTrue(L("server { location ^~ / { auth_request /outpost.goauthentik.io/auth/nginx; } }"))
        self.assertFalse(L("server {\n  # auth_request /authelia;\n  location / { }\n}"))
        self.assertFalse(L("server { auth_request off; }"))
        self.assertFalse(L('server { auth_basic off; }'))
        self.assertFalse(L("server { location /admin { auth_request /authelia; } location / { } }"))
        self.assertFalse(L("server { include /snippets/authelia-location.conf; location / { } }"))
        self.assertFalse(L("server { location = / { auth_request /authelia; } }"), "= / covers only the bare /")
        self.assertFalse(L('server { auth_basic "off"; auth_request \'off\'; }'), "quoted off is off")
        self.assertFalse(L("server { auth_request /authelia; location / { auth_request off; } }"), "location / switches it off")
        self.assertFalse(L("server { location / { limit_except GET { auth_basic x; } } }"), "only for some methods")
        self.assertFalse(L('server { location /admin { add_header X "}"; auth_request /authelia; } location / { } }'),
                         "a brace inside quotes doesn't end the block")
        self.assertTrue(L('server { add_header X "a#b"; auth_request /authelia; }'), "a # inside quotes isn't a comment")

    def test_sign_in_parser_is_linear(self):
        app = load_app()
        t = time.monotonic()
        app.has_login("server { }\n" + "x" * 200_000)
        app.has_login("}\n" + "# c\n" * 50_000)
        app.has_login('server { x "' + '\\"' * 40_000)                      # an unclosed quote full of escaped quotes
        app.has_login("server { " + "location /a { " * 20_000 + "x; " * 20_000)   # deep nesting + many directives
        self.assertLess(time.monotonic() - t, 1.0, "a big unterminated tail must not take quadratic time")

    def test_integration_that_stopped_answering(self):
        app = load_app(QBIT_URL="http://127.0.0.1:9", QBIT_USER="u", QBIT_PASS="p")
        st = {i["id"]: i for i in app.setup_checks()}
        self.assertEqual(st["integrations"]["state"], "ok", "set up, never tried yet")
        app._answered["/api/qbit"] = (False, time.time())
        st = {i["id"]: i for i in app.setup_checks()}
        self.assertEqual(st["integrations"]["state"], "warn")
        self.assertIn("Torrents", st["integrations"]["detail"])
        app._answered["/api/qbit"] = (False, time.time() - 3600)
        self.assertEqual({i["id"]: i for i in app.setup_checks()}["integrations"]["state"], "ok", "an old failure is forgotten")
        app._answered["/api/qbit"] = (False, time.time())
        app.apply_settings()
        self.assertEqual({i["id"]: i for i in app.setup_checks()}["integrations"]["state"], "ok", "a save starts fresh")

    def test_test_button_explains(self):
        """Test says what a failed connection usually means instead of a Python error."""
        app = load_app(QBIT_URL="http://no-such-host.invalid:8080", QBIT_USER="u", QBIT_PASS="p")
        r = app.test_integration("qbittorrent")
        self.assertFalse(r["ok"])
        self.assertIn("Can't find 'no-such-host.invalid'", r["detail"])

    def test_listen_wildcard_is_not_locked(self):
        app = load_app(TRUSTED_PROXY="127.0.0.1", LISTEN="0.0.0.0", AUTH="login")
        self.assertEqual(self.states(app)["listen"], "todo")   # (with AUTH: proxy it's a warning: test_listen)

    def test_no_secrets_in_the_page_data(self):
        app = load_app(TRUSTED_PROXY="127.0.0.1")
        store = app.cfgstore.apply_secret_changes(app.ED, app.ED, {"qbit_user": "admin-user-x", "qbit_pass": "pass-word-x",
                                                                   "seerr_key": "seerr-key-x", "tautulli_key": "taut-key-x"}, app.STORE)
        app.cfgstore.save(app.ED, store)
        app.apply_settings()
        dumped = json.dumps(app.settings_view())
        for secret in ("admin-user-x", "pass-word-x", "seerr-key-x", "taut-key-x"):
            self.assertNotIn(secret, dumped)

    def test_a_half_written_save_never_sends_the_old_key_to_a_new_url(self):
        # (settings.json was written before secrets.json: a disk error between the two left a new URL paired with the
        # old key on the next start, the pairing apply_secret_changes refuses; audit round 3)
        app = load_app()
        store = app.cfgstore.apply_secret_changes(app.ED, app.ED, {"seerr_key": "old-key"}, app.STORE)
        app.cfgstore.save(app.cfgstore.validate({**app.ED, "seerr": {"url": "http://seerr:5055"}}, {}), store)
        app.apply_settings()
        new = app.cfgstore.validate({**app.ED, "seerr": {"url": "http://elsewhere:5055"}}, {})
        store = app.cfgstore.apply_secret_changes(new, app.ED, {"seerr_key": "typed-key"}, app.STORE)
        write, calls = app.cfgstore._write_json, []

        def full_disk(path, data):
            calls.append(path)
            if len(calls) == 2:
                raise OSError(28, "No space left on device")
            write(path, data)
        app.cfgstore._write_json = full_disk
        try:
            with self.assertRaises(OSError):
                app.cfgstore.save(new, store)
        finally:
            app.cfgstore._write_json = write
        url, key = app.cfgstore.load()["seerr"]["url"], app.cfgstore.load_secrets().get("seerr_key")
        self.assertFalse(url.startswith("http://elsewhere") and key == "old-key", (url, key))

    def test_demo(self):
        app = load_app(DEMO="1")
        st = self.states(app)
        self.assertNotIn("problem", st.values(), "the public demo shows a working setup")
        self.assertIn("collector", st)


@unittest.skipUnless(CHROME, "needs Chrome or Chromium")
class Page(unittest.TestCase):
    def test_page(self):
        app = load_app()
        srv, url = serve(app.Handler)
        b = Browser(1440, 900)
        try:
            b.send("Page.navigate", {"url": url + "/settings"})
            self.assertTrue(b.wait_for("!!document.querySelector('.chk')", 15), "the Setup checklist renders")
            self.assertEqual(b.js("document.getElementById('nb-setup').textContent"), "3")
            self.assertEqual(b.js("document.querySelector('.set-nav a[aria-current]').hash"), "#setup")
            b.js("(el => { el.value = '10.0.0'; el.dispatchEvent(new Event('input', {bubbles: true})); })(document.getElementById('f-known'))")
            b.js("document.getElementById('save').click()")
            self.assertTrue(b.wait_for("!document.getElementById('err-sum').hidden", 10), "a refused save shows the summary")
            self.assertEqual(b.js("document.querySelector('#err-sum a').getAttribute('href')"), "#f-known", "links to the field itself")
            self.assertEqual(b.js("document.getElementById('f-known').getAttribute('aria-invalid')"), "true")
            self.assertEqual(b.csp_violations(), [])
        finally:
            b.close()
            srv.shutdown()

class SettingsText(unittest.TestCase):
    """A lone surrogate ("\\ud800", which JSON allows) passed the text checks, and every save then failed halfway,
    leaving a half-written temp file in data/: a signed-in browser could fill the disk (audit, the fuzzer)."""

    def test_a_lone_surrogate_is_refused(self):
        app = load_app()
        with self.assertRaises(app.cfgstore.Invalid):
            app.cfgstore.validate({**app.ED, "site": {**app.ED["site"], "title": "x\ud800"}}, {})

    def test_a_lone_surrogate_in_a_collector_setting_is_refused(self):
        app = load_app()
        with self.assertRaises(app.cfgstore.Invalid):
            app.check_collector_changes({"security.bad_process_names": {"from": "", "to": ["x\ud800"]}})

    def test_a_save_reaches_the_disk(self):
        # (settings.json and secrets.json were replaced without fsync: a power cut could leave a torn file the dashboard
        # can't start with; audit, the fuzzer)
        app = load_app()
        synced, real = [], os.fsync
        os.fsync = lambda fd: (synced.append(fd), real(fd))
        try:
            app.cfgstore._write_json(app.cfgstore.SETTINGS_FILE, {"x": 1})
        finally:
            os.fsync = real
        self.assertGreaterEqual(len(synced), 2, "the file and its folder")

    def test_a_failed_write_leaves_no_temp_file(self):
        app = load_app()
        folder = os.path.dirname(app.cfgstore.SETTINGS_FILE)
        with self.assertRaises(Exception):
            app.cfgstore._write_json(app.cfgstore.SETTINGS_FILE, {"x": "\ud800"})
        self.assertEqual([f for f in os.listdir(folder) if f.startswith(".tmp-")], [])

class OddFields(unittest.TestCase):
    def test_non_text_fields_are_a_400_not_a_502(self):
        # (the fuzzer: /api/cron/approve and /api/settings/test answered 502 to {"hash": 5}; on the demo, without
        # signing in; audit 2026-10-30)
        import json as _json
        import urllib.request
        app = load_app()
        srv, url = serve(app.Handler)
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        for path, body in (("/api/cron/approve", {"hash": 5}), ("/api/settings/test", {"name": ["qbit"]})):
            req = urllib.request.Request(url + path, data=_json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "Origin": url})
            try:
                st = urllib.request.urlopen(req, timeout=10).status
            except urllib.error.HTTPError as e:
                st = e.code
            self.assertEqual(st, 400, path)

class IntegrationUrls(unittest.TestCase):
    """A page user (a stolen session, anyone past the SSO) must not read other things on the LAN through the
    integrations: a Tautulli URL of http://camera/snap.jpg# made /api/plexart return the camera's picture (urllib drops
    what follows #), and a Test of a service that isn't HTTP showed its first line (audit round 2)."""

    def test_integration_urls_have_no_query_fragment_or_user(self):
        app = load_app()
        for bad in ("http://camera/snap.jpg#", "http://camera/x?y=1", "http://me@camera/"):
            with self.assertRaises(app.cfgstore.Invalid, msg=bad):
                app.cfgstore.validate({**app.ED, "tautulli": {"url": bad}}, {})
        app.cfgstore.validate({**app.ED, "tautulli": {"url": "http://nas:8181/tautulli"}}, {})
        loaded = app.cfgstore.validate({**app.ED, "tautulli": {"url": "http://me@nas:8181/t?x=1#y"}}, None)   # (a saved file)
        self.assertEqual(loaded["tautulli"]["url"], "http://nas:8181/t", "an older saved file still loads, cleaned")

    def test_an_older_url_that_cant_be_cleaned_doesnt_stop_the_start(self):
        # (the cleaning raised ValueError for these, which the old URL rule accepted: the container exited at every start)
        app = load_app()
        for old in ("http://admin:pa#ss@qbit:8080/", "http://admin:p?ss@qbit:8080/", "http://tautulli:8181x/?apikey=abc",
                    "http://[fd00::5/?a"):
            loaded = app.cfgstore.validate({**app.ED, "tautulli": {"url": old}}, None)
            self.assertEqual(loaded["tautulli"]["url"], "", old)
        loaded = app.cfgstore.validate({**app.ED, "tautulli": {"url": "http://[fd00::5]:8181/?apikey=abc"}}, None)
        self.assertEqual(loaded["tautulli"]["url"], "http://[fd00::5]:8181/", "an IPv6 address keeps its brackets")

    def test_a_test_shows_no_text_from_a_service_that_isnt_http(self):
        import socketserver
        import threading

        class Banner(socketserver.StreamRequestHandler):
            def handle(self):
                self.wfile.write(b"SSH-2.0-OpenSSH_9.6 secret-banner\r\n")
        srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Banner)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        app = load_app(TAUTULLI_URL=f"http://127.0.0.1:{srv.server_address[1]}", TAUTULLI_KEY="k")
        r = app.test_integration("tautulli")
        self.assertFalse(r["ok"])
        self.assertNotIn("banner", r["detail"])

    def test_one_integration_test_at_a_time(self):
        # (Tests aren't cached and took turns on qBittorrent's lock: 80 at once from a page user held the connection
        # table for minutes, the owner refused; audit round 3)
        import threading
        import time
        import urllib.request
        app = load_app()
        started = threading.Event()

        def slow(name):
            started.set()
            time.sleep(1.5)
            return {"ok": False, "detail": "slow"}
        real = app.test_integration
        app.test_integration = slow
        self.addCleanup(lambda: setattr(app, "test_integration", real))
        srv, url = serve(app.Handler)
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))

        def post():
            req = urllib.request.Request(url + "/api/settings/test", data=json.dumps({"name": "qbit"}).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "Origin": url})
            try:
                return urllib.request.urlopen(req, timeout=10).status
            except urllib.error.HTTPError as e:
                return e.code
        first = []
        t = threading.Thread(target=lambda: first.append(post()))
        t.start()
        started.wait(5)
        self.assertEqual(post(), 429, "a second Test while one runs is turned away, not queued")
        t.join()
        self.assertEqual(first, [200])


class NewCode(unittest.TestCase):
    def test_code_changed_on_disk_asks_for_a_restart(self):
        # (the app is a bind mount: "git pull, then docker compose up -d" left the old code running, as Compose saw no
        # change; audit round 3)
        app = load_app()
        self.assertNotIn("restart", {i["id"] for i in app.setup_checks()})
        app._code_at_start = ()                       # (as if the files changed since it started)
        item = {i["id"]: i for i in app.setup_checks()}.get("restart")
        self.assertEqual((item or {}).get("state"), "warn")
        self.assertIn("--force-recreate", json.dumps(item))


if __name__ == "__main__":
    unittest.main()
