#!/usr/bin/env python3
"""Per-user, verified static-core updates. No arbitrary URLs or root helper."""
import fcntl
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.request import Request, ProxyHandler, build_opener

from download_lib import DownloadError, atomic_json, core_path
from fetch_core import ASSETS

REPOSITORY = 'abcfy2/aria2-static-build'
BUSY = ('pending', 'checking', 'downloading', 'installing')


def state(m):
    try:
        result = json.loads((m.var / 'core-update.json').read_text())
        if result.get('state') in BUSY and time.time() - result.get('time', 0) > 30:
            try:
                cmd = Path('/proc/%d/cmdline' % int(result.get('workerPid', 0))).read_bytes()
                active = b'core_update.py' in cmd and str(m.home).encode() in cmd
            except (OSError, ValueError):
                active = False
            if not active:
                result.update(state='error', message='上次核心更新已中断，请重新检查版本；任务文件未被删除')
        return result
    except (OSError, ValueError):
        return {}


def write_state(m, **values):
    values['time'] = time.time()
    values['workerPid'] = os.getpid()
    atomic_json(m.var / 'core-update.json', values)


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'v?\d+\.\d+\.\d+', value):
        raise DownloadError('发布版本格式不支持')
    return tuple(map(int, value.lstrip('v').split('.')))


def binary_version(path):
    try:
        result = subprocess.run([str(path), '--version'], capture_output=True, text=True, timeout=10, check=True)
        match = re.search(r'aria2 version (\d+\.\d+\.\d+)', result.stdout)
        if not match: raise ValueError()
        return match.group(1)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise DownloadError('核心自检失败，无法获取 aria2 版本') from exc


def fetch(url, proxy, maximum):
    proxies = {'http': 'http://127.0.0.1:7890', 'https': 'http://127.0.0.1:7890'} if proxy else {}
    request = Request(url, headers={'User-Agent': 'MinasDownloadCenter', 'Accept': 'application/vnd.github+json' if 'api.github.com/' in url else 'application/octet-stream'})
    with build_opener(ProxyHandler(proxies)).open(request, timeout=20) as response:
        data = response.read(maximum + 1)
    if len(data) > maximum: raise DownloadError('下载内容超过大小限制')
    return data


def release(proxy, arch=None):
    arch = arch or platform.machine()
    if arch not in ASSETS: raise DownloadError('此设备架构暂不支持自动更新')
    data = json.loads(fetch('https://api.github.com/repos/' + REPOSITORY + '/releases/latest', proxy, 2 * 1024 * 1024))
    tag = data.get('tag_name'); version_tuple(tag)
    if data.get('draft') or data.get('prerelease'): raise DownloadError('不安装测试版或草稿版本')
    filename, pinned = ASSETS[arch]
    assets = [a for a in data.get('assets', []) if a.get('name') == filename]
    if len(assets) != 1: raise DownloadError('发布页没有匹配设备架构的静态核心')
    asset = assets[0]
    digest = asset.get('digest') or ('sha256:' + pinned if tag.lstrip('v') == '1.37.0' else '')
    if not re.fullmatch(r'sha256:[a-fA-F0-9]{64}', digest):
        raise DownloadError('发布方未提供有效 SHA-256，已拒绝更新')
    if not 0 < asset.get('size', 0) <= 40 * 1024 * 1024:
        raise DownloadError('核心压缩包大小异常')
    return {'latest': tag.lstrip('v'), 'digest': digest[7:].lower(), 'size': asset['size'], 'arch': arch,
            'url': 'https://github.com/' + REPOSITORY + '/releases/download/' + tag + '/' + filename}


def unpack(data, info, target):
    if len(data) != info['size'] or hashlib.sha256(data).hexdigest() != info['digest']:
        raise DownloadError('核心压缩包 SHA-256 或大小不匹配，拒绝安装')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        files = [f for f in archive.infolist() if Path(f.filename).name == 'aria2c' and not f.is_dir()]
        if len(files) != 1 or not 0 < files[0].file_size <= 80 * 1024 * 1024:
            raise DownloadError('核心压缩包内容异常')
        binary = archive.read(files[0])
    machine = 183 if info['arch'] == 'aarch64' else 62
    if len(binary) < 64 or binary[:6] != b'\x7fELF\x02\x01' or int.from_bytes(binary[18:20], 'little') != machine:
        raise DownloadError('核心不是匹配设备架构的 64 位 Linux ELF')
    with target.open('xb') as stream: stream.write(binary)
    target.chmod(0o755)
    if binary_version(target) != info['latest']: raise DownloadError('核心版本与发布信息不一致')


def wait_running(m, expected):
    for _ in range(40):
        try:
            if m.rpc('getVersion')['version'] == expected: return
        except DownloadError:
            pass
        time.sleep(.25)
    raise DownloadError('新核心未能正常启动')


def install_candidate(m, candidate, version):
    from service import control_locked, live
    target = m.var / 'core/aria2c'
    target.parent.mkdir(mode=0o700, exist_ok=True)
    # Never modify src: firmware verifies it at boot and uninstalls on mismatch.
    # Staging and rollback stay on the runtime filesystem for atomic replacement.
    with tempfile.TemporaryDirectory(prefix='.core-update-', dir=target.parent) as folder:
        pending, previous = Path(folder)/'aria2c.new', Path(folder)/'aria2c.previous'
        shutil.copy2(candidate, pending)
        shutil.copy2(core_path(m), previous)
        old_version = binary_version(previous)
        with (m.var/'lifecycle.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            enabled = (m.var/'enabled').exists()
            running = bool(live(m))
            stopped = False
            try:
                if running:
                    control_locked(m, 'stop'); stopped = True
                os.replace(pending, target)
                if running:
                    control_locked(m, 'start'); wait_running(m, version)
            except Exception as exc:
                if running and not stopped: raise
                if running:
                    try:
                        control_locked(m, 'stop')
                    except Exception as stop_error:
                        shutil.copy2(previous, m.var/'aria2c.recovery')
                        raise DownloadError('新核心尚未停止，未强制替换；旧核心已保留在 var/aria2c.recovery，请检查日志') from stop_error
                os.replace(previous, target)
                if running:
                    control_locked(m, 'start')
                    try: wait_running(m, old_version)
                    except DownloadError as rollback_error:
                        raise DownloadError('新版本启动失败，旧核心已恢复但服务未启动，请查看服务日志') from rollback_error
                raise DownloadError('核心切换失败，已恢复旧核心：' + str(exc)) from exc
            finally:
                if enabled: (m.var/'enabled').touch(mode=0o600)
                else: (m.var/'enabled').unlink(missing_ok=True)


def queue(m, command, proxy=False):
    from qb_update import state as qb_state
    if qb_state(m).get('state') in BUSY:
        raise DownloadError('qBittorrent 正在检查或升级，请稍后再操作 aria2')
    previous = state(m)
    if previous.get('state') in BUSY and time.time() - previous.get('time', 0) < 900:
        raise DownloadError('已有核心检查或更新正在执行，请稍后查看结果')
    write_state(m, state='pending', command=command, message='正在准备核心' + ('检查' if command == 'check' else '更新'))
    try:
        with (m.var/'core-update.log').open('a') as log:
            subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), command, str(m.home), 'proxy' if proxy else 'direct'],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True)
    except OSError as exc:
        write_state(m, state='error', message='无法启动更新进程')
        raise DownloadError('无法启动更新进程') from exc
    return {'pending': True}


def run(m, command, proxy):
    from service import tracker_error
    if command not in ('check', 'apply'): raise DownloadError('无效核心更新操作')
    if os.geteuid() == 0: raise DownloadError('核心更新禁止以 root 运行')
    m.ready()
    with (m.var/'core-update.lock').open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return
        common = {'command': command, 'proxy': proxy}
        try:
            write_state(m, **common, state='checking', message='正在检查静态核心发布版本…')
            current = binary_version(core_path(m))
            info = release(proxy)
            available = version_tuple(info['latest']) > version_tuple(current)
            common.update(current=current, latest=info['latest'], updateAvailable=available)
            if command == 'check' or not available:
                write_state(m, **common, state='done', message='发现新版本，可下载并更新' if available else '当前已是此构建渠道的最新版本')
                return
            with tempfile.TemporaryDirectory(prefix='core-download-', dir=m.var) as folder:
                write_state(m, **common, state='downloading', message='正在下载并校验核心 SHA-256…')
                candidate = Path(folder)/'aria2c'
                unpack(fetch(info['url'], proxy, 40 * 1024 * 1024), info, candidate)
                write_state(m, **common, state='installing', message='正在保存会话并切换核心，下载将短暂中断…')
                install_candidate(m, candidate, info['latest'])
            common.update(current=info['latest'], updateAvailable=False)
            write_state(m, **common, state='done', message='核心更新成功，任务与设置已保留')
        except Exception as exc:
            # Shared network formatter mentions subscriptions; use core terminology.
            message = tracker_error(exc).replace('订阅', '核心发布源').replace('8 秒', '20 秒')
            write_state(m, **common, state='error', message=message)


if __name__ == '__main__':
    from service import manager
    os.umask(0o077)
    run(manager(sys.argv[2]), sys.argv[1], sys.argv[3] == 'proxy')
