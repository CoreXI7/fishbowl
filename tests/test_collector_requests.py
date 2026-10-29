"""The root collector reads approval requests from a folder the dash container (uid 1000) can write to.
Whatever the dash puts there (a FIFO, a symlink, a huge file) must not stall the collector or make root
open a file the dash chose."""
import importlib.machinery
import os
import tempfile
import threading
import unittest

from helpers import ROOT

COLLECTOR = os.path.join(ROOT, "collector", "homelab-dash-collector.py")


def load_collector():
    tmp = tempfile.mkdtemp(prefix="hd-col-")
    conf = os.path.join(tmp, "collector.toml")
    with open(conf, "w") as f:
        f.write(f'out_dir = "{tmp}/out"\ndash_uid = {os.getuid()}\n[security]\ncron_watch = true\n')
    os.environ["HOMELAB_DASH_COLLECTOR_CONFIG"] = conf
    col = importlib.machinery.SourceFileLoader("collector_under_test", COLLECTOR).load_module()
    os.makedirs(col.REQ_DIR, exist_ok=True)
    return col, tmp


class RequestFolder(unittest.TestCase):
    def setUp(self):
        self.col, self.tmp = load_collector()

    def run_with_timeout(self, secs=3):
        done, out = threading.Event(), []
        threading.Thread(target=lambda: (out.append(self.col.handle_cron_approval()), done.set()), daemon=True).start()
        return done.wait(secs), out

    def test_fifo_does_not_block(self):
        os.mkfifo(self.col.REQ_FILE)
        finished, out = self.run_with_timeout()
        if not finished:                       # unblock the stuck thread so the test run can end
            fd = os.open(self.col.REQ_FILE, os.O_WRONLY | os.O_NONBLOCK)
            os.close(fd)
        self.assertTrue(finished, "handle_cron_approval() blocked on a FIFO: the collector's main loop would stop")
        self.assertEqual(out, [False])
        self.assertFalse(os.path.lexists(self.col.REQ_FILE))

    def test_symlink_is_not_followed(self):
        want = self.col.cron_hash(self.col.cron_files())
        target = os.path.join(self.tmp, "elsewhere")
        with open(target, "w") as f:
            f.write(want)                     # a valid approval, but reached through a link
        os.symlink(target, self.col.REQ_FILE)
        finished, out = self.run_with_timeout()
        self.assertTrue(finished)
        self.assertEqual(out, [False], "the collector followed a symlink in the request folder")
        self.assertFalse(os.path.lexists(self.col.REQ_FILE))
        self.assertTrue(os.path.exists(target), "only the link is removed, never its target")

    def test_regular_file_still_works(self):
        with open(self.col.REQ_FILE, "w") as f:
            f.write(self.col.cron_hash(self.col.cron_files()))
        finished, out = self.run_with_timeout()
        self.assertEqual(out, [True])
        self.assertFalse(os.path.exists(self.col.REQ_FILE))

    def test_wrong_hash_is_refused(self):
        with open(self.col.REQ_FILE, "w") as f:
            f.write("0" * 64)
        self.assertEqual(self.run_with_timeout()[1], [False])


if __name__ == "__main__":
    unittest.main()
