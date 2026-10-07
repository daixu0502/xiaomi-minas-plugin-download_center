from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'payload/files'))
from service import session_without_force_save, normalize_aria_session


class AriaSessionTests(unittest.TestCase):
    def test_rewrite_only_indented_option_preserves_resume(self):
        original = b'# session\r\nhttps://example.test/force-save=true\r\n gid=0123456789abcdef\r\n force-save=true\r\n pause=true\r\n dir=/user/files\r\n\n'
        actual = session_without_force_save(original)
        self.assertEqual(actual, original.replace(b' force-save=true', b' force-save=false'))
        self.assertEqual(session_without_force_save(actual), actual)

    def test_drop_only_known_completed_gid(self):
        failed = b'https://example.test/failed\n gid=2222222222222222\n pause=true\n'
        complete = b'https://example.test/completed\n gid=1111111111111111\n force-save=true\n'
        untouched = b'https://example.test/queued\n dir=/user/files\n'
        self.assertEqual(session_without_force_save(complete + failed + untouched, ['1111111111111111']), failed + untouched)
        self.assertEqual(session_without_force_save(failed, ['not-a-gid']), failed)

    def test_atomic_migration_private_and_no_leftovers(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'aria2.session'
            p.write_bytes(b'https://example.test/file\n force-save=true\n')
            normalize_aria_session(p)
            self.assertIn(b'force-save=false', p.read_bytes())
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            self.assertEqual([f.name for f in Path(folder).iterdir()], ['aria2.session'])

    def test_startup_disables_forced_completed_control_files(self):
        source = (Path(__file__).resolve().parents[1] / 'payload/files/service.py').read_text()
        self.assertIn('"force-save": "false"', source)
        self.assertIn('"continue": "true"', source)
        self.assertIn('normalize_aria_session(session)', source)


if __name__ == '__main__':
    unittest.main()
