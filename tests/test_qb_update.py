"""No network: release checks, rollback and tracker application contracts."""
import hashlib
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'payload/files'))
from download_lib import DownloadError, atomic_json
from dual_engine import DualManager
import qb_update as update


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        (home / 'var').mkdir(); (home / 'data').mkdir(); (home / 'src/files').mkdir(parents=True)
        self.m = DualManager(home, home / 'data', False)
        (home / 'src/files/qbittorrent-nox').write_bytes(b'old')

    def test_version_probe_isolates_inherited_root_home(self):
        seen = []
        def probe(args, **kwargs):
            env = kwargs['env']
            self.assertNotEqual(env['HOME'], '/root')
            self.assertTrue(Path(env['HOME']).is_dir())
            for key in ('XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_RUNTIME_DIR'):
                self.assertTrue(Path(env[key]).is_relative_to(env['HOME']))
            seen.append(Path(env['HOME']))
            return subprocess.CompletedProcess(args, 0, 'qBittorrent v5.2.4\n', '')
        with patch.dict(os.environ, HOME='/root', XDG_CACHE_HOME='/root/.cache'), patch.object(update.subprocess, 'run', side_effect=probe):
            self.assertEqual(update.binary_version(self.m.home / 'src/files/qbittorrent-nox'), '5.2.4')
        self.assertFalse(seen[0].exists())

    def test_version_probe_keeps_specific_failure(self):
        for exc, message in ((subprocess.TimeoutExpired('qb', 10), '10 秒'),
                             (subprocess.CalledProcessError(-6, 'qb'), '信号 6'),
                             (PermissionError(13, 'denied'), 'errno=13')):
            with patch.object(update.subprocess, 'run', side_effect=exc), self.assertRaisesRegex(DownloadError, message):
                update.binary_version(self.m.home / 'src/files/qbittorrent-nox')

    def test_release_filters_and_requires_digest(self):
        def release(tag, **extra):
            return dict(tag_name=tag, assets=[dict(name='aarch64-qbittorrent-nox', size=80, digest='sha256:'+'a'*64)], **extra)
        data = [release('release-5.2.4_v2.0.15'), release('release-5.2.4_v2.0.16'),
                release('release-6.0.0_v2.0.16'), release('release-5.3.0_v2.0.16', prerelease=True)]
        with patch.object(update, 'fetch', return_value=json.dumps(data)):
            result = update.release(False, 'aarch64')
        self.assertEqual(result['libtorrent'], '2.0.16')
        self.assertTrue(update.newer(result, dict(current='5.2.4', currentLibtorrent='2.0.15')))
        self.assertFalse(update.newer(result, dict(current='5.3.0', currentLibtorrent='2.0.15')))
        data[1]['assets'][0].pop('digest')
        with patch.object(update, 'fetch', return_value=json.dumps(data)), self.assertRaises(DownloadError):
            update.release(False, 'aarch64')

    def test_invalid_binary_never_executed(self):
        content = b'not an executable'
        info = dict(size=len(content), digest=hashlib.sha256(content).hexdigest(), arch='aarch64', latest='5.2.4')
        with patch.object(update, 'binary_version', side_effect=AssertionError('must not execute')):
            with self.assertRaises(DownloadError): update.unpack(content, info, self.m.var / 'candidate')
        self.assertFalse((self.m.var / 'candidate').exists())

    def test_rollback_restores_profile_and_enabled_state(self):
        profile = self.m.var / 'qb-profile'; profile.mkdir(); (profile / 'state').write_text('original')
        candidate = self.m.var / 'candidate'; candidate.write_bytes(b'new')
        (self.m.var / 'enabled').touch()
        calls = []
        def control(m, op):
            calls.append(op)
            if op == 'start' and calls.count('start') == 1: (profile / 'state').write_text('updated')
        with patch('service.live', return_value=1), patch('service.control_locked', side_effect=control), \
             patch.object(update, 'binary_version', return_value='5.2.4'), \
             patch.object(update, 'wait_running', side_effect=[DownloadError('test failure'), None]):
            with self.assertRaises(DownloadError): update.install_candidate(self.m, candidate, '5.2.5')
        self.assertEqual(calls, ['stop', 'start', 'stop', 'start'])
        self.assertEqual(update.core_path(self.m).read_bytes(), b'old')
        self.assertEqual((profile / 'state').read_text(), 'original')
        self.assertTrue((self.m.var / 'enabled').exists())
        self.assertEqual((self.m.home / 'src/files/qbittorrent-nox').read_bytes(), b'old')

    def test_disabled_update_selftests_then_stops(self):
        candidate = self.m.var / 'candidate'; candidate.write_bytes(b'new')
        with patch('service.live', return_value=None), patch('service.control_locked') as control, \
             patch.object(update, 'binary_version', return_value='5.2.4'), patch.object(update, 'wait_running'):
            update.install_candidate(self.m, candidate, '5.2.5')
        self.assertEqual([c.args[1] for c in control.call_args_list], ['start', 'stop'])
        self.assertFalse((self.m.var / 'enabled').exists())
        self.assertEqual(update.core_path(self.m).read_bytes(), b'new')

    def test_update_mutual_exclusion(self):
        import core_update
        with patch.object(core_update, 'state', return_value={'state': 'downloading'}), self.assertRaises(DownloadError):
            update.queue(self.m, 'check')
        with patch.object(update, 'state', return_value={'state': 'installing'}), self.assertRaises(DownloadError):
            core_update.queue(self.m, 'check')

    def test_tracker_verification_does_not_mutate_tasks(self):
        atomic_json(self.m.settings_path, dict(self.m.settings(), trackers=['https://example.test/announce']))
        def request(endpoint):
            if 'properties' in endpoint: return {'is_private': False}
            if 'trackers?' in endpoint: return [{'url': 'https://example.test/announce', 'tier': 0, 'status': 2}, {'url': '** [DHT] **', 'tier': -1, 'status': 2}]
            raise AssertionError(endpoint)
        with patch.object(self.m.qb, 'torrents', return_value=[{'hash':'a'*40,'name':'external'}]), patch.object(self.m.qb, 'request', side_effect=request):
            result = self.m.verify_trackers()
        self.assertEqual(result['rows'][0]['matched'], 1)
        self.assertEqual(result['rows'][0]['working'], 1)
        self.assertFalse(result['rows'][0]['owned'])
        self.assertNotIn('url', result['rows'][0])

    def test_public_tracker_called_only_after_metadata(self):
        atomic_json(self.m.settings_path, dict(self.m.settings(), trackers=['https://example.test/announce']))
        with patch.object(self.m.qb, 'files', return_value=[{'name':'test'}]), patch.object(self.m.qb, 'request', return_value={'is_private':False}) as api:
            self.assertEqual(self.m.apply_qb_trackers({'gid':'a'*40}), 'applied')
        self.assertEqual(api.call_args.args, ('torrents/addTrackers', {'hash':'a'*40,'urls':'https://example.test/announce'}))
        with patch.object(self.m.qb, 'files', return_value=[]), patch.object(self.m.qb, 'request', return_value={'is_private':False}) as api:
            self.assertEqual(self.m.apply_qb_trackers({'gid':'a'*40}), 'metadata')
        self.assertEqual(api.call_count, 1)


if __name__ == '__main__': unittest.main()
