"""Service icons come from attacker-controllable services and are served on the dashboard's own origin (audit
2026-10-30): an SVG must never get through, whatever its spelling; the fetch must stay on the service's own origin (a
service URL with a path or query isn't a way to read other pages of that host); and anything passed through is a
sandboxed resource that other sites can't load."""
import http.client
import unittest

from helpers import Upstream, load_app, serve
from test_redirects import PNG, npm_conf

SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><foreignObject><form action="https://evil.example/"></form></foreignObject></svg>'


class Icons(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.svg = Upstream({"/": (200, {"Content-Type": "text/html"}, '<link rel="icon" href="/a.svg">'),
                            "/a.svg": (200, {"Content-Type": "image/SVG+xml"}, SVG),
                            "/favicon.ico": (200, {"Content-Type": "image/svg+xml "}, SVG)})
        cls.cam = Upstream({"/cam/latest.jpg": (200, {"Content-Type": "image/jpeg"}, b"\xff\xd8\xff secret frame")})
        cls.nice = Upstream({"/favicon.ico": (200, {"Content-Type": "image/png"}, PNG)})
        cls.app = load_app()
        npm_conf(cls.app.NPM_DIR, "svg.example", cls.svg.url)
        npm_conf(cls.app.NPM_DIR, "nice.example", cls.nice.url)

    @classmethod
    def tearDownClass(cls):
        for u in (cls.svg, cls.cam, cls.nice):
            u.close()

    def test_an_svg_never_gets_through(self):
        self.assertIsNone(self.app.favicon("svg.example"), "image/SVG+xml and 'image/svg+xml ' are SVG too")

    def test_only_the_services_own_origin(self):
        real = self.app.read_npm_hosts
        self.app.read_npm_hosts = lambda: [{"name": "cam.lan", "upstream": self.cam.url + "/cam/latest.jpg?x=", "auth": False}]
        try:
            self.assertIsNone(self.app.favicon("cam.lan"), "the camera's snapshot isn't the service's icon")
        finally:
            self.app.read_npm_hosts = real
        self.assertEqual(sorted(h[1] for h in self.cam.hits), ["/", "/favicon.ico"])

    def test_passed_through_answers_are_sandboxed(self):
        srv, url = serve(self.app.Handler)
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        c = http.client.HTTPConnection(url.split("//")[1])
        c.request("GET", "/api/icon?host=nice.example")
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 200)
        self.assertIn("sandbox", r.getheader("Content-Security-Policy"))
        self.assertEqual(r.getheader("Cross-Origin-Resource-Policy"), "same-origin", "other sites can't load it")

    def test_unknown_names_leave_nothing_behind(self):
        # (each unknown host= added a lock that was never removed: a page in the owner's browser could fill the
        # memory with /api/icon?host=<random>; audit round 2)
        before = len(self.app._icon_locks)
        for i in range(500):
            self.app.favicon(f"nope-{i}.example")
        self.assertEqual(len(self.app._icon_locks), before)

    def test_api_reads_from_other_sites_are_refused(self):
        # a sibling subdomain's page (same-site: the SameSite=Strict cookie goes along) or any other site can't make
        # the owner's browser read or time the API (audit round 2)
        srv, url = serve(self.app.Handler)
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        for site, want in (("same-site", 403), ("cross-site", 403), ("same-origin", 200), ("none", 200), (None, 200)):
            c = http.client.HTTPConnection(url.split("//")[1])
            c.request("GET", "/api/icon?host=nice.example", headers={"Sec-Fetch-Site": site} if site else {})
            r = c.getresponse()
            r.read()
            self.assertEqual(r.status, want, site)

    def test_pages_keep_their_window_to_themselves(self):
        srv, url = serve(self.app.Handler)
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        c = http.client.HTTPConnection(url.split("//")[1])
        c.request("GET", "/")
        r = c.getresponse()
        r.read()
        self.assertEqual(r.getheader("Cross-Origin-Opener-Policy"), "same-origin")


if __name__ == "__main__":
    unittest.main()
