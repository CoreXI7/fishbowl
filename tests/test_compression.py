"""JSON, CSS, JS and HTML go out gzip-compressed when the browser accepts it (the proxy only compresses HTML).
No response carries a secret, so compression can't leak one (BREACH needs a secret next to reflected input)."""
import gzip
import json
import unittest
import urllib.request

from helpers import load_app, serve


class Compression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = load_app(DEMO="1")
        cls.srv, cls.url = serve(cls.app.Handler)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def get(self, path, enc):
        req = urllib.request.Request(self.url + path, headers={"Accept-Encoding": enc} if enc else {})
        with urllib.request.urlopen(req) as r:
            return r.headers, r.read()

    def test_json_is_gzipped(self):
        h, body = self.get("/api/system", "gzip, deflate, br")
        self.assertEqual(h["Content-Encoding"], "gzip")
        self.assertIn("Accept-Encoding", h["Vary"])
        plain_h, plain = self.get("/api/system", None)
        self.assertIsNone(plain_h["Content-Encoding"])
        self.assertLess(len(body), len(plain) / 2)
        self.assertEqual(json.loads(gzip.decompress(body)).keys(), json.loads(plain).keys())

    def test_static_text_is_gzipped(self):
        for path in ("/", "/style.css", "/js/core.js"):
            h, body = self.get(path, "gzip")
            self.assertEqual(h["Content-Encoding"], "gzip", path)
            self.assertTrue(gzip.decompress(body))

    def test_images_and_refusals_are_not(self):
        h, _ = self.get("/static/favicon.svg", "gzip")      # tiny + already compact; images are never compressed
        self.assertIsNone(h["Content-Encoding"])
        h, _ = self.get("/api/system", "gzip;q=0, identity")
        self.assertIsNone(h["Content-Encoding"])


if __name__ == "__main__":
    unittest.main()
