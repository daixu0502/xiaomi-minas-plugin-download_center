"""Explicit credential access; edits require both download cores to be stopped."""
import fcntl
import os
from pathlib import Path
import tempfile
from download_lib import DownloadError

FILES = {'aria2': 'rpc.secret', 'qbittorrent': 'qb.secret'}


def validate(data):
    result = {}
    for key in FILES:
        value = data.get(key, '')
        if not isinstance(value, str): raise DownloadError('密钥格式无效')
        if not value: continue
        if not 8 <= len(value) <= 128 or any(not 33 <= ord(c) <= 126 for c in value):
            raise DownloadError('密钥须为 8–128 位英文、数字或符号，不能包含空格和换行')
        result[key] = value
    if not result: raise DownloadError('请至少填写一项新密钥')
    return result


def write(path, value):
    if path.is_symlink(): raise DownloadError('拒绝符号链接')
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='.credential-', delete=False) as out:
            name = Path(out.name)
            os.fchmod(out.fileno(), 0o600)
            out.write(value)
            out.flush(); os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if name: name.unlink(missing_ok=True)


def read(m):
    return {key: (m.var / filename).read_text().strip() for key, filename in FILES.items()}


def save(m, data):
    from service import live
    updates = validate(data)
    with (m.var / 'core-update.lock').open('a') as core, (m.var / 'lifecycle.lock').open('a') as lifecycle:
        for lock in (core, lifecycle):
            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise DownloadError('服务正在启动或更新，请稍后再试')
        if live(m): raise DownloadError('请先停止下载服务，再保存密钥；保存后重新启动服务')
        # Also refuse live orphaned cores, even when the supervisor PID is absent.
        for file in ('rpc.port', 'qb.port'):
            import socket
            try:
                with socket.create_connection(('127.0.0.1', int((m.var / file).read_text())), timeout=.2):
                    raise DownloadError('下载核心尚未停止，请稍后再试')
            except OSError: pass
        with m.locked():
            previous = read(m)
            try:
                for key, value in updates.items(): write(m.var / FILES[key], value)
            except Exception:
                for key in updates: write(m.var / FILES[key], previous[key])
                raise DownloadError('保存失败，已恢复原密钥')
    return {'saved': True, 'message': '密钥已保存，请启动下载服务，并更新外部客户端的连接密码'}
