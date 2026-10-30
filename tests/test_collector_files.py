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


if __name__ == "__main__":
    unittest.main()
