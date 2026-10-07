#!/usr/bin/env python3
"""Per-user download manager. No root privileges and no arbitrary RPC forwarding."""
import base64
import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import time
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, parse_qs, unquote
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError

VERSION = "1.1.1"
MAX_TORRENT = 4 * 1024 * 1024
DEFAULTS = {"directory": "", "concurrent": 3, "connections": 4, "downloadKiB": 0,
            "uploadKiB": 1024, "seedRatio": 1.0, "seedMinutes": 60,
            "trackers": [], "sources": [], "autoTrackers": False}
BT_DEFAULTS = {"ipv6": True, "dht": True, "dht6": False, "pex": True, "lpd": True,
               "maxPeers": 55, "peerSpeedKiB": 50}
DEFAULTS.update(BT_DEFAULTS)
LIVE = {"active", "waiting", "paused"}


class DownloadError(Exception):
    pass


def core_path(m):
    # Updated binaries are runtime state, outside plugin.sh's immutable src tree.
    updated = m.var / 'core/aria2c'
    return updated if updated.is_file() else m.home / 'src/files/aria2c'


def atomic_json(path, data):
    temp = path.with_name(path.name + "." + secrets.token_hex(6))
    try:
        with temp.open("x", encoding="utf-8") as stream:
            os.chmod(temp, 0o600)
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def valid_url(value, tracker=False):
    if not isinstance(value, str) or len(value) > 8192 or re.search(r"[\s\x00-\x1f\x7f]", value):
        raise DownloadError("链接格式无效，不能包含空格或控制字符")
    try:
        u = urlsplit(value)
        if tracker:
            allowed = ("http", "https", "udp")
        else:
            allowed = ("http", "https", "ftp", "sftp", "magnet")
        if u.scheme.lower() not in allowed:
            raise ValueError()
        if u.scheme == "magnet":
            if not any(re.fullmatch(r"urn:btih:(?:[a-fA-F0-9]{40}|[A-Z2-7a-z]{32})", x)
                       for x in parse_qs(u.query).get("xt", [])):
                raise ValueError()
        elif not u.hostname or u.username or u.password:
            raise ValueError()
        if u.port is not None and not 1 <= u.port <= 65535:
            raise ValueError()
    except ValueError as exc:
        raise DownloadError("链接无效或协议不支持（不接受链接内嵌账户密码）") from exc
    return value


def tracker_list(value):
    lines = value.splitlines() if isinstance(value, str) else value
    if not isinstance(lines, list) or len(lines) > 4000:
        raise DownloadError("Tracker 列表过大")
    result = []
    for item in lines:
        if not isinstance(item, str):
            raise DownloadError("Tracker 格式错误")
        item = item.strip()
        if not item or item.startswith("#"):
            continue
        valid_url(item, True)
        if "," in item:
            raise DownloadError("Tracker 地址不能包含逗号")
        if item not in result:
            result.append(item)
    if len(result) > 500:
        raise DownloadError("最多保存 500 个 Tracker")
    return result


def parse_tracker_subscription(raw):
    if len(raw) > 256 * 1024:
        raise DownloadError("订阅超过 256 KiB")
    try:
        lines = raw.decode('utf-8-sig').splitlines()
    except UnicodeError as exc:
        raise DownloadError("订阅不是有效的 UTF-8 文本") from exc
    if len(lines) > 4000:
        raise DownloadError("订阅超过 4000 行")
    values, unsupported, invalid = [], {}, 0
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#'): continue
        try:
            scheme = urlsplit(line).scheme.lower()
            if scheme and scheme not in ('http', 'https', 'udp'):
                label = scheme[:24]
                unsupported[label] = unsupported.get(label, 0) + 1
                continue
            valid_url(line, True)
            if ',' in line: raise DownloadError('地址包含逗号')
        except (DownloadError, ValueError):
            invalid += 1
            continue
        if line not in values: values.append(line)
    if not values:
        raise DownloadError("没有可用的 HTTP/HTTPS/UDP Tracker（可能返回了网页、空内容或不支持的协议）")
    return {"trackers": values[:500], "unsupported": unsupported, "invalid": invalid,
            "omitted": max(0, len(values) - 500)}


def tracker_cache(settings):
    """Migrate old combined lists without guessing which source owns a tracker."""
    sources = settings['sources']
    if 'trackerSourceCache' in settings:
        cache = {url: item for url, item in settings['trackerSourceCache'].items() if url in sources}
        return cache, settings.get('legacySubscribedTrackers', [])
    old = settings.get('subscribedTrackers', [])
    if len(sources) == 1:
        return {sources[0]: {'trackers': old, 'updatedAt': 0}}, []
    return {}, old if sources else []


def merged_trackers(manual, cache, sources, legacy=()):
    values = list(dict.fromkeys(manual + [value for url in sources for value in cache.get(url, {}).get('trackers', [])] + list(legacy)))
    return values[:500], max(0, len(values) - 500)


def torrent_info(raw):
    """Bounded bencode parser, rejects traversal before a torrent reaches aria2."""
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_TORRENT:
        raise DownloadError("种子文件不能为空或超过 4 MiB")
    pos, nodes, info_hash = 0, 0, ''

    def read(depth=0):
        nonlocal pos, nodes, info_hash
        nodes += 1
        if depth > 40 or nodes > 200000 or pos >= len(raw):
            raise ValueError()
        t = raw[pos:pos + 1]
        pos += 1
        if t == b"i":
            end = raw.index(b"e", pos)
            if end - pos > 24:
                raise ValueError()
            n = int(raw[pos:end]); pos = end + 1
            return n
        if t in (b"l", b"d"):
            result = [] if t == b"l" else {}
            while raw[pos:pos + 1] != b"e":
                key = read(depth + 1)
                if t == b"l":
                    result.append(key)
                else:
                    if not isinstance(key, bytes) or key in result:
                        raise ValueError()
                    start = pos
                    result[key] = read(depth + 1)
                    if depth == 0 and key == b'info':
                        info_hash = hashlib.sha1(raw[start:pos]).hexdigest()
            pos += 1
            return result
        if t.isdigit():
            end = raw.index(b":", pos - 1)
            if end - pos > 12:
                raise ValueError()
            size = int(raw[pos - 1:end]); pos = end + 1
            if size < 0 or pos + size > len(raw):
                raise ValueError()
            value = raw[pos:pos + size]; pos += size
            return value
        raise ValueError()

    def component(b):
        s = b.decode("utf-8", errors="strict")
        if not s or s in (".", "..") or re.search(r"[/\\\x00-\x1f\x7f]", s) or ":" in s:
            raise ValueError()
        return s

    try:
        obj = read()
        if pos != len(raw):
            raise ValueError()
        info = obj[b"info"]
        name = component(info[b"name"])
        # Validate both legacy and UTF-8 alternate names; aria2 may use either.
        if b"name.utf-8" in info:
            component(info[b"name.utf-8"])
        if b"pieces" not in info:
            raise DownloadError("目前核心支持 BT v1 / 混合种子，不支持纯 BT v2 种子")
        files = []
        entries = info.get(b"files", [{b"path": [], b"length": info.get(b"length")}])
        if not isinstance(entries, list) or not entries or len(entries) > 10000:
            raise ValueError()
        for index, entry in enumerate(entries, 1):
            parts = [component(v) for v in entry[b"path"]]
            if b"path.utf-8" in entry:
                for v in entry[b"path.utf-8"]:
                    component(v)
            if b"symlink path" in entry or b"l" in entry.get(b"attr", b""):
                raise ValueError()
            size = entry[b"length"]
            if type(size) is not int or size < 0:
                raise ValueError()
            files.append({"index": str(index), "path": "/".join([name] + parts), "length": str(size)})
        return {"name": name, "files": files, "multi": b"files" in info, "infoHash": info_hash,
                "torrentHash": hashlib.sha1(raw).hexdigest(),
                "total": sum(int(f["length"]) for f in files)}
    except DownloadError:
        raise
    except (ValueError, KeyError, TypeError, IndexError, AttributeError, UnicodeError) as exc:
        raise DownloadError("种子损坏、编码不支持或包含不安全路径") from exc


class Manager:
    def __init__(self, home, root, require_mount=True):
        self.home, self.root = Path(home), Path(root)
        self.var = self.home / "var"
        self.require_mount = require_mount
        self.settings_path = self.var / "settings.json"

    def ready(self):
        if self.require_mount:
            pool = self.root.parent.parent
            if not os.path.ismount(pool):
                raise DownloadError("存储池尚未挂载，已禁止启动或写入下载任务")
        if not self.root.is_dir() or self.root.is_symlink():
            raise DownloadError("当前用户文件空间不可用")
        if not self.var.is_dir():
            raise DownloadError("插件状态目录不存在，请重新安装")

    @contextlib.contextmanager
    def locked(self):
        self.ready()
        with (self.var / "api.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def db(self):
        conn = sqlite3.connect(str(self.var / "tasks.sqlite"), timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, gid TEXT, created REAL, source TEXT, snapshot TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS task_files (task TEXT, path TEXT, dev INTEGER, ino INTEGER, PRIMARY KEY(task,path))")
        return conn

    def settings(self):
        data = dict(DEFAULTS)
        try:
            data.update(json.loads(self.settings_path.read_text()))
        except FileNotFoundError:
            pass
        return data

    def directory(self, relative="", create=False):
        if not isinstance(relative, str) or len(relative) > 2048 or "\\" in relative or re.search(r"[\x00-\x1f]", relative):
            raise DownloadError("目录格式无效")
        p = PurePosixPath(relative)
        if p.is_absolute() or ".." in p.parts:
            raise DownloadError("下载目录必须位于当前用户的我的文件中")
        root = self.root.resolve(strict=True)
        path = root
        # Never traverse symlinks, even if the final target currently points inside.
        for part in p.parts:
            path = path / part
            if path.is_symlink():
                raise DownloadError("为避免越界，不允许选择符号链接目录")
        try:
            path.resolve().relative_to(root)
        except ValueError as exc:
            raise DownloadError("目录超出当前用户空间") from exc
        if create:
            path.mkdir(parents=False, exist_ok=False)
        if not path.is_dir() or not os.access(path, os.W_OK | os.X_OK):
            raise DownloadError("目录不存在或当前用户没有写入权限")
        return path

    def browse(self, relative=""):
        path = self.directory(relative)
        entries = sorted((p for p in path.iterdir() if not p.is_symlink() and p.is_dir()), key=lambda p: p.name.casefold())
        if len(entries) > 2000:
            raise DownloadError("子目录超过 2000 个，请先整理目录")
        return {"path": relative, "entries": [{"name": p.name, "path": p.relative_to(self.root.resolve()).as_posix()} for p in entries],
                "free": shutil.disk_usage(path).free}

    @contextlib.contextmanager
    def torrent_directory(self, relative):
        # Anchor every component to an open directory; never follow symlinks.
        if not isinstance(relative, str) or len(relative) > 2048 or "\\" in relative or re.search(r"[\x00-\x1f]", relative):
            raise DownloadError("路径格式无效")
        parts = PurePosixPath(relative)
        if parts.is_absolute() or ".." in parts.parts:
            raise DownloadError("只能选择当前用户文件空间中的种子")
        fd = os.open(str(self.root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts.parts:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd); fd = child
            yield fd
        except OSError as exc:
            raise DownloadError("无法读取目录或种子，不允许符号链接及越界访问") from exc
        finally:
            os.close(fd)

    def browse_torrents(self, relative=""):
        entries = []
        with self.torrent_directory(relative) as fd, os.scandir(fd) as scan:
            for count, entry in enumerate(scan):
                if count >= 20000 or len(entries) >= 2000:
                    raise DownloadError("目录内容过多，请先整理目录")
                if entry.is_symlink(): continue
                is_dir = entry.is_dir(follow_symlinks=False)
                if not is_dir and not (entry.name.lower().endswith('.torrent') and entry.is_file(follow_symlinks=False)):
                    continue
                entries.append({"name": entry.name, "path": str(PurePosixPath(relative) / entry.name),
                                "directory": is_dir, "size": 0 if is_dir else entry.stat(follow_symlinks=False).st_size})
        entries.sort(key=lambda item: (not item['directory'], item['name'].casefold()))
        return {"path": relative, "entries": entries}

    def nas_torrent(self, relative):
        if not isinstance(relative, str) or not relative.lower().endswith('.torrent') or "\\" in relative or re.search(r"[\x00-\x1f]", relative):
            raise DownloadError("请选择 .torrent 文件")
        path = PurePosixPath(relative)
        if path.is_absolute() or '..' in path.parts or len(relative) > 2048:
            raise DownloadError("只能选择当前用户文件空间中的种子")
        with self.torrent_directory(str(path.parent)) as fd:
            file_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(file_fd, 'rb') as stream:
                meta = os.fstat(stream.fileno())
                if not stat.S_ISREG(meta.st_mode) or meta.st_size > MAX_TORRENT:
                    raise DownloadError("种子必须为不超过 4 MiB 的普通文件")
                raw = stream.read(MAX_TORRENT + 1)
        info = torrent_info(raw)
        if len(info['files']) > 2000:
            raise DownloadError("此种子文件超过 2000 个，暂不支持导入")
        return dict(info, torrent=base64.b64encode(raw).decode('ascii'), filename=path.name)

    def rpc(self, method, *params):
        try:
            secret = (self.var / "rpc.secret").read_text().strip()
            port = int((self.var / "rpc.port").read_text())
            body = json.dumps({"jsonrpc": "2.0", "id": "downloadcenter", "method": "aria2." + method,
                               "params": ["token:" + secret, *params]}).encode()
            req = Request("http://127.0.0.1:%d/jsonrpc" % port, body, {"Content-Type": "application/json"})
            try:
                with build_opener(ProxyHandler({})).open(req, timeout=4) as response:
                    result = json.load(response)
            except HTTPError as response:
                result = json.load(response)
            if "error" in result:
                raise DownloadError("下载核心：" + str(result["error"].get("message", "操作失败")))
            return result["result"]
        except DownloadError:
            raise
        except Exception as exc:
            raise DownloadError("下载核心未就绪，请检查服务状态或运行日志") from exc

    def options(self, settings=None):
        s = settings or self.settings()
        return {"max-concurrent-downloads": str(s["concurrent"]), "max-overall-download-limit": str(s["downloadKiB"]) + "K",
                "max-overall-upload-limit": str(s["uploadKiB"]) + "K", "split": str(s["connections"]),
                "max-connection-per-server": str(s["connections"]), "seed-ratio": str(s["seedRatio"]),
                "seed-time": str(s["seedMinutes"]), "bt-tracker": ",".join(s["trackers"])}

    def bt_network_options(self, settings=None):
        s = settings or self.settings()
        boolean = lambda value: "true" if value else "false"
        return {"disable-ipv6": boolean(not s["ipv6"]), "enable-dht": boolean(s["dht"]),
                "enable-dht6": boolean(s["ipv6"] and s["dht6"]), "enable-peer-exchange": boolean(s["pex"]),
                "bt-enable-lpd": boolean(s["lpd"]), "bt-max-peers": str(s["maxPeers"]),
                "bt-request-peer-speed-limit": str(s["peerSpeedKiB"]) + "K"}

    def bt_state(self):
        # Read only our whitelisted startup options; never expose the RPC secret.
        active = self.bt_network_options(dict(DEFAULTS))
        try:
            for line in (self.var / "aria2.conf").read_text().splitlines():
                key, sep, value = line.partition("=")
                if sep and key in active:
                    active[key] = value
        except OSError:
            pass
        try:
            port = int((self.var / "peer.port").read_text())
        except (OSError, ValueError):
            port = None
        return {"peerPort": port, "restartRequired": active != self.bt_network_options(),
                "ipv6Detected": Path("/proc/net/if_inet6").exists()}

    def save_bt_settings(self, data):
        s = self.settings()
        for key, default in BT_DEFAULTS.items():
            value = data.get(key, s[key])
            if isinstance(default, bool):
                if type(value) is not bool:
                    raise DownloadError("开关值无效：" + key)
            elif type(value) is not int or not 1 <= value <= (500 if key == "maxPeers" else 10240):
                raise DownloadError("BT 参数超出范围：" + key)
            s[key] = value
        if s["dht6"] and not s["ipv6"]:
            raise DownloadError("IPv6 DHT 需要先启用 IPv6 连接")
        atomic_json(self.settings_path, s)
        return self.bt_state()

    def rows(self):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT id,gid,created,snapshot FROM tasks ORDER BY created DESC")]

    def row(self, ident):
        if not isinstance(ident, str):
            raise DownloadError("任务 ID 无效")
        with self.db() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise DownloadError("任务不存在")
        return dict(row)

    def unused_name(self, directory, name):
        # Normal collision suffixes, never replace or resume an unrelated existing file.
        if not name or name in (".", "..") or re.search(r"[/\\\x00-\x1f]", name):
            raise DownloadError("资源文件名无效")
        name = name[:180]
        reserved = set()
        for row in self.rows():
            s = json.loads(row["snapshot"])
            reserved.update(s.get("ownedPaths", []))
        for index in range(1000):
            p = Path(name)
            candidate = name if not index else "%s (%d)%s" % (p.stem, index, p.suffix)
            target = directory / candidate
            rel = target.relative_to(self.root.resolve()).as_posix()
            if not os.path.lexists(target) and not os.path.lexists(str(target) + ".aria2") and rel not in reserved:
                return candidate
        raise DownloadError("同名资源过多，请选择其他目录")

    def bt_options(self, info, relative):
        parent = self.directory(relative)
        folder = self.unused_name(parent, info["name"])
        location = str(PurePosixPath(relative) / folder)
        directory = self.directory(location, create=True)
        names = []
        for f in info["files"]:
            path = PurePosixPath(f["path"])
            names.append(PurePosixPath(*path.parts[1:]).as_posix() if info["multi"] else path.name)
        return location, {"dir": str(directory), "index-out": [f["index"] + "=" + name for f, name in zip(info["files"], names)]}, [str(PurePosixPath(location) / name) for name in names]

    def resolve_magnet(self, row, item, db):
        source = json.loads(self.row(row["id"])["source"])
        old = json.loads(row["snapshot"])
        if not old.get("metadataPending"):
            return item, old
        if not item.get("followedBy"):
            return item, old
        child_gid = item["followedBy"][0]
        child = self.rpc("tellStatus", child_gid)
        if child["status"] != "paused":
            raise DownloadError("磁链元数据任务未暂停，已拒绝自动创建下载")
        info_hash = child.get("infoHash", "").lower()
        if not re.fullmatch(r"[0-9a-f]{40}", info_hash):
            raise DownloadError("磁链返回了无效的种子哈希")
        files = [self.var / "metadata" / row["id"] / (info_hash + ".torrent")]
        if not files[0].is_file() or files[0].is_symlink() or files[0].stat().st_size > MAX_TORRENT:
            raise DownloadError("磁链种子元数据尚未就绪或过大")
        raw = files[0].read_bytes(); info = torrent_info(raw)
        location, options, paths = self.bt_options(info, old["directory"])
        options.update(gid=secrets.token_hex(8), pause=source.get("requestedPause", "false"), **{"continue": "false", "allow-overwrite": "false", "auto-file-renaming": "false"})
        self.rpc("remove", child_gid)
        # Same public task ID, new validated download; metadata never writes user data.
        source.update(torrent=base64.b64encode(raw).decode(), options=options)
        old.update(directory=location, ownedPaths=paths, metadataPending=False, name=info["name"], createdDirectory=True, infoHash=info['infoHash'], torrentHash=info['torrentHash'])
        db.execute("UPDATE tasks SET gid=?,source=?,snapshot=? WHERE id=?", (options["gid"], json.dumps(source), json.dumps(old), row["id"]))
        self.rpc("addTorrent", source["torrent"], [], options)
        return self.rpc("tellStatus", options["gid"]), old

    def sync(self, ident=None):
        rows = [r for r in self.rows() if json.loads(r['snapshot']).get('engine') != 'qbittorrent'] if ident is None else [self.row(ident)]
        if ident is None:
            active = self.rpc("tellActive")
            waiting = self.rpc("tellWaiting", 0, 1000)
            stopped = self.rpc("tellStopped", 0, 1000)
            remote = {s["gid"]: s for s in active + waiting + stopped}
        else:
            try:
                item = self.rpc("tellStatus", rows[0]["gid"])
                remote = {item["gid"]: item}
            except DownloadError:
                self.rpc("getVersion")  # Do not mark tasks missing on a dead core.
                remote = {}
        root = self.root.resolve()
        with self.db() as db:
            for row in rows:
                gid = row["gid"]
                item = remote.get(gid)
                old = json.loads(row["snapshot"])
                # Follow metadata tasks into their actual torrent download.
                if item and item.get("followedBy"):
                    if old.get("metadataPending"):
                        try:
                            item, old = self.resolve_magnet(row, item, db)
                        except DownloadError as exc:
                            old.update(status="error", errorMessage=str(exc))
                            db.execute("UPDATE tasks SET snapshot=? WHERE id=?", (json.dumps(old), row["id"]))
                            continue
                        gid = item["gid"]
                    else:
                        gid = item["followedBy"][0]
                        item = remote.get(gid) or self.rpc("tellStatus", gid)
                if item:
                    name = item.get("bittorrent", {}).get("info", {}).get("name")
                    if not name:
                        name = next((Path(f["path"]).name for f in item.get("files", []) if f.get("path")), old["name"])
                    files = [{k: f.get(k) for k in ("index", "path", "length", "completedLength", "selected")} for f in item.get("files", [])]
                    allowed = set(old.get("ownedPaths", []))
                    candidates = {str(self.root / path) for path in allowed} | {str(self.root / path) + ".aria2" for path in allowed}
                    bt_name = item.get("bittorrent", {}).get("info", {}).get("name", "")
                    if bt_name and Path(bt_name).name == bt_name and not old.get("metadataPending"):
                        candidates.add(str(self.root / old["directory"] / (bt_name + ".aria2")))
                    registered = {r[0] for r in db.execute("SELECT path FROM task_files WHERE task=?", (row["id"],))}
                    for filename in candidates:
                        try:
                            target = Path(filename)
                            rel = target.relative_to(root).as_posix()
                            if rel in registered:
                                continue  # Ownership by recorded path; no repeated HDD stat.
                            stat = target.lstat()
                            import stat as statmod
                            if statmod.S_ISREG(stat.st_mode):
                                db.execute("INSERT OR IGNORE INTO task_files VALUES (?,?,?,?)", (row["id"], rel, stat.st_dev, stat.st_ino))
                        except (ValueError, OSError):
                            pass
                    # Large torrents remain bounded on the client; detail fetch provides pagination.
                    snap = dict(old)
                    snap.update({k: item.get(k, "0") for k in ("status", "totalLength", "completedLength", "downloadSpeed", "uploadSpeed", "uploadLength", "connections", "numSeeders", "errorCode")})
                    snap.update({"name": name, "errorMessage": item.get("errorMessage", ""), "directory": old["directory"],
                                 "isBT": bool(item.get("bittorrent") or old.get("isBT")), "fileCount": len(files)})
                    if snap != old or gid != row["gid"]:
                        db.execute("UPDATE tasks SET gid=?,snapshot=? WHERE id=?", (gid, json.dumps(snap), row["id"]))
                elif json.loads(row["snapshot"]).get("status") in LIVE:
                    snap = json.loads(row["snapshot"])
                    snap.update(status="error", errorMessage="核心中未找到任务，可能上次异常断电；可重试恢复", downloadSpeed="0", uploadSpeed="0")
                    db.execute("UPDATE tasks SET snapshot=? WHERE id=?", (json.dumps(snap), row["id"]))

    def snapshot(self):
        running, error, stats, version = False, "", {}, ""
        try:
            # Supervisor owns full synchronization. UI reads must not rescan
            # every torrent/file while holding the same lock as task actions.
            stats = self.rpc("getGlobalStat")
            version = self.rpc("getVersion")["version"]
            running = True
        except DownloadError as exc:
            error = str(exc)
        tasks = []
        for row in self.rows():
            s = json.loads(row["snapshot"])
            if not running:
                s.update(downloadSpeed="0", uploadSpeed="0")
            tasks.append(dict(s, id=row["id"], created=row["created"]))
        try:
            tracker_status = json.loads((self.var / "tracker-status.json").read_text())
        except (OSError, ValueError):
            tracker_status = {}
        try:
            job = json.loads((self.var / "job.json").read_text())
        except (OSError, ValueError):
            job = {}
        return {"running": running, "error": error, "stats": stats, "tasks": tasks, "version": VERSION,
                "engineVersion": version, "settings": self.settings(), "bt": self.bt_state(), "trackerStatus": tracker_status,
                "coreUpdate": self.core_state(),
                "free": shutil.disk_usage(self.root).free, "enabled": (self.var / "enabled").exists(),
                "job": {k: job.get(k) for k in ("state", "count", "failed", "total", "id", "command")}}

    def core_state(self):
        from core_update import state
        return state(self)

    def save_session(self):
        self.rpc("saveSession")

    def add(self, data):
        if len(self.rows()) >= 1000:
            raise DownloadError("最多保留 1000 条任务，请清理历史记录")
        with self.db() as db:
            saved_size = db.execute("SELECT COALESCE(SUM(LENGTH(source)),0) FROM tasks").fetchone()[0]
        if saved_size + len(data.get("torrent", "")) > 64 * 1024 * 1024:
            raise DownloadError("种子及链接记录达到 64 MiB 限额，请清理不再需要的历史记录")
        relative = data.get("directory", self.settings()["directory"])
        directory = self.directory(relative)
        opts = {"dir": str(directory), "pause": "true" if data.get("paused") else "false", "continue": "false", "auto-file-renaming": "false", "allow-overwrite": "false"}
        # No arbitrary options/headers are accepted: prevents overriding paths/RPC/hooks.
        uri, raw, info = data.get("url", ""), None, None
        if data.get("torrent"):
            try:
                raw = base64.b64decode(data["torrent"], validate=True)
            except (ValueError, TypeError) as exc:
                raise DownloadError("种子文件编码错误") from exc
            info = torrent_info(raw)
            for f in info["files"]:
                target = directory
                for part in PurePosixPath(f["path"]).parts:
                    target = target / part
                    if target.is_symlink():
                        raise DownloadError("种子目标路径存在符号链接，已拒绝下载")
            if data.get("selected") is not None:
                selected = self.selection(data["selected"], len(info["files"]))
                opts["select-file"] = selected
        else:
            valid_url(uri)
        name = data.get("name", "")
        if name:
            if not isinstance(name, str) or len(name) > 200 or name in (".", "..") or re.search(r"[/\\\x00-\x1f]", name):
                raise DownloadError("文件名不能包含路径或控制字符")
            if raw or uri.startswith("magnet:"):
                raise DownloadError("BT 任务不支持在创建时重命名")
            if (directory / name).is_symlink():
                raise DownloadError("目标文件是符号链接")
            opts["out"] = name
        gid = secrets.token_hex(8)
        opts["gid"] = gid
        # Record first, so a failed RPC/response can never produce an unowned invisible task.
        ident = secrets.token_hex(12)
        task_relative, paths = relative, []
        is_magnet = uri.startswith("magnet:")
        if raw:
            task_relative, bt_opts, paths = self.bt_options(info, relative)
            opts.update(bt_opts)
        elif is_magnet:
            metadata = self.var / "metadata" / ident
            metadata.mkdir(parents=True, mode=0o700)
            opts.update({"dir": str(metadata), "bt-save-metadata": "true", "pause-metadata": "true"})
        else:
            suggested = name or unquote(urlsplit(uri).path.rsplit("/", 1)[-1]) or "download"
            filename = self.unused_name(directory, suggested)
            opts["out"] = filename
            paths = [str(PurePosixPath(relative) / filename)]
        source = {"url": uri, "torrent": base64.b64encode(raw).decode() if raw else "", "options": opts, "requestedPause": opts["pause"]}
        snap = {"status": "waiting", "name": info["name"] if info else (name or uri), "directory": task_relative,
                "totalLength": str(info["total"]) if info else "0", "completedLength": "0", "isBT": bool(raw or is_magnet),
                "ownedPaths": paths, "metadataPending": is_magnet, "createdDirectory": bool(raw),
                "infoHash": info['infoHash'] if info else '', "torrentHash": info['torrentHash'] if info else ''}
        with self.db() as db:
            db.execute("INSERT INTO tasks VALUES (?,?,?,?,?)", (ident, gid, time.time(), json.dumps(source), json.dumps(snap)))
        try:
            if raw:
                self.rpc("addTorrent", source["torrent"], [], opts)
            else:
                self.rpc("addUri", [uri], opts)
        except DownloadError as exc:
            snap.update(status="error", errorMessage=str(exc))
            with self.db() as db:
                db.execute("UPDATE tasks SET snapshot=? WHERE id=?", (json.dumps(snap), ident))
            raise
        self.save_session()
        settings = self.settings()
        settings["recentDirectories"] = list(dict.fromkeys([relative] + settings.get("recentDirectories", [])))[:8]
        atomic_json(self.settings_path, settings)
        return {"id": ident}

    @staticmethod
    def selection(value, count):
        if not isinstance(value, list) or not value or len(value) > 10000:
            raise DownloadError("至少选择一个文件")
        try:
            values = sorted({int(x) for x in value})
        except (TypeError, ValueError) as exc:
            raise DownloadError("文件选择无效") from exc
        if values[0] < 1 or values[-1] > count:
            raise DownloadError("文件编号超出范围")
        return ",".join(str(x) for x in values)

    def operate(self, ident, command, delete_files=False):
        row = self.row(ident)
        state = json.loads(row["snapshot"])["status"]
        gid = row["gid"]
        if command == "pause":
            self.rpc("pause", gid)
        elif command == "resume":
            self.directory(json.loads(row["snapshot"])["directory"])
            self.rpc("unpause", gid)
        elif command == "top":
            self.rpc("changePosition", gid, 0, "POS_SET")
        elif command == "retry":
            if state not in ("error", "removed"):
                raise DownloadError("仅失败或已取消的任务可以重试")
            source = json.loads(row["source"])
            relative = json.loads(row["snapshot"])["directory"]
            self.directory(relative)
            snap = json.loads(row["snapshot"])
            for rel in snap.get("ownedPaths", []):
                current = self.root
                for part in PurePosixPath(rel).parts:
                    current = current / part
                    if current.is_symlink():
                        raise DownloadError("任务文件路径包含符号链接，不能重试")
            opts = dict(source["options"], gid=secrets.token_hex(8), pause="false")
            if source["torrent"]:
                torrent_info(base64.b64decode(source["torrent"]))
            else:
                valid_url(source["url"])
            with self.db() as db:
                db.execute("UPDATE tasks SET gid=? WHERE id=?", (opts["gid"], ident))
            if source["torrent"]:
                self.rpc("addTorrent", source["torrent"], [], opts)
            else:
                self.rpc("addUri", [source["url"]], opts)
            self.save_session()
            return {"id": ident}
        elif command == "remove":
            if state in LIVE:
                self.rpc("remove", gid)
                self.save_session()
                # Removing is asynchronous in aria2. Do not unlink until writers have stopped.
                for _ in range(10):
                    try:
                        current = self.rpc("tellStatus", gid)
                    except DownloadError:
                        remaining = self.rpc("tellActive") + self.rpc("tellWaiting", 0, 1000)
                        if not any(task["gid"] == gid for task in remaining):
                            break
                        raise
                    if current["status"] not in LIVE:
                        break
                    time.sleep(.15)
                else:
                    raise DownloadError("任务仍在停止，请稍后重试删除")
                self.sync(ident)
            else:
                try:
                    self.rpc("removeDownloadResult", gid)
                except DownloadError:
                    pass
            failed_files = []
            try:
                self.delete_generated_torrent(row)
            except (DownloadError, OSError, ValueError) as exc:
                failed_files.append('下载目录内的自动种子副本：' + str(exc))
            if delete_files:
                try:
                    failed_files.extend(self.delete_files(row))
                except (DownloadError, OSError, ValueError) as exc:
                    failed_files.append('任务文件清理未完成：' + str(exc))
            # Generated magnet metadata is private task state, not the user's
            # original imported .torrent. Remove it even when keeping downloads.
            try:
                self.delete_metadata(ident)
            except (DownloadError, OSError, ValueError) as exc:
                failed_files.append('自动生成的种子缓存：' + str(exc))
            with self.db() as db:
                db.execute("DELETE FROM task_files WHERE task=?", (ident,))
                db.execute("DELETE FROM tasks WHERE id=?", (ident,))
            warning = ''
            if failed_files:
                warning = '任务记录已删除；部分文件未能删除，请手动检查：' + '，'.join(failed_files[:3])
            try:
                self.save_session()
            except DownloadError:
                warning += '；核心会话保存失败，请检查服务状态'
            return {'removed': True, 'failedFiles': failed_files, 'warning': warning}
        else:
            raise DownloadError("不支持的任务操作")
        if command in ("pause", "resume") and json.loads(row["snapshot"]).get("metadataPending"):
            source = json.loads(row["source"])
            source["requestedPause"] = "true" if command == "pause" else "false"
            with self.db() as db:
                db.execute("UPDATE tasks SET source=? WHERE id=?", (json.dumps(source), ident))
        self.save_session()
        return {}

    def delete_files(self, row):
        """Recorded task files by name; remove created directories only if empty."""
        import stat as statmod
        snap = json.loads(row['snapshot'])
        relative = snap['directory']
        with self.db() as db:
            entries = list(db.execute("SELECT * FROM task_files WHERE task=?", (row["id"],)))
        failed = []
        for record in entries:
            parts = PurePosixPath(record["path"])
            try:
                parts.relative_to(PurePosixPath(relative))
                if parts.is_absolute() or ".." in parts.parts:
                    raise ValueError()
                fd = os.open(str(self.root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    for part in parts.parts[:-1]:
                        next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                        os.close(fd); fd = next_fd
                    st = os.stat(parts.name, dir_fd=fd, follow_symlinks=False)
                    if not statmod.S_ISREG(st.st_mode):
                        raise ValueError()
                    os.unlink(parts.name, dir_fd=fd)
                finally:
                    os.close(fd)
            except FileNotFoundError:
                pass
            except (ValueError, OSError):
                failed.append(record["path"])
        # Remove only empty known subdirectories; user-added content is untouched.
        if json.loads(row["snapshot"]).get("createdDirectory"):
            folders = set()
            base = PurePosixPath(relative)
            if relative and not base.is_absolute() and '..' not in base.parts:
                folders.add(base)
                for record in entries:
                    parent = PurePosixPath(record['path']).parent
                    while parent != base and base in parent.parents:
                        folders.add(parent); parent = parent.parent
                for folder in sorted(folders, key=lambda item: len(item.parts), reverse=True):
                    try:
                        with self.torrent_directory(str(folder.parent)) as fd:
                            os.rmdir(folder.name, dir_fd=fd)
                    except (DownloadError, OSError):
                        pass
        return failed

    def delete_generated_torrent(self, row):
        """aria2 RPC saves an uploaded seed in the task directory for restart.

        RPC upload uses SHA1 of the entire torrent; magnet metadata uses infoHash.
        Remove only these task-specific copies, not the imported seed.
        Old task snapshots can recover the hash from their stored torrent bytes.
        """
        snap = json.loads(row['snapshot'])
        if not snap.get('isBT') or not snap.get('createdDirectory') or snap.get('metadataPending'):
            return
        relative = snap.get('directory', '')
        if not relative or not PurePosixPath(relative).parts:
            raise DownloadError('拒绝清理用户根目录中的种子')
        hashes = {h.lower() for h in (snap.get('infoHash', ''), snap.get('torrentHash', ''))
                  if isinstance(h, str) and re.fullmatch(r'[a-fA-F0-9]{40}', h)}
        if not snap.get('torrentHash'):
            raw = json.loads(row.get('source', '{}')).get('torrent', '')
            if raw:
                info = torrent_info(base64.b64decode(raw, validate=True))
                hashes.update((info['infoHash'], info['torrentHash']))
        if not hashes: return
        try:
            with self.torrent_directory(relative) as fd:
                for name in (h + suffix for h in hashes for suffix in ('.torrent', '.torrent.aria2', '.torrent.tmp')):
                    try:
                        meta = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        if not stat.S_ISREG(meta.st_mode):
                            raise DownloadError('种子副本不是普通文件，已保留')
                        os.unlink(name, dir_fd=fd)
                    except FileNotFoundError:
                        pass
        except DownloadError as exc:
            if isinstance(exc.__cause__, FileNotFoundError): return
            raise

    def delete_metadata(self, ident):
        result = {'files': 0, 'bytes': 0, 'directories': 0, 'retained': 0}
        if not isinstance(ident, str) or not re.fullmatch(r'[a-f0-9]{24}', ident):
            raise DownloadError('种子缓存任务编号无效')
        # Anchor at plugin var; neither metadata nor the task child may be a link.
        root_fd = os.open(str(self.var), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            try: fd = os.open('metadata', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            except FileNotFoundError: return result
            try:
                try: task_fd = os.open(ident, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except FileNotFoundError: return result
                try:
                    # aria2's generated magnet seed names are info-hash based.
                    # Never sweep arbitrary .torrent files or user-added content.
                    with os.scandir(task_fd) as entries:
                        for entry in entries:
                            if re.fullmatch(r'[a-fA-F0-9]{40}\.torrent(?:\.aria2|\.tmp)?', entry.name) and entry.is_file(follow_symlinks=False):
                                try:
                                    size = entry.stat(follow_symlinks=False).st_size
                                    os.unlink(entry.name, dir_fd=task_fd)
                                    result['files'] += 1; result['bytes'] += size
                                except FileNotFoundError: pass
                            else: result['retained'] += 1
                    try:
                        os.rmdir(ident, dir_fd=fd)
                        result['directories'] += 1
                    except OSError as exc:
                        if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT): raise
                finally: os.close(task_fd)
            finally: os.close(fd)
        finally:
            os.close(root_fd)
        return result

    def clean_cache(self):
        """Only orphan private metadata; never scan user download directories."""
        # Called under api.lock. Keep ALL recorded tasks, including paused,
        # failed and completed ones: they may still need metadata for retry.
        protected = {row['id'] for row in self.rows()}
        # Also protect engine jobs created before a failed database insertion.
        # If RPC is unavailable, fail closed rather than guessing task liveness.
        active = self.rpc('tellActive')
        waiting = self.rpc('tellWaiting', 0, 10000)
        if len(waiting) >= 10000:
            raise DownloadError('核心任务过多，无法安全确认缓存占用，请稍后再试')
        for job in active + waiting:
            paths = [job.get('dir', '')] + [f.get('path', '') for f in job.get('files', [])]
            for path in paths:
                try:
                    parts = Path(path).relative_to(self.var / 'metadata').parts
                    if parts: protected.add(parts[0])
                except ValueError: pass
        result = {'files': 0, 'bytes': 0, 'directories': 0, 'retained': 0,
                  'protected': 0, 'failed': [], 'partial': False}
        root_fd = os.open(str(self.var), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            try: fd = os.open('metadata', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            except FileNotFoundError: return result
            try:
                started = time.monotonic()
                with os.scandir(fd) as entries:
                    for count, entry in enumerate(entries):
                        if count >= 2000 or time.monotonic() - started > 5:
                            result['partial'] = True; break
                        if entry.name in protected:
                            result['protected'] += 1; continue
                        if not re.fullmatch(r'[a-f0-9]{24}', entry.name) or not entry.is_dir(follow_symlinks=False):
                            result['retained'] += 1; continue
                        try:
                            cleaned = self.delete_metadata(entry.name)
                            for key in ('files', 'bytes', 'directories', 'retained'):
                                result[key] += cleaned[key]
                        except (OSError, DownloadError) as exc:
                            result['failed'].append(entry.name + '：' + str(exc))
            finally: os.close(fd)
        finally: os.close(root_fd)
        return result

    def detail(self, ident, offset=0):
        row = self.row(ident)
        try:
            item = self.rpc("tellStatus", row["gid"])
        except DownloadError:
            old = json.loads(row["snapshot"])
            if old.get("status") in LIVE:
                raise
            return dict(old, id=ident, files=[], offset=0, fileCount=0, trackers=[],
                        errorMessage=old.get("errorMessage", "") + "（历史任务已退出核心，文件列表不再可选）")
        files = item.get("files", [])
        offset = max(0, int(offset))
        snapshot = json.loads(row['snapshot'])
        display_files = []
        base = self.root / snapshot['directory']
        for file in files[offset:offset + 200]:
            entry = {k: file.get(k) for k in ('index', 'path', 'length', 'completedLength', 'selected')}
            try: entry['displayPath'] = Path(file['path']).relative_to(base).as_posix()
            except (KeyError, ValueError): entry['displayPath'] = Path(file.get('path', '')).name
            display_files.append(entry)
        result = dict(snapshot, id=ident, files=display_files,
                      offset=offset, fileCount=len(files), trackers=item.get("bittorrent", {}).get("announceList", []))
        return result

    def select_files(self, data):
        row = self.row(data.get("id"))
        item = self.rpc("tellStatus", row["gid"])
        if item["status"] != "paused":
            raise DownloadError("请先暂停任务，再选择文件")
        value = self.selection(data.get("selected"), len(item.get("files", [])))
        self.rpc("changeOption", row["gid"], {"select-file": value})
        source = json.loads(row["source"]); source["options"]["select-file"] = value
        with self.db() as db:
            db.execute("UPDATE tasks SET source=? WHERE id=?", (json.dumps(source), row["id"]))
        self.save_session()

    def save_settings(self, data):
        s = self.settings()
        for key, low, high in (("concurrent", 1, 10), ("connections", 1, 16), ("downloadKiB", 0, 1048576), ("uploadKiB", 0, 1048576), ("seedMinutes", 0, 10080)):
            v = data.get(key, s[key])
            if type(v) is not int or not low <= v <= high:
                raise DownloadError("设置数值超出范围：" + key)
            s[key] = v
        ratio = data.get("seedRatio", s["seedRatio"])
        if not isinstance(ratio, (int, float)) or not 0 <= ratio <= 100:
            raise DownloadError("分享率应在 0–100 之间")
        s["seedRatio"] = ratio
        s["directory"] = data.get("directory", s["directory"])
        self.directory(s["directory"])
        atomic_json(self.settings_path, s)
        try:
            self.rpc("changeGlobalOption", self.options(s))
            return {"applied": True}
        except DownloadError:
            return {"applied": False}

    def save_trackers(self, data):
        s = self.settings()
        manual = tracker_list(data.get("trackers", []))
        sources = data.get("sources", [])
        if not isinstance(sources, list) or len(sources) > 3:
            raise DownloadError("最多添加 3 个 Tracker 订阅地址")
        for url in sources:
            valid_url(url)
            if urlsplit(url).scheme not in ("http", "https"):
                raise DownloadError("订阅地址仅支持 HTTP/HTTPS")
        sources = list(dict.fromkeys(sources))
        cache, legacy = tracker_cache(s)
        cache = {url: value for url, value in cache.items() if url in sources}
        if set(s['sources']) != set(sources) or not sources:
            legacy = []
        s['trackerSourceCache'] = cache
        s['legacySubscribedTrackers'] = legacy
        s['subscribedTrackers'], _ = merged_trackers([], cache, sources, legacy)
        s["manualTrackers"] = manual
        s["trackers"], _ = merged_trackers(manual, cache, sources, legacy)
        s["sources"] = sources
        s["autoTrackers"] = data.get("autoTrackers") is True
        atomic_json(self.settings_path, s)
        return self.apply_trackers()

    def apply_trackers(self):
        trackers = ",".join(self.settings()["trackers"])
        failures, applied = [], 0
        try:
            self.rpc("changeGlobalOption", {"bt-tracker": trackers})
        except DownloadError:
            return {"applied": 0, "deferred": True, "failed": []}
        for row in self.rows():
            s = json.loads(row["snapshot"])
            if s.get("isBT") and s.get("status") in LIVE and s.get('engine') != 'qbittorrent':
                try:
                    self.rpc("changeOption", row["gid"], {"bt-tracker": trackers})
                    applied += 1
                except DownloadError:
                    failures.append(s["name"])
        return {"applied": applied, "failed": failures, "deferred": False}

    def queue_job(self, command):
        self.rpc("getVersion")
        try:
            previous = json.loads((self.var / "job.json").read_text())
        except (OSError, ValueError):
            previous = {}
        if previous.get("state") == "pending":
            raise DownloadError("已有批量任务执行中，请稍后再试")
        self.sync()
        ids = []
        for row in self.rows():
            state = json.loads(row["snapshot"])["status"]
            if ((command == "pause" and state in ("active", "waiting")) or
                (command == "resume" and state == "paused") or
                (command == "clear" and state not in LIVE)):
                ids.append(row["id"])
        job = {"id": secrets.token_hex(8), "state": "pending", "command": command,
               "remaining": ids, "total": len(ids), "count": 0, "failed": []}
        atomic_json(self.var / "job.json", job)
        return {"pending": True, "id": job["id"]}

    def process_job(self):
        for _ in range(10):
            with self.locked():
                try:
                    job = json.loads((self.var / "job.json").read_text())
                except (OSError, ValueError):
                    return
                if job.get("state") != "pending":
                    return
                if not job["remaining"]:
                    job["state"] = "done"
                else:
                    ident = job["remaining"].pop(0)
                    try:
                        self.operate(ident, "remove" if job["command"] == "clear" else job["command"])
                        job["count"] += 1
                    except DownloadError as exc:
                        job["failed"].append(str(exc))
                atomic_json(self.var / "job.json", job)

    def dispatch(self, action, data):
        with self.locked():
            if (self.core_state().get('state') == 'installing' or getattr(self, 'qb_update_state', lambda: {})().get('state') == 'installing') and action not in ('status', 'browse', 'torrent_browse', 'torrent_preview', 'torrent_nas_preview', 'detail'):
                raise DownloadError('正在切换下载核心，请稍后再操作')
            if action in ('core_check', 'core_apply'):
                from core_update import queue
                return queue(self, 'check' if action == 'core_check' else 'apply', data.get('proxy') is True)
            if action == "status":
                return self.snapshot()
            if action == "browse":
                return self.browse(data.get("path", ""))
            if action == "torrent_browse":
                return self.browse_torrents(data.get("path", ""))
            if action == "torrent_nas_preview":
                return self.nas_torrent(data.get("path"))
            if action == "mkdir":
                parent = data.get("path", ""); name = data.get("name", "")
                if not isinstance(name, str) or name in ("", ".", "..") or "/" in name or "\\" in name or len(name) > 150:
                    raise DownloadError("目录名称无效")
                self.directory(parent)
                rel = str(PurePosixPath(parent) / name)
                self.directory(rel, create=True)
                return {"path": rel}
            if action == "torrent_preview":
                try:
                    raw = base64.b64decode(data.get("torrent", ""), validate=True)
                except (ValueError, TypeError) as exc:
                    raise DownloadError("种子编码无效") from exc
                info = torrent_info(raw)
                if len(info["files"]) > 2000:
                    raise DownloadError("此种子文件超过 2000 个，第一版暂不支持导入")
                return info
            if action == "add":
                return self.add(data)
            if action == "task":
                ident = data.get("id")
                self.row(ident)  # Validate before choosing the targeted sync path.
                if data.get("command") not in ("pause", "resume", "top", "retry", "remove"):
                    raise DownloadError("不支持的任务操作")
                self.sync(ident)
                result = self.operate(ident, data.get("command"), data.get("deleteFiles") is True)
                if data.get("command") != "remove":
                    self.sync(ident)
                return result
            if action == "batch":
                if data.get("command") not in ("pause", "resume"):
                    raise DownloadError("无效批量操作")
                return self.queue_job(data["command"])
            if action == "detail":
                return self.detail(data.get("id"), data.get("offset", 0))
            if action == "select_files":
                self.select_files(data); return {}
            if action == "clear_history":
                return self.queue_job("clear")
            if action == "cache_clean":
                return self.clean_cache()
            if action == "settings":
                return self.save_settings(data)
            if action == "bt_settings":
                return self.save_bt_settings(data)
            if action == "trackers":
                return self.save_trackers(data)
            if action == "tracker_update":
                if not self.settings()["sources"]:
                    raise DownloadError("请先保存 Tracker 订阅地址")
                self.rpc("getVersion")
                atomic_json(self.var / "tracker-status.json", {"state": "pending", "time": time.time(), "message": "正在后台更新"})
                return {}
            raise DownloadError("未知操作")
