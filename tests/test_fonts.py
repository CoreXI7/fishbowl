"""Fonts are served by the dashboard itself: no request to Google (privacy, works offline), and the CSP
allows fonts and styles from the dashboard only."""
import os
import re
import unittest
import urllib.error
import urllib.request

from browser import CHROME, Browser
from helpers import APP_DIR, load_app, serve


class Fonts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = load_app(DEMO="1")
        cls.srv, cls.url = serve(cls.app.Handler)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_no_third_party_fonts(self):
        for page in ("index.html", "settings.html"):
            self.assertFalse(re.search(r"fonts\.(googleapis|gstatic)\.com", open(os.path.join(APP_DIR, page)).read()), page)
        csp = urllib.request.urlopen(self.url + "/").headers["Content-Security-Policy"]
        directives = dict((d.split()[0], d.split()[1:]) for d in csp.split(";") if d.strip())
        self.assertEqual(directives["font-src"], ["'self'"])
        self.assertNotIn("https://fonts.googleapis.com", directives["style-src"])

    def test_font_files_served(self):
        css = urllib.request.urlopen(self.url + "/fonts.css").read().decode()
        files = re.findall(r"url\((/static/fonts/[\w.-]+\.woff2)\)", css)
        self.assertEqual(len(set(files)), 5)
        for f in files:
            r = urllib.request.urlopen(self.url + f)
            self.assertEqual(r.headers["Content-Type"], "font/woff2")
            self.assertEqual(r.read(4), b"wOF2")

    def test_uploads_never_serve_fonts_or_subfolders(self):
        for path in ("/uploads/x.woff2", "/uploads/fonts/x.woff2", "/static/../app.py", "/static/fonts/../../app.py"):
            with self.assertRaises(urllib.error.HTTPError, msg=path) as e:
                urllib.request.urlopen(self.url + path)
            self.assertIn(e.exception.code, (400, 404), path)

    @unittest.skipUnless(CHROME, "needs Chrome or Chromium")
    def test_fonts_load_in_the_browser(self):
        b = Browser()
        try:
            b.send("Page.navigate", {"url": self.url + "/"})
            self.assertTrue(b.wait_for("document.fonts.status === 'loaded' && document.fonts.check('600 16px Fredoka') "
                                       "&& [...document.fonts].some(f => f.family.includes('Fredoka') && f.status === 'loaded')", 15))
            self.assertEqual(b.csp_violations(), [])
        finally:
            b.close()


if __name__ == "__main__":
    unittest.main()
