"""Service icons come from attacker-controllable services and are served on the dashboard's own origin (audit
2026-10-30): an SVG must never get through, whatever its spelling; the fetch must stay on the service's own origin (a
service URL with a path or query isn't a way to read other pages of that host)."""
import unittest

from helpers import Upstream, load_app
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


if __name__ == "__main__":
    unittest.main()
