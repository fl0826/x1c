"""本地服务：托管网页 + 打印机接口（同源，无跨域）。

用法:
    python server.py                 # http://127.0.0.1:8080
    python server.py --web 路径/dist --port 9000

接口:
    GET  /api/status    打印机状态（后台缓存）
    GET  /api/files     outbox/ 里的切片文件
    GET  /api/progress  上传进度
    POST /api/print     {"name": "文件.gcode.3mf", "ams_slot": 0}
                        没上传会先上传，再发打印指令
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_WEB = Path(r"D:\桌面\code\smart-home-atlas-demo-v9\dist")
OUTBOX = ROOT / "outbox"

sys.path.insert(0, str(ROOT))
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import api  # noqa: E402

# 后台线程维护的状态缓存
CACHE: dict = {
    "status": {"ok": False, "error": "正在连接打印机…"},
    "last": None,      # 最近一次成功的结果，断线时继续显示
    "ts": 0.0,         # 最近一次成功的时间
    "fails": 0,        # 连续失败次数
}

# 上传进度（前端每秒来问一次）
UPLOAD: dict = {
    "busy": False,
    "phase": "",      # uploading / printing
    "name": "",
    "sent": 0,
    "total": 0,
}
ACTIVE_STATES = ("RUNNING", "PREPARE", "PAUSE", "SLICING")


# ---------------------------------------------------------------- 后台轮询


def poll_status() -> None:
    """后台刷状态。打印中 3 秒，空闲 10 秒。

    打印时打印机偶尔拒连，失败时保留上次数据，前端显示"重连中"。
    """
    while True:
        try:
            result = api.get_status()
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": f"读取状态失败: {exc}"}

        if result.get("ok"):
            CACHE["status"] = result
            CACHE["last"] = result
            CACHE["ts"] = time.time()
            CACHE["fails"] = 0
        else:
            CACHE["fails"] += 1
            merged = dict(result)
            merged["fails"] = CACHE["fails"]
            if CACHE["last"] is not None:
                merged["last"] = CACHE["last"]
                merged["stale_seconds"] = int(time.time() - CACHE["ts"])
            CACHE["status"] = merged

        last = CACHE["last"] or {}
        busy = last.get("state") in ACTIVE_STATES
        time.sleep(3 if busy else 10)


def list_outbox() -> list[dict]:
    """列出 outbox 里可以打印的文件（新的在前）。"""
    if not OUTBOX.is_dir():
        return []
    items: list[dict] = []
    for path in OUTBOX.glob("*.3mf"):
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append({
            "name": path.name,
            "size": stat.st_size,
            "mtime": int(stat.st_mtime),
        })
    return sorted(items, key=lambda item: item["mtime"], reverse=True)


def _needs_upload(local: Path, info: dict) -> bool:
    """打印机上那份是不是旧的。大小不同或修改时间差太多就重传。"""
    stat = local.stat()
    if info.get("size") != stat.st_size:
        return True
    remote_time = info.get("mtime")
    if remote_time is not None:
        if abs(remote_time - int(stat.st_mtime)) > 60:
            return True
    return False


def _on_upload_progress(sent: int, total: int) -> None:
    UPLOAD["sent"] = sent
    UPLOAD["total"] = total


# ---------------------------------------------------------------- HTTP


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, directory: str | None = None, **kwargs):
        super().__init__(*args, directory=directory, **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("  " + (fmt % args) + "\n")

    # ---------- GET ----------

    def do_GET(self) -> None:  # noqa: N802
        route = self.path.split("?")[0]
        if route == "/api/status":
            self._json(CACHE["status"])
            return
        if route == "/api/files":
            self._json({"ok": True, "files": list_outbox(),
                        "dir": str(OUTBOX)})
            return
        if route == "/api/progress":
            percent = 0
            if UPLOAD["total"]:
                percent = int(UPLOAD["sent"] * 100 / UPLOAD["total"])
            self._json(dict(UPLOAD, percent=percent))
            return
        super().do_GET()

    # ---------- POST ----------

    def do_POST(self) -> None:  # noqa: N802
        route = self.path.split("?")[0]
        if route != "/api/print":
            self.send_error(404, "unknown api")
            return

        body = self._body()
        name = str(body.get("name", "")).strip()
        if not name:
            self._json({"ok": False, "error": "缺少文件名"})
            return

        local = OUTBOX / Path(name).name
        if not local.is_file():
            self._json({"ok": False,
                        "error": f"outbox 里没有 {Path(name).name}"})
            return

        slot = body.get("ams_slot", 0)
        try:
            slot = None if slot is None else int(slot)
        except (TypeError, ValueError):
            slot = 0
        if slot is not None and not 0 <= slot <= 3:
            self._json({"ok": False, "error": f"AMS 槽位只能是 0-3，收到 {slot}"})
            return

        # 打印机上已有"同名 + 同大小 + 同修改时间"的文件才跳过上传
        info = api.remote_file_info(local.name)
        if not info.get("connected"):
            self._json({"ok": False, "error": info.get("error", "连不上打印机"),
                        "stage": "connect"})
            return
        if _needs_upload(local, info):
            UPLOAD.update(busy=True, phase="uploading", name=local.name,
                          sent=0, total=local.stat().st_size)
            try:
                uploaded = api.upload_file(local, progress=_on_upload_progress)
            finally:
                UPLOAD.update(busy=False, phase="", sent=0, total=0)
            if not uploaded.get("ok"):
                self._json({"ok": False, "stage": "upload",
                            "error": uploaded.get("error")})
                return

        # 只等 10 秒确认（网页每 5 秒会自己轮询状态，不需要后端傻等 70 秒）
        result = api.start_print(local.name, ams_slot=slot, wait_seconds=10)
        self._json(result)
        # 打印指令已发出，让前端把"上传中"切回普通状态
        UPLOAD.update(busy=False, phase="", sent=0, total=0)

    # ---------- 工具 ----------

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw.decode("utf-8", "replace") or "{}")
        except (ValueError, json.JSONDecodeError):
            return {}

    def _json(self, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


# ---------------------------------------------------------------- 入口


def main() -> int:
    parser = argparse.ArgumentParser(description="网页 + 打印机接口")
    parser.add_argument("--web", default=str(DEFAULT_WEB), help="页面目录")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="0.0.0.0",
                        help="监听地址；0.0.0.0 允许局域网/VR 访问")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    web_dir = Path(args.web)
    if not web_dir.is_dir():
        print(f"页面目录不存在: {web_dir}")
        print("用 --web 指定 smart-home-atlas 的 dist 目录")
        return 1

    OUTBOX.mkdir(exist_ok=True)

    threading.Thread(target=poll_status, daemon=True).start()

    def handler(*a, **kw):
        kw["directory"] = str(web_dir)
        return Handler(*a, **kw)

    server = ThreadingHTTPServer((args.host, args.port), handler)
    url = f"http://127.0.0.1:{args.port}/#digital-twin"
    print(f"页面: {url}")
    print(f"页面目录: {web_dir}")
    print(f"打印队列: {OUTBOX}")
    print("按 Ctrl+C 停止\n")

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
