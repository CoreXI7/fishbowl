"""Strict Content-Security-Policy: scripts only from the dashboard's own files, no inline script or inline
event handlers anywhere, so even markup that slipped into the page can't run anything."""
import glob
import os
import re
import unittest
import urllib.request

from browser import CHROME, Browser
from helpers import APP_DIR, load_app, serve

INLINE_HANDLER = re.compile(r"""<[^>]*\son[a-z]+\s*=""", re.I)


class CSP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = load_app(DEMO="1")
        cls.srv, cls.url = serve(cls.app.Handler)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_header_has_no_unsafe_inline_script(self):
        for path in ("/", "/settings"):
            csp = urllib.request.urlopen(self.url + path).headers["Content-Security-Policy"]
            script = next(d for d in csp.split(";") if d.strip().startswith("script-src"))
            self.assertEqual(script.split()[1:], ["'self'"], path)

    def test_no_inline_script_or_handlers_in_html(self):
        for page in ("index.html", "settings.html"):
            html = open(os.path.join(APP_DIR, page)).read()
            self.assertEqual(re.findall(r"<script(?![^>]*\ssrc=)[^>]*>", html), [], page)
            self.assertEqual(INLINE_HANDLER.findall(html), [], page)

    def test_no_inline_handlers_built_by_the_scripts(self):
        for js in glob.glob(os.path.join(APP_DIR, "js", "*.js")):
            self.assertEqual(INLINE_HANDLER.findall(open(js).read()), [], os.path.basename(js))

    @unittest.skipUnless(CHROME, "needs Chrome or Chromium")
    def test_page_runs_without_csp_violations(self):
        b = Browser()
        try:
            for path in ("/?wall", "/settings"):
                b.events.clear()
                b.send("Page.navigate", {"url": self.url + path})
                self.assertTrue(b.wait_for("document.readyState === 'complete' && !document.querySelector('.empty') "
                                           "|| location.pathname === '/settings' && !!document.querySelector('#integ input')", 20), path)
                self.assertEqual(b.csp_violations(), [], path)
            b.send("Page.navigate", {"url": self.url + "/"})
            b.wait_for("!!document.querySelector('#dl .tor')")
            b.js("document.getElementById('t-seed').click()")
            self.assertTrue(b.js("document.getElementById('dl').hidden && !document.getElementById('seed').hidden"),
                            "the torrent tabs work without inline onclick")
            b.js("""(() => { const i = document.createElement('img'); i.dataset.letter = 'Z'; i.src = '/static/nope.png';
                             document.getElementById('svc').appendChild(i); })()""")
            self.assertTrue(b.wait_for("[...document.querySelectorAll('#svc .lt')].some(d => d.textContent === 'Z')", 5),
                            "a broken icon falls back to its letter without inline onerror")
        finally:
            b.close()


if __name__ == "__main__":
    unittest.main()
