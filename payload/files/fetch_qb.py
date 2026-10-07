#!/usr/bin/env python3
"""Pinned third-party qBittorrent static build; verify before installation."""
import hashlib
import os
import sys
from pathlib import Path
from urllib.request import urlopen

VERSION = '5.2.4'
RELEASE = 'release-5.2.4_v2.0.15'
ASSETS = {
    'aarch64': 'f12e821d5782c39e3093d788689182e9ac823fa01b3a82d6c00fdb993e4428d8',
    'x86_64': '14d29323f1c12c1e8892ac69496f8b51ddebb74b4b590fc13e343aa213fecaf4',
}


def install(destination, arch='aarch64', archive=None):
    if archive:
        data = Path(archive).read_bytes()
    else:
        url = 'https://github.com/userdocs/qbittorrent-nox-static/releases/download/' + RELEASE + '/' + arch + '-qbittorrent-nox'
        print('下载 qBittorrent %s 静态核心（第三方构建，固定 SHA-256）……' % VERSION, flush=True)
        with urlopen(url, timeout=90) as response:
            data = response.read(100 * 1024 * 1024 + 1)
    if len(data) > 100 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != ASSETS[arch]:
        raise RuntimeError('qBittorrent SHA-256 不匹配，拒绝安装')
    machine = 183 if arch == 'aarch64' else 62
    if data[:6] != b'\x7fELF\x02\x01' or int.from_bytes(data[18:20], 'little') != machine:
        raise RuntimeError('qBittorrent 架构不匹配')
    path = Path(destination)
    temporary = path.with_name(path.name + '.new')
    with temporary.open('xb') as stream:
        stream.write(data)
    temporary.chmod(0o755)
    os.replace(temporary, path)


if __name__ == '__main__':
    install(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else 'aarch64', sys.argv[3] if len(sys.argv) > 3 else None)
