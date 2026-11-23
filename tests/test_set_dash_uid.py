#!/usr/bin/env python3
"""Tests for collector/set_dash_uid.py: set/replace the dash_uid line of collector.toml."""

import importlib.util
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(HERE, '..', 'collector', 'set_dash_uid.py')

_spec = importlib.util.spec_from_file_location('set_dash_uid', HELPER)
_helper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helper)
set_dash_uid = _helper.set_dash_uid

BAD_UIDS = [
    '0', '-1', '', '1000;rm -rf /', '1e3', ' 1000',
    '01000', '2147483647', '99999999999', '\uff10\uff10\uff10\uff10',
]


def dash_lines(text):
    """The lines of text that talk about dash_uid."""
    return [line for line in text.splitlines() if 'dash_uid' in line]


def dash_value(line):
    """The value of the dash_uid setting on a line ('' if it is not such a line)."""
    match = re.match(r'^\s*#?\s*dash_uid\s*=\s*(\S+)\s*$', line)
    return match.group(1) if match else ''


class SetDashUidTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='set_dash_uid_test_')
        self.path = os.path.join(self.dir, 'collector.toml')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, text, path=None):
        path = self.path if path is None else path
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(text)
        return path

    def read(self, path=None):
        path = self.path if path is None else path
        with open(path, 'r', encoding='utf-8') as fh:
            return fh.read()

    def run_cli(self, path, uid):
        return subprocess.run([sys.executable, HELPER, path, uid],
                              capture_output=True, text=True)

    def test_existing_line_is_replaced_other_lines_are_untouched(self):
        original = ('# collector config\n'
                    'out_dir = "/var/lib/homelab-dash"\n'
                    'dash_uid = 1000\n'
                    'poll = 30\n')
        self.write(original)
        lines = set_dash_uid(self.path, '1001').splitlines()
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[0], '# collector config')
        self.assertEqual(lines[1], 'out_dir = "/var/lib/homelab-dash"')
        self.assertEqual(lines[2].strip(), 'dash_uid = 1001')
        self.assertEqual(lines[3], 'poll = 30')

    def test_commented_line_is_replaced(self):
        self.write('# collector config\n# dash_uid = 1000\npoll = 30\n')
        lines = set_dash_uid(self.path, '1001').splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], '# collector config')
        self.assertEqual(lines[2], 'poll = 30')
        self.assertNotIn('1000', '\n'.join(lines))
        found = dash_lines('\n'.join(lines))
        self.assertEqual(len(found), 1)
        self.assertEqual(dash_value(found[0]), '1001')

    def test_missing_line_is_appended_on_its_own_line(self):
        for original in ('out_dir = "/var/lib"\npoll = 30\n', 'out_dir = "/var/lib"\npoll = 30'):
            with self.subTest(trailing_newline=original.endswith('\n')):
                self.write(original)
                lines = set_dash_uid(self.path, '1001').splitlines()
                self.assertEqual(lines[0], 'out_dir = "/var/lib"')
                self.assertEqual(lines[1], 'poll = 30')
                self.assertEqual(lines[2].strip(), 'dash_uid = 1001')

    def test_missing_file_is_created(self):
        path = os.path.join(self.dir, 'fresh.toml')
        proc = self.run_cli(path, '1001')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.isfile(path))
        found = dash_lines(self.read(path))
        self.assertEqual(len(found), 1)
        self.assertEqual(dash_value(found[0]), '1001')

    def test_backup_of_the_old_file_is_written(self):
        original = 'dash_uid = 1000\npoll = 30\n'
        self.write(original)
        proc = self.run_cli(self.path, '1001')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        backup = self.path + '.bak'
        self.assertTrue(os.path.isfile(backup))
        self.assertEqual(self.read(backup), original)

    def test_file_mode_is_kept(self):
        self.write('dash_uid = 1000\n')
        os.chmod(self.path, 0o640)
        proc = self.run_cli(self.path, '1001')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o640)

    def test_bad_uid_raises_and_leaves_the_file_alone(self):
        original = 'dash_uid = 1000\npoll = 30\n'
        self.write(original)
        for bad in BAD_UIDS:
            with self.subTest(uid=bad):
                with self.assertRaises(ValueError):
                    set_dash_uid(self.path, bad)
                self.assertEqual(self.read(self.path), original)

    def test_symlinked_path_is_refused(self):
        target = self.write('dash_uid = 1000\n', os.path.join(self.dir, 'target.toml'))
        link = os.path.join(self.dir, 'link.toml')
        os.symlink(target, link)
        proc = self.run_cli(link, '1001')
        self.assertEqual(proc.returncode, 1)
        self.assertTrue(os.path.islink(link))
        self.assertEqual(self.read(target), 'dash_uid = 1000\n')

    def test_command_line_exits_2_on_a_bad_uid(self):
        self.write('dash_uid = 1000\n')
        proc = self.run_cli(self.path, '0')
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(self.read(self.path), 'dash_uid = 1000\n')


if __name__ == '__main__':
    unittest.main()
