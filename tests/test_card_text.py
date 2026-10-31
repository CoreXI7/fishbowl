"""Long words in the cards (file paths in the backup log) wrap inside the card instead of running past its edge,
where the card cut them off."""
import unittest

from browser import CHROME, Browser
from helpers import load_app, serve


class CardText(unittest.TestCase):
    @unittest.skipUnless(CHROME, "needs Chrome or Chromium")
    def test_a_long_path_in_the_backup_log_wraps(self):
        app = load_app(DEMO="1")
        srv, url = serve(app.Handler)
        b = Browser(1440, 900)
        try:
            b.send("Page.navigate", {"url": url + "/"})
            self.assertTrue(b.wait_for("!!document.querySelector('details[data-k=\"backup-log\"] .mono')", 20))
            fits = b.js("""(() => { const d = document.querySelector('details[data-k="backup-log"]'); d.open = true;
                const m = d.querySelector('.mono');
                m.textContent = '2026-10-31 02:00:11 sqlite copy of /home/someone/Desktop/containers/' + 'x'.repeat(120) + '/db.sqlite3 ok';
                return m.scrollWidth <= m.clientWidth + 1; })()""")
            self.assertTrue(fits, "the path wraps inside the box")
        finally:
            b.close()
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
