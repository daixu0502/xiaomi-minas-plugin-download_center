#!/usr/bin/env python3
"""Explicit admin opt-in deployment helper. Updates only Openlist's qB URL.

Not run by plugin installation. Requires separate permission to restart Openlist.
No credentials are printed. Rolls the setting back if startup validation fails.
"""
import http.cookiejar
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode
from urllib.request import Request, build_opener, ProxyHandler, HTTPCookieProcessor


def run(user, container):
    if os.geteuid() != 0 or not re.fullmatch(r'u[0-9]+', user) or not re.fullmatch(r'[A-Za-z0-9_.-]+', container):
        raise RuntimeError('Must run as NAS administrator with explicit user/container')
    docker = '/data/docker/docker'
    def command(*args):
        return subprocess.check_output([docker, *args], text=True, stderr=subprocess.DEVNULL, timeout=50)
    info = json.loads(command('inspect', container))[0]
    if info['HostConfig']['NetworkMode'] != 'host': raise RuntimeError('Openlist must use host networking')
    volume = next(v for v in info['Mounts'] if v['Destination'] == '/opt/openlist/data')
    temporary = next(v for v in info['Mounts'] if v['Destination'] == '/opt/openlist/data/temp')
    home = Path('/home') / user / 'plugin/downloadcenter'
    sys.path.insert(0, str(home / 'src/files'))
    from service import manager
    m = manager(home)
    target = m.root / m.settings().get('openlistDirectory', 'Download')
    if Path(temporary['Source']).resolve() != target.resolve(): raise RuntimeError('Temporary directory mapping does not match')
    endpoint = 'http://127.0.0.1:%d/' % int((m.var / 'openlist.port').read_text())
    secret = (m.var / 'qb.secret').read_text().strip()
    url = endpoint.replace('http://', 'http://downloadcenter:' + secret + '@')
    opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(http.cookiejar.CookieJar()))
    request = Request(endpoint + 'api/v2/auth/login', urlencode({'username':'downloadcenter', 'password':secret}).encode())
    if opener.open(request, timeout=5).read() != b'Ok.': raise RuntimeError('Adapter authentication failed')
    if not opener.open(endpoint + 'api/v2/app/version', timeout=5).read().startswith(b'v5.2.4'): raise RuntimeError('Unexpected core')
    db = Path(volume['Source']) / 'data.db'
    with sqlite3.connect(str(db)) as connection:
        old = connection.execute("SELECT value FROM x_setting_items WHERE key='qbittorrent_url'").fetchone()
        if old is None: raise RuntimeError('qBittorrent setting not found')
    backup_dir = Path(tempfile.mkdtemp(prefix='minas-openlist-qb-'))
    backup = backup_dir / 'previous-setting.json'
    with backup.open('x') as stream: json.dump({'qbittorrent_url': old[0]}, stream)
    backup.chmod(0o600)
    changed, safe_to_remove_backup = False, False
    was_running = info['State']['Running']
    try:
        if was_running: command('stop', '--time', '30', container)
        with sqlite3.connect(str(db)) as connection:
            connection.execute("UPDATE x_setting_items SET value=? WHERE key='qbittorrent_url'", (url,))
        changed = True
        if was_running:
            since = str(int(time.time()))
            app_log = Path(volume['Source']) / 'log/log.log'
            log_offset = app_log.stat().st_size if app_log.exists() else 0
            command('start', container)
            for _ in range(35):
                # Docker merges the application's stdout/stderr into this captured result.
                logs = subprocess.run([docker, 'logs', '--since', since, container], capture_output=True, text=True, timeout=5)
                text = logs.stdout + logs.stderr
                if app_log.exists():
                    with app_log.open('rb') as stream:
                        stream.seek(log_offset if app_log.stat().st_size >= log_offset else 0)
                        text += stream.read(256 * 1024).decode(errors='replace')
                if 'init offline download tool qBittorrent success' in text: break
                time.sleep(1)
            else: raise RuntimeError('Openlist did not confirm qBittorrent initialization')
        safe_to_remove_backup = True
        print(json.dumps({'configured': True, 'container': container, 'restarted': was_running,
                          'adapterPort': int((m.var / 'openlist.port').read_text()), 'credentialsPrinted': False}))
    except Exception:
        if changed:
            command('stop', '--time', '30', container)
            with sqlite3.connect(str(db)) as connection:
                connection.execute("UPDATE x_setting_items SET value=? WHERE key='qbittorrent_url'", (old[0],))
            if was_running: command('start', container)
        safe_to_remove_backup = True
        raise
    finally:
        # Only this helper's one-file temporary backup; no recursive deletion.
        if safe_to_remove_backup:
            backup.unlink(); backup_dir.rmdir()
        else:
            print('Rollback incomplete; private setting backup retained at: ' + str(backup), file=sys.stderr)


if __name__ == '__main__':
    if len(sys.argv) != 4 or sys.argv[1] != '--apply': raise SystemExit('Usage: --apply USER CONTAINER')
    run(sys.argv[2], sys.argv[3])
