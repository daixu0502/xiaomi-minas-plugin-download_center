import contextlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'payload/files'))
import credentials
import lan_rpc
from download_lib import DownloadError


class CredentialsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = types.SimpleNamespace(var=Path(self.tmp.name), locked=contextlib.nullcontext)
        for name, value in [('rpc.secret', 'old-aria-secret'), ('qb.secret', 'old-qb-secret'), ('rpc.port', '19300'), ('qb.port', '19500')]:
            (self.m.var / name).write_text(value)

    def test_validation(self):
        for value in ['short', 'spaces forbidden', 'new\nline', '中文密钥测试内容', 'x' * 129]:
            with self.assertRaises(DownloadError): credentials.validate({'aria2': value})
        with self.assertRaises(DownloadError): credentials.validate({})

    def test_running_service_rejected(self):
        with patch('service.live', return_value=42):
            with self.assertRaises(DownloadError): credentials.save(self.m, {'aria2': 'new-password'})
        self.assertEqual(credentials.read(self.m)['aria2'], 'old-aria-secret')

    def test_save_preserves_blank_field_and_permissions(self):
        with patch('service.live', return_value=0), patch('socket.create_connection', side_effect=OSError):
            self.assertTrue(credentials.save(self.m, {'aria2': 'new-aria-@#:', 'qbittorrent': ''})['saved'])
        self.assertEqual(credentials.read(self.m), {'aria2': 'new-aria-@#:', 'qbittorrent': 'old-qb-secret'})
        self.assertEqual((self.m.var / 'rpc.secret').stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(self.m.var.glob('.credential-*')))

    def test_orphan_core_rejected(self):
        with patch('service.live', return_value=0), patch('socket.create_connection', return_value=contextlib.nullcontext()):
            with self.assertRaises(DownloadError): credentials.save(self.m, {'aria2': 'new-password'})


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        good = data.get('params') == ['token:test-only-secret']
        raw = json.dumps({'result': {'version': 'test'}} if good else {'error': {'message': 'Unauthorized'}}).encode()
        self.send_response(200 if good else 400)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers(); self.wfile.write(raw)


class RPCTests(unittest.TestCase):
    def test_auth_cors_paths_and_network_restrictions(self):
        upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        server = ThreadingHTTPServer(('127.0.0.1', 0), lan_rpc.Handler)
        server.rpc_port = upstream.server_port
        server.networks = [ipaddress.ip_network('127.0.0.0/8')]
        threads = [threading.Thread(target=s.serve_forever) for s in (upstream, server)]
        for t in threads: t.start()
        def request(method, path='/jsonrpc', token=None, headers=None):
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            body = json.dumps({'jsonrpc': '2.0', 'method': 'aria2.getVersion', 'params': ['token:' + token] if token else []})
            conn.request(method, path, body, headers or {'Content-Type': 'application/json'})
            result = conn.getresponse(); raw = result.read()
            code, cors = result.status, result.getheader('Access-Control-Allow-Origin')
            conn.close(); return code, cors, raw
        try:
            self.assertEqual(request('POST')[0], 400)
            self.assertEqual(request('POST', token='wrong')[0], 400)
            self.assertEqual(request('POST', token='test-only-secret')[:2], (200, '*'))
            self.assertEqual(request('OPTIONS')[0], 204)
            self.assertEqual(request('GET')[0], 405)
            self.assertEqual(request('POST', '/not-jsonrpc')[0], 403)
            self.assertEqual(request('POST', headers={'Host': 'evil.example'})[0], 403)
            server.networks = [ipaddress.ip_network('192.168.1.0/24')]
            self.assertEqual(request('POST', token='test-only-secret')[0], 403)
        finally:
            for s in (upstream, server): s.shutdown(); s.server_close()
            for t in threads: t.join()


if __name__ == '__main__': unittest.main()
