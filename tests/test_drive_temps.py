"""Drive temperatures in the ZFS pool list (GitHub issues #11 and #12).
#11: NVMe pool members showed "–" while the Temperatures card had their temperature: smartctl got -n standby (meant for
disks that spin down) and some versions answer that with an error for NVMe. #12: a long by-id member name pushed the
temperature chip out of the card."""
import json
import os
import shutil
import tempfile
import unittest

from browser import CHROME, Browser
from helpers import load_app, serve
from test_collector_requests import load_collector

NVME_JSON = {"temperature": {"current": 41}, "smart_status": {"passed": True}, "power_on_time": {"hours": 10},
             "nvme_smart_health_information_log": {"temperature": 41, "media_errors": 0, "percentage_used": 2}}


class SmartTemps(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.col, cls.tmp = load_collector()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def fake_run(self, answer):
        calls = []
        old = self.col.run
        self.col.run = lambda cmd, **kw: (calls.append(cmd), answer)[1]
        self.addCleanup(setattr, self.col, "run", old)
        return calls

    def fake_hwmon(self, value):
        """A /sys/block/<dev>/device/hwmon*/temp1_input that says value (None = no sensor)."""
        d = tempfile.mkdtemp(dir=self.tmp)
        f = os.path.join(d, "temp1_input")
        if value is not None:
            with open(f, "w") as fh:
                fh.write(value)
        old = self.col.glob.glob
        self.col.glob.glob = lambda pat: [f] if value is not None and pat.endswith("temp1_input") else []
        self.addCleanup(setattr, self.col.glob, "glob", old)

    def test_nvme_is_asked_without_standby_and_sata_with_it(self):
        calls = self.fake_run(json.dumps(NVME_JSON))
        self.assertEqual(self.col.smart("/dev/nvme0n1")["temp"], 41)
        self.col.smart("/dev/sda")
        self.assertNotIn("-n", calls[0], "NVMe: no -n standby")
        self.assertEqual(calls[1][1:3], ["-n", "standby"], "a disk that spins down is still left asleep")

    def test_nvme_log_temperature_when_the_top_level_one_is_missing(self):
        self.fake_run(json.dumps({**NVME_JSON, "temperature": {}}))
        self.fake_hwmon(None)
        self.assertEqual(self.col.smart("/dev/nvme1n1")["temp"], 41)
        self.fake_run(json.dumps({**NVME_JSON, "temperature": {"current": None}}))   # present but null
        self.assertEqual(self.col.smart("/dev/nvme1n1")["temp"], 41)

    def test_kernel_sensor_when_smartctl_gives_no_temperature(self):
        self.fake_run(json.dumps({"smartctl": {"messages": [{"string": "some error"}]}}))
        self.fake_hwmon("38850\n")
        self.assertEqual(self.col.smart("/dev/nvme0n1")["temp"], 39)
        self.fake_run("")                        # smartctl missing or failing outright
        self.assertEqual(self.col.smart("/dev/nvme0n1")["temp"], 39)
        self.fake_hwmon("38500")                # half a degree rounds up, the way people read it
        self.assertEqual(self.col.smart("/dev/nvme0n1")["temp"], 39)

    def test_kernel_sensor_garbage_and_odd_names_give_nothing(self):
        self.fake_hwmon("not a number")
        self.assertIsNone(self.col.nvme_hwmon_temp("/dev/nvme0n1"))
        self.fake_hwmon("999999")               # 999 °C: not a reading
        self.assertIsNone(self.col.nvme_hwmon_temp("/dev/nvme0n1"))
        self.fake_hwmon("40000")
        for dev in ("/dev/sda", "/dev/nvme0n1p1", "/dev/nvme0n1/../x", "/dev/nvme*"):
            self.assertIsNone(self.col.nvme_hwmon_temp(dev), dev)

    def test_sata_without_temperature_stays_without(self):
        self.fake_run(json.dumps({"smart_status": {"passed": True}}))
        self.fake_hwmon("40000")
        self.assertIsNone(self.col.smart("/dev/sdb")["temp"], "the NVMe sensor is never used for another disk")


class LongMemberNames(unittest.TestCase):
    @unittest.skipUnless(CHROME, "needs Chrome or Chromium")
    def test_the_temperature_stays_in_the_card(self):
        app = load_app(DEMO="1")
        srv, url = serve(app.Handler)
        for w in (1440, 2060, 390):
            b = Browser(w, 900)
            try:
                b.send("Page.navigate", {"url": url + "/"})
                self.assertTrue(b.wait_for("!!document.querySelector('.drv .zm .zc .chip')", 20), "a pool member with a chip")
                r = b.js("""(() => { const row = [...document.querySelectorAll('.drv .zm')].find(r => r.querySelector('.zc .chip'));
                    const orig = row.querySelector('.zn').textContent;
                    row.querySelector('.zn').textContent = 'nvme-Samsung_SSD_970_EVO_Plus_1TB_S4EWNX0R' + 'X'.repeat(60) + '-part1';
                    const card = row.closest('.drv').getBoundingClientRect(), chip = row.querySelector('.zc .chip').getBoundingClientRect();
                    return {inside: chip.right <= card.right + 1 && chip.left >= card.left, one_line: row.getBoundingClientRect().height < 30,
                            tip: !!orig && row.title.startsWith(orig)}; })()""")
                self.assertEqual(r, {"inside": True, "one_line": True, "tip": True}, f"width {w}")
            finally:
                b.close()
        srv.shutdown()


if __name__ == "__main__":
    unittest.main()
