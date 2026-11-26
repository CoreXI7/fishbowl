"""GitHub issue #23: the watching tile is named after the servers that are set up ("Plex", "Jellyfin" or
"Plex & Jellyfin"), and each stream gets a source chip only when both are set up."""
import unittest

from browser import CHROME, Browser
from helpers import Upstream, load_app, serve
from test_issues_17_20 import KEY, SESSION

TAUTULLI = {"response": {"result": "success", "data": {
    "stream_count": 1, "stream_count_transcode": 0, "total_bandwidth": 5000,
    "sessions": [{"session_key": "1", "friendly_name": "bob", "full_title": "A Movie", "title": "A Movie",
                  "media_type": "movie", "state": "playing", "transcode_decision": "direct play", "location": "lan",
                  "bandwidth": 5000, "progress_percent": 10, "duration": 6000000, "view_offset": 600000}]}}}

SOURCES = "[...document.querySelectorAll('#plex .chip')].map(x => x.textContent).filter(t => t === 'Plex' || t === 'Jellyfin').sort()"


@unittest.skipUnless(CHROME, "needs Chrome or Chromium")
class TileAndChips(unittest.TestCase):
    def setUp(self):
        self.jf = Upstream({"/Sessions": (200, {}, [SESSION])})
        self.tt = Upstream({"/api/v2": (200, {}, TAUTULLI)})
        self.addCleanup(self.jf.close)
        self.addCleanup(self.tt.close)

    def page(self, **env):
        app = load_app(**env)
        srv, url = serve(app.Handler)
        b = Browser(1440, 900)
        self.addCleanup(srv.shutdown)
        self.addCleanup(b.close)
        b.send("Page.navigate", {"url": url + "/"})
        self.assertTrue(b.wait_for("!!document.querySelector('#plex .stream, #plex .card, #plex .chip')", 20), "Now Playing fills")
        return b

    def test_both_servers(self):
        b = self.page(JELLYFIN_URL=self.jf.url, JELLYFIN_KEY=KEY, TAUTULLI_URL=self.tt.url, TAUTULLI_KEY="t")
        self.assertEqual(b.js("$('k-plex-l').textContent"), "🎬 Plex & Jellyfin")
        self.assertEqual(b.js(SOURCES), ["Jellyfin", "Plex"], "a source chip on both streams")

    def test_jellyfin_only(self):
        b = self.page(JELLYFIN_URL=self.jf.url, JELLYFIN_KEY=KEY)
        self.assertEqual(b.js("$('k-plex-l').textContent"), "🎬 Jellyfin")
        self.assertEqual(b.js(SOURCES), [], "no source chips with one server")

    def test_plex_only(self):
        b = self.page(TAUTULLI_URL=self.tt.url, TAUTULLI_KEY="t")
        self.assertEqual(b.js("$('k-plex-l').textContent"), "🎬 Plex")
        self.assertEqual(b.js(SOURCES), [], "no source chips with one server")


if __name__ == "__main__":
    unittest.main()
