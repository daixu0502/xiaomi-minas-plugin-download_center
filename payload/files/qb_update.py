#!/usr/bin/env python3
"""Verified qBittorrent 5 / libtorrent 2 updates, isolated from firmware src."""
import fcntl
import json
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from download_lib import DownloadError, atomic_json
from core_update import fetch, BUSY
from fetch_qb import RELEASE, ASSETS

REPOSITORY = 'userdocs/qbittorrent-nox-static'


def core_path(m):
    updated = m.var / 'core/qbittorrent-nox'
    return updated if updated.is_file() else m.home / 'src/files/qbittorrent-nox'


def state(m):
    try:
        result = json.loads((m.var / 'qb-update.json').read_text())
        if result.get('state') in BUSY and time.time() - result.get('time', 0) > 30:
            try:
                cmd = Path('/proc/%d/cmdline' % int(result.get('workerPid', 0))).read_bytes()
                active = b'qb_update.py' in cmd and str(m.home).encode() in cmd
            except (OSError, ValueError): active = False
            if not active: result.update(state='error', message='上次 qBittorrent 更新已中断，请重新检查')
        return result
    except (OSError, ValueError): return {}


def write_state(m, **values):
    atomic_json(m.var / 'qb-update.json', dict(values, time=time.time(), workerPid=os.getpid()))


def parts(version):
    if not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+(?:\.\d+)?', version):
        raise DownloadError('核心版本格式不支持')
    return tuple(map(int, version.split('.')[:3]))


def binary_version(path):
    # The NAS CGI launcher can retain HOME=/root after dropping privileges.
    # Qt initializes standard directories even for --version. Never reuse the
    # running core's profile or trust inherited HOME/XDG paths for this probe.
    try:
        with tempfile.TemporaryDirectory(prefix='qb-version-') as folder:
            env = dict(os.environ, HOME=folder,
                       XDG_CONFIG_HOME=folder + '/config', XDG_CACHE_HOME=folder + '/cache',
                       XDG_DATA_HOME=folder + '/data', XDG_STATE_HOME=folder + '/state',
                       XDG_RUNTIME_DIR=folder)
            run = subprocess.run([str(path), '--version'], stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, check=True, timeout=10, env=env)
        match = re.search(r'qBittorrent v(\d+\.\d+\.\d+)', run.stdout)
        if not match: raise DownloadError('qBittorrent 版本自检失败：输出中没有有效版本号')
        return match.group(1)
    except subprocess.TimeoutExpired as exc:
        raise DownloadError('qBittorrent 版本自检失败：执行超过 10 秒，请检查设备负载') from exc
    except subprocess.CalledProcessError as exc:
        detail = ('被信号 %d 终止' % -exc.returncode) if exc.returncode < 0 else ('退出码 %d' % exc.returncode)
        raise DownloadError('qBittorrent 版本自检失败：' + detail + '，请检查核心是否可运行') from exc
    except OSError as exc:
        raise DownloadError('qBittorrent 版本自检失败：无法执行或创建临时目录（errno=%s）' % exc.errno) from exc


def current(m):
    result = {'current': binary_version(core_path(m)), 'currentLibtorrent': ''}
    try: result['currentLibtorrent'] = m.qb.request('app/buildInfo')['libtorrent']
    except (DownloadError, KeyError):
        try:
            saved = json.loads((m.var / 'core/qb-release.json').read_text())
            if saved['latest'] == result['current']: result['currentLibtorrent'] = saved['libtorrent']
        except (OSError, ValueError, KeyError): pass
        if not result['currentLibtorrent'] and core_path(m) == m.home / 'src/files/qbittorrent-nox':
            result['currentLibtorrent'] = RELEASE.rsplit('_v', 1)[1]
    return result


def release(proxy, arch=None):
    arch = arch or platform.machine()
    if arch not in ASSETS: raise DownloadError('不支持此设备架构')
    releases = json.loads(fetch('https://api.github.com/repos/' + REPOSITORY + '/releases?per_page=50', proxy, 4 * 1024 * 1024))
    candidates = []
    for item in releases:
        match = re.fullmatch(r'release-(5\.\d+\.\d+)_v(2\.\d+\.\d+)', item.get('tag_name', ''))
        if match and not item.get('draft') and not item.get('prerelease'):
            candidates.append((parts(match[1]), parts(match[2]), item, match))
    if not candidates: raise DownloadError('未找到稳定的 qBittorrent 5 / libtorrent 2 构建；跨大版本需先更新插件')
    _, _, item, match = max(candidates, key=lambda v: (v[0], v[1]))
    name = arch + '-qbittorrent-nox'
    assets = [a for a in item.get('assets', []) if a.get('name') == name]
    if len(assets) != 1: raise DownloadError('发布页缺少当前架构核心')
    asset = assets[0]
    digest = asset.get('digest') or ('sha256:' + ASSETS[arch] if item['tag_name'] == RELEASE else '')
    if not re.fullmatch(r'sha256:[a-fA-F0-9]{64}', digest): raise DownloadError('发布方未提供 SHA-256，拒绝更新')
    if type(asset.get('size')) is not int or not 0 < asset['size'] <= 100 * 1024 * 1024: raise DownloadError('核心大小异常')
    return {'latest': match[1], 'libtorrent': match[2], 'digest': digest[7:].lower(), 'size': asset['size'],
            'arch': arch, 'url': 'https://github.com/' + REPOSITORY + '/releases/download/' + item['tag_name'] + '/' + name}


def newer(info, installed):
    return (parts(info['latest']), parts(info['libtorrent'])) > (parts(installed['current']), parts(installed['currentLibtorrent']) if installed['currentLibtorrent'] else (0, 0, 0))


def unpack(data, info, target):
    if len(data) != info['size'] or hashlib.sha256(data).hexdigest() != info['digest']: raise DownloadError('qBittorrent 大小或 SHA-256 不匹配')
    machine = 183 if info['arch'] == 'aarch64' else 62
    if len(data) < 64 or data[:6] != b'\x7fELF\x02\x01' or int.from_bytes(data[18:20], 'little') != machine: raise DownloadError('核心架构不匹配')
    with target.open('xb') as stream: stream.write(data)
    target.chmod(0o755)
    if binary_version(target) != info['latest']: raise DownloadError('核心版本与发布信息不一致')


def wait_running(m, expected):
    for _ in range(40):
        try:
            if m.qb.request('app/version').strip().lstrip('v') == expected:
                m.qb.request('transfer/info'); m.rpc('getVersion')
                return
        except DownloadError: pass
        time.sleep(.25)
    raise DownloadError('新 qBittorrent 未能正常启动')


def install_candidate(m, candidate, version):
    from service import control_locked, live
    target = m.var / 'core/qbittorrent-nox'
    target.parent.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.qb-update-', dir=target.parent) as folder:
        stage = Path(folder)
        pending, previous, profile = stage / 'new', stage / 'old', m.var / 'qb-profile'
        shutil.copy2(candidate, pending); shutil.copy2(core_path(m), previous)
        old_version = binary_version(previous)
        with (m.var / 'lifecycle.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            enabled, running = (m.var / 'enabled').exists(), bool(live(m))
            stopped, switched, saved_profile = False, False, False
            try:
                if running: control_locked(m, 'stop'); stopped = True
                if profile.is_symlink(): raise DownloadError('qBittorrent 状态目录异常，拒绝更新')
                if profile.exists():
                    shutil.copytree(profile, stage / 'profile', symlinks=True)
                    saved_profile = True
                os.replace(pending, target); switched = True
                # Start even when normally disabled to verify API compatibility, then stop again.
                control_locked(m, 'start'); wait_running(m, version)
                if not running: control_locked(m, 'stop')
            except Exception as exc:
                if running and not stopped: raise
                if switched:
                    try: control_locked(m, 'stop')
                    except Exception as stop_error:
                        shutil.copy2(previous, m.var / 'qbittorrent-nox.recovery')
                        if saved_profile:
                            # Preserve profile together with binary; never discard the only rollback copy.
                            recovery = m.var / ('qb-profile-recovery-' + str(int(time.time())))
                            os.replace(stage / 'profile', recovery)
                        raise DownloadError('新核心尚未退出，未强制替换；旧核心与状态已保存为 recovery，请检查日志') from stop_error
                    os.replace(previous, target)
                    if saved_profile:
                        if profile.exists(): os.replace(profile, stage / 'failed-profile')
                        os.replace(stage / 'profile', profile)
                    elif profile.exists():
                        # A failed first start must not leave a newly created profile
                        # that cannot be read by the original bundled core.
                        os.replace(profile, stage / 'failed-profile')
                if running:
                    control_locked(m, 'start'); wait_running(m, old_version)
                raise DownloadError('升级未完成，原核心和任务状态已保留／恢复：' + str(exc)) from exc
            finally:
                if enabled: (m.var / 'enabled').touch(mode=0o600)
                else: (m.var / 'enabled').unlink(missing_ok=True)


def queue(m, command, proxy=False):
    from core_update import state as aria_state
    if any(s.get('state') in BUSY for s in (state(m), aria_state(m))): raise DownloadError('已有核心检查或更新在执行，请稍后再试')
    write_state(m, state='pending', command=command, message='正在准备' + ('检查更新…' if command == 'check' else '下载更新…'))
    try:
        with (m.var / 'qb-update.log').open('a') as log:
            subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), command, str(m.home), 'proxy' if proxy else 'direct'],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True)
    except OSError as exc:
        write_state(m, state='error', message='无法启动 qBittorrent 更新进程'); raise DownloadError('更新进程启动失败') from exc
    return {'pending': True}


def run(m, command, proxy):
    from service import tracker_error
    if os.geteuid() == 0 or command not in ('check', 'apply'): raise DownloadError('更新必须由插件所属用户执行')
    m.ready()
    with (m.var / 'core-update.lock').open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            write_state(m, state='error', message='另一个核心正在更新，请稍后重试'); return
        common = {'command': command, 'proxy': proxy}
        try:
            write_state(m, **common, state='checking', message='正在检查当前渠道的最新版本…')
            installed, info = current(m), release(proxy)
            available = newer(info, installed)
            common.update(installed, latest=info['latest'], libtorrent=info['libtorrent'], updateAvailable=available)
            if command == 'check' or not available:
                write_state(m, **common, state='done', message='发现新版本，可下载并更新。' if available else '当前已是此渠道的最新版本。'); return
            with tempfile.TemporaryDirectory(prefix='qb-download-', dir=m.var) as folder:
                write_state(m, **common, state='downloading', message='正在下载并校验 SHA-256…')
                candidate = Path(folder) / 'qbittorrent-nox'
                unpack(fetch(info['url'], proxy, 100 * 1024 * 1024), info, candidate)
                write_state(m, **common, state='installing', message='正在保存任务并切换内核，下载将短暂中断…')
                install_candidate(m, candidate, info['latest'])
            atomic_json(m.var / 'core/qb-release.json', {k: info[k] for k in ('latest', 'libtorrent', 'digest')})
            common.update(current=info['latest'], currentLibtorrent=info['libtorrent'], updateAvailable=False)
            write_state(m, **common, state='done', message='更新完成，任务、设置和 Openlist 连接已保留。')
        except Exception as exc:
            write_state(m, **common, state='error', message=tracker_error(exc).replace('订阅', '核心发布源').replace('8 秒', '20 秒'))


if __name__ == '__main__':
    from service import manager
    os.umask(0o077)
    run(manager(sys.argv[2]), sys.argv[1], sys.argv[3] == 'proxy')
