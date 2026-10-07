#!/usr/bin/env python3
"""Private authenticated loopback qBittorrent API. No user-supplied endpoints."""
import base64
import configparser
import hashlib
import json
import os
import re
import secrets
from urllib.parse import urlencode
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError
from download_lib import DownloadError, atomic_json

QB_DEFAULTS = {'ipv6': False, 'dht': True, 'pex': True, 'lpd': True, 'upnp': False,
               'maxPeers': 100, 'globalPeers': 300}


def added_ok(result):
    if isinstance(result, dict):
        return result.get('failure_count', 0) == 0 and result.get('success_count', 0) + result.get('pending_count', 0) > 0
    return isinstance(result, str) and result.strip() in ('Ok.', '')


class QB:
    def __init__(self, manager):
        self.m = manager
        self.cookie = ''
        try:
            saved = json.loads((self.m.var / 'qb-session.json').read_text()).get('cookie', '')
            if re.fullmatch(r'(?:SID|QBT_SID_[0-9]+)=[A-Za-z0-9_+/%=\-]+', saved): self.cookie = saved
        except (OSError, ValueError): pass

    def request(self, path, data=None, raw=None, content_type=None, binary=False, retry=True):
        try:
            port = int((self.m.var / 'qb.port').read_text())
            url = 'http://127.0.0.1:%d' % port
            headers = {'Referer': url + '/', 'Origin': url}
            if self.cookie:
                headers['Cookie'] = self.cookie
            if raw is not None:
                body = raw
                headers['Content-Type'] = content_type
            else:
                body = urlencode(data).encode() if data is not None else None
                if body is not None:
                    headers['Content-Type'] = 'application/x-www-form-urlencoded'
            request = Request(url + '/api/v2/' + path, body, headers)
            with build_opener(ProxyHandler({})).open(request, timeout=3) as response:
                value = response.read(12 * 1024 * 1024 + 1)
                if len(value) > 12 * 1024 * 1024:
                    raise DownloadError('qBittorrent 返回数据过大')
                cookie = response.headers.get('Set-Cookie', '')
                if cookie.startswith(('SID=', 'QBT_SID_')):
                    self.cookie = cookie.split(';', 1)[0]
                    atomic_json(self.m.var / 'qb-session.json', {'cookie': self.cookie})
                if binary:
                    return value
                text = value.decode('utf-8')
                if text.startswith(('{', '[')):
                    return json.loads(text)
                return text
        except HTTPError as exc:
            if exc.code == 403 and retry and path != 'auth/login':
                password = (self.m.var / 'qb.secret').read_text().strip()
                if self.request('auth/login', {'username': 'downloadcenter', 'password': password}, retry=False) not in ('Ok.', '') or not self.cookie:
                    raise DownloadError('qBittorrent 本机身份验证失败')
                return self.request(path, data, raw, content_type, binary, retry=False)
            raise DownloadError('qBittorrent 接口错误（HTTP %d）' % exc.code) from exc
        except DownloadError:
            raise
        except Exception as exc:
            raise DownloadError('qBittorrent 未就绪，请检查下载服务与日志') from exc

    def torrents(self, hash_value=None):
        query = '?hashes=' + hash_value if hash_value else ''
        return self.request('torrents/info' + query)

    def files(self, hash_value):
        return self.request('torrents/files?hash=' + hash_value)

    def add(self, source, save_path, metadata=False):
        fields = {'savepath': str(save_path), 'autoTMM': 'false', 'contentLayout': 'NoSubfolder',
                  'useDownloadPath': 'false', 'stopped': 'false' if metadata else 'true',
                  'stopCondition': 'MetadataReceived' if metadata else 'None',
                  'rename': '', 'tags': 'downloadcenter'}
        torrent = source.get('torrent')
        if not torrent:
            fields['urls'] = source['url']
            result = self.request('torrents/add', fields)
        else:
            boundary = 'Minas' + secrets.token_hex(16)
            chunks = []
            for key, value in fields.items():
                chunks.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (boundary, key, value)).encode())
            chunks += [('--%s\r\nContent-Disposition: form-data; name="torrents"; filename="task.torrent"\r\nContent-Type: application/x-bittorrent\r\n\r\n' % boundary).encode(),
                       base64.b64decode(torrent), ('\r\n--%s--\r\n' % boundary).encode()]
            result = self.request('torrents/add', raw=b''.join(chunks), content_type='multipart/form-data; boundary=' + boundary)
        if not added_ok(result):
            raise DownloadError('qBittorrent 拒绝添加种子')

    def prepare(self):
        """Called only while our child is stopped; retain qB resume/session files."""
        profile = self.m.var / 'qb-profile'
        config_dir = profile / 'qBittorrent/config'
        config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        secret = self.m.var / 'qb.secret'
        if not secret.exists():
            with secret.open('x') as stream:
                stream.write(secrets.token_hex(32))
            secret.chmod(0o600)
        salt = os.urandom(16)
        digest = hashlib.pbkdf2_hmac('sha512', secret.read_text().strip().encode(), salt, 100000)
        password = '@ByteArray(' + base64.b64encode(salt).decode() + ':' + base64.b64encode(digest).decode() + ')'
        path = config_dir / 'qBittorrent.conf'
        conf = configparser.RawConfigParser(strict=False)
        conf.optionxform = str
        if path.exists():
            conf.read(path)
        sections = {
            'LegalNotice': {'Accepted': 'true'},
            'Preferences': {'WebUI\\Address': '127.0.0.1', 'WebUI\\Port': (self.m.var / 'qb.port').read_text().strip(),
                            'WebUI\\Username': 'downloadcenter', 'WebUI\\Password_PBKDF2': password,
                            'WebUI\\LocalHostAuth': 'true', 'WebUI\\AuthSubnetWhitelistEnabled': 'false',
                            'WebUI\\CSRFProtection': 'true', 'WebUI\\HostHeaderValidation': 'true',
                            'WebUI\\ServerDomains': '127.0.0.1;localhost', 'WebUI\\UseUPnP': 'false',
                            'Connection\\UPnP': 'false'},
            'BitTorrent': {'Session\\DHTEnabled': 'false', 'Session\\LSDEnabled': 'false',
                          'Session\\PeXEnabled': str(self.m.settings()['pex']).lower(),
                          'Session\\Port': (self.m.var / 'peer.port').read_text().strip(),
                          'Session\\InterfaceAddress': '' if self.m.settings()['ipv6'] else '0.0.0.0',
                          'Session\\DefaultSavePath': str(self.m.var / 'qb-incomplete'),
                          'Session\\DisableAutoTMMByDefault': 'true',
                          'Session\\Preallocation': 'false'},
        }
        for section, values in sections.items():
            if not conf.has_section(section): conf.add_section(section)
            for key, value in values.items(): conf.set(section, key, value)
        with path.open('w') as stream:
            conf.write(stream, space_around_delimiters=False)
        path.chmod(0o600)
        return profile

    def preferences(self, settings=None):
        s = settings or self.m.settings()
        return {'dht': s['dht'], 'pex': s['pex'], 'lsd': s['lpd'], 'upnp': s['upnp'],
                'current_network_interface': '', 'current_interface_address': '' if s['ipv6'] else '0.0.0.0',
                'listen_port': int((self.m.var / 'peer.port').read_text()), 'random_port': False,
                'max_connec_per_torrent': s['maxPeers'], 'max_connec': s['globalPeers'],
                'queueing_enabled': True, 'max_active_downloads': s['concurrent'],
                'max_active_uploads': s['concurrent'], 'max_active_torrents': s['concurrent'] * 2,
                'max_active_checking_torrents': 1, 'dl_limit': s['downloadKiB'] * 1024,
                'up_limit': s['uploadKiB'] * 1024, 'max_ratio_enabled': True, 'max_ratio': s['seedRatio'],
                'max_seeding_time_enabled': True, 'max_seeding_time': s['seedMinutes'],
                'max_ratio_act': 0, 'preallocate_all': False, 'incomplete_files_ext': False,
                'temp_path_enabled': False, 'auto_tmm_enabled': False, 'save_resume_data_interval': 15,
                'add_trackers_enabled': False, 'add_trackers': '', 'web_ui_upnp': False}

    def apply_preferences(self):
        self.request('app/setPreferences', {'json': json.dumps(self.preferences())})

    def mark_applied(self):
        atomic_json(self.m.var / 'qb-applied.json', {k: self.m.settings()[k] for k in QB_DEFAULTS})
