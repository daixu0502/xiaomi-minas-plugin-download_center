#!/usr/bin/env python3
"""qBittorrent for new BT tasks, aria2 for direct URLs and retained legacy jobs."""
import base64
import json
import os
import re
import secrets
import time
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, urlsplit, quote
from download_lib import Manager, DownloadError, LIVE, MAX_TORRENT, atomic_json, torrent_info, valid_url
from qb_client import QB, QB_DEFAULTS


class DualManager(Manager):
    @property
    def qb(self):
        if not hasattr(self, '_qb'):
            self._qb = QB(self)
        return self._qb

    def settings(self):
        s = super().settings()
        # aria2's IPv6 DHT was disabled by default. Do not silently enable it.
        if s.get('btEngine') != 'qbittorrent':
            s.update(QB_DEFAULTS)
        s['btEngine'] = 'qbittorrent'
        for k, v in QB_DEFAULTS.items(): s.setdefault(k, v)
        return s

    @staticmethod
    def is_qb(row):
        return json.loads(row['snapshot']).get('engine') == 'qbittorrent'

    def save_row(self, row, snap, source=None):
        with self.db() as db:
            db.execute('UPDATE tasks SET snapshot=? WHERE id=?', (json.dumps(snap), row['id']))
            if source is not None:
                db.execute('UPDATE tasks SET source=? WHERE id=?', (json.dumps(source), row['id']))

    def bt_state(self):
        try:
            active = json.loads((self.var / 'qb-applied.json').read_text())
        except (OSError, ValueError):
            active = {}
        s = self.settings()
        try: port = int((self.var / 'peer.port').read_text())
        except (OSError, ValueError): port = None
        return {'peerPort': port, 'engine': 'qbittorrent', 'restartRequired': active != {k: s[k] for k in QB_DEFAULTS},
                'ipv6Detected': Path('/proc/net/if_inet6').exists()}

    def save_bt_settings(self, data):
        s = self.settings()
        for key, default in QB_DEFAULTS.items():
            value = data.get(key, s[key])
            if isinstance(default, bool):
                if type(value) is not bool: raise DownloadError('开关值无效：' + key)
            elif type(value) is not int or not 1 <= value <= (500 if key == 'maxPeers' else 2000):
                raise DownloadError('连接数超出范围：' + key)
            s[key] = value
        if s['globalPeers'] < s['maxPeers']:
            raise DownloadError('全局连接上限不能小于单任务连接上限')
        atomic_json(self.settings_path, s)
        return self.bt_state()

    def snapshot(self):
        result = super().snapshot()
        result['engines'] = {'aria2': {'running': result['running'], 'version': result['engineVersion']}}
        try:
            stats = self.qb.request('transfer/info')
            version = self.qb.request('app/version').strip().lstrip('v')
            build = self.qb.request('app/buildInfo')
            result['engines']['qbittorrent'] = {'running': True, 'version': version, 'libtorrent': build.get('libtorrent', '')}
            result['bt'].update(connection=stats.get('connection_status'), dhtNodes=stats.get('dht_nodes', 0))
            # Show this plugin's traffic; external Openlist jobs stay separate.
            for stat in ('downloadSpeed', 'uploadSpeed'):
                result['stats'][stat] = str(int(result['stats'].get(stat, 0)) + sum(int(t.get(stat, 0)) for t in result['tasks'] if t.get('engine') == 'qbittorrent'))
        except DownloadError as exc:
            result['engines']['qbittorrent'] = {'running': False, 'version': ''}
            result['error'] = '；'.join(filter(None, [result['error'], str(exc)]))
            for t in result['tasks']:
                if t.get('engine') == 'qbittorrent': t.update(downloadSpeed='0', uploadSpeed='0')
        result['running'] = all(e['running'] for e in result['engines'].values())
        result['qbUpdate'] = self.qb_update_state()
        from lan_rpc import status as lan_status
        from service import live
        result['lanInterfaces'] = lan_status(self)
        result['credentialsEditable'] = not bool(live(self))
        try: result['trackerApply'] = json.loads((self.var / 'tracker-apply.json').read_text())
        except (OSError, ValueError): result['trackerApply'] = {}
        return result

    def qb_update_state(self):
        from qb_update import state
        return state(self)

    def add(self, data):
        uri = data.get('url', '')
        if not data.get('torrent') and not (isinstance(uri, str) and uri.startswith('magnet:')):
            return super().add(data)
        self.qb.request('app/version')
        rows = self.rows()
        if len(rows) >= 1000: raise DownloadError('最多保留 1000 条任务，请清理历史记录')
        with self.db() as db:
            if db.execute('SELECT COALESCE(SUM(LENGTH(source)),0) FROM tasks').fetchone()[0] + len(data.get('torrent', '')) > 64 * 1024 * 1024:
                raise DownloadError('种子及链接记录达到 64 MiB 限额')
        relative = data.get('directory', self.settings()['directory'])
        self.directory(relative)
        raw = None
        if data.get('torrent'):
            try: raw = base64.b64decode(data['torrent'], validate=True)
            except (TypeError, ValueError) as exc: raise DownloadError('种子编码错误') from exc
            info = torrent_info(raw)
            if len(info['files']) > 2000: raise DownloadError('最多导入 2000 个文件')
            hash_value = info['infoHash']
            selected = self.selection(data.get('selected', [f['index'] for f in info['files']]), len(info['files']))
        else:
            valid_url(uri)
            hashes = [v[9:] for v in parse_qs(urlsplit(uri).query).get('xt', []) if v.lower().startswith('urn:btih:')]
            if not hashes: raise DownloadError('目前磁链需要 BT v1 infoHash')
            hash_value = hashes[0].lower()
            if len(hash_value) == 32:
                try: hash_value = base64.b32decode(hash_value.upper()).hex()
                except ValueError as exc: raise DownloadError('无效磁链哈希') from exc
            if not re.fullmatch('[0-9a-f]{40}', hash_value): raise DownloadError('无效磁链哈希')
            selected = None
        if data.get('name'): raise DownloadError('BT 任务不支持在创建时重命名')
        if self.qb.torrents(hash_value) or any(r['gid'] == hash_value for r in rows):
            raise DownloadError('同一种子已在 qBittorrent 中（可能由 Openlist 创建），请勿重复添加')
        ident = secrets.token_hex(12)
        if raw:
            location, options, paths = self.bt_options(info, relative)
            destination = self.root / location
        else:
            location, paths = relative, []
            destination = self.var / 'metadata' / ident
            destination.mkdir(parents=True, mode=0o700)
        source = {'url': uri, 'torrent': base64.b64encode(raw).decode() if raw else '',
                  'selected': selected, 'requestedPause': bool(data.get('paused')), 'options': {}}
        snap = {'engine': 'qbittorrent', 'status': 'waiting', 'name': info['name'] if raw else '正在获取磁链信息',
                'directory': location, 'totalLength': str(info['total']) if raw else '0', 'completedLength': '0',
                'downloadSpeed': '0', 'uploadSpeed': '0', 'isBT': True, 'ownedPaths': paths,
                'metadataPending': not bool(raw), 'createdDirectory': bool(raw), 'infoHash': hash_value,
                'initializing': True, 'fileCount': len(info['files']) if raw else 0, 'addedAt': time.time()}
        with self.db() as db:
            db.execute('INSERT INTO tasks VALUES (?,?,?,?,?)', (ident, hash_value, time.time(), json.dumps(source), json.dumps(snap)))
        try:
            self.qb.add(source, destination, metadata=not bool(raw))
        except DownloadError as exc:
            snap.update(status='error', errorMessage=str(exc))
            self.save_row({'id': ident}, snap)
            raise
        s = self.settings()
        s['recentDirectories'] = list(dict.fromkeys([relative] + s.get('recentDirectories', [])))[:8]
        atomic_json(self.settings_path, s)
        return {'id': ident}

    def validated_files(self, row, files, register=False):
        snap = json.loads(row['snapshot'])
        base = self.directory(snap['directory'])
        if len(files) > 2000: raise DownloadError('种子文件数量超出限制')
        paths = []
        for f in files:
            name = f.get('name', '')
            p = PurePosixPath(name)
            if not name or p.is_absolute() or '..' in p.parts or re.search(r'[\\\x00-\x1f]', name):
                raise DownloadError('qBittorrent 返回了不安全的文件路径')
            current = base
            for part in p.parts:
                current = current / part
                if current.is_symlink(): raise DownloadError('任务目录包含符号链接，已禁止操作')
            paths.append(current.relative_to(self.root.resolve()).as_posix())
        if register:
            with self.db() as db:
                for path in paths:
                    db.execute('INSERT OR IGNORE INTO task_files VALUES (?,?,0,0)', (row['id'], path))
        return paths

    def set_selection(self, row, files, selected):
        values = {int(n) for n in self.selection(selected, len(files)).split(',')}
        # File indices exposed to the UI remain one-based, qB API is zero-based.
        for priority in (0, 1):
            ids = [str(f['index']) for f in files if (int(f['index']) + 1 in values) == bool(priority)]
            if ids: self.qb.request('torrents/filePrio', {'hash': row['gid'], 'id': '|'.join(ids), 'priority': priority})

    def prepare_task(self, row, item, snap):
        source = json.loads(self.row(row['id'])['source'])
        if not snap.get('initializing'): return snap
        files = self.qb.files(row['gid'])
        if not files: return snap
        if not item['state'].startswith(('stopped', 'paused')) and item['state'] != 'moving':
            self.qb.request('torrents/stop', {'hashes': row['gid']})
            return snap
        if snap.get('metadataPending'):
            raw = self.qb.request('torrents/export?hash=' + row['gid'], binary=True)
            info = torrent_info(raw)
            if info['infoHash'] != row['gid']: raise DownloadError('磁链元数据哈希不一致')
            if len(info['files']) > 2000: raise DownloadError('磁链超过 2000 个文件')
            location, opts, paths = self.bt_options(info, snap['directory'])
            source.update(torrent=base64.b64encode(raw).decode(), selected=','.join(f['index'] for f in info['files']))
            snap.update(metadataPending=False, relocationPending=True, directory=location, createdDirectory=True,
                        name=info['name'], ownedPaths=paths, totalLength=str(info['total']))
            # Persist destination before the asynchronous move, so a restart is recoverable.
            self.save_row(row, snap, source)
        target = self.directory(snap['directory'])
        if Path(item['save_path']) != target:
            if not snap.get('relocationPending'): raise DownloadError('任务保存路径已在外部改变，拒绝自动操作')
            self.qb.request('torrents/setLocation', {'hashes': row['gid'], 'location': str(target)})
            return snap
        if item['state'] == 'moving': return snap
        row = dict(row, snapshot=json.dumps(snap))
        snap['ownedPaths'] = self.validated_files(row, files, register=True)
        if source.get('selected'):
            self.set_selection(row, files, source['selected'].split(','))
        self.apply_qb_trackers(row)
        if not source.get('requestedPause'):
            self.qb.request('torrents/start', {'hashes': row['gid']})
        snap.update(initializing=False, relocationPending=False, fileCount=len(files))
        self.save_row(row, snap)
        return snap

    def sync(self, ident=None):
        if ident is not None and not self.is_qb(self.row(ident)):
            return super().sync(ident)
        if ident is None: super().sync()
        rows = [self.row(ident)] if ident else [r for r in self.rows() if self.is_qb(r)]
        if not rows: return
        items = {v['hash']: v for v in self.qb.torrents(rows[0]['gid'] if ident else None)}
        for row in rows:
            snap = json.loads(row['snapshot'])
            item = items.get(row['gid'])
            if item is None:
                if snap['status'] in LIVE and time.time() - snap.get('addedAt', 0) > 30:
                    snap.update(status='error', errorMessage='qBittorrent 中未找到任务，可重试恢复', downloadSpeed='0', uploadSpeed='0')
                    self.save_row(row, snap)
                continue
            try:
                snap = self.prepare_task(row, item, snap)
            except DownloadError as exc:
                self.qb.request('torrents/stop', {'hashes': row['gid']})
                snap.update(status='error', errorMessage=str(exc), initializing=False, downloadSpeed='0', uploadSpeed='0')
                self.save_row(row, snap)
                continue
            state = item['state']
            if state in ('error', 'missingFiles', 'unknown'): status = 'error'
            elif snap.get('initializing'): status = 'paused' if json.loads(self.row(row['id'])['source']).get('requestedPause') else 'waiting'
            elif state.startswith(('stopped', 'paused')): status = 'complete' if item.get('progress', 0) >= 1 else 'paused'
            elif state.startswith('queued'): status = 'waiting'
            else: status = 'active'
            # Keep failed path validation visible until an explicit retry.
            if snap.get('errorMessage') and snap.get('status') == 'error': status = 'error'
            snap.update(status=status, qbState=state, downloadSpeed=str(item.get('dlspeed', 0)), uploadSpeed=str(item.get('upspeed', 0)),
                        totalLength=str(item.get('size', 0)), completedLength=str(max(0, item.get('size', 0) - item.get('amount_left', 0))),
                        connections=str(item.get('num_seeds', 0) + item.get('num_leechs', 0)), numSeeders=str(item.get('num_seeds', 0)))
            if not snap.get('metadataPending'): snap['name'] = item['name']
            if status == 'error' and not snap.get('errorMessage'): snap['errorMessage'] = 'qBittorrent 状态：' + state
            if snap != json.loads(row['snapshot']): self.save_row(row, snap)

    def operate(self, ident, command, delete_files=False):
        row = self.row(ident)
        if not self.is_qb(row): return super().operate(ident, command, delete_files)
        snap, source = json.loads(row['snapshot']), json.loads(row['source'])
        gid = row['gid']
        if command in ('pause', 'resume'):
            if command == 'resume':
                if not snap.get('metadataPending'):
                    self.validated_files(row, self.qb.files(gid))
                    item = self.qb.torrents(gid)
                    if not item or Path(item[0]['save_path']) != self.root / snap['directory']:
                        raise DownloadError('任务路径已改变，拒绝继续')
            source['requestedPause'] = command == 'pause'
            self.save_row(row, snap, source)
            # During initialization, resume only metadata; payload starts after validation.
            if command == 'pause' or not snap.get('initializing') or snap.get('metadataPending'):
                self.qb.request('torrents/stop' if command == 'pause' else 'torrents/start', {'hashes': gid})
            snap.update(status='paused' if command == 'pause' else 'waiting', errorMessage='')
            self.save_row(row, snap)
        elif command == 'top':
            self.qb.request('torrents/topPrio', {'hashes': gid})
        elif command == 'retry':
            if snap['status'] not in ('error', 'removed'): raise DownloadError('仅失败任务可以重试')
            existing = self.qb.torrents(gid)
            if existing:
                self.validated_files(row, self.qb.files(gid))
                if Path(existing[0]['save_path']) != self.root / snap['directory']: raise DownloadError('任务路径已改变')
                self.qb.request('torrents/recheck', {'hashes': gid})
                self.qb.request('torrents/start', {'hashes': gid})
            else:
                destination = self.var / 'metadata' / ident if snap.get('metadataPending') else self.directory(snap['directory'])
                if source.get('torrent'): torrent_info(base64.b64decode(source['torrent']))
                self.qb.add(source, destination, metadata=snap.get('metadataPending', False))
            source['requestedPause'] = False
            snap.update(status='waiting', errorMessage='', initializing=True, addedAt=time.time())
            self.save_row(row, snap, source)
        elif command == 'remove':
            # Never use qB's recursive deleteFiles: retain unregistered/user-added files.
            if self.qb.torrents(gid):
                self.qb.request('torrents/delete', {'hashes': gid, 'deleteFiles': 'false'})
                for _ in range(10):
                    if not self.qb.torrents(gid): break
                    time.sleep(.1)
                else: raise DownloadError('任务仍在停止，请稍后重试删除')
            failed = []
            if delete_files:
                try: failed.extend(self.delete_files(row))
                except (DownloadError, OSError) as exc: failed.append(str(exc))
            try: self.delete_metadata(ident)
            except (DownloadError, OSError) as exc: failed.append('缓存：' + str(exc))
            with self.db() as db:
                db.execute('DELETE FROM task_files WHERE task=?', (ident,))
                db.execute('DELETE FROM tasks WHERE id=?', (ident,))
            return {'removed': True, 'failedFiles': failed, 'warning': '记录已删除；部分文件未删除：' + '，'.join(failed[:3]) if failed else ''}
        else: raise DownloadError('不支持的操作')
        return {}

    def detail(self, ident, offset=0):
        row = self.row(ident)
        if not self.is_qb(row): return super().detail(ident, offset)
        snap = json.loads(row['snapshot'])
        files = self.qb.files(row['gid'])
        offset = max(0, int(offset))
        visible = []
        for f in files[offset:offset + 200]:
            visible.append({'index': str(f['index'] + 1), 'path': f['name'], 'displayPath': f['name'],
                            'length': str(f['size']), 'completedLength': str(int(f['size'] * f['progress'])),
                            'selected': 'true' if f['priority'] else 'false'})
        trackers = [[t['url']] for t in self.qb.request('torrents/trackers?hash=' + row['gid']) if t.get('tier', -1) >= 0]
        return dict(snap, id=ident, files=visible, offset=offset, fileCount=len(files), trackers=trackers)

    def select_files(self, data):
        row = self.row(data.get('id'))
        if not self.is_qb(row): return super().select_files(data)
        item = self.qb.torrents(row['gid'])
        if not item or not item[0]['state'].startswith(('stopped', 'paused')): raise DownloadError('请先暂停任务再选择文件')
        files = self.qb.files(row['gid'])
        self.validated_files(row, files, register=True)
        selected = self.selection(data.get('selected'), len(files))
        self.set_selection(row, files, selected.split(','))
        source = json.loads(row['source']); source['selected'] = selected
        self.save_row(row, json.loads(row['snapshot']), source)

    def save_settings(self, data):
        result = super().save_settings(data)
        try:
            # Apply speed/queue preferences only; pending network changes need confirmation.
            prefs = self.qb.preferences()
            network_keys = ('dht', 'pex', 'lsd', 'upnp', 'current_network_interface', 'current_interface_address',
                            'max_connec_per_torrent', 'max_connec', 'listen_port', 'random_port')
            for key in network_keys: prefs.pop(key, None)
            self.qb.request('app/setPreferences', {'json': json.dumps(prefs)})
        except DownloadError: result['applied'] = False
        return result

    def apply_qb_trackers(self, row):
        trackers = self.settings()['trackers']
        if not trackers: return 'empty'
        props = self.qb.request('torrents/properties?hash=' + row['gid'])
        if props.get('is_private') or props.get('private'): return 'private'
        if not self.qb.files(row['gid']): return 'metadata'
        self.qb.request('torrents/addTrackers', {'hash': row['gid'], 'urls': '\n'.join(trackers)})
        return 'applied'

    def apply_trackers(self):
        # aria2 only owns retained legacy jobs. External Openlist jobs are untouched.
        result = super().apply_trackers()
        result.update(qbTasks=0, qbApplied=0, qbPrivate=0, qbMetadata=0, qbFailed=0, qbNoTrackers=0)
        for row in self.rows():
            if self.is_qb(row):
                result['qbTasks'] += 1
                try:
                    outcome = self.apply_qb_trackers(row)
                    result[{'applied':'qbApplied', 'private':'qbPrivate', 'metadata':'qbMetadata', 'empty':'qbNoTrackers'}[outcome]] += 1
                    if outcome == 'applied': result['applied'] += 1
                except DownloadError:
                    result['qbFailed'] += 1
                    result['failed'].append(json.loads(row['snapshot'])['name'])
        atomic_json(self.var / 'tracker-apply.json', dict(result, time=time.time()))
        return result

    def verify_trackers(self):
        configured = set(self.settings()['trackers'])
        own = {r['gid'] for r in self.rows() if self.is_qb(r)}
        tasks = self.qb.torrents()
        rows, started = [], time.monotonic()
        for task in tasks:
            if len(rows) >= 20 or time.monotonic() - started > 8: break
            props = self.qb.request('torrents/properties?hash=' + task['hash'])
            trackers = [t for t in self.qb.request('torrents/trackers?hash=' + task['hash']) if t.get('tier', -1) >= 0]
            actual = {t['url'] for t in trackers}
            rows.append({'name': task['name'], 'owned': task['hash'] in own, 'private': bool(props.get('private', props.get('is_private'))),
                         'matched': len(configured & actual), 'total': len(actual),
                         'working': sum(t.get('status') == 2 for t in trackers), 'failed': sum(t.get('status') == 4 for t in trackers)})
        return {'configured': len(configured), 'tasks': len(tasks), 'checked': len(rows), 'rows': rows,
                'partial': len(rows) < len(tasks)}

    def dispatch(self, action, data):
        if action in ('credentials_get', 'credentials_save'):
            from credentials import read, save
            if action == 'credentials_save': return save(self, data)
            with self.locked(): return read(self)
        if action in ('qb_check', 'qb_apply', 'tracker_verify'):
            with self.locked():
                if action == 'tracker_verify': return self.verify_trackers()
                from qb_update import queue
                return queue(self, 'check' if action == 'qb_check' else 'apply', data.get('proxy') is True)
        if action == 'openlist_info':
            with self.locked():
                port = int((self.var / 'openlist.port').read_text())
                password = (self.var / 'qb.secret').read_text().strip()
                from openlist_gateway import lan_config
                cfg = lan_config(self)
                address = cfg[0] if cfg else '127.0.0.1'
                return {'url': 'http://downloadcenter:%s@%s:%d/' % (quote(password, safe=''), address, port),
                        'directory': str(self.root / self.settings().get('openlistDirectory', 'Download'))}
        return super().dispatch(action, data)
