"""Poster art comes through /api/plexart from Tautulli. A hostile Tautulli must not be able to serve script on the
dashboard's own origin (an SVG with <script>, or HTML/JS), so only raster image types pass, with a no-script CSP."""
import unittest
import urllib.error
import urllib.request

from helpers import Upstream, load_app, serve

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16


class PlexArt(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.answer = [(200, {"Content-Type": "image/png"}, PNG)]
        cls.up = Upstream({"/api/v2": lambda h: cls.answer[0]})
        cls.app = load_app(TAUTULLI_URL=cls.up.url, TAUTULLI_KEY="k")
        cls.srv, cls.url = serve(cls.app.Handler)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.up.close()

    def get(self):
        return urllib.request.urlopen(self.url + "/api/plexart?img=/library/metadata/1/thumb/2")

    def test_png_passes_with_a_no_script_csp(self):
        self.answer[0] = (200, {"Content-Type": "image/png"}, PNG)
        r = self.get()
        self.assertEqual(r.headers["Content-Type"], "image/png")
        self.assertEqual(r.headers["Content-Security-Policy"], "default-src 'none'")

    def test_script_types_are_refused(self):
        for ctype, body in (("image/svg+xml", b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"),
                            ("text/html", b"<script>alert(1)</script>"), ("text/javascript", b"alert(1)"),
                            ("application/octet-stream", b"alert(1)")):
            self.answer[0] = (200, {"Content-Type": ctype}, body)
            with self.assertRaises(urllib.error.HTTPError, msg=ctype) as e:
                self.get()
            self.assertEqual(e.exception.code, 404, ctype)


if __name__ == "__main__":
    unittest.main()
