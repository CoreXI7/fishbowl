"""The Containers row on the dashboard lists which containers are not running (2026-11-17). The collector supplies the
names: only the ones that are not running, sorted, capped, with a name, a state, the full id and whether it is a leftover
the page may offer a Remove button for (2026-11-18)."""
import unittest

from test_collector_requests import load_collector

A, B, C, D = ("a" * 64, "b" * 64, "c" * 64, "d" * 64)


def row(cid, name, state, oneoff="False", project="containers"):
    return f"{cid}\t{name}\t{state}\t{oneoff}\t{project}\n"


class Stopped(unittest.TestCase):
    def setUp(self):
        self.col, self.tmp = load_collector()
        self.col.have = lambda cmd: True

    def fake(self, text):
        self.col.run = lambda cmd, timeout=20: text

    def test_names_of_the_ones_that_are_not_running(self):
        self.fake(row(A, "web", "running") + row(B, "old-wiki", "exited") + row(C, "staging-db", "created") + row(D, "db", "running"))
        got = self.col.containers()
        self.assertEqual((got["running"], got["total"]), (2, 4))
        self.assertEqual([(c["name"], c["state"], c["id"]) for c in got["stopped"]], [("old-wiki", "exited", B), ("staging-db", "created", C)])

    def test_removable_only_for_leftovers(self):
        self.fake(row(A, "old-wiki", "exited") + row(B, "app_xhdl_run_a31773e56a19", "created", "True", "app")
                  + row(C, "never-started", "created") + row(D, "by-hand", "created", "", ""))
        got = {c["name"]: c["removable"] for c in self.col.containers()["stopped"]}
        # "never-started" is a real compose service that failed to start: not junk (security review 2026-11-18)
        self.assertEqual(got, {"old-wiki": False, "app_xhdl_run_a31773e56a19": True, "never-started": False, "by-hand": False})

    def test_the_plex_stack_is_never_offered(self):
        self.fake(row(A, "plex", "created") + row(B, "gluetun", "exited", "True") + row(C, "qbit", "created"))
        self.assertFalse(any(c["removable"] for c in self.col.containers()["stopped"]))

    def test_a_bad_id_is_never_removable(self):
        self.fake(row("abc", "x_run_1", "exited", "True"))
        (c,) = self.col.containers()["stopped"]
        self.assertEqual((c["id"], c["removable"]), ("", False))

    def test_nothing_stopped_is_an_empty_list(self):
        self.fake(row(A, "web", "running"))
        self.assertEqual(self.col.containers()["stopped"], [])

    def test_the_list_is_capped_and_long_names_are_cut(self):
        self.fake("".join(row(f"{i:064x}", f"{'n' * 200}{i:03d}", "exited") for i in range(100)))
        got = self.col.containers()
        self.assertEqual(got["total"], 100)
        self.assertEqual(len(got["stopped"]), 40)
        self.assertTrue(all(len(c["name"]) <= 80 and set(c) == {"name", "state", "id", "removable"} for c in got["stopped"]))

    def test_a_broken_line_is_skipped(self):
        self.fake(row(A, "web", "running") + "onlyid\n\n")
        got = self.col.containers()
        self.assertEqual((got["running"], got["total"], got["stopped"]), (1, 1, []))


if __name__ == "__main__":
    unittest.main()
