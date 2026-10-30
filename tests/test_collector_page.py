"""The collector's settings from the dash's settings page. The dash (a container, uid 1000) writes what the page changed
into req/collector-settings.json; root must only ever take from it what can't make root read a file, run a pattern
or a command the dash chose. Those wait for a person (`--apply`)."""
import contextlib
import importlib.machinery
import io
import json
import os
import tempfile
import threading
import tomllib
import unittest

from helpers import ROOT

COLLECTOR = os.path.join(ROOT, "collector", "homelab-dash-collector.py")
DET = {"mounts": ["/", "/mnt/disk1", "/media/me/big disk"], "ifaces": ["eth0", "br0"], "jails": ["sshd", "recidive"],
       "ssh_units": ["ssh"], "authelia": ["authelia"], "authelia_db": {}, "gpu": False, "firewall": "ufw"}
BASE = 'interval = 5\n[security]\nfail2ban_jail = "sshd"\n[backup]\nlog = "/var/log/backup.log"\n'


def load_collector(toml_text="", tmp=None):
    tmp = tmp or tempfile.mkdtemp(prefix="hd-colp-")
    conf = os.path.join(tmp, "collector.toml")
    with open(conf, "w") as f:
        f.write(f'out_dir = "{tmp}/out"\ndash_uid = {os.getuid()}\n' + toml_text)
    os.environ["HOMELAB_DASH_COLLECTOR_CONFIG"] = conf
    os.makedirs(os.path.join(tmp, "out", "req"), exist_ok=True)
    col = importlib.machinery.SourceFileLoader(f"collector_page_{os.path.basename(tmp)}_{len(os.listdir(tmp))}", COLLECTOR).load_module()
    return col, tmp


def send(col, changes, rid=None):
    """What the dash writes after a save: each change with the fingerprint of the value the page showed."""
    out = {k: ({"from": "" if c["shown"] is None else col.digest(c["shown"]), "to": c["to"]} if "shown" in c else c)
           for k, c in changes.items()}
    with open(col.PAGE_REQ, "w") as f:
        json.dump({"v": 3, "id": rid or os.urandom(8).hex(), "changes": out}, f)


def ch(shown, to):
    return {"shown": shown, "to": to}


class PageSettings(unittest.TestCase):
    def setUp(self):
        self.col, self.tmp = load_collector(BASE)

    def test_safe_values_apply_by_themselves(self):
        col = self.col
        send(col, {"interval": ch(5, 7), "net_if": ch(None, "br0"), "mounts": ch(None, ["/", "/mnt/disk1"]),
                   "mount_labels": ch(None, {"/mnt/disk1": "data"}), "gpu": ch(None, False),
                   "security.fail2ban_jail": ch("sshd", "recidive"), "security.process_watch": ch(None, False),
                   "security.bad_process_names": ch(None, ["xmrig"])})
        st = col.page_round(DET)
        self.assertEqual(st["rejected"], [])
        self.assertEqual((col.CFG["interval"], col.NET_IF, col.CFG["mounts"]), (7, "br0", ["/", "/mnt/disk1"]))
        self.assertEqual((col.SEC["fail2ban_jail"], col.SEC["process_watch"]), ("recidive", False))
        self.assertEqual(oct(os.stat(col.PAGE_STATE_FILE).st_mode & 0o777), "0o600")
        col.apply_config(col.effective())   # a restart keeps them
        self.assertEqual(col.CFG["interval"], 7)

    def test_paths_and_patterns_wait_for_approval(self):
        col = self.col
        send(col, {"security.authelia_db": ch(None, "/srv/authelia/db.sqlite3"), "security.immutable_files": ch(None, ["/etc/passwd"]),
                   "backup.log": ch("/var/log/backup.log", "/etc/shadow"), "backup.success_regex": ch(None, "ok$"),
                   "backup.jobs": ch(None, [{"label": "x", "regex": "done"}])})
        st = col.page_round(DET)
        self.assertEqual(col.BAK["log"], "/var/log/backup.log", "not applied until a person approves it")
        self.assertNotIn("authelia_db", col.SEC)
        self.assertEqual(st["pending"], ["backup.jobs", "backup.log", "backup.success_regex",
                                          "security.authelia_db", "security.immutable_files"])
        self.assertEqual(st["pending_values"]["backup.log"], "/etc/shadow")
        self.assertFalse(os.path.exists(col.PAGE_TOML))

    def test_refused_values_keep_what_was_there(self):
        col = self.col
        send(col, {"interval": ch(5, 7)})
        col.page_round(DET)
        bad = {"out_dir": ch(None, "/tmp/x"), "dash_uid": ch(None, 0), "net_if": ch(None, "wlan9"), "interval": ch(7, 1),
               "mounts": ch(None, ["/etc"]), "mount_labels": ch(None, {"/etc": "x"}), "security.fail2ban_jail": ch("sshd", "nope"),
               "security.authelia_container": ch(None, "evil"), "security.bad_process_names": ch(None, ["a b"]), "security.rm": ch(None, True),
               "security.authelia_db": ch(None, "relative/path"), "security.fail2ban_log": ch(None, "/var/log/../../etc/shadow"),
               "backup.success_regex": ch(None, "("), "backup.log": ch("/var/log/backup.log", "/var/log/x\x1b[2J"),
               "backup.jobs": ch(None, [{"label": "x", "shell": "rm -rf /"}]), "gpu": ch(None, 1), "usage": {"from": None}}
        send(col, bad)
        st = col.page_round(DET)
        keys = {k for k, _ in st["rejected"]}
        for k in bad:
            self.assertIn(k, keys, k)
        self.assertEqual(col.CFG["interval"], 7, "a refused change keeps the value from before")
        self.assertEqual(st["pending"], [])

    def test_kernel_files_and_the_collectors_own_folder_are_never_paths(self):
        col = self.col
        for p in ("/proc/self/root/etc/shadow", "/sys/x", "/dev/sda", col.OUT_DIR + "/req/log", col.OUT_DIR,
                  "/p*/self/environ", os.path.dirname(col.OUT_DIR) + "/*/req/x", "/*/x"):
            with self.assertRaises(col.PageInvalid, msg=p):
                col._path(p, "x", glob_ok=True)
        self.assertEqual(col._path("/media/*/backups/*.tar", "x", glob_ok=True), "/media/*/backups/*.tar")
        self.assertEqual(col._path("", "x", blank=True), "")

    def test_hostile_request_files_keep_the_settings(self):
        """A FIFO, a symlink to a root file, something huge, deep JSON, bad bytes, another version: refused, never stuck,
        and what the page set before stays."""
        col = self.col
        send(col, {"interval": ch(5, 7)})
        col.page_round(DET)
        os.remove(col.PAGE_REQ)
        os.symlink("/etc/shadow", col.PAGE_REQ)
        self.assertTrue(col.page_round(DET)["error"], "a symlink isn't followed")
        os.remove(col.PAGE_REQ)
        os.mkfifo(col.PAGE_REQ)
        done = threading.Event()
        threading.Thread(target=lambda: (col.page_round(DET), done.set()), daemon=True).start()
        finished = done.wait(5)
        if not finished:
            os.close(os.open(col.PAGE_REQ, os.O_WRONLY | os.O_NONBLOCK))
        self.assertTrue(finished, "a FIFO must not block the collector")
        os.remove(col.PAGE_REQ)
        with open(col.PAGE_REQ, "w") as f:
            f.write(" " * (col.PAGE_MAX + 1))
        self.assertTrue(col.page_round(DET)["error"])
        for raw in (b"[" * 100000, b"\xff\xfe", b"", b"{}", b'{"v": 1, "set": {"interval": 9}}', b'{"v": 2, "changes": {}}', b'{"v": 3, "changes": []}'):
            with open(col.PAGE_REQ, "wb") as f:
                f.write(raw)
            st = col.page_round(DET)
            self.assertTrue(st["rejected"], raw[:20])
            self.assertEqual(col.CFG["interval"], 7, raw[:20])

    def test_a_change_made_on_the_host_meanwhile_wins(self):
        col = self.col
        send(col, {"interval": ch(4, 7)})   # the page showed 4, but the host has 5 now
        st = col.page_round(DET)
        self.assertIn("changed on the host", dict(st["rejected"])["interval"])
        self.assertEqual(col.CFG["interval"], 5)

    def test_a_later_hand_edit_of_collector_toml_wins(self):
        """The page set the jail and the interval; then collector.toml's interval is edited by hand: the edit counts,
        the jail stays, and the old request is never applied again."""
        col = self.col
        send(col, {"interval": ch(5, 7), "security.fail2ban_jail": ch("sshd", "recidive")})
        col.page_round(DET)
        col2, _ = load_collector(BASE.replace("interval = 5", "interval = 9"), tmp=self.tmp)   # edited + restarted
        st = col2.page_round(DET)
        self.assertEqual((col2.CFG["interval"], col2.SEC["fail2ban_jail"]), (9, "recidive"))
        self.assertNotIn("interval", st["from_page"])
        col2.page_round(DET)
        self.assertEqual(col2.CFG["interval"], 9)

    def test_value_equal_to_the_file_hands_it_back(self):
        col = self.col
        send(col, {"interval": ch(5, 7)})
        col.page_round(DET)
        send(col, {"interval": ch(7, 5)})
        col.page_round(DET)
        self.assertEqual(col.read_state()["safe"], {})

    def test_a_waiting_change_drops_when_the_host_changes_that_setting(self):
        col = self.col
        send(col, {"backup.log": ch("/var/log/backup.log", "/var/log/new.log")})
        col.page_round(DET)
        col2, _ = load_collector(BASE.replace("backup.log", "other.log"), tmp=self.tmp)
        st = col2.page_round(DET)
        self.assertEqual(st["pending"], [])
        self.assertIn("backup.log", dict(st["rejected"]))

    def test_setting_it_back_cancels_a_waiting_change(self):
        col = self.col
        send(col, {"backup.log": ch("/var/log/backup.log", "/var/log/typo.log")})
        self.assertEqual(col.page_round(DET)["pending"], ["backup.log"])
        send(col, {"backup.log": ch("/var/log/backup.log", "/var/log/backup.log")})
        self.assertEqual(col.page_round(DET)["pending"], [])

    def test_the_same_change_again_counts(self):
        """Picking the same choice again after it was refused (Authelia wasn't running) is a new request (its own id);
        the very same request (same id) is still handled once."""
        col = self.col
        send(col, {"security.authelia_container": ch(None, "auth2")}, rid="a")
        self.assertIn("security.authelia_container", dict(col.page_round(DET)["rejected"]))
        det2 = {**DET, "authelia": ["authelia", "auth2"]}
        send(col, {"security.authelia_container": ch(None, "auth2")}, rid="a")
        col.page_round(det2)
        self.assertNotIn("authelia_container", col.SEC, "same request: handled once")
        send(col, {"security.authelia_container": ch(None, "auth2")}, rid="b")
        col.page_round(det2)
        self.assertEqual(col.SEC["authelia_container"], "auth2")

    def test_fingerprint_of_any_value(self):
        """The page can change a value it can't show (a float, an odd table): it sends the fingerprint it was given."""
        col, _ = load_collector('interval = 5\n[backup]\nmax_age_hours = 26.5\n')
        st = col.page_round(DET)
        send(col, {"backup.max_age_hours": {"from": st["fp"]["backup.max_age_hours"], "to": 30}})
        self.assertEqual(col.page_round(DET)["rejected"], [])
        self.assertEqual(col.BAK["max_age_hours"], 30)

    def test_network_interface_back_to_automatic(self):
        col = self.col
        send(col, {"net_if": ch(None, "br0")})
        col.page_round(DET)
        send(col, {"net_if": ch("br0", "")})
        col.page_round(DET)
        self.assertEqual(col.NET_IF, col.default_iface())

    def test_a_drive_name_can_be_cleared_while_unplugged(self):
        col, _ = load_collector('[mount_labels]\n"/mnt/gone" = "old"\n')
        send(col, {"mount_labels": ch({"/mnt/gone": "old"}, {})})
        self.assertEqual(col.page_round(DET)["rejected"], [])
        self.assertEqual(col.CFG["mount_labels"], {})

    def test_refusals_are_news_for_a_day(self):
        col = self.col
        send(col, {"interval": ch(5, 1)})
        self.assertTrue(col.page_round(DET)["rejected"])
        s = col.read_state()
        s["rejected_at"] -= 2 * 86400
        col.write_state(s)
        self.assertEqual(col.page_round(DET)["rejected"], [])

    def test_two_backup_jobs_with_one_name_are_refused(self):
        col = self.col
        send(col, {"backup.jobs": ch(None, [{"label": "Nightly", "regex": "a"}, {"label": "Nightly", "regex": "b"}])})
        self.assertIn("two are called", dict(col.page_round(DET)["rejected"])["backup.jobs"])

    def test_a_request_that_changes_nothing_costs_one_tiny_write(self):
        col = self.col
        send(col, {"interval": ch(5, 7)})
        col.page_round(DET)
        before = os.stat(col.PAGE_STATE_FILE).st_mtime_ns
        for i in range(5):
            send(col, {"interval": ch(7, 7)}, rid=f"r{i}")
            col.page_round(DET)
        self.assertEqual(os.stat(col.PAGE_STATE_FILE).st_mtime_ns, before, "page-state.json untouched")
        with open(col.PAGE_LAST_FILE) as f:
            self.assertEqual(len(f.read()), 64)
        col.apply_config(col.effective())
        self.assertEqual(col.CFG["interval"], 7)

    def test_choice_survives_a_flaky_detection(self):
        col = self.col
        send(col, {"security.fail2ban_jail": ch("sshd", "recidive")})
        col.page_round(DET)
        col.page_round({**DET, "jails": []})   # fail2ban didn't answer this time
        self.assertEqual(col.SEC["fail2ban_jail"], "recidive")

    def test_unplugged_drive_stays_allowed(self):
        col, _ = load_collector('mounts = ["/", "/mnt/old"]\n')
        send(col, {"mount_labels": ch(None, {"/mnt/old": "old"})})
        st = col.page_round(DET)
        self.assertEqual(st["rejected"], [])
        self.assertEqual(col.CFG["mount_labels"], {"/mnt/old": "old"})

    def test_big_waiting_values_stay_out_of_hw_json(self):
        col = self.col
        jobs = [{"label": f"j{i}", "regex": "x" * 290, "files": [f"/srv/{i}/{'f' * 400}/*"] * 15} for i in range(30)]
        send(col, {"backup.jobs": ch(None, jobs), "backup.log": ch("/var/log/backup.log", "/var/log/n.log")})
        st = col.page_round(DET)
        self.assertEqual(st["pending"], ["backup.jobs", "backup.log"])
        self.assertEqual(list(st["pending_values"]), ["backup.log"])

    def test_many_refusals_are_capped(self):
        col = self.col
        send(col, {f"junk{i}\x1b[2J": ch(None, 1) for i in range(500)})
        st = col.page_round(DET)
        self.assertEqual((len(st["rejected"]), st["rejected_more"]), (20, 480))
        self.assertNotIn("\x1b", json.dumps(st["rejected"], ensure_ascii=False))

    def test_state_file_from_someone_else_or_damaged_is_ignored(self):
        col = self.col
        for junk in ("[]", '{"v": 2, "safe": {"out_dir": {"v": "/x"}, "interval": {"v": 1}}, "pending": {"interval": {"from": 1, "to": 2}}}',
                     '{"v": 2, "safe": []}', "nope"):
            with open(col.PAGE_STATE_FILE, "w") as f:
                f.write(junk)
            os.chmod(col.PAGE_STATE_FILE, 0o600)
            s = col.read_state()
            self.assertNotIn("out_dir", s["safe"])
            self.assertEqual(s["pending"], {})
            col.page_round(DET)

    def test_leftover_temp_files_dont_block_saving(self):
        col = self.col
        open(f"{col.PAGE_STATE_FILE}.{os.getpid()}.tmp", "w").close()
        send(col, {"interval": ch(5, 7)})
        self.assertEqual(col.page_round(DET)["error"], "")
        self.assertEqual(col.CFG["interval"], 7)


class Approval(unittest.TestCase):
    def setUp(self):
        self.col, self.tmp = load_collector(BASE)

    def pending(self, changes):
        send(self.col, changes)
        self.col.page_round(DET)
        state = self.col.read_state()
        return [(k, self.col.APPROVAL_KEYS[self.col.PATHS[k]](p["to"], DET)) for k, p in state["pending"].items()], state["pending"]

    def test_approve_writes_page_toml_with_what_it_replaced(self):
        col = self.col
        plan, seen = self.pending({"backup.log": ch("/var/log/backup.log", "/var/log/new.log"),
                                   "security.immutable_files": ch(None, ["/etc/x"])})
        col.approve(plan, seen)
        with open(col.PAGE_TOML, "rb") as f:
            d = tomllib.load(f)
        self.assertEqual(d["backup"]["log"], "/var/log/new.log")
        self.assertEqual(d["_against"], {"backup.log": "/var/log/backup.log"})
        self.assertEqual(col.read_state()["pending"], {})
        st = col.page_round(DET)
        self.assertEqual((col.BAK["log"], col.SEC["immutable_files"]), ("/var/log/new.log", ["/etc/x"]))
        self.assertIn("backup.log", st["from_page"])
        # a later hand edit of that setting in collector.toml wins over the approved value
        col2, _ = load_collector(BASE.replace("backup.log", "hand.log"), tmp=self.tmp)
        col2.page_round(DET)
        self.assertEqual((col2.BAK["log"], col2.SEC["immutable_files"]), ("/var/log/hand.log", ["/etc/x"]))

    def test_approve_refuses_when_the_page_changed_it_meanwhile(self):
        col = self.col
        plan, seen = self.pending({"backup.log": ch("/var/log/backup.log", "/var/log/a.log")})
        send(col, {"backup.log": ch("/var/log/backup.log", "/var/log/b.log")})
        col.page_round(DET)
        with self.assertRaises(col.CheckFailed):
            col.approve(plan, seen)
        self.assertFalse(os.path.exists(col.PAGE_TOML))

    def test_broken_page_toml_is_ignored_not_a_crash(self):
        col = self.col
        with open(col.PAGE_TOML, "w") as f:
            f.write("[backup\nlog = ")
        st = col.page_round(DET)
        self.assertIn("can't be used", st["error"])
        self.assertEqual(col.BAK["log"], "/var/log/backup.log")
        load_collector(BASE, tmp=self.tmp)   # and a restart doesn't crash either

    def test_only_approval_keys_count_in_page_toml(self):
        col = self.col
        with open(col.PAGE_TOML, "w") as f:
            f.write('out_dir = "/elsewhere"\ninterval = 9\n[backup]\nlog = "/var/log/approved.log"\n[_against]\n"backup.log" = "/var/log/backup.log"\n')
        col.page_round(DET)
        self.assertEqual(col.read_approved(), {"backup": {"log": "/var/log/approved.log"}})
        self.assertEqual((col.BAK["log"], col.CFG["interval"]), ("/var/log/approved.log", 5))
        self.assertNotEqual(col.OUT_DIR, "/elsewhere")

    def test_an_approval_that_lost_to_a_hand_edit_stays_out(self):
        """Approved X over A; collector.toml edited to B (B wins); collector.toml back to A: X must not come back
        without --apply showing it again."""
        col = self.col
        plan, seen = self.pending({"backup.log": ch("/var/log/backup.log", "/var/log/x.log")})
        col.approve(plan, seen)
        col2, _ = load_collector(BASE.replace("backup.log", "b.log"), tmp=self.tmp)
        col2.page_round(DET)
        self.assertEqual(col2.BAK["log"], "/var/log/b.log")
        col3, _ = load_collector(BASE, tmp=self.tmp)
        col3.page_round(DET)
        self.assertEqual(col3.BAK["log"], "/var/log/backup.log", "the old approval stays out")
        send(col3, {"backup.log": ch("/var/log/backup.log", "/var/log/y.log")})
        col3.page_round(DET)
        st = col3.read_state()
        col3.approve([("backup.log", "/var/log/y.log")], st["pending"])
        self.assertEqual(col3.read_state()["superseded"], {})
        col3.page_round(DET)
        self.assertEqual(col3.BAK["log"], "/var/log/y.log")

    def test_approved_paths_are_never_read_through_a_link(self):
        col = self.col
        d = os.path.join(self.tmp, "dashdata")
        os.mkdir(d)
        secret = os.path.join(self.tmp, "secret.txt")
        with open(secret, "w") as f:
            f.write("2026-10-29 01:00 root-only line\n")
        os.symlink(secret, d + "/backup.log")
        os.symlink(self.tmp, d + "/sub")
        for path in (d + "/backup.log", d + "/sub/secret.txt"):
            plan, seen = self.pending({"backup.log": ch(col.CFG["backup"]["log"], path)})
            col.approve(plan, seen)
            col.page_round(DET)
            self.assertEqual(col.BAK["log"], path)
            self.assertIn("backup.log", col.APPROVED_NOW)
            self.assertEqual(col.tail_lines(path, key="backup.log"), [], path)
            self.assertIsNone(col.backups()["last_run"], path)
        self.assertTrue(col.tail_lines(secret, key="backup.log"), "a plain file is still read")
        with self.assertRaises(col.CheckFailed):   # how the Authelia database is read when the page set its path
            col.read_regular(d + "/sub/secret.txt", 1000, no_links=True)
        self.assertTrue(col.read_regular(secret, 1000, no_links=True))

    def test_undo_drops_everything_even_with_a_folder_in_the_way(self):
        col = self.col
        plan, seen = self.pending({"backup.log": ch("/var/log/backup.log", "/var/log/new.log")})
        col.approve(plan, seen)
        send(col, {"interval": ch(5, 7)})
        col.page_round(DET)
        os.remove(col.PAGE_REQ)
        os.mkdir(col.PAGE_REQ)
        fd = os.open(col.PAGE_REQ, os.O_RDONLY | os.O_DIRECTORY)
        for _ in range(3000):   # deeper than Python's recursion limit and longer than any path may be
            os.mkdir("d", dir_fd=fd)
            nfd = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = nfd
        os.symlink("/etc", "link", dir_fd=fd)
        os.close(fd)
        with contextlib.redirect_stdout(io.StringIO()):
            problems, aside = col.undo_page_settings()
            self.assertEqual(problems, [])
            col.remove_tree(aside)
        st = col.page_round(DET)
        self.assertEqual((col.CFG["interval"], col.BAK["log"], st["from_page"]), (5, "/var/log/backup.log", []))
        self.assertFalse(os.path.lexists(col.PAGE_REQ))
        self.assertFalse(os.path.lexists(col.PAGE_LAST_FILE))
        self.assertFalse([n for n in os.listdir(col.OUT_DIR) if n.startswith(".undo-")])


class Display(unittest.TestCase):
    """What --apply shows root's terminal: nothing from the page can steer it, and nothing is cut off."""
    def setUp(self):
        self.col, self.tmp = load_collector()

    def test_shown_escapes_everything_but_plain_ascii(self):
        s = self.col.shown("a\x1b[8m\x07\x1b]52;c;x\x07‮\u3164а\\u001b")
        for raw in ("\x1b", "\x07", "‮", "\u3164", "а"):
            self.assertNotIn(raw, s)
        self.assertIn("\\\\u001b", s, "a typed backslash can't pass for an escaped character")

    def test_only_the_change_is_shown_and_marked(self):
        col = self.col
        jobs = [{"label": f"Job {i}", "regex": "done", "files": [f"/srv/b/{i}/*.tar"], "every": "nightly"} for i in range(7)]
        after = [dict(j) for j in jobs]
        after[3] = {**after[3], "files": after[3]["files"] + ["/root/*"]}
        lines = col.diff_lines(jobs, after)
        text = "\n".join(lines)
        self.assertIn("~ Job 3:", text)
        self.assertIn('+ "/root/*"', text)
        self.assertIn("(6 unchanged)", text)
        self.assertLess(len(lines), 8, text)
        self.assertEqual(col.summary(jobs, after), "1 changed")
        long = "done " + "(a+)+" * 60
        text = "\n".join(col.diff_lines("/var/log/a.log", long))
        self.assertIn(long, text, "never cut")
        self.assertIn("+ new entry: X", "\n".join(col.diff_lines(None, [{"label": "X", "regex": "y"}])))
        self.assertIn("- removed entry: Job 0", "\n".join(col.diff_lines(jobs, jobs[1:])))
        w = col.diff_lines({"label": "w", "heartbeat": "/a"}, {"label": "w", "heartbeat": "/b", "checks": ["c"]})
        self.assertEqual([x.strip() for x in w], ['~ heartbeat: "/a" -> "/b"', "+ checks:", '+ "c"'])
        self.assertIn("\\u001b", "\n".join(col.diff_lines(None, ["/x\x1b[2J"])))

    def test_folder_warnings(self):
        col = self.col
        d = os.path.join(self.tmp, "mine")
        os.mkdir(d)
        self.assertTrue(any("the dashboard container runs as" in w for w in col.folder_warnings(d + "/x.log")) or os.getuid() == 0)
        os.symlink("/etc/hostname", d + "/link")
        self.assertTrue(any("is a link to" in w for w in col.folder_warnings(d + "/link")))
        os.symlink("/etc", d + "/dirlink")
        ws = col.folder_warnings(d + "/dirlink/hostname")
        self.assertTrue(any("is a link to" in w for w in ws) and not any("anyone can write" in w for w in ws), ws)
        self.assertTrue(col.folder_warnings(d + "/*/x.log"), "the folder a pattern lists is checked too")


class Mounts(unittest.TestCase):
    def test_sandbox_binds_are_not_drives(self):
        col, tmp = load_collector()
        info = os.path.join(tmp, "mountinfo")
        out = col.OUT_DIR
        with open(info, "w") as f:
            f.write("\n".join([
                "1 0 8:2 / / ro,relatime shared:1 - ext4 /dev/nvme0n1p2 rw",
                "2 1 8:2 /etc /etc ro,relatime - ext4 /dev/nvme0n1p2 rw",
                "3 1 8:2 /usr /usr ro,relatime - ext4 /dev/nvme0n1p2 rw",
                "4 1 8:2 /tmp/systemd-private-abc/tmp /tmp rw - ext4 /dev/nvme0n1p2 rw",
                f"5 1 8:2 {out} {out} rw - ext4 /dev/nvme0n1p2 rw",
                "6 1 8:3 / /home ro - ext4 /dev/nvme0n1p3 rw",
                "7 1 0:40 /data /mnt/data rw - btrfs /dev/sdb rw",
                "8 1 8:17 / /media/me/big\\040disk rw - ext4 /dev/sdc1 rw",
                "9 1 0:22 / /proc rw - proc proc rw",
                "10 1 8:2 /root /root ro - ext4 /dev/nvme0n1p2 rw",
                "11 1 0:41 /data /data rw - btrfs /dev/sdb rw",
                "12 1 8:2 /srv/www /srv/www ro - ext4 /dev/nvme0n1p2 rw"]) + "\n")
        self.assertEqual(col.real_mounts(info), ["/", "/data", "/home", "/media/me/big disk", "/mnt/data"])


class Startup(unittest.TestCase):
    def test_a_toml_date_in_collector_toml_is_fine(self):
        col, _ = load_collector("note_installed = 2026-10-29\n" + BASE)
        send(col, {"interval": ch(5, 7)})
        col.page_round(DET)
        self.assertEqual(col.CFG["interval"], 7)


class NoNameClash(unittest.TestCase):
    def test_existing_names_still_mean_what_they_did(self):
        """The page-settings code added many module names; none may replace an existing one (PAGE, the memory page
        size, once got shadowed and broke the CPU/memory breakdown)."""
        col, _ = load_collector()
        self.assertIsInstance(col.PAGE, int)
        self.assertIsNotNone(col.USAGE.sample() or True)
        import time as _t; _t.sleep(0.3)
        self.assertIsInstance(col.USAGE.sample(), (dict, type(None)))


class Toml(unittest.TestCase):
    def setUp(self):
        self.col, _ = load_collector()

    def test_round_trip(self):
        d = {"security": {"authelia_db": "/srv/a b/db.sqlite3", "immutable_files": ["/etc/a", '/q"uote\\d'],
                          "watcher": {"label": "check_miner.sh (root cron)", "heartbeat": "/var/lib/w/high", "every_minutes": 5,
                                      "checks": ["known names (tcrond, xmrig…)", "programs from /tmp"], "kinds": {"cpu": "high CPU", "tmp-x": "from /tmp"}}},
             "backup": {"log": "/var/log/b.log", "success_regex": r"backup (ok|done)\s+\d{2}$", "failure_regex": "(?i)error",
                        "checks": [{"label": "Offsite → backup box", "regex": "copy .* ok", "min_count": 2}],
                        "artifacts": [{"label": "DB", "glob": "/srv/db/*.sql.gz"}],
                        "jobs": [{"label": "Nightly", "regex": "done", "files": ["/m/*.tar"], "dest": "/m", "size": True, "every": "nightly"},
                                 {"label": "Second", "regex": "x", "max_age_hours": 30.5}]}}
        against = {"backup.log": "/old.log", "backup.jobs": [{"label": "Old", "regex": "y"}]}
        back = tomllib.loads(self.col.toml_dumps(d, against))
        self.assertEqual(back.pop("_against"), against)
        self.assertEqual(back, d)

    def test_unprintable_characters_are_escaped(self):
        s = self.col.toml_str("/var/log/x\x1b[2J‮gnp.exe\x00")
        for raw in ("\x1b", "‮", "\x00"):
            self.assertNotIn(raw, s)
        self.assertEqual(tomllib.loads(f"v = {s}")["v"], "/var/log/x\x1b[2J‮gnp.exe\x00")


class SelfTest(unittest.TestCase):
    def test_facts_only(self):
        col, _ = load_collector(BASE)
        send(col, {"security.firewall": ch(None, False)})   # page values never steer the self-test
        col.page_round(DET)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            col.selftest()
        res = json.loads(buf.getvalue().strip().splitlines()[-1])
        self.assertEqual(res["write"], "ok")
        self.assertEqual(os.listdir(col.OUT_DIR).count(".selftest"), 0)
        self.assertFalse([n for n in os.listdir(col.OUT_DIR) if n.startswith(".selftest-")], "the probe folder is removed")
        for k, v in res.items():
            if k != "detect.mounts":
                self.assertLess(len(v), 60, (k, v))
                self.assertNotRegex(v, r"\d\d:\d\d", (k, v))


if __name__ == "__main__":
    unittest.main()
