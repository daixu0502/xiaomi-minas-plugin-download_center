"""Isolated routing/security tests; optional real core via QB_TEST_BINARY."""
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlencode
from urllib.request import Request, build_opener, ProxyHandler, HTTPCookieProcessor
from http.cookiejar import CookieJar
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'payload/files'))
from download_lib import DownloadError, atomic_json, torrent_info
from dual_engine import DualManager
from openlist_gateway import map_path, start_gateway


def bencode(obj):
    if isinstance(obj, int): return b'i' + str(obj).encode() + b'e'
    if isinstance(obj, bytes): return str(len(obj)).encode() + b':' + obj
    if isinstance(obj, list): return b'l' + b''.join(bencode(i) for i in obj) + b'e'
    return b'd' + b''.join(bencode(k) + bencode(obj[k]) for k in sorted(obj)) + b'e'


def torrent(name=b'test-data', content=b'hello local test', private=1):
    return bencode({b'info': {b'name': name, b'length': len(content), b'piece length': 16384,
                             b'pieces': b''.join(hashlib.sha1(content[n:n+16384]).digest() for n in range(0, len(content), 16384)), b'private': private}})


def port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0)); return s.getsockname()[1]


class DualTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='minas-qb-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        (self.path / 'var').mkdir(); (self.path / 'data/Download').mkdir(parents=True)
        self.m = DualManager(self.path, self.path / 'data', False)
        for key in ('peer', 'qb', 'openlist'):
            (self.m.var / (key + '.port')).write_text(str(port()))
        with self.m.db(): pass

    def test_settings_are_staged_and_ipv6_not_silently_enabled(self):
        self.assertFalse(self.m.settings()['ipv6'])
        with patch.object(self.m.qb, 'request', side_effect=AssertionError('do not apply before confirmation')):
            self.assertTrue(self.m.save_bt_settings({'ipv6': True})['restartRequired'])
        self.assertTrue(self.m.settings()['ipv6'])
        with self.assertRaises(DownloadError): self.m.save_bt_settings({'globalPeers': 1})
        with self.assertRaises(DownloadError): self.m.save_bt_settings({'dht': 1})

    def test_aria_sync_never_reads_qb_tasks(self):
        snap = {'engine': 'qbittorrent', 'status': 'active', 'directory': '', 'name': 'x'}
        with self.m.db() as db: db.execute('INSERT INTO tasks VALUES (?,?,?,?,?)', ('qb', 'a'*40, 1, '{}', json.dumps(snap)))
        with patch.object(self.m, 'rpc', return_value=[]):
            from download_lib import Manager
            Manager.sync(self.m)
        self.assertEqual(json.loads(self.m.row('qb')['snapshot']), snap)

    def test_openlist_path_mapping_and_escape_rejection(self):
        self.assertEqual(map_path(self.m, '/opt/openlist/data/temp/task'), self.m.root / 'Download/task')
        for bad in ('/tmp/task', '/opt/openlist/data/temp/../escape', '/nas/another-user/a', 'relative', '/etc/passwd'):
            with self.subTest(bad=bad), self.assertRaises(DownloadError): map_path(self.m, bad)

    def test_validation_rejects_symlinks_before_registering(self):
        (self.m.root / 'evil').symlink_to(self.path)
        row = {'id': 'a', 'snapshot': json.dumps({'directory': ''})}
        for name in ('../x', '/etc/passwd', 'evil/x', 'a\\b'):
            with self.subTest(name=name), self.assertRaises(DownloadError):
                self.m.validated_files(row, [{'name': name}], True)

    def test_duplicate_external_torrent_not_imported(self):
        with patch.object(self.m.qb, 'request', return_value='5.2.4'), patch.object(self.m.qb, 'torrents', return_value=[{'hash': 'existing'}]):
            with self.assertRaises(DownloadError): self.m.add({'torrent': base64.b64encode(torrent()).decode()})
        self.assertEqual(self.m.rows(), [])

    def test_private_tracker_is_not_modified(self):
        atomic_json(self.m.settings_path, dict(self.m.settings(), trackers=['https://example.com/announce']))
        with patch.object(self.m.qb, 'request', return_value={'private': True}) as api:
            self.m.apply_qb_trackers({'gid': 'a'*40})
        self.assertEqual(api.call_count, 1)


@unittest.skipUnless(os.environ.get('QB_TEST_BINARY'), 'Set QB_TEST_BINARY for a real isolated core')
class RealCore(DualTests):
    def setUp(self):
        super().setUp()
        s = self.m.settings(); s.update(dht=False, pex=False, lpd=False, upnp=False)
        atomic_json(self.m.settings_path, s)
        profile = self.m.qb.prepare()
        self.output = (self.path / 'test.log').open('w')
        self.proc = subprocess.Popen([os.environ['QB_TEST_BINARY'], '--profile=' + str(profile), '--confirm-legal-notice'], stdout=self.output, stderr=self.output)
        self.addCleanup(self.stop_core)
        for _ in range(80):
            if self.proc.poll() is not None: self.fail((self.path / 'test.log').read_text())
            try: self.m.qb.request('app/version'); break
            except DownloadError as exc:
                self.start_error = repr(exc) + ' / ' + repr(exc.__cause__)
                time.sleep(.1)
        else: self.fail(self.start_error + '\n' + (self.path / 'test.log').read_text())
        self.m.qb.apply_preferences(); self.m.qb.mark_applied()
        self.gateway = start_gateway(self.m)
        self.addCleanup(lambda: (self.gateway.shutdown(), self.gateway.server_close()))

    def stop_core(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=15)
        self.output.close()

    def wait_task(self, ident, predicate):
        for _ in range(300):
            self.m.sync(ident)
            snap = json.loads(self.m.row(ident)['snapshot'])
            if predicate(snap): return snap
            time.sleep(.1)
        self.fail('Task did not settle: ' + json.dumps(snap) + ' peers=' + json.dumps(self.m.qb.request('sync/torrentPeers?hash=' + self.m.row(ident)['gid'])))

    def test_real_add_pause_selection_resume_remove(self):
        raw = torrent()
        ident = self.m.add({'torrent': base64.b64encode(raw).decode(), 'directory': 'Download', 'paused': True})['id']
        snap = self.wait_task(ident, lambda s: not s.get('initializing'))
        self.assertEqual(snap['status'], 'paused')
        detail = self.m.detail(ident)
        self.assertEqual(detail['files'][0]['displayPath'], 'test-data')
        self.assertEqual(detail['files'][0]['index'], '1')
        self.m.select_files({'id': ident, 'selected': ['1']})
        self.m.operate(ident, 'resume')
        self.wait_task(ident, lambda s: s['status'] == 'active')
        self.m.operate(ident, 'pause')
        self.wait_task(ident, lambda s: s['status'] == 'paused')
        directory = self.m.root / snap['directory']
        (directory / 'keep-user-file.txt').write_text('must survive')
        # Manually emulate a downloaded partial file; deletion is by recorded filename.
        (directory / 'test-data').write_text('partial')
        self.m.operate(ident, 'remove', True)
        self.assertFalse((directory / 'test-data').exists())
        self.assertTrue((directory / 'keep-user-file.txt').exists())
        self.assertEqual(self.m.rows(), [])

    def test_real_private_preferences_and_gateway_auth(self):
        prefs = self.m.qb.request('app/preferences')
        self.assertFalse(prefs['dht']); self.assertFalse(prefs['upnp'])
        self.assertEqual(prefs['current_interface_address'], '0.0.0.0')
        self.assertEqual(prefs['max_connec_per_torrent'], 100)
        self.assertEqual(prefs['max_connec'], 300)
        endpoint = 'http://127.0.0.1:' + (self.m.var / 'openlist.port').read_text() + '/api/v2/'
        opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
        with self.assertRaises(HTTPError) as e: opener.open(endpoint + 'app/version')
        self.assertEqual(e.exception.code, 403)
        password = (self.m.var / 'qb.secret').read_text().strip()
        req = Request(endpoint + 'auth/login', urlencode({'username': 'downloadcenter', 'password': password}).encode())
        self.assertEqual(opener.open(req).read(), b'Ok.')
        self.assertIn(b'5.2.4', opener.open(endpoint + 'app/version').read())
        with self.assertRaises(HTTPError): opener.open(endpoint + 'app/preferences')
        with self.assertRaises(HTTPError):
            opener.open(Request(endpoint + 'torrents/add', urlencode({'urls': 'magnet:?xt=urn:btih:'+'a'*40, 'tags':'openlist-test', 'savepath':'/etc/test'}).encode()))
        fields = {'urls': 'magnet:?xt=urn:btih:'+'a'*40, 'tags':'openlist-test', 'savepath':'/opt/openlist/data/temp/test'}
        self.assertEqual(opener.open(Request(endpoint + 'torrents/add', urlencode(fields).encode())).read(), b'Ok.')
        for _ in range(30):
            jobs = json.load(opener.open(endpoint + 'torrents/info?tag=openlist-test'))
            if jobs: break
            time.sleep(.1)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(Path(jobs[0]['save_path']), self.m.root / 'Download/test')
        fields = {'hashes': 'a'*40, 'deleteFiles': 'false'}
        self.assertEqual(opener.open(Request(endpoint + 'torrents/delete', urlencode(fields).encode())).read(), b'Ok.')

    def test_real_magnet_metadata_and_local_peer_download(self):
        seedhome = self.path / 'seed'; (seedhome / 'var').mkdir(parents=True); (seedhome / 'data').mkdir()
        seed = DualManager(seedhome, seedhome / 'data', False)
        for key in ('peer', 'qb'): (seed.var / (key + '.port')).write_text(str(port()))
        atomic_json(seed.settings_path, dict(seed.settings(), dht=False, pex=False, lpd=False, upnp=False))
        seedprofile = seed.qb.prepare()
        process = subprocess.Popen([os.environ['QB_TEST_BINARY'], '--profile=' + str(seedprofile), '--confirm-legal-notice'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def stop():
            if process.poll() is None: process.terminate(); process.wait(timeout=15)
        self.addCleanup(stop)
        for _ in range(50):
            try: seed.qb.request('app/version'); break
            except DownloadError: time.sleep(.1)
        seed.qb.apply_preferences()
        content = b'Private local test data, not an internet download.\n' * 4000
        raw = torrent(b'local-magnet.bin', content, private=0); info = torrent_info(raw)
        (seed.root / 'local-magnet.bin').write_bytes(content)
        seed.qb.add({'torrent': base64.b64encode(raw).decode()}, seed.root)
        for _ in range(50):
            if seed.qb.torrents(info['infoHash']): break
            time.sleep(.1)
        seed.qb.request('torrents/recheck', {'hashes': info['infoHash']})
        for _ in range(80):
            items = seed.qb.torrents(info['infoHash'])
            if items and items[0]['progress'] == 1: break
            time.sleep(.1)
        self.assertEqual(items[0]['progress'], 1)
        seed.qb.request('torrents/start', {'hashes': info['infoHash']})
        ident = self.m.add({'url': 'magnet:?xt=urn:btih:' + info['infoHash'], 'directory': 'Download', 'paused': True})['id']
        for _ in range(10):
            if self.m.qb.torrents(info['infoHash']): break
            time.sleep(.1)
        self.m.qb.request('torrents/addPeers', {'hashes': info['infoHash'], 'peers': '127.0.0.1:' + (seed.var / 'peer.port').read_text()})
        snap = self.wait_task(ident, lambda s: not s.get('initializing'))
        self.assertFalse(snap['metadataPending']); self.assertEqual(snap['status'], 'paused')
        self.assertEqual(snap['directory'], 'Download/local-magnet.bin')
        self.m.operate(ident, 'resume')
        self.m.qb.request('torrents/addPeers', {'hashes': info['infoHash'], 'peers': '127.0.0.1:' + (seed.var / 'peer.port').read_text()})
        snap = self.wait_task(ident, lambda s: int(s['completedLength']) == len(content))
        self.assertEqual((self.m.root / snap['directory'] / 'local-magnet.bin').read_bytes(), content)
        self.m.operate(ident, 'remove', True)
        self.assertFalse((self.m.root / snap['directory']).exists())


if __name__ == '__main__': unittest.main()
