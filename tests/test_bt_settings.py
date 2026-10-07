"""BT settings and task responsiveness; isolated files, no live NAS/network."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'payload/files'))
from download_lib import Manager, DownloadError, DEFAULTS, atomic_json
import service


class BtTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        home = Path(self.temp.name)
        (home / 'var').mkdir()
        (home / 'data').mkdir()
        self.m = Manager(home, home / 'data', require_mount=False)
        (self.m.var / 'peer.port').write_text('19400')
        (self.m.var / 'aria2.conf').write_text('enable-dht=true\nenable-dht6=false\nrpc-secret=do-not-expose\n')
        with self.m.db():
            pass

    def add_task(self, ident='test', state='active'):
        snap = {'status': state, 'directory': '', 'name': 'test.bin', 'ownedPaths': ['test.bin']}
        with self.m.db() as db:
            db.execute('INSERT INTO tasks VALUES (?,?,?,?,?)', (ident, ident, 1, '{}', json.dumps(snap)))

    def test_legacy_defaults_and_secret_redaction(self):
        self.assertFalse(self.m.bt_state()['restartRequired'])
        self.assertNotIn('secret', json.dumps(self.m.bt_state()))
        options = self.m.bt_network_options()
        self.assertEqual(options['enable-dht6'], 'false')
        self.assertEqual(options['bt-max-peers'], '55')
        self.assertEqual(options['disable-ipv6'], 'false')

    def test_save_preserves_other_settings_and_does_not_call_core(self):
        atomic_json(self.m.settings_path, dict(DEFAULTS, uploadKiB=512, trackers=['https://example.com/announce']))
        with patch.object(self.m, 'rpc', side_effect=AssertionError('must not change core')):
            result = self.m.save_bt_settings({'dht6': True, 'maxPeers': 128, 'peerSpeedKiB': 1024})
        self.assertTrue(result['restartRequired'])
        self.assertEqual(self.m.settings()['uploadKiB'], 512)
        self.assertEqual(len(self.m.settings()['trackers']), 1)
        opts = self.m.bt_network_options()
        self.assertEqual(opts['enable-dht6'], 'true')
        (self.m.var / 'aria2.conf').write_text('\n'.join(k+'='+v for k,v in opts.items()))
        self.assertFalse(self.m.bt_state()['restartRequired'])

    def test_reject_invalid_settings_without_writing(self):
        for data in [{'dht': 'true'}, {'ipv6': False, 'dht6': True}, {'maxPeers': 0}, {'maxPeers': True}, {'maxPeers': 501}, {'peerSpeedKiB': -1}]:
            with self.subTest(data=data), self.assertRaises(DownloadError):
                self.m.save_bt_settings(data)
        self.assertFalse(self.m.settings_path.exists())

    def test_snapshot_does_not_full_sync_or_inspect_files(self):
        def rpc(method, *args):
            return {'version':'1.37.0'} if method == 'getVersion' else {}
        with patch.object(self.m, 'sync', side_effect=AssertionError('full sync in read')), patch.object(self.m, 'rpc', side_effect=rpc):
            result = self.m.snapshot()
        self.assertTrue(result['running'])
        self.assertEqual(result['bt']['peerPort'], 19400)

    def test_target_sync_never_enumerates_all_tasks(self):
        self.add_task()
        self.add_task('unrelated')
        methods=[]
        def rpc(method, *args):
            methods.append(method)
            self.assertEqual(method, 'tellStatus')
            self.assertEqual(args[0], 'test')
            return {'gid':'test', 'status':'paused', 'files':[]}
        with patch.object(self.m, 'rpc', side_effect=rpc):
            self.m.sync('test')
        self.assertEqual(methods, ['tellStatus'])
        self.assertEqual(json.loads(self.m.row('test')['snapshot'])['status'], 'paused')
        self.assertEqual(json.loads(self.m.row('unrelated')['snapshot'])['status'], 'active')

    def test_registered_files_are_not_statted_again(self):
        self.add_task()
        (self.m.root / 'test.bin').write_bytes(b'test')
        item={'gid':'test','status':'paused','files':[]}
        with patch.object(self.m, 'rpc', return_value=item):
            self.m.sync('test')
            calls=[]
            original=Path.lstat
            def tracked(p, *args, **kwargs):
                calls.append(p)
                return original(p, *args, **kwargs)
            with patch.object(Path, 'lstat', tracked):
                self.m.sync('test')
        self.assertNotIn(self.m.root / 'test.bin', calls)

    def test_task_dispatch_uses_target_sync(self):
        self.add_task()
        with patch.object(self.m, 'sync') as sync, patch.object(self.m, 'operate', return_value={}) as operate:
            self.m.dispatch('task', {'id':'test','command':'pause'})
        self.assertEqual([c.args for c in sync.call_args_list], [('test',), ('test',)])
        operate.assert_called_once_with('test','pause',False)

    def test_restart_does_not_kill_or_stop_other_services(self):
        # Existing isolated stop/start helpers, never system reboot/docker control.
        original=service.control_locked
        commands=[]
        def child(m, command):
            commands.append(command)
        with patch.object(service,'control_locked',side_effect=child):
            original(self.m,'restart')
        self.assertEqual(commands,['stop','start'])


if __name__ == '__main__':
    unittest.main()
