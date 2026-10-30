"""The root collector reads files in folders other parties can write to (a backup container's log folder, the
Authelia database folder, a user's ~/.ssh). Found in the pre-release audit (2026-10-30): root opened whatever was
there before checking its type (opening a device node runs its driver: /dev/watchdog resets the host), followed
symlinks in paths from collector.toml (showing root-only files on the card), and briefly wrote the unredacted crontab
snapshot readable by everyone."""
import os
import stat
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


if __name__ == "__main__":
    unittest.main()
