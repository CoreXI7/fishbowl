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


def npm_conf(folder, name):
    with open(os.path.join(folder, f"{name}.conf"), "w") as f:
        f.write(f'server {{\n  server_name {name};\n  set $forward_scheme http;\n  set $server "127.0.0.1";\n  set $port 9;\n}}\n')


class ViaProxy(unittest.TestCase):
    def test_validation(self):
        app = load_app()
        ok = app.cfgstore.validate({"via_proxy": ["a.example.com", "b.example.com/alive", "c.example.com/x/y-z_1.html",
                                                  "a.example.com"]}, {})
        self.assertEqual(ok["via_proxy"], ["a.example.com", "b.example.com/alive", "c.example.com/x/y-z_1.html"])
        for bad in ["bad host", "a.example.com/x?y=1", "a.example.com/x#y", "a.example.com/../admin",
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
        finally:
            srv.shutdown()


class Setup(unittest.TestCase):
    def states(self, app):
        return {i["id"]: i["state"] for i in app.setup_checks()}

    def test_fresh_install(self):
        app = load_app()
        st = self.states(app)
        self.assertEqual(st["proxy"], "problem", "no TRUSTED_PROXY: anyone who reaches it can change settings")
        self.assertEqual(st["origin"], "warn")
        self.assertEqual(st["saving"], "ok")
        self.assertEqual(st["services"], "warn", "NPM folder mounted but empty")
        self.assertEqual(st["collector"], "todo")
        self.assertEqual(st["integrations"], "todo")
        self.assertNotIn("listen", st, "locking to the proxy only makes sense once TRUSTED_PROXY is set")
        steps = [s["code"] for i in app.setup_checks() for s in i["steps"]]
        self.assertIn("TRUSTED_PROXY: nginx", steps)
        self.assertIn("ORIGIN: %ORIGIN%", steps)
        self.assertIn("sudo bash collector/install.sh", steps)

    def test_no_npm_folder(self):
        app = load_app()
        app.NPM_DIR = os.path.join(app.TEST_TMP, "not-there")
        st = {i["id"]: i for i in app.setup_checks()}
        self.assertEqual(st["services"]["state"], "problem")
        self.assertIn(":" + app.NPM_DIR + ":ro", " ".join(s["code"] for s in st["services"]["steps"]))

    def test_all_set(self):
        app = load_app(TRUSTED_PROXY="127.0.0.1", ORIGIN="https://dash.example.com", LISTEN="127.0.0.1 10.250.81.2")
        npm_conf(app.NPM_DIR, "plex.example.com")
        with open(app.HW_FILE, "w") as f:
            json.dump({"updated": int(time.time())}, f)
        st = self.states(app)
        for k in ("proxy", "origin", "saving", "services", "collector", "listen"):
            self.assertEqual(st[k], "ok", k)
        with open(app.HW_FILE, "w") as f:
            json.dump({"updated": int(time.time()) - 3600}, f)
        os.utime(app.HW_FILE)                       # read_hw() re-reads on a changed mtime
        self.assertEqual(self.states(app)["collector"], "problem", "a collector that stopped writing is a problem")

    def test_listen_wildcard_is_not_locked(self):
        app = load_app(TRUSTED_PROXY="127.0.0.1", LISTEN="0.0.0.0")
        self.assertEqual(self.states(app)["listen"], "todo")

    def test_no_secrets_in_the_page_data(self):
        app = load_app(TRUSTED_PROXY="127.0.0.1")
        store = app.cfgstore.apply_secret_changes(app.ED, app.ED, {"qbit_user": "admin-user-x", "qbit_pass": "pass-word-x",
                                                                   "seerr_key": "seerr-key-x", "tautulli_key": "taut-key-x"}, app.STORE)
        app.cfgstore.save(app.ED, store)
        app.apply_settings()
        dumped = json.dumps(app.settings_view())
        for secret in ("admin-user-x", "pass-word-x", "seerr-key-x", "taut-key-x"):
            self.assertNotIn(secret, dumped)

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
            self.assertEqual(b.js("document.querySelector('#err-sum a').getAttribute('href')"), "#security")
            self.assertEqual(b.js("document.getElementById('f-known').getAttribute('aria-invalid')"), "true")
            self.assertEqual(b.csp_violations(), [])
        finally:
            b.close()
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
