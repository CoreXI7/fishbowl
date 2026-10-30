"""The settings page's side of the collector's settings: it shows what the collector reports (hw.json's "collector"
block), hands what you changed to the collector as a request file, and says in Setup what waits for `--apply`."""
import hashlib
import json
import os
import time
import unittest

from helpers import load_app, serve
from test_settings_page import post

_tick = [0]


def hw(app, collector=None):
    with open(app.HW_FILE, "w") as f:
        json.dump({"updated": int(time.time()), **({"collector": collector} if collector is not None else {})}, f)
    with open(app.SEC_DETAIL_FILE, "w") as f:
        json.dump({"updated": int(time.time())}, f)
    _tick[0] += 1
    t = time.time() + _tick[0]
    os.utime(app.HW_FILE, (t, t))   # read_hw() re-reads on a changed mtime


BLOCK = {"v": 3, "detected": {"mounts": ["/"], "ifaces": ["eth0"], "jails": ["sshd"]}, "settings": {"interval": 5},
         "from_page": [], "pending": [], "pending_values": {}, "rejected": [], "rejected_more": 0, "error": "", "last": ""}


def ch(frm, to):
    """frm = the fingerprint of the value the page showed ("" = not set)."""
    return {"from": frm, "to": to}


def load(**env):
    app = load_app(**env)
    app.COLLECTOR_WAIT = 0   # no collector runs in the tests: a save doesn't wait for one
    return app


def changes_in(app):
    with open(app.COLLECTOR_REQ) as f:
        d = json.load(f)
    assert d["v"] == 3 and len(d["id"]) == 16, d
    return d["changes"]


class CollectorSettings(unittest.TestCase):
    def states(self, app):
        return {i["id"]: i for i in app.setup_checks()}

    def test_view(self):
        app = load()
        self.assertEqual(app.collector_view()["installed"], False)
        hw(app)
        v = app.collector_view()
        self.assertEqual((v["installed"], v["supported"]), (True, False), "an older collector: no block")
        self.assertEqual(self.states(app)["collector_page"]["state"], "todo")
        for old in (1, 2):
            hw(app, {**BLOCK, "v": old})
            self.assertFalse(app.collector_view()["supported"], "the drafts' formats aren't spoken any more")
        hw(app, BLOCK)
        v = app.collector_view()
        self.assertTrue(v["supported"])
        self.assertEqual(v["detected"]["ifaces"], ["eth0"])
        self.assertNotIn("collector_page", self.states(app))

    def test_pending_and_refused_show_in_setup(self):
        app = load()
        hw(app, {**BLOCK, "pending": ["backup.log", "security.authelia_db"], "rejected": [["net_if", "isn't one found"]]})
        st = self.states(app)
        self.assertEqual(st["collector_pending"]["state"], "warn")
        self.assertIn("2 collector settings wait", st["collector_pending"]["title"])
        self.assertIn("sudo homelab-dash-collector --apply", [s["code"] for s in st["collector_pending"]["steps"]])
        self.assertIn("Network interface", st["collector_refused"]["detail"])

    def test_save_writes_the_changes(self):
        app = load()
        hw(app, BLOCK)
        srv, url = serve(app.Handler)
        try:
            base = app.settings_view()["settings"]
            changes = {"interval": ch("0123456789abcdef", 9), "security.fail2ban_jail": ch("", "sshd"), "backup.log": ch("", "/var/log/b.log")}
            code, body = post(url + "/api/settings", {"settings": base, "secrets": {}, "collector": changes})
            self.assertEqual(code, 200, body)
            self.assertEqual(changes_in(app), changes)
            self.assertEqual(body["collector"]["wanted"], {k: c["to"] for k, c in changes.items()},
                             "the page shows what it sent until the collector has taken it")
            for bad in ("x", {"x": ch("", 1)}, {"interval": {"to": 9}}, {"interval": ch("", None)}, {"interval": ch("", "9")},
                        {"interval": ch(5, 9)}, {"interval": ch(None, 9)}, {"interval": ch("XYZ", 9)}, {"gpu": ch("", 1)},
                        {"backup.jobs": ch("", [{"files": "/b"}])}, {"__proto__": ch("", 1)}):
                code, body = post(url + "/api/settings", {"settings": base, "secrets": {}, "collector": bad})
                self.assertEqual((code, body.get("section")), (400, "collector"), bad)
            code, _ = post(url + "/api/settings", {"settings": base, "secrets": {}, "collector": {"mounts": ch("", ["y" * 300_000])}})
            self.assertEqual(code, 400, "too big")
            self.assertEqual(changes_in(app), changes, "a refused save leaves the last request alone")
            # a save without a "collector" part doesn't touch the request
            mtime = os.stat(app.COLLECTOR_REQ).st_mtime_ns
            post(url + "/api/settings", {"settings": base, "secrets": {}})
            self.assertEqual(os.stat(app.COLLECTOR_REQ).st_mtime_ns, mtime)
        finally:
            srv.shutdown()

    def test_unhandled_changes_stay_until_the_collector_took_them(self):
        app = load()
        hw(app, BLOCK)
        srv, url = serve(app.Handler)
        try:
            base = app.settings_view()["settings"]
            post(url + "/api/settings", {"settings": base, "secrets": {}, "collector": {"interval": ch("", 9), "gpu": ch("", False)}})
            post(url + "/api/settings", {"settings": base, "secrets": {}, "collector": {"interval": ch("", 7)}})
            self.assertEqual(changes_in(app), {"interval": ch("", 7), "gpu": ch("", False)}, "not taken yet: kept")
            with open(app.COLLECTOR_REQ, "rb") as f:
                hw(app, {**BLOCK, "last": hashlib.sha256(f.read()).hexdigest()})   # the collector took it
            self.assertEqual(app.collector_view()["wanted"], {})
            post(url + "/api/settings", {"settings": base, "secrets": {}, "collector": {"usage": ch("", False)}})
            self.assertEqual(changes_in(app), {"usage": ch("", False)})
        finally:
            srv.shutdown()

    def test_demo_never_writes(self):
        app = load(DEMO="1")
        srv, url = serve(app.Handler)
        try:
            base = app.settings_view()["settings"]
            code, _ = post(url + "/api/settings", {"settings": base, "secrets": {}, "collector": {"interval": ch("", 9)}})
            self.assertEqual(code, 200)
            self.assertFalse(os.path.exists(app.COLLECTOR_REQ))
            self.assertTrue(app.collector_view()["supported"], "the demo shows fake collector settings")
        finally:
            srv.shutdown()

    def test_read_only_never_hands_over(self):
        app = load(SETTINGS_UI="0")
        with self.assertRaises(app.cfgstore.Invalid):
            app.check_collector_changes({"interval": ch("", 9)})
        self.assertFalse(os.path.exists(app.COLLECTOR_REQ))

    def test_a_refused_save_hands_nothing_over(self):
        app = load()
        hw(app, BLOCK)
        srv, url = serve(app.Handler)
        try:
            bad = {**app.settings_view()["settings"], "known_ips": ["not an ip"]}
            code, _ = post(url + "/api/settings", {"settings": bad, "secrets": {}, "collector": {"interval": ch("", 9)}})
            self.assertEqual(code, 400)
            self.assertFalse(os.path.exists(app.COLLECTOR_REQ), "the collector's part waits for the dashboard's save")
        finally:
            srv.shutdown()

    def test_view_keeps_only_sane_shapes(self):
        app = load()
        hw(app, {"v": 3, "detected": ["x"], "settings": {"interval": "5", "gpu": True, "x": 1}, "from_page": ["a", 3, None],
                 "pending": "backup.log", "pending_values": {"backup.log": 5, "backup.jobs": [{"label": "x"}]},
                 "rejected": [["net_if", "why"], "junk", [1, 2, 3]], "rejected_more": "9", "error": "e" * 1000,
                 "sandbox": "strict" * 20, "last": 5})
        v = app.collector_view()
        self.assertEqual((v["detected"]["mounts"], v["settings"], v["from_page"], v["pending"]), ([], {"gpu": True}, ["a"], []))
        self.assertEqual(v["pending_values"], {"backup.jobs": [{"label": "x"}]})
        self.assertEqual((v["rejected"], v["rejected_more"]), ([["net_if", "why"]], 0))
        self.assertEqual((len(v["error"]), len(v["sandbox"])), (300, 40))
        with open(app.COLLECTOR_REQ, "w") as f:   # a request file the page can't have written
            json.dump({"v": 3, "changes": {"interval": ch("", "9")}}, f)
        self.assertEqual(app.collector_view()["wanted"], {})
        app.setup_checks()   # and Setup still works with all of it

    def test_detected_is_sanitised(self):
        app = load()
        hw(app, {**BLOCK, "detected": {"ifaces": "eth0", "mounts": ["/", 5], "authelia_db": {"a": 1, "b": "/x"}, "gpu": "yes"}})
        d = app.collector_view()["detected"]
        self.assertEqual((d["ifaces"], d["mounts"], d["authelia_db"], d["gpu"]), ([], ["/"], {"b": "/x"}, False))

    def test_a_failed_hand_over_keeps_the_dashboards_save(self):
        app = load()
        hw(app, BLOCK)
        os.chmod(app.REQ_DIR, 0o500)
        srv, url = serve(app.Handler)
        try:
            base = {**app.settings_view()["settings"]}
            base["site"] = {**base["site"], "title": "new title"}
            code, body = post(url + "/api/settings", {"settings": base, "secrets": {}, "collector": {"interval": ch("", 9)}})
            self.assertEqual(code, 200, body)
            self.assertIn("couldn't be handed over", body["collector_warning"])
            self.assertEqual(app.settings_view()["settings"]["site"]["title"], "new title", "in use, not only on disk")
        finally:
            os.chmod(app.REQ_DIR, 0o700)
            srv.shutdown()

    def test_collector_error_shows_in_setup(self):
        app = load()
        hw(app, {**BLOCK, "error": "collector.page.toml can't be used (TOMLDecodeError)"})
        self.assertIn("TOMLDecodeError", self.states(app)["collector_error"]["detail"])

    def test_sandbox_in_setup(self):
        app = load()
        hw(app, {**BLOCK, "sandbox": "none"})
        self.assertEqual(self.states(app)["sandbox"]["state"], "todo")
        hw(app, {**BLOCK, "sandbox": "strict"})
        self.assertEqual(self.states(app)["sandbox"]["state"], "ok")
        hw(app, BLOCK)
        self.assertNotIn("sandbox", self.states(app), "sandbox.sh never ran: nothing to say")

    def test_pending_links_to_its_section(self):
        app = load()
        hw(app, {**BLOCK, "pending": ["backup.jobs"], "rejected": [["security.ssh_unit", "not found"]]})
        st = self.states(app)
        self.assertEqual(st["collector_pending"]["link"], "#backups")
        self.assertIn("Backup jobs", st["collector_pending"]["detail"])
        self.assertEqual(st["collector_refused"]["link"], "#security")
        self.assertIn("Where SSH logins are logged", st["collector_refused"]["detail"])


if __name__ == "__main__":
    unittest.main()
