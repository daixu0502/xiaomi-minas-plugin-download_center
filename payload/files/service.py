#!/usr/bin/env python3
"""Unprivileged supervisor: graceful shutdown, session persistence and tracker refresh."""
import fcntl
import json
import os
import signal
import socket
import ssl
import subprocess
import sys
import time
import threading
from pathlib import Path
from urllib.request import build_opener, ProxyHandler, Request
from urllib.error import HTTPError, URLError
from download_lib import Manager, DownloadError, atomic_json, parse_tracker_subscription, tracker_cache, merged_trackers, core_path
from dual_engine import DualManager


def manager(home):
    home = Path(home)
    # Derive data root from installer-validated src; never from a web request.
    src = (home / "src").resolve(strict=True)
    return DualManager(home, src.parent.parent.parent / "data")


def live(m):
    try:
        pid = int((m.var / "service.pid").read_text())
        cmd = Path("/proc/%d/cmdline" % pid).read_bytes().split(b"\0")
        return pid if b"serve" in cmd and str(m.home).encode() in cmd and any(x.endswith(b"/service.py") for x in cmd) else 0
    except (OSError, ValueError):
        return 0


def tracker_error(exc):
    if isinstance(exc, HTTPError):
        reason = {403: "服务器拒绝访问", 404: "订阅地址不存在", 429: "请求过于频繁",
                  500: "订阅服务器内部错误", 502: "上游服务异常", 503: "订阅服务不可用",
                  522: "订阅站点回源连接超时", 524: "订阅站点回源响应超时"}.get(exc.code, "订阅服务器返回错误")
        return "HTTP %d：%s" % (exc.code, reason)
    reason = exc.reason if isinstance(exc, URLError) else exc
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "连接或读取超时（8 秒），请检查订阅站点与网络"
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "HTTPS 证书验证失败，请检查设备时间或订阅站点证书"
    if isinstance(reason, ssl.SSLError):
        return "HTTPS 握手失败"
    if isinstance(reason, socket.gaierror):
        return "DNS 解析失败"
    if isinstance(exc, DownloadError):
        return str(exc)
    if isinstance(reason, OSError):
        return "网络连接失败（错误码 %s）" % reason.errno
    return "订阅处理失败（%s）" % type(exc).__name__


def update_trackers(m):
    # Prevent duplicate workers without blocking normal API calls.
    with (m.var / "tracker-update.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        _update_trackers(m)


def _update_trackers(m):
    with m.locked():
        original = m.settings()
        atomic_json(m.var / "tracker-status.json", {"state": "updating", "time": time.time(), "message": "正在分别更新订阅"})
    cache, legacy = tracker_cache(original)
    results, successes = [], 0
    for i, url in enumerate(original["sources"], 1):
        row = {"index": i, "url": url}
        try:
            request = Request(url, headers={"User-Agent": "MinasDownloadCenter/1.0", "Accept": "text/plain"})
            with build_opener(ProxyHandler({})).open(request, timeout=8) as response:
                if response.geturl().split(":", 1)[0] not in ("http", "https"):
                    raise DownloadError("不允许的跳转协议")
                parsed = parse_tracker_subscription(response.read(256 * 1024 + 1))
            cache[url] = {"trackers": parsed["trackers"], "updatedAt": time.time()}
            successes += 1
            notes = ["已更新 %d 个 Tracker" % len(parsed["trackers"])]
            for scheme, count in sorted(parsed["unsupported"].items()):
                notes.append("跳过 %d 条不支持的 %s 地址" % (count, scheme.upper()))
            if parsed["invalid"]: notes.append("跳过 %d 条格式错误的地址" % parsed["invalid"])
            if parsed["omitted"]: notes.append("此源超过 500 个，省略 %d 个" % parsed["omitted"])
            row.update(state="done", count=len(parsed["trackers"]), message="；".join(notes),
                       unsupported=parsed["unsupported"], invalid=parsed["invalid"])
        except Exception as exc:
            old_count = len(cache.get(url, {}).get("trackers", []))
            row.update(state="error", count=old_count, message=tracker_error(exc) + (
                "；保留该源上次成功的 %d 个 Tracker" % old_count if old_count else "；该源暂无可用的历史列表"))
        results.append(row)
    with m.locked():
        current = m.settings()
        keys = ("sources", "manualTrackers", "trackers", "trackerSourceCache", "legacySubscribedTrackers")
        if any(current.get(key) != original.get(key) for key in keys):
            result = {"state": "error", "message": "更新期间 Tracker 设置已改变，请重新更新；未覆盖新设置"}
        else:
            if successes == len(original["sources"]): legacy = []
            manual = original.get("manualTrackers", original["trackers"])
            current["trackerSourceCache"] = cache
            current["legacySubscribedTrackers"] = legacy
            current["subscribedTrackers"], _ = merged_trackers([], cache, original["sources"], legacy)
            current["trackers"], omitted = merged_trackers(manual, cache, original["sources"], legacy)
            atomic_json(m.settings_path, current)
            failed = len(results) - successes
            state = "partial" if successes and failed else "error" if failed else "done"
            message = "订阅成功 %d 个，失败 %d 个；当前共 %d 个 Tracker" % (successes, failed, len(current["trackers"]))
            if legacy: message += "；暂保留旧版未区分来源的列表，全部源成功后自动替换"
            if omitted: message += "；合并超过 500 个，已优先保留手动列表并省略 %d 个" % omitted
            try:
                applied = m.apply_trackers()
                if applied["deferred"]: message += "；核心未运行，已保存并在下次启动时应用"
                elif applied["failed"]: message += "；%d 个任务应用失败，可稍后重新保存应用" % len(applied["failed"])
            except DownloadError as exc:
                message += "；列表已保存，但应用到核心失败：" + str(exc)
                state = "partial" if successes else "error"
            result = {"state": state, "message": message, "sources": results}
        result["time"] = time.time()
        atomic_json(m.var / "tracker-status.json", result)


def serve(m):
    m.ready()
    lock = (m.var / "supervisor.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    # Refuse root even if launched outside the lifecycle script.
    if os.geteuid() == 0:
        raise DownloadError("下载核心禁止以 root 运行")
    (m.var / "service.pid").write_text(str(os.getpid()))
    stopping = [False]
    signal.signal(signal.SIGTERM, lambda *_: stopping.__setitem__(0, True))
    signal.signal(signal.SIGINT, lambda *_: stopping.__setitem__(0, True))
    session = m.var / "aria2.session"
    session.touch(mode=0o600, exist_ok=True)
    settings = m.settings()
    directory = m.directory(settings["directory"])
    conf = {"enable-rpc": "true", "rpc-listen-all": "false", "rpc-allow-origin-all": "false",
            "rpc-listen-port": (m.var / "rpc.port").read_text().strip(), "rpc-secret": (m.var / "rpc.secret").read_text().strip(),
            "listen-port": (m.var / "aria-peer.port").read_text().strip(), "dht-listen-port": (m.var / "aria-peer.port").read_text().strip(),
            "dir": str(directory), "input-file": str(session), "save-session": str(session), "save-session-interval": "15",
            "save-not-found": "true", "force-save": "true", "continue": "true", "auto-file-renaming": "true",
            "allow-overwrite": "false", "file-allocation": "none", "check-certificate": "true", "max-tries": "5", "retry-wait": "5",
            "connect-timeout": "15", "timeout": "60", "max-download-result": "1000", "follow-torrent": "false", "follow-metalink": "false",
            "bt-save-metadata": "false", "dht-file-path6": str(m.var / "dht6.dat"),
            "dht-file-path": str(m.var / "dht.dat"), "disk-cache": "16M", "console-log-level": "warn", "summary-interval": "0",
            "log-level": "warn", "log": str(m.var / "aria2.log"), **m.options(settings), **m.bt_network_options(settings)}
    path = m.var / "aria2.conf"
    path.write_text("\n".join(k + "=" + v for k, v in conf.items()) + "\n")
    os.chmod(path, 0o600)
    child, qb_child, gateway = None, None, None
    try:
        child = subprocess.Popen([str(core_path(m)), "--conf-path=" + str(path)], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        profile = m.qb.prepare()
        qb_log = m.var / 'qbittorrent.log'
        if qb_log.exists() and qb_log.stat().st_size > 2 * 1024 * 1024:
            os.replace(qb_log, m.var / 'qbittorrent.log.1')
        with qb_log.open('a') as output:
            from qb_update import core_path as qb_core_path
            qb_child = subprocess.Popen([str(qb_core_path(m)), '--profile=' + str(profile),
                                         '--confirm-legal-notice'], stdin=subprocess.DEVNULL, stdout=output, stderr=output)
        for _ in range(20):
            if child.poll() is not None or qb_child.poll() is not None:
                raise DownloadError("下载核心启动失败，请查看 aria2.log / qbittorrent.log")
            try:
                m.rpc("getVersion"); m.qb.request('app/version'); break
            except DownloadError:
                time.sleep(.3)
        else:
            raise DownloadError('下载核心启动超时')
        m.qb.apply_preferences()
        m.qb.mark_applied()
        from openlist_gateway import start_gateway
        gateway = start_gateway(m)
        last_save, last_auto, tracker_worker = 0, time.time(), None
        while not stopping[0] and child.poll() is None and qb_child.poll() is None:
            m.ready()  # Pool lost: stop instead of writing into an empty mountpoint.
            with m.locked():
                m.sync()
                if time.time() - last_save > 15:
                    m.save_session(); last_save = time.time()
                try:
                    status = json.loads((m.var / "tracker-status.json").read_text())
                except (OSError, ValueError):
                    status = {}
                s = m.settings()
                update = status.get("state") == "pending" or (s["autoTrackers"] and s["sources"] and time.time() - max(status.get("time", 0), last_auto) > 86400)
            if update and not (tracker_worker and tracker_worker.is_alive()):
                tracker_worker = threading.Thread(target=update_trackers, args=(manager(m.home),), daemon=True)
                tracker_worker.start(); last_auto = time.time()
            if not stopping[0]:
                m.process_job()
            for _ in range(10):
                if stopping[0]:
                    break
                time.sleep(.3)
    finally:
        if gateway:
            gateway.shutdown(); gateway.server_close()
        if qb_child and qb_child.poll() is None:
            qb_child.terminate()
            try:
                qb_child.wait(timeout=25)
            except subprocess.TimeoutExpired:
                qb_child.kill(); qb_child.wait()
        if child and child.poll() is None:
            try:
                m.save_session()
            except DownloadError:
                pass
            child.terminate()
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait()
        (m.var / "service.pid").unlink(missing_ok=True)


def control(m, command):
    m.ready()
    with (m.var / "lifecycle.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        control_locked(m, command)


def control_locked(m, command):
    # Caller owns lifecycle.lock (also used by the core updater).
    if command == "restart":
        enabled = (m.var / "enabled").exists()
        try:
            control_locked(m, "stop")
            control_locked(m, "start")
        except Exception:
            if enabled:
                (m.var / "enabled").touch(mode=0o600)
            raise
        return
    if command in ("stop", "disable", "preuninstall", "preupgrade"):
        (m.var / "enabled").unlink(missing_ok=True)
        pid = live(m)
        if pid:
            os.kill(pid, signal.SIGTERM)
            for _ in range(240):
                if not live(m):
                    break
                time.sleep(.25)
            if live(m):
                raise DownloadError("下载进程仍在退出，请稍后重试；未强制终止")
        return
    if command == "status":
        if not live(m):
            raise DownloadError("服务未运行")
        return
    if command == "ensure" and not (m.var / "enabled").exists():
        return
    if command not in ("start", "ensure", "enable", "postinstall", "postupgrade"):
        raise DownloadError("未知服务操作")
    (m.var / "enabled").touch(mode=0o600)
    if live(m):
        return
    logfile = m.var / "service.log"
    if logfile.exists() and logfile.stat().st_size > 1024 * 1024:
        os.replace(logfile, m.var / "service.log.1")
    with logfile.open("a") as out:
        subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "serve", str(m.home)],
                         stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True, close_fds=True)


if __name__ == "__main__":
    try:
        os.umask(0o077)
        m = manager(sys.argv[2])
        if sys.argv[1] == "serve":
            serve(m)
        else:
            control(m, sys.argv[1])
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
