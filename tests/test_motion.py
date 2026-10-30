"""Background motion: the dashboard default (site.motion) and the 🫧 button, with and without the device's
reduce-motion setting. One click must always flip what you see."""
import unittest

from browser import CHROME, Browser
from helpers import load_app, serve


class Motion(unittest.TestCase):
    def test_validation(self):
        app = load_app()
        self.assertEqual(app.cfgstore.validate({"site": {"motion": "on"}}, {})["site"]["motion"], "on")
        self.assertEqual(app.cfgstore.validate({"site": {"motion": "wobble"}}, {})["site"]["motion"], "auto")
        self.assertEqual(app.cfgstore.validate({"site": {}}, {})["site"]["motion"], "auto")

    def page(self, site_motion, reduce):
        app = load_app(DEMO="1")
        app.cfgstore.save(app.cfgstore.validate({**app.ED, "site": {**app.ED["site"], "motion": site_motion}}, {}), app.STORE)
        app.apply_settings()
        srv, url = serve(app.Handler)
        b = Browser()
        b.send("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-reduced-motion", "value": "reduce" if reduce else ""}]})
        b.send("Page.navigate", {"url": url + "/"})
        self.assertTrue(b.wait_for("document.title !== '' && !!document.querySelector('#svc .app')", 15))
        return b, srv

    def state(self, b):
        return b.js("document.documentElement.dataset.motion"), b.js("document.querySelectorAll('#bubbles i').length")

    @unittest.skipUnless(CHROME, "needs Chrome or Chromium")
    def test_device_reduce_and_one_click_flips(self):
        b, srv = self.page("auto", reduce=True)
        try:
            self.assertEqual(self.state(b), ("still", 0))      # follows the device
            b.js("document.getElementById('motion-btn').click()")
            self.assertEqual(self.state(b), ("move", 18))      # one click: on
            b.js("document.getElementById('motion-btn').click()")
            self.assertEqual(self.state(b), ("still", 0))      # one click: off
            b.js("document.getElementById('motion-btn').click()")
            self.assertEqual(self.state(b), ("move", 18))      # and back on, never a dead click
        finally:
            b.close(); srv.shutdown()

    @unittest.skipUnless(CHROME, "needs Chrome or Chromium")
    def test_site_default_on_wins_over_device(self):
        b, srv = self.page("on", reduce=True)
        try:
            self.assertEqual(self.state(b), ("move", 18))
        finally:
            b.close(); srv.shutdown()

    @unittest.skipUnless(CHROME, "needs Chrome or Chromium")
    def test_site_default_off(self):
        b, srv = self.page("off", reduce=False)
        try:
            self.assertEqual(self.state(b), ("still", 0))
        finally:
            b.close(); srv.shutdown()


if __name__ == "__main__":
    unittest.main()
