"""Panels can be switched off on the settings page (site.hidden_panels); their summary tiles stay."""
import unittest

from browser import CHROME, Browser
from helpers import load_app, serve


class Panels(unittest.TestCase):
    def test_validation(self):
        app = load_app()
        v = app.cfgstore.validate({"site": {"hidden_panels": ["drives", "plex", "drives", "nope"]}}, {})
        self.assertEqual(v["site"]["hidden_panels"], ["drives", "plex"])
        with self.assertRaises(app.cfgstore.Invalid):
            app.cfgstore.validate({"site": {"hidden_panels": "drives"}}, {})

    @unittest.skipUnless(CHROME, "needs Chrome or Chromium")
    def test_page_hides_them(self):
        app = load_app(DEMO="1")
        app.cfgstore.save(app.cfgstore.validate({**app.ED, "site": {**app.ED["site"], "hidden_panels": ["drives", "plex", "mascot"]}}, {}), app.STORE)
        app.apply_settings()
        srv, url = serve(app.Handler)
        b = Browser()
        try:
            b.send("Page.navigate", {"url": url + "/"})
            self.assertTrue(b.wait_for("!!document.querySelector('#req .req')", 15))
            shown = lambda sel: b.js(f"(e => !!e && e.getClientRects().length > 0)(document.querySelector('{sel}'))")
            self.assertFalse(shown("#s-drv"))
            self.assertFalse(shown("#s-plex"))
            self.assertFalse(shown("#mascot"))
            self.assertTrue(shown("#s-req"))
            self.assertTrue(shown("#k-plex"), "the summary tile stays")
            self.assertTrue(shown("#k-disk"))
            req_w, main_w = b.js("[document.getElementById('s-req').offsetWidth, document.querySelector('.duo').offsetWidth]")
            self.assertGreater(req_w, main_w * 0.9, "Requests takes the whole row when Plex is hidden")
        finally:
            b.close()
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
