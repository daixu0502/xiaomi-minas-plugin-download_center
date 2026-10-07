"""Opt-in HTTP JSON-RPC LAN listener; the aria2 core stays on loopback."""
import http.client
import ipaddress
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from openlist_gateway import lan_config


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def allowed(self):
        peer = ipaddress.ip_address(self.client_address[0])
        return (any(peer in n for n in self.server.networks)
                and self.headers.get('Host', '').split(':')[0] == self.server.server_address[0]
                and self.path == '/jsonrpc')

    def respond(self, status, body=b''):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        # AriaNg uses explicit RPC tokens, never browser cookies.
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        if not self.allowed(): return self.respond(403)
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self): self.respond(405)

    def do_POST(self):
        upstream = None
        try:
            self.connection.settimeout(15)
            if not self.allowed() or self.headers.get('Transfer-Encoding'):
                return self.respond(403)
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 8 * 1024 * 1024: return self.respond(413)
            body = self.rfile.read(length)
            payload = json.loads(body)
            if not isinstance(payload, (dict, list)): return self.respond(400)
            # Fixed upstream only. Core verifies the RPC token for protected methods.
            upstream = http.client.HTTPConnection('127.0.0.1', self.server.rpc_port, timeout=15)
            upstream.request('POST', '/jsonrpc', body, {'Content-Type': 'application/json'})
            result = upstream.getresponse()
            raw = result.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024: return self.respond(502)
            self.respond(result.status, raw)
        except (ValueError, OSError, http.client.HTTPException):
            self.respond(502)
        finally:
            if upstream: upstream.close()


class Listener:
    def __init__(self, m):
        self.stop = threading.Event()
        self.server = None
        self.worker = threading.Thread(target=self.start, args=(m,), daemon=True)
        self.worker.start()

    def start(self, m):
        try:
            cfg = lan_config(m, 'aria2-lan.json')
            if not cfg: return
            address, networks = cfg
            port = int((m.var / 'rpc.port').read_text())
        except (ValueError, KeyError, TypeError, OSError):
            print('aria2 LAN configuration invalid; loopback remains available', flush=True)
            return
        while not self.stop.is_set():
            try: server = ThreadingHTTPServer((address, port), Handler)
            except OSError:
                self.stop.wait(5)
                continue
            server.daemon_threads = True
            server.rpc_port, server.networks = port, networks
            if self.stop.is_set():
                server.server_close()
                return
            self.server = server
            threading.Thread(target=server.serve_forever, daemon=True).start()
            return

    def shutdown(self):
        self.stop.set()
        self.worker.join()
        if self.server: self.server.shutdown()

    def server_close(self):
        if self.server: self.server.server_close()


def port_listening(address, port):
    try:
        encoded = socket.inet_aton(address)[::-1].hex().upper() + ':%04X' % port
        return any(line.split()[1] == encoded and line.split()[3] == '0A'
                   for line in Path('/proc/net/tcp').read_text().splitlines()[1:])
    except (OSError, ValueError, IndexError): return False


def status(m):
    rows = {}
    for name, config, portfile, path in [('aria2', 'aria2-lan.json', 'rpc.port', '/jsonrpc'),
                                        ('qbittorrent', 'openlist-lan.json', 'openlist.port', '/')]:
        row = {'configured': False, 'listening': False, 'address': '', 'port': None, 'url': ''}
        try:
            row['port'] = int((m.var / portfile).read_text())
            cfg = lan_config(m, config)
            if cfg:
                row.update(configured=True, address=cfg[0], listening=port_listening(cfg[0], row['port']))
                row['url'] = 'http://%s:%d%s' % (cfg[0], row['port'], path)
        except (OSError, ValueError, KeyError, TypeError): row['invalid'] = True
        rows[name] = row
    return rows
