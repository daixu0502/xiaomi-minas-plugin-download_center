#!/usr/bin/env python3
"""Match plugin.sh: C-sorted regular-file hashes, newline separated, then SHA256."""
import hashlib
import os
import stat
import sys
from pathlib import Path


def source_abstract(source):
    files = []
    for parent, _, names in os.walk(source, followlinks=False):
        for name in names:
            path = Path(parent) / name
            if stat.S_ISREG(path.lstat().st_mode):
                files.append(path)
    result = hashlib.sha256()
    for path in sorted(files, key=lambda p: os.fsencode(str(p))):
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
        result.update((digest.hexdigest() + '\n').encode('ascii'))
    return result.hexdigest()


if __name__ == '__main__':
    print(source_abstract(sys.argv[1]))
