"""Regression: qB 5.2 changes a hybrid magnet API hash after metadata."""
import json
import sys
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'payload/files'))
from dual_engine import DualManager
from download_lib import DownloadError
from qb_client import QBAPIError


class HybridTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        (home / 'var/metadata/task').mkdir(parents=True)
        (home / 'data').mkdir()
        self.m = DualManager(home, home / 'data', False)
        self.v1, self.v2 = 'a' * 40, 'b' * 40
        self.snap = dict(engine='qbittorrent', infoHash=self.v1, metadataPending=True,
                         initializing=True, status='waiting', directory='', addedAt=time.time() - 60)
        self.source = dict(url='magnet:?xt=urn:btih:' + self.v1, requestedPause=False)
        with self.m.db() as db:
            db.execute('INSERT INTO tasks VALUES (?,?,?,?,?)', ('task', self.v1, 0, json.dumps(self.source), json.dumps(self.snap)))
        self.item = dict(hash=self.v2, infohash_v1=self.v1, infohash_v2=self.v2 + 'b' * 24,
                         state='stoppedDL', save_path=str(home / 'var/metadata/task'))

    def test_torrent_lookup_matches_v1_alias(self):
        with patch.object(self.m.qb, 'request', return_value=[self.item]):
            self.assertEqual(self.m.qb.torrents(self.v1), [self.item])
            self.assertEqual(self.m.qb.torrents('c' * 40), [])

    def test_sync_rebinds_canonical_hash_without_losing_identity(self):
        with patch.object(self.m.qb, 'torrents', return_value=[self.item]), patch.object(self.m.qb, 'files', return_value=[]):
            self.m.sync('task')
        row = self.m.row('task')
        self.assertEqual(row['gid'], self.v2)
        snap = json.loads(row['snapshot'])
        self.assertEqual(snap['infoHash'], self.v1)
        self.assertEqual(snap['status'], 'waiting')
        self.assertNotIn('errorMessage', snap)

    def test_missing_metadata_409_is_pending_but_404_is_error(self):
        with patch.object(self.m.qb, 'request', side_effect=QBAPIError('torrents/files', 409)):
            self.assertEqual(self.m.qb.files(self.v2), [])
        with patch.object(self.m.qb, 'request', side_effect=QBAPIError('torrents/files', 404)), self.assertRaises(DownloadError):
            self.m.qb.files(self.v2)

    def test_retry_pending_metadata_uses_canonical_hash_without_readding(self):
        self.snap.update(status='error', errorMessage='qBittorrent 中未找到任务，可重试恢复')
        self.m.save_row({'id': 'task'}, self.snap)
        with patch.object(self.m.qb, 'torrents', return_value=[self.item]), \
             patch.object(self.m.qb, 'request') as api, \
             patch.object(self.m.qb, 'files', side_effect=AssertionError('metadata not ready')), \
             patch.object(self.m.qb, 'add', side_effect=AssertionError('must not duplicate')):
            self.m.operate('task', 'retry')
        api.assert_called_once_with('torrents/start', {'hashes': self.v2})
        self.assertEqual(json.loads(self.m.row('task')['snapshot'])['errorMessage'], '')

    def test_metadata_retry_refuses_changed_path(self):
        self.snap['status'] = 'error'; self.m.save_row({'id': 'task'}, self.snap)
        self.item['save_path'] = str(self.m.root)
        with patch.object(self.m.qb, 'torrents', return_value=[self.item]), patch.object(self.m.qb, 'request') as api:
            with self.assertRaisesRegex(DownloadError, '缓存路径已改变'): self.m.operate('task', 'retry')
        api.assert_not_called()

    def test_export_409_keeps_initializing(self):
        row, item = self.m.resolve_qb_row(self.m.row('task'), [self.item])
        snap = json.loads(row['snapshot'])
        with patch.object(self.m.qb, 'files', return_value=[{'name': 'pending'}]), \
             patch.object(self.m.qb, 'request', side_effect=QBAPIError('torrents/export', 409)):
            self.assertEqual(self.m.prepare_task(row, item, snap), snap)
        self.assertTrue(snap['initializing'])

    def test_metadata_still_validated_against_original_v1(self):
        row, item = self.m.resolve_qb_row(self.m.row('task'), [self.item])
        snap = json.loads(row['snapshot'])
        with patch.object(self.m.qb, 'files', return_value=[{'name': 'test'}]), \
             patch.object(self.m.qb, 'request', return_value=b'torrent'), \
             patch('dual_engine.torrent_info', return_value={'infoHash': self.v1, 'files': []}), \
             patch.object(self.m, 'bt_options', side_effect=DownloadError('passed hash verification')):
            with self.assertRaisesRegex(DownloadError, 'passed hash verification'):
                self.m.prepare_task(row, item, snap)

    def test_no_match_by_filename_or_path(self):
        self.item['infohash_v1'] = 'c' * 40
        row, item = self.m.resolve_qb_row(self.m.row('task'), [self.item])
        self.assertIsNone(item)
        self.assertEqual(row['gid'], self.v1)


if __name__ == '__main__': unittest.main()
