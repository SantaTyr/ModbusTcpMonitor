"""内置 HTTP 服务：静态页面 + REST 配置接口 + SSE 实时推送。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import queue
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__
from .config import Config, RecordConfig, save_config, validate


class EventBus:
    """事件总线：把 asyncio 线程里产生的事件分发给各个浏览器（线程安全的队列）。"""

    def __init__(self, ring: deque, maxsize: int = 2000) -> None:
        self.ring = ring
        self.maxsize = maxsize
        self._subs: set[queue.Queue] = set()
        self._lock = threading.Lock()
        self.dropped = 0

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self.maxsize)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def publish(self, event: dict) -> None:
        if event.get("type") == "frame":
            self.ring.append(event)
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                with contextlib.suppress(queue.Empty):
                    q.get_nowait()
                with contextlib.suppress(queue.Full):
                    q.put_nowait(event)
                self.dropped += 1


class AppContext:
    def __init__(self, cfg: Config, base_dir: Path, static_dir: Path, log: logging.Logger) -> None:
        self.cfg = cfg
        self.base_dir = base_dir
        self.static_dir = static_dir
        self.log = log
        self.loop: asyncio.AbstractEventLoop | None = None
        self.proxy: Any = None
        self.recorder: Any = None
        self.bus: EventBus | None = None
        self.config_path: Path | None = None
        self.frames: deque = deque(maxlen=8000)
        self.stopping = False
        self.started_at = time.time()
        self.web_port = cfg.web_port
        self.restart_required = False

    def call(self, coro, timeout: float = 15.0):
        assert self.loop is not None
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=timeout)


def build_state(ctx: AppContext) -> dict:
    state = ctx.proxy.status() if ctx.proxy is not None else {}
    state["config"] = ctx.cfg.to_dict()
    state["version"] = __version__
    state["web_port"] = ctx.web_port
    state["base_dir"] = str(ctx.base_dir)
    state["restart_required"] = ctx.restart_required
    state["dropped"] = ctx.bus.dropped if ctx.bus is not None else 0
    state["server_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return state


def make_handler(ctx: AppContext):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "ModbusMonitor/1.0"

        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            ctx.log.debug("HTTP %s - %s", self.address_string(), fmt % args)

        # ------------------------------ GET ------------------------------ #
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            try:
                if path == "/api/events":
                    return self.serve_sse()
                if path == "/api/state":
                    return self.send_json(build_state(ctx))
                if path == "/api/config":
                    return self.send_json(ctx.cfg.to_dict())
                if path == "/api/frames":
                    return self.api_frames(parsed.query)
                if path == "/api/export.csv":
                    return self.api_export()
                if path in ("/", "/index.html"):
                    return self.serve_file("index.html", "text/html; charset=utf-8")
                if path == "/app.js":
                    return self.serve_file("app.js", "application/javascript; charset=utf-8")
                if path == "/style.css":
                    return self.serve_file("style.css", "text/css; charset=utf-8")
                if path == "/favicon.ico":
                    return self.send_bytes(b"", "image/x-icon")
                self.send_error(404, "Not Found")
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                self.close_connection = True
            except Exception:
                ctx.log.exception("处理 GET %s 失败", self.path)
                with contextlib.suppress(Exception):
                    self.send_error(500, "Internal Server Error")

        # ------------------------------ POST ----------------------------- #
        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            try:
                if path == "/api/config":
                    return self.api_config()
                if path == "/api/control":
                    return self.api_control()
                if path == "/api/clear":
                    ctx.frames.clear()
                    return self.send_json({"ok": True})
                if path == "/api/open-folder":
                    return self.api_open_folder()
                self.send_error(404, "Not Found")
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                self.close_connection = True
            except Exception:
                ctx.log.exception("处理 POST %s 失败", self.path)
                with contextlib.suppress(Exception):
                    self.send_error(500, "Internal Server Error")

        # ----------------------------- 静态文件 -------------------------- #
        def serve_file(self, name: str, content_type: str) -> None:
            path = ctx.static_dir / name
            if not path.is_file():
                self.send_error(500, f"缺少前端文件 {name}")
                return
            self.send_bytes(path.read_bytes(), content_type)

        def send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if body:
                self.wfile.write(body)

        def send_json(self, obj: Any, status: int = 200) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            if length > 2 * 1024 * 1024:
                raise ValueError("请求体过大")
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("请求体必须是 JSON 对象")
            return data

        # ------------------------------- API ----------------------------- #
        def api_frames(self, query: str) -> None:
            params = parse_qs(query)
            try:
                limit = int(params.get("limit", ["1000"])[0])
            except ValueError:
                limit = 1000
            limit = max(1, min(limit, 8000))
            items = list(ctx.frames)[-limit:]
            self.send_json({"frames": items})

        def api_export(self) -> None:
            lines = ["时间,方向,TID,功能码,摘要,原始数据(HEX)"]
            for ev in ctx.frames:
                direction = {"up": "平台→设备", "down": "设备→平台", "gw": "网关→平台"}.get(ev.get("dir"), "")
                summary = str(ev.get("sum", "")).replace('"', '""')
                lines.append(
                    f'{ev.get("ts","")},{direction},{ev.get("tid","")},{ev.get("fc","")},'
                    f'"{summary}",{ev.get("hex","")}'
                )
            body = ("\ufeff" + "\r\n".join(lines) + "\r\n").encode("utf-8")
            name = time.strftime("modbus_frames_%Y%m%d_%H%M%S.csv")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def api_config(self) -> None:
            data = self.read_json()
            new = ctx.cfg.copy()
            try:
                for key, value in data.items():
                    if key == "record":
                        if not isinstance(value, dict):
                            raise ValueError("record 必须是对象")
                        new.record = RecordConfig.from_dict(value)
                    elif hasattr(new, key) and key not in ("record",):
                        setattr(new, key, value)
                for key in ("listen_port", "device_port", "web_port", "address_offset"):
                    setattr(new, key, int(getattr(new, key)))
                for key in ("connect_timeout", "response_timeout", "idle_close"):
                    setattr(new, key, float(getattr(new, key)))
                new.record.rotate_mb = int(new.record.rotate_mb)
                new.record.keep_days = int(new.record.keep_days)
                new.record.enabled = bool(new.record.enabled)
                new.record.flush = bool(new.record.flush)
            except (TypeError, ValueError) as exc:
                self.send_json({"ok": False, "errors": [f"参数格式错误：{exc}"]}, 400)
                return

            errors = validate(new)
            if errors:
                self.send_json({"ok": False, "errors": errors}, 400)
                return

            old_web = ctx.cfg.web_port
            ctx.cfg = new
            assert ctx.config_path is not None
            save_config(ctx.config_path, new)
            result = ctx.call(ctx.proxy.apply(new))
            ctx.restart_required = old_web != new.web_port
            self.send_json({
                "ok": True,
                "restart_required": ctx.restart_required,
                "result": result,
                "state": build_state(ctx),
            })

        def api_control(self) -> None:
            data = self.read_json()
            action = data.get("action")
            if action == "start":
                ok = ctx.call(ctx.proxy.start())
                return self.send_json({"ok": bool(ok), "state": build_state(ctx)})
            if action == "stop":
                ctx.call(ctx.proxy.stop())
                return self.send_json({"ok": True, "state": build_state(ctx)})
            self.send_json({"ok": False, "errors": ["未知操作"]}, 400)

        def api_open_folder(self) -> None:
            target = None
            if ctx.recorder is not None:
                target = ctx.recorder.session_dir
            if target is None:
                target = Path(ctx.cfg.record.dir)
                if not target.is_absolute():
                    target = ctx.base_dir / target
            try:
                if not Path(target).exists():
                    Path(target).mkdir(parents=True, exist_ok=True)
                os.startfile(str(target))  # type: ignore[attr-defined]
            except Exception as exc:
                self.send_json({"ok": False, "errors": [str(exc)]}, 500)
                return
            self.send_json({"ok": True, "path": str(target)})

        # ------------------------------- SSE ----------------------------- #
        def serve_sse(self) -> None:
            assert ctx.bus is not None
            q = ctx.bus.subscribe()
            self.close_connection = True
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                self.wfile.write(b"retry: 1500\n\n")
                self._sse({"type": "state", "state": build_state(ctx)})
                self.wfile.flush()
                while not ctx.stopping:
                    try:
                        event = q.get(timeout=10.0)
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    self._sse(event)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
                pass
            finally:
                ctx.bus.unsubscribe(q)

        def _sse(self, event: dict) -> None:
            body = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
            self.wfile.write(f"data: {body}\n\n".encode("utf-8"))

    return Handler


class QuietHTTPServer(ThreadingHTTPServer):
    """浏览器/探活连接随时断开，不把这些常见异常打印成堆栈。"""

    daemon_threads = True

    def handle_error(self, request, client_address) -> None:  # noqa: ANN001
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class WebServer:
    def __init__(self, ctx: AppContext, host: str, port: int) -> None:
        self.ctx = ctx
        self.httpd = QuietHTTPServer((host, int(port)), make_handler(ctx))
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="http-server", daemon=True)

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            self.httpd.shutdown()
        with contextlib.suppress(Exception):
            self.httpd.server_close()
