#!/usr/bin/env python3
"""Fetch pinned static aria2; verify SHA256 before extracting only the executable."""
import hashlib
import io
import os
import sys
import urllib.request
import zipfile
from pathlib import Path

ASSETS = {
    "aarch64": ("aria2-aarch64-linux-musl_static.zip", "0c681a89a40e0f82d1f5137608e86257eb0af201459c002941ea098f2b8c26b6"),
    "x86_64": ("aria2-x86_64-linux-musl_static.zip", "e0a09b12ef67f35f8a8e4fdddbec851d235b7c31da549d0578bff459032b499a"),
}


def install(destination, arch="aarch64", archive=None):
    filename, expected = ASSETS[arch]
    if archive:
        data = Path(archive).read_bytes()
    else:
        url = "https://github.com/abcfy2/aria2-static-build/releases/download/1.37.0/" + filename
        print("下载 aria2 1.37.0 静态核心（第三方构建，固定 SHA-256）……", flush=True)
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read(40 * 1024 * 1024 + 1)
    if len(data) > 40 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != expected:
        raise RuntimeError("核心压缩包 SHA-256 不匹配，拒绝安装")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        entries = [e for e in z.infolist() if Path(e.filename).name == "aria2c" and not e.is_dir()]
        if len(entries) != 1 or entries[0].file_size > 80 * 1024 * 1024:
            raise RuntimeError("核心压缩包内容异常")
        binary = z.read(entries[0])
    if not binary.startswith(b"\x7fELF"):
        raise RuntimeError("不是 Linux ELF 核心")
    destination = Path(destination)
    temporary = destination.with_name(destination.name + ".new")
    with temporary.open("xb") as f:
        f.write(binary)
    os.chmod(temporary, 0o755)
    os.replace(temporary, destination)


if __name__ == "__main__":
    install(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "aarch64", sys.argv[3] if len(sys.argv) > 3 else None)
