"""The collector reads the SSH journal once a minute for both the card and the detail views, without -n
(which made journalctl walk the whole journal), keeping only the newest lines and skipping giant ones."""
import datetime
import os
import stat
import sys
import tempfile
import unittest

from test_collector_requests import load_collector

FAKE = """#!{python}
import os, sys
open(os.environ["FAKE_JOURNAL_LOG"], "a").write(" ".join(sys.argv[1:]) + "\\n")
sys.stdout.write(open(os.environ["FAKE_JOURNAL"]).read())
"""


def iso(dt):
    return dt.astimezone().isoformat(timespec="seconds")


class Journal(unittest.TestCase):
    def setUp(self):
        self.col, tmp = load_collector()
        self.bin = tempfile.mkdtemp(prefix="hd-bin-")
        fake = os.path.join(self.bin, "journalctl")
        with open(fake, "w") as f:
            f.write(FAKE.format(python=sys.executable))
        os.chmod(fake, stat.S_IRWXU)
        self.journal, self.log = os.path.join(tmp, "journal.txt"), os.path.join(tmp, "calls.txt")
        os.environ.update(FAKE_JOURNAL=self.journal, FAKE_JOURNAL_LOG=self.log, PATH=self.bin + os.pathsep + os.environ["PATH"])
        now = datetime.datetime.now()
        old, recent = iso(now - datetime.timedelta(days=3)), iso(now - datetime.timedelta(hours=2))
        lines = [f"{old} host sshd[10]: Failed password for root from 203.0.113.9 port 4000 ssh2",
                 f"{old} host sshd[11]: Accepted publickey for alice from 198.51.100.4 port 5000 ssh2",
                 f"{recent} host sshd[12]: Failed password for root from 203.0.113.9 port 4001 ssh2",
                 f"{recent} host sshd[13]: Invalid user bob from 203.0.113.10 port 4002",
                 f"{recent} host sshd[14]: " + "x" * 5000,
                 # a user name crafted to look like a login, in a FAILED attempt from an attacker
                 f"{recent} host sshd[16]: Invalid user Accepted publickey for root from 198.51.100.4 from 203.0.113.66 port 5555",
                 f"{recent} host sshd[15]: Accepted password for carol from 198.51.100.5 port 5001 ssh2"]
        with open(self.journal, "w") as f:
            f.write("\n".join(lines) + "\n")
        self.col.SEC.update(ssh_unit="ssh", process_watch=False, cron_watch=False, firewall=False, fail2ban_jail="",
                            authelia_container="")

    def tearDown(self):
        os.environ["PATH"] = os.environ["PATH"].split(os.pathsep, 1)[1]

    def calls(self):
        return open(self.log).read().splitlines() if os.path.exists(self.log) else []

    def test_one_read_per_minute_without_n(self):
        sec = self.col.security()
        det = self.col.ssh_detail()
        self.assertEqual(len(self.calls()), 1, self.calls())
        self.assertNotIn(" -n ", " " + self.calls()[0] + " ")
        self.assertEqual([l["user"] for l in sec["ssh"]["logins"]], ["carol", "alice"])
        self.assertEqual(sec["ssh"]["failed_24h"], 3)     # incl. the crafted one: it IS a failed attempt
        self.assertEqual(det["login_count"], 2)

    def test_crafted_user_name_is_not_a_login(self):
        logins = self.col.security()["ssh"]["logins"]
        self.assertNotIn("root", [l["user"] for l in logins])
        self.assertEqual(len(logins), 2)

    def test_busy_lock_uses_the_last_lines(self):
        self.col.security()                              # fills the cache
        self.col._journal["at"] = float("-inf")         # expired, and the other thread is reading right now:
        with self.col._journal["lock"]:
            lines = self.col.ssh_journal(["-u", "ssh"], 30, wait=0.1)
        self.assertTrue(lines)
        self.assertEqual(len(self.calls()), 1, "no second read while the first is running")

    def test_newest_lines_kept(self):
        self.col.JOURNAL_MAX_LINES = 2
        sec = self.col.security()
        self.assertEqual([l["user"] for l in sec["ssh"]["logins"]], ["carol"])


if __name__ == "__main__":
    unittest.main()
