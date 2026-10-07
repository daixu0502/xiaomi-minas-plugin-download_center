#!/usr/bin/env python3
import json
import os
import pwd
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs

sys.dont_write_bytecode = True
UI = Path(__file__).resolve().parent
sys.path.insert(0, str(UI.parent / "files"))
from download_lib import DownloadError
from service import manager


def output(data, mime="application/json; charset=utf-8"):
    sys.stdout.write("Content-Type: " + mime + "\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n\r\n")
    sys.stdout.write(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False))


try:
    action = parse_qs(os.environ.get("QUERY_STRING", "")).get("action", [""])[0]
    if not action:
        name = os.environ.get("REQUEST_URI", "").split("?", 1)[0].rsplit("/", 1)[-1]
        mime = {"style.css": "text/css", "palette.css": "text/css", "app.js": "application/javascript", "client-bridge.js": "application/javascript"}
        if name not in mime:
            name = "index.html"
        output((UI / name).read_text(), mime.get(name, "text/html") + "; charset=utf-8")
    else:
        if os.environ.get("REQUEST_METHOD") != "POST" or not os.environ.get("CONTENT_TYPE", "").lower().startswith("application/json"):
            raise DownloadError("仅接受 JSON POST 请求")
        # The NAS /plugin gateway authenticates the token/cookie/client certificate
        # before dispatching this CGI as the owning user. The desktop bridge is
        # legitimately cross-site; Fetch Metadata is not an authentication test.
        # Keep JSON POST, installation-path and effective-UID checks below.
        match = re.search(r"/nas/pool[0-9]+/(u[0-9]+)/plugin/pluginsrc/downloadcenter/ui$", str(UI))
        if not match:
            raise DownloadError("插件安装路径无效")
        user = match.group(1)
        if os.geteuid() != pwd.getpwnam(user).pw_uid:
            raise DownloadError("插件必须以所属用户身份运行")
        length = int(os.environ.get("CONTENT_LENGTH", "0"))
        if not 0 < length <= 6 * 1024 * 1024:
            raise DownloadError("请求过大或为空")
        data = json.loads(sys.stdin.buffer.read(length))
        if not isinstance(data, dict):
            raise DownloadError("请求格式无效")
        home = Path("/home") / user / "plugin/downloadcenter"
        m = manager(home)
        if action == "service":
            if m.core_state().get('state') == 'installing' or m.qb_update_state().get('state') == 'installing':
                raise DownloadError('正在切换下载核心，请稍后再操作服务')
            command = data.get("command")
            if command not in ("start", "stop", "restart"):
                raise DownloadError("无效服务操作")
            # Fire-and-poll: never hold a CGI request open while stopping the engine.
            m.ready()
            with (m.var / "control.log").open("w") as log:
                subprocess.Popen([sys.executable, "-B", str(UI.parent / "files/service.py"), command, str(home)],
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True)
            result = {"pending": True}
        else:
            result = m.dispatch(action, data)
        output(dict(result, ok=True))
except (DownloadError, ValueError, OSError) as exc:
    output({"ok": False, "error": str(exc)})
except Exception:
    output({"ok": False, "error": "内部错误，请检查下载插件日志"})
