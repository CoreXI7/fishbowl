"""Tests for the ZFS collector helpers described in ZFS-SPEC.md."""

import os
import shutil
import unittest

from test_collector_requests import load_collector

FIXTURE_LIST = "datos\t32006096306176\t15998753579008\t16007342727168\t3\t49\tONLINE\n"

FIXTURE_STATUS_OK = (
    "  pool: datos\n state: ONLINE\n"
    "  scan: scrub repaired 0B in 08:12:43 with 0 errors on Sun Nov  9 08:36:44 2026\nconfig:\n\n"
    "\tNAME                                                STATE     READ WRITE CKSUM\n"
    "\tdatos                                               ONLINE       0     0     0\n"
    "\t  raidz1-0                                          ONLINE       0     0     0\n"
    "\t    /dev/disk/by-id/ata-HGST_HUH728080ALE600_AAAA-part1  ONLINE       0     0     0\n" "\t    /dev/disk/by-id/ata-HGST_HUH728080ALE600_BBBB-part1  ONLINE       0     0     0\n"
    "\t    /dev/disk/by-id/ata-HGST_HUH728080ALE600_CCCC-part1  ONLINE       0     0     0\n" "\t    /dev/disk/by-id/ata-HGST_HUH728080ALE600_DDDD-part1  ONLINE       0     0     0\n"
    "\tcache\n"
    "\t  /dev/disk/by-id/nvme-Samsung_SSD_970_EEEE-part1   ONLINE       0     0     0\n"
    "\nerrors: No known data errors\n"
)

FIXTURE_STATUS_DEGRADED = (
    "  pool: datos\n state: DEGRADED\n"
    "status: One or more devices could not be opened.\n\tReplicas exist, pool is degraded.\n"
    "action: Attach the missing device.\n\tSee 'zpool status' for more information.\n"
    "  scan: resilver in progress since Tue Nov 18 10:00:00 2026\n\t1.20T / 12.0T scanned, 600G / 12.0T issued at 150M/s\n"
    "config:\n\n"
    "\tNAME                                                STATE     READ WRITE CKSUM\n"
    "\tdatos                                               DEGRADED     0     0     0\n"
    "\t  raidz1-0                                          DEGRADED     0     0     0\n"
    "\t    /dev/disk/by-id/ata-HGST_HUH728080ALE600_AAAA-part1  ONLINE       0     0     0\n" "\t    /dev/disk/by-id/ata-HGST_HUH728080ALE600_BBBB-part1  ONLINE       0     0     0\n"
    "\t    /dev/disk/by-id/ata-HGST_HUH728080ALE600_CCCC-part1  UNAVAIL      0     0     0  was /dev/sdc1\n" "\t    /dev/disk/by-id/ata-HGST_HUH728080ALE600_DDDD-part1  ONLINE       0     0     7\n"
    "\tcache\n"
    "\t  /dev/disk/by-id/nvme-Samsung_SSD_970_EEEE-part1   ONLINE       0     0     0\n"
    "\nerrors: 2 data errors, use '-v' for a list\n"
)

# (name, depth, kind, state) of every vdev row of FIXTURE_STATUS_OK
OK_VDEVS = [("raidz1-0", 0, "group", "ONLINE")] + [
    ("ata-HGST_HUH728080ALE600_%s-part1" % tag, 1, "disk", "ONLINE")
    for tag in ("AAAA", "BBBB", "CCCC", "DDDD")
] + [("cache", 0, "class", None), ("nvme-Samsung_SSD_970_EEEE-part1", 1, "disk", "ONLINE")]


def _rows(parsed):
    """Normalise a parse_* result (list or dict) into a list of dicts."""
    if isinstance(parsed, dict) and "pools" in parsed:
        parsed = parsed["pools"]
    items = parsed.items() if isinstance(parsed, dict) else [(None, r) for r in parsed or []]
    return [dict(r, name=r.get("name") or r.get("pool") or key) for key, r in items]


def _shape(vdevs):
    return [(v["name"], v["depth"], v["kind"], v["state"]) for v in vdevs]


class ZfsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.col, cls.tmp = load_collector()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _patch(self, fake):
        # zpool runs through zfs_run (hang-proof); the kernel's pool states are faked empty unless a test sets them
        old_run, old_k = self.col.zfs_run, self.col.zfs_kstat_states
        self.col.zfs_run, self.col.zfs_kstat_states = fake, getattr(self, "kstat", lambda base=None: {})
        self.addCleanup(setattr, self.col, "zfs_run", old_run)
        self.addCleanup(setattr, self.col, "zfs_kstat_states", old_k)

    def test_parse_zpool_list(self):
        rows = _rows(self.col.parse_zpool_list(FIXTURE_LIST))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["name"], "datos")
        self.assertEqual((row["size"], row["alloc"], row["free"]), (32006096306176, 15998753579008, 16007342727168))
        self.assertEqual((row["frag"], row["cap"]), (3, 49))
        self.assertEqual(row.get("health", row.get("state")), "ONLINE")
        dash = _rows(self.col.parse_zpool_list("otro\t1024\t512\t512\t-\t-\tDEGRADED\n"))[0]
        self.assertIsNone(dash["frag"])
        self.assertIsNone(dash["cap"])

    # (2) status OK
    def test_parse_zpool_status_ok(self):
        pools = _rows(self.col.parse_zpool_status(FIXTURE_STATUS_OK))
        self.assertEqual(len(pools), 1)
        pool = pools[0]
        self.assertEqual((pool["name"], pool["state"]), ("datos", "ONLINE"))
        self.assertEqual(pool["data_errors"], 0)
        self.assertEqual(pool["errors"], "No known data errors")
        self.assertTrue(pool["scan"].startswith("scrub repaired 0B"))
        self.assertNotIn("\n", pool["scan"])
        vdevs = pool["vdevs"]
        self.assertEqual(len(vdevs), 7)
        self.assertEqual(_shape(vdevs), OK_VDEVS)
        self.assertEqual([(v["read"], v["write"], v["cksum"]) for v in vdevs if v["kind"] != "class"],
                         [(0, 0, 0)] * 6)

    # (3) status DEGRADED
    def test_parse_zpool_status_degraded(self):
        pools = _rows(self.col.parse_zpool_status(FIXTURE_STATUS_DEGRADED))
        self.assertEqual(len(pools), 1)
        pool = pools[0]
        self.assertEqual(pool["state"], "DEGRADED")
        self.assertEqual(pool["data_errors"], 2)
        self.assertIn("2 data errors", pool["errors"])
        scan = pool["scan"]
        self.assertNotIn("\n", scan)
        self.assertIn("resilver in progress", scan)
        self.assertIn("scanned", scan)
        by_name = {v["name"]: v for v in pool["vdevs"]}
        self.assertEqual(len(by_name), 7)
        self.assertEqual((by_name["raidz1-0"]["state"], by_name["raidz1-0"]["depth"]), ("DEGRADED", 0))
        cccc = by_name["ata-HGST_HUH728080ALE600_CCCC-part1"]
        self.assertEqual((cccc["state"], cccc["depth"], cccc["note"]), ("UNAVAIL", 1, "was /dev/sdc1"))
        dddd = by_name["ata-HGST_HUH728080ALE600_DDDD-part1"]
        self.assertEqual((dddd["state"], dddd["read"], dddd["write"], dddd["cksum"]), ("ONLINE", 0, 0, 7))

    # (4) robustness
    def test_empty_and_configless_input(self):
        self.assertEqual(_rows(self.col.parse_zpool_list("")), [])
        self.assertEqual(_rows(self.col.parse_zpool_status("")), [])
        pools = _rows(self.col.parse_zpool_status("  pool: solo\n state: ONLINE\n"))
        self.assertEqual(len(pools), 1)
        self.assertEqual(pools[0]["name"], "solo")
        self.assertEqual(pools[0]["vdevs"], [])

    def test_many_vdevs_are_capped(self):
        rows = "".join("\t  /dev/sd%03d  ONLINE  0  0  0\n" % i for i in range(2000))
        text = ("  pool: big\n state: ONLINE\nconfig:\n\n"
                "\tNAME STATE READ WRITE CKSUM\n"
                "\tbig ONLINE 0 0 0\n" + rows)
        pools = _rows(self.col.parse_zpool_status(text))
        self.assertLessEqual(len(pools[0]["vdevs"]), 256)

    def test_pool_name_is_plain_text(self):
        text = ("  pool: <script>alert(1)</script>\n state: ONLINE\nconfig:\n\n"
                "\tNAME STATE READ WRITE CKSUM\n"
                "\t<script>alert(1)</script> ONLINE 0 0 0\n")
        pools = _rows(self.col.parse_zpool_status(text))
        name = pools[0]["name"]
        self.assertIsInstance(name, str)
        self.assertEqual(name, "<script>alert(1)</script>")

    def test_counter_suffixes_and_garbage(self):
        text = ("  pool: datos\n state: ONLINE\nconfig:\n\n"
                "\tNAME STATE READ WRITE CKSUM\n"
                "\tdatos ONLINE 0 0 0\n"
                "\t  /dev/sdb ONLINE 1.2K 0 0\n"
                "\t  /dev/sdc ONLINE 0 0 xyz\n")
        vdevs = {v["name"]: v for v in _rows(self.col.parse_zpool_status(text))[0]["vdevs"]}
        self.assertEqual(vdevs["sdb"]["read"], 1228)   # zfs nicenum: 1.2K = 1.2 * 1024
        self.assertIsNone(vdevs["sdc"]["cksum"])

    def test_crlf_matches_lf(self):
        lf = _rows(self.col.parse_zpool_status(FIXTURE_STATUS_OK))[0]
        crlf = _rows(self.col.parse_zpool_status(FIXTURE_STATUS_OK.replace("\n", "\r\n")))[0]
        self.assertEqual((crlf["name"], crlf["state"], crlf["data_errors"]),
                         (lf["name"], lf["state"], lf["data_errors"]))
        self.assertEqual(crlf["scan"].strip(), lf["scan"].strip())
        self.assertEqual(crlf["errors"].strip(), lf["errors"].strip())
        self.assertEqual(_shape(crlf["vdevs"]), _shape(lf["vdevs"]))
        for a, b in zip(crlf["vdevs"], lf["vdevs"]):
            self.assertEqual((a["read"], a["write"], a["cksum"]), (b["read"], b["write"], b["cksum"]))

    # (5) zfs_pools()
    def test_zfs_pools(self):
        def fake_run(cmd, timeout=None, **kwargs):
            args = list(cmd)
            if "list" in args:
                return FIXTURE_LIST
            if "status" in args:
                return FIXTURE_STATUS_OK
            raise AssertionError("unexpected command %r" % (cmd,))

        self._patch(fake_run)
        res = self.col.zfs_pools()
        self.assertIn("pools", res)
        self.assertEqual(len(res["pools"]), 1)
        pool = res["pools"][0]
        self.assertEqual((pool["name"], pool["state"]), ("datos", "ONLINE"))
        self.assertEqual((pool["size"], pool["alloc"], pool["free"]), (32006096306176, 15998753579008, 16007342727168))
        self.assertEqual((pool["frag"], pool["cap"]), (3, 49))
        self.assertTrue(pool["scan"].startswith("scrub repaired 0B"))
        self.assertEqual(pool["data_errors"], 0)
        self.assertEqual(pool["errors"], "No known data errors")
        self.assertEqual(_shape(pool["vdevs"]), OK_VDEVS)

        def boom(cmd, timeout=None, **kwargs):
            raise RuntimeError("boom")

        self._patch(boom)
        res = self.col.zfs_pools()
        self.assertIn("error", res)
        self.assertIsInstance(res["error"], str)
        self.assertIn("boom", res["error"])

    def test_zpool_hangs_pool_still_reported(self):
        # zpool gives no answer (stuck on a suspended pool): the kernel's state is reported, marked stuck
        class Hung:                                # a zpool child that never exits
            def poll(self):
                return None
        self.kstat = lambda base=None: {"datos": "SUSPENDED"}
        self._patch(lambda cmd, timeout=None, **kw: None)
        self.col._ZFS_STUCK[:] = [Hung()]
        self.addCleanup(self.col._ZFS_STUCK.clear)
        res = self.col.zfs_pools()
        self.assertTrue(res.get("stuck"))
        self.assertEqual([(p["name"], p["state"], p["stuck"]) for p in res["pools"]], [("datos", "SUSPENDED", True)])

    def test_zpool_fails_fast_is_not_called_a_hang(self):
        # zpool exits with an error at once: kernel state still shown, marked failed, not stuck
        self.kstat = lambda base=None: {"datos": "ONLINE"}
        self._patch(lambda cmd, timeout=None, **kw: None)
        self.col._ZFS_STUCK.clear()
        res = self.col.zfs_pools()
        self.assertFalse(res.get("stuck"))
        self.assertTrue(res.get("failed"))
        self.assertEqual([(p["name"], p["state"], p["failed"]) for p in res["pools"]], [("datos", "ONLINE", True)])

    def test_kstat_states_reads_and_validates(self):
        base = os.path.join(self.tmp, "kstat")
        for name, state in (("datos", "ONLINE\n"), ("bad name!", "ONLINE"), ("evil", "<img src=x>")):
            os.makedirs(os.path.join(base, name), exist_ok=True)
            with open(os.path.join(base, name, "state"), "w") as f:
                f.write(state)
        self.assertEqual(self.col.zfs_kstat_states(base), {"datos": "ONLINE"})

