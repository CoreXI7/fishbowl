"""The root collector only changes when someone runs collector/install.sh again; a stranger updating the dashboard
with git pull would keep a collector with known problems and never know (audit round 2). The collector writes its
build into hw.json, and the Setup checklist asks for the update when it's older than the dashboard expects."""
import json
import os
import time
import unittest

from helpers import load_app
from test_collector_requests import load_collector


class CollectorBuild(unittest.TestCase):
    def test_an_old_collector_is_a_setup_item(self):
        app = load_app()
        ids = lambda: {i["id"]: i for i in app.setup_checks()}
        with open(app.HW_FILE, "w") as f:
            json.dump({"updated": int(time.time())}, f)     # (a collector from before builds were written)
        item = ids().get("collector_update")
        self.assertEqual((item or {}).get("state"), "warn")
        self.assertIn("sudo bash collector/install.sh", json.dumps(item))
        with open(app.HW_FILE, "w") as f:
            json.dump({"updated": int(time.time()), "build": app.COLLECTOR_BUILD}, f)
        self.assertNotIn("collector_update", ids())

    def test_the_collector_writes_its_build(self):
        col, _ = load_collector()
        app = load_app()
        self.assertGreaterEqual(col.BUILD, app.COLLECTOR_BUILD, "this repo's collector is new enough for this repo's dash")


if __name__ == "__main__":
    unittest.main()
