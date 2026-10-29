"""Outbound requests must not follow a redirect to another host: a hostile service could otherwise make the
dash fetch internal addresses (SSRF), return their content through /api/icon, or carry an API key there."""
import os
import unittest

from helpers import Upstream, load_app

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


def npm_conf(folder, name, upstream_url):
    host, port = upstream_url.split("//")[1].split(":")
    with open(os.path.join(folder, f"{name}.conf"), "w") as f:
        f.write(f'server {{\n  server_name {name};\n  set $forward_scheme http;\n  set $server "{host}";\n  set $port {port};\n}}\n')


class Redirects(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.internal = Upstream({"/secret.png": (200, {"Content-Type": "image/png"}, PNG),
                                 "/x": (200, {"Content-Type": "application/json"}, {"results": []})})
        cls.evil = Upstream({
            "/": (302, {"Location": cls.internal.url + "/secret.png"}, b""),
            "/favicon.ico": (302, {"Location": cls.internal.url + "/secret.png"}, b""),
            "/api/v1/request": (302, {"Location": cls.internal.url + "/x"}, b""),
        })
        cls.nice = Upstream({"/": (302, {"Location": "/login"}, b""), "/login": (200, {}, "<html></html>"),
                             "/favicon.ico": (301, {"Location": "/static/icon.png"}, b""),
                             "/static/icon.png": (200, {"Content-Type": "image/png"}, PNG)})
        cls.app = load_app(SEERR_URL=cls.evil.url, SEERR_KEY="the-key")
        npm_conf(cls.app.NPM_DIR, "evil.example", cls.evil.url)
        npm_conf(cls.app.NPM_DIR, "nice.example", cls.nice.url)

    @classmethod
    def tearDownClass(cls):
        for u in (cls.internal, cls.evil, cls.nice):
            u.close()

    def setUp(self):
        self.internal.hits.clear()

    def host(self, name):
        return next(h for h in self.app.read_npm_hosts() if h["name"] == name)

    def test_icon_does_not_follow_offsite_redirect(self):
        self.assertIsNone(self.app.favicon("evil.example"))
        self.assertEqual(self.internal.hits, [])

    def test_icon_follows_redirect_on_the_same_host(self):
        self.assertEqual(self.app.favicon("nice.example"), (PNG, "image/png"))

    def test_health_check_counts_a_redirect_as_up_without_following_it(self):
        r = self.app.check_once(self.host("evil.example"))
        self.assertEqual((r["up"], r["code"]), (True, 302))
        self.assertEqual(self.internal.hits, [])

    def test_api_key_never_follows_a_redirect(self):
        with self.assertRaises(Exception):
            self.app.requests_pending()
        self.assertEqual(self.internal.hits, [])


if __name__ == "__main__":
    unittest.main()
