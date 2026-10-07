#!/usr/bin/env python3
"""Authenticated loopback / opt-in LAN adapter for Openlist 4.x.

Only Openlist-tagged tasks are exposed. Never forward settings, filesystem deletion,
or arbitrary endpoints. Translate the container's temp alias to the owner's data.
"""
import hmac
import ipaddress
import json
import re
import secrets
import threading
import time
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, urlsplit
from qb_client import QB, added_ok
from download_lib import DownloadError, valid_url


def lan_config(m, filename='openlist-lan.json'):
    path = m.var / filename
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    address = ipaddress.IPv4Address(value['address'])
    private = [ipaddress.IPv4Network(v) for v in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]
    if not any(address in block for block in private):
        raise ValueError('Openlist LAN listener requires an RFC1918 address')
    networks = [ipaddress.IPv4Network(v) for v in value['allowedNetworks']]
    if not networks or not all(any(n.subnet_of(b) for b in private) for n in networks):
        raise ValueError('Openlist LAN clients must use explicitly allowed private networks')
    return str(address), networks


class Gateway(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, m, address='127.0.0.1', networks=None):
        self.allowed_hosts = {'127.0.0.1', 'localhost'} if address == '127.0.0.1' else {address}
        self.allowed_networks = networks or [ipaddress.IPv4Network('127.0.0.0/8')]
        super().__init__((address, int((m.var / 'openlist.port').read_text())), Handler)
        self.m, self.sessions, self.session_lock = m, {}, threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass  # URLs, passwords, cookies and filenames are private.

    def respond(self, value, status=200, cookie=None):
        raw = (json.dumps(value) if isinstance(value, (dict, list)) else value).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json' if isinstance(value, (dict, list)) else 'text/plain')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        if cookie: self.send_header('Set-Cookie', 'SID=' + cookie + '; HttpOnly; SameSite=Strict; Path=/')
        self.end_headers(); self.wfile.write(raw)

    def do_GET(self): self.handle_api()
    def do_POST(self): self.handle_api()

    def handle_api(self):
        try:
            self.connection.settimeout(5)
            peer = ipaddress.ip_address(self.client_address[0])
            if not any(peer in network for network in self.server.allowed_networks):
                return self.respond('Forbidden', 403)
            host = self.headers.get('Host', '').split(':')[0]
            if host not in self.server.allowed_hosts: return self.respond('Forbidden', 403)
            if self.headers.get('Origin') or self.headers.get('Sec-Fetch-Site'):
                return self.respond('Browser access is disabled', 403)
            if self.headers.get('Transfer-Encoding'): return self.respond('Forbidden', 403)
            length = int(self.headers.get('Content-Length', 0))
            if not 0 <= length <= 1024 * 1024: return self.respond('Too large', 413)
            url = urlsplit(self.path)
            params = {k: v[0] for k, v in parse_qs(url.query).items()}
            body = self.rfile.read(length)
            ct = self.headers.get('Content-Type', '')
            if ct.startswith('multipart/form-data'):
                msg = BytesParser(policy=default).parsebytes(('Content-Type: ' + ct + '\r\nMIME-Version: 1.0\r\n\r\n').encode() + body)
                if not msg.is_multipart(): raise DownloadError('无效请求')
                for part in msg.iter_parts():
                    if part.get_filename(): raise DownloadError('此接口仅支持 Openlist 链接下载')
                    params[part.get_param('name', header='content-disposition')] = part.get_payload(decode=True).decode()
            elif body:
                params.update({k: v[0] for k, v in parse_qs(body.decode()).items()})
            m = self.server.m
            if url.path == '/api/v2/auth/login':
                if self.command != 'POST': return self.respond('Forbidden', 403)
                password = (m.var / 'qb.secret').read_text().strip()
                if params.get('username') != 'downloadcenter' or not hmac.compare_digest(params.get('password', ''), password):
                    return self.respond('Fails.', 403)
                sid = secrets.token_hex(32)
                with self.server.session_lock:
                    self.server.sessions = {k: v for k, v in self.server.sessions.items() if v > time.time()}
                    if len(self.server.sessions) >= 128: return self.respond('Too many sessions', 429)
                    self.server.sessions[sid] = time.time() + 86400
                return self.respond('Ok.', cookie=sid)
            cookie = SimpleCookie(self.headers.get('Cookie', ''))
            sid = cookie.get('SID')
            with self.server.session_lock:
                authenticated = sid and self.server.sessions.get(sid.value, 0) > time.time()
            if not authenticated: return self.respond('Forbidden', 403)
            qb = QB(m)
            action = url.path.removeprefix('/api/v2/') if hasattr(str, 'removeprefix') else url.path[8:]
            if not url.path.startswith('/api/v2/'): return self.respond('Not found', 404)
            if action == 'app/version': return self.respond(qb.request('app/version'))
            if action == 'app/webapiVersion': return self.respond(qb.request('app/webapiVersion'))
            if action == 'torrents/add':
                if self.command != 'POST': return self.respond('Forbidden', 403)
                tag = params.get('tags', '')
                if not re.fullmatch(r'openlist-[A-Za-z0-9_-]{1,100}', tag): raise DownloadError('缺少 Openlist 任务标签')
                uri = params.get('urls', '')
                valid_url(uri)
                if urlsplit(uri).scheme not in ('magnet', 'http', 'https'): raise DownloadError('不支持的 BT 链接')
                destination = map_path(m, params.get('savepath', ''))
                # Keep a fresh explicit directory, with every component checked.
                safe_mkdir(m, destination)
                result = qb.request('torrents/add', {'urls': uri, 'savepath': str(destination), 'tags': tag,
                                    'autoTMM': 'false', 'stopped': 'false', 'stopCondition': 'None',
                                    'contentLayout': 'Original', 'useDownloadPath': 'false'})
                if not added_ok(result): raise DownloadError('qBittorrent 拒绝创建任务')
                return self.respond('Ok.')
            # Only expose this adapter's tasks, never plugin-managed or manually added jobs.
            jobs = [v for v in qb.torrents() if owned(m, v)]
            if action == 'torrents/info':
                tag = params.get('tag', '')
                if not re.fullmatch(r'openlist-[A-Za-z0-9_-]{1,100}', tag): raise DownloadError('无效标签')
                items = [dict(v) for v in jobs if tag in v.get('tags', '').split(', ')]
                for v in items:
                    v['state'] = {'stoppedUP': 'pausedUP', 'stoppedDL': 'pausedDL'}.get(v['state'], v['state'])
                return self.respond(items)
            if action == 'torrents/files':
                value = params.get('hash', '')
                if not any(v['hash'] == value for v in jobs): return self.respond('Not found', 404)
                files = qb.files(value)
                for f in files:
                    p = PurePosixPath(f['name'])
                    if p.is_absolute() or '..' in p.parts or '\\' in f['name']: raise DownloadError('不安全的文件路径')
                return self.respond(files)
            if action == 'torrents/delete':
                if self.command != 'POST' or params.get('deleteFiles', 'false') != 'false':
                    return self.respond('File deletion is not allowed', 403)
                hashes = params.get('hashes', '').split('|')
                if not hashes or not set(hashes) <= {v['hash'] for v in jobs}: return self.respond('Not found', 404)
                qb.request('torrents/delete', {'hashes': '|'.join(hashes), 'deleteFiles': 'false'})
                return self.respond('Ok.')
            if action == 'torrents/deleteTags':
                tags = params.get('tags', '').split(',')
                if self.command != 'POST' or not all(re.fullmatch(r'openlist-[A-Za-z0-9_-]{1,100}', t) for t in tags):
                    return self.respond('Forbidden', 403)
                qb.request('torrents/deleteTags', {'tags': ','.join(tags)})
                return self.respond('Ok.')
            return self.respond('Unsupported endpoint', 403)
        except (DownloadError, ValueError, OSError):
            return self.respond('Openlist adapter request failed; check service and download paths', 400)


def map_path(m, value):
    alias = PurePosixPath('/opt/openlist/data/temp')
    p = PurePosixPath(value)
    if '..' in p.parts or '\\' in value or re.search(r'[\x00-\x1f]', value): raise DownloadError('不安全路径')
    root = m.root.resolve()
    if p == alias or alias in p.parents:
        folder = m.settings().get('openlistDirectory', 'Download')
        base = m.directory(folder)
        return base.joinpath(*p.relative_to(alias).parts)
    try: p.relative_to(root)
    except ValueError as exc: raise DownloadError('Openlist 下载路径必须在所属用户空间') from exc
    return Path(p)


def safe_mkdir(m, target):
    current = m.root.resolve()
    for part in target.relative_to(current).parts:
        current = current / part
        if current.is_symlink(): raise DownloadError('拒绝符号链接')
        current.mkdir(exist_ok=True)
        if not current.is_dir(): raise DownloadError('路径不是目录')


def owned(m, task):
    if not any(re.fullmatch(r'openlist-[A-Za-z0-9_-]{1,100}', t.strip()) for t in task.get('tags', '').split(',')): return False
    try:
        path = Path(task['save_path'])
        path.relative_to(m.root.resolve())
        m.directory(path.relative_to(m.root.resolve()).as_posix())
        return True
    except (ValueError, KeyError, DownloadError): return False


class GatewayGroup:
    def __init__(self, m):
        self.stopping = threading.Event()
        self.servers = [Gateway(m)]
        threading.Thread(target=self.servers[0].serve_forever, daemon=True).start()
        # A delayed LAN interface at boot must not stop either download engine.
        # Keep loopback available and retry the explicit LAN bind in background.
        self.worker = threading.Thread(target=self.start_lan, args=(m,), daemon=True)
        self.worker.start()

    def start_lan(self, m):
        try:
            cfg = lan_config(m)
        except (OSError, ValueError, KeyError, TypeError):
            print('Openlist LAN configuration invalid; loopback remains available', flush=True)
            return
        if not cfg:
            return
        while not self.stopping.is_set():
            try:
                server = Gateway(m, *cfg)
            except OSError:
                self.stopping.wait(5)
                continue
            if self.stopping.is_set():
                server.server_close()
                return
            self.servers.append(server)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            return

    def shutdown(self):
        self.stopping.set()
        self.worker.join()
        for server in self.servers:
            server.shutdown()

    def server_close(self):
        for server in self.servers:
            server.server_close()


def start_gateway(m):
    return GatewayGroup(m)
