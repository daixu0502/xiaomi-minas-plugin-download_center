import http.cookiejar
import importlib.util
import ipaddress
import json
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
qb = types.ModuleType('qb_client')
qb.QB = lambda manager: types.SimpleNamespace(request=lambda action: 'test-version')
qb.added_ok = lambda result: True
lib = types.ModuleType('download_lib')
lib.DownloadError = type('DownloadError', (Exception,), {})
lib.valid_url = lambda value: None
saved = {name: sys.modules.get(name) for name in ('qb_client', 'download_lib')}
try:
    sys.modules['qb_client'] = qb
    sys.modules['download_lib'] = lib
    spec = importlib.util.spec_from_file_location('openlist_gateway_lan_test', ROOT / 'payload/files/openlist_gateway.py')
    gateway = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gateway)
finally:
    for name, module in saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


class LANTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = types.SimpleNamespace(var=Path(self.tmp.name))
        (self.m.var / 'openlist.port').write_text('0')
        (self.m.var / 'qb.secret').write_text('test-only-password')

    def tearDown(self):
        self.tmp.cleanup()

    def test_config_default_and_validation(self):
        self.assertIsNone(gateway.lan_config(self.m))
        p = self.m.var / 'openlist-lan.json'
        for address, networks in [('0.0.0.0', ['10.0.0.0/24']), ('8.8.8.8', ['10.0.0.0/24']),
                                  ('10.0.0.2', ['0.0.0.0/0']), ('10.0.0.2', [])]:
            p.write_text(json.dumps({'address': address, 'allowedNetworks': networks}))
            with self.assertRaises(ValueError):
                gateway.lan_config(self.m)
        p.write_text(json.dumps({'address': '10.0.0.2', 'allowedNetworks': ['10.0.0.0/24', '172.18.0.0/16']}))
        self.assertEqual(gateway.lan_config(self.m)[0], '10.0.0.2')

    def test_auth_host_origin_and_peer_checks(self):
        server = gateway.Gateway(self.m)
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            base = 'http://127.0.0.1:%d/api/v2/' % server.server_port
            op = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                 urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            def forbidden(req):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    op.open(req, timeout=3)
                self.assertEqual(caught.exception.code, 403)
            forbidden(base + 'app/version')
            wrong = urllib.parse.urlencode({'username': 'downloadcenter', 'password': 'wrong'}).encode()
            forbidden(urllib.request.Request(base + 'auth/login', wrong))
            body = urllib.parse.urlencode({'username': 'downloadcenter', 'password': 'test-only-password'}).encode()
            with op.open(urllib.request.Request(base + 'auth/login', body), timeout=3) as r:
                self.assertEqual(r.read(), b'Ok.')
            with op.open(base + 'app/version', timeout=3) as r:
                self.assertEqual(r.read(), b'test-version')
            forbidden(urllib.request.Request(base + 'app/version', headers={'Host': 'evil.example'}))
            forbidden(urllib.request.Request(base + 'app/version', headers={'Origin': 'http://example.com'}))
            server.allowed_networks = [ipaddress.ip_network('10.0.0.0/24')]
            forbidden(base + 'app/version')
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_missing_lan_address_does_not_break_loopback(self):
        (self.m.var / 'openlist-lan.json').write_text(json.dumps({
            'address': '10.255.254.253', 'allowedNetworks': ['10.255.254.0/24']}))
        group = gateway.start_gateway(self.m)
        try:
            self.assertGreater(group.servers[0].server_port, 0)
        finally:
            group.shutdown()
            group.server_close()


if __name__ == '__main__':
    unittest.main()
