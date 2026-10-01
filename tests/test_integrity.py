"""Regression tests for firmware boot verification and out-of-tree core updates."""
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'payload/files'))
from integrity import source_abstract
from download_lib import core_path, DownloadError
import core_update
import service


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        home = Path(self.temp.name)
        (home / 'src/files').mkdir(parents=True)
        (home / 'var').mkdir()
        self.m = SimpleNamespace(home=home, var=home / 'var')
        self.binary = home / 'src/files/aria2c'
        self.binary.write_bytes(b'old-core')
        self.candidate = home / 'candidate'
        self.candidate.write_bytes(b'new-core')

    def test_matches_firmware_pipeline(self):
        src = self.m.home / 'src'
        (src / 'z').write_bytes(b'z')
        (src / 'A').write_bytes(b'a')
        (src / '中文').write_bytes(b'utf8')
        (src / 'link').symlink_to(self.candidate)
        shell = '''find "$1/" -type f | LC_COLLATE=C sort | while IFS= read -r p; do sha256sum "$p" | cut -d ' ' -f 1; done | sha256sum | cut -d ' ' -f 1'''
        expected = subprocess.check_output(['sh', '-c', shell, 'verify', str(src)], text=True).strip()
        self.assertEqual(source_abstract(src), expected)
        self.assertNotEqual(expected, hashlib.sha256(self.binary.read_bytes()).hexdigest())

    def test_update_does_not_change_source_abstract(self):
        before = source_abstract(self.m.home / 'src')
        self.assertEqual(core_path(self.m), self.binary)
        with patch.object(service, 'live', return_value=0), patch.object(core_update, 'binary_version', return_value='1.37.0'):
            core_update.install_candidate(self.m, self.candidate, '1.38.0')
        self.assertEqual(core_path(self.m), self.m.var / 'core/aria2c')
        self.assertEqual(core_path(self.m).read_bytes(), b'new-core')
        self.assertEqual(source_abstract(self.m.home / 'src'), before)
        self.assertEqual(list((self.m.var / 'core').iterdir()), [core_path(self.m)])
        self.assertFalse((self.m.var / 'enabled').exists())

    def test_failed_update_rolls_back_without_changing_source(self):
        (self.m.var / 'enabled').touch()
        before = source_abstract(self.m.home / 'src')
        with patch.object(service, 'live', return_value=123), patch.object(service, 'control_locked'), \
                patch.object(core_update, 'binary_version', return_value='1.37.0'), \
                patch.object(core_update, 'wait_running', side_effect=[DownloadError('test failure'), None]):
            with self.assertRaises(DownloadError):
                core_update.install_candidate(self.m, self.candidate, '1.38.0')
        self.assertEqual(core_path(self.m).read_bytes(), b'old-core')
        self.assertTrue((self.m.var / 'enabled').exists())
        self.assertEqual(source_abstract(self.m.home / 'src'), before)


if __name__ == '__main__':
    unittest.main()
