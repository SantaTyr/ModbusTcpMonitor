"""透明代理：平台(TCP Server) ← 本机 → 设备(TCP Client)。

设计要点：
* 单平台连接、单设备连接（设备本身只允许一条 TCP），TID 原样透传，不改写报文；
* 设备断线自动重连，平台侧请求进入发送队列，重连成功后继续发送；
* 每一帧先落盘、再转发，解析失败不影响转发。
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import mbap
from .config import Config
from .recorder import Recorder

UP = "平台→设备"
DOWN = "设备→平台"
GW = "网关→平台"


def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def clock() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def _tune(writer: asyncio.StreamWriter) -> None:
    sock = writer.get_extra_info("socket")
    if sock is None:
        return
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        pass


def _peer(writer: asyncio.StreamWriter) -> str:
    info = writer.get_extra_info("peername")
    if isinstance(info, tuple) and len(info) >= 2:
        return f"{info[0]}:{info[1]}"
    return str(info)


@dataclass
class Session:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    peer: str
    opened: float = field(default_factory=time.time)
    buf: bytes = b""
    frames_in: int = 0
    frames_out: int = 0
    bytes_in: int = 0
    bytes_out: int = 0


class ModbusProxy:
    def __init__(self, cfg: Config, bus: Any, recorder: Recorder, log: Any) -> None:
        self.cfg = cfg
        self.bus = bus
        self.rec = recorder
        self.log = log

        self.server: asyncio.AbstractServer | None = None
        self.session: Session | None = None
        self.running = False
        self.listen_error: str | None = None
        self.mode: str | None = None            # "mbap" | "raw"

        self.up_task: asyncio.Task | None = None
        self.up_reader: asyncio.StreamReader | None = None
        self.up_writer: asyncio.StreamWriter | None = None
        self.up_state = "未连接"
        self.up_error: str | None = None
        self.up_since: float | None = None
        self.up_reconnects = 0

        self.pending: dict[int, dict] = {}
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=2000)

        self.idle_handle: asyncio.TimerHandle | None = None
        self._idle_expired = True
        self._closing = False

        self.started_at: float | None = None
        self.stats = {
            "frames_up": 0, "frames_down": 0,
            "bytes_up": 0, "bytes_down": 0,
            "timeouts": 0, "rejected": 0, "errors": 0,
        }
        self._marks: deque[float] = deque(maxlen=4000)

    # ------------------------------------------------------------------ #
    # 启停
    # ------------------------------------------------------------------ #

    async def start(self) -> bool:
        await self.stop()
        self.listen_error = None
        self._closing = False
        host, port = self.cfg.listen_host, self.cfg.listen_port
        try:
            # Windows 下不显式开启 SO_REUSEADDR：避免两个实例同时“监听”同一端口
            self.server = await asyncio.start_server(
                self._on_client, host, port, backlog=8
            )
        except OSError as exc:
            self.listen_error = f"监听 {host}:{port} 失败：{exc}"
            self._note(self.listen_error, "error")
            self._publish_state()
            return False
        self.running = True
        self.started_at = time.time()
        self._publish_state()
        self._note(f"开始监听 {host}:{port}")
        return True

    async def stop(self) -> None:
        if not self.running and self.server is None and self.up_task is None:
            return
        self.running = False
        self._closing = True
        if self.idle_handle is not None:
            self.idle_handle.cancel()
            self.idle_handle = None
        if self.server is not None:
            self.server.close()
            with contextlib.suppress(Exception):
                await self.server.wait_closed()
            self.server = None
        await self._close_session("代理停止")
        await self._stop_upstream()
        self._publish_state()
        self._note("已停止监听")

    # ------------------------------------------------------------------ #
    # 平台侧
    # ------------------------------------------------------------------ #

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = _peer(writer)
        if not self.running:
            writer.close()
            return
        if self.session is not None:
            old = self.session
            self.stats["rejected"] += 1
            self._note(f"平台新连接 {peer} 接管旧连接 {old.peer}", "warn")
            self.session = None
            self._cancel_pending("平台连接被接管")
            with contextlib.suppress(Exception):
                old.writer.close()

        _tune(writer)
        self.mode = None
        sess = Session(reader, writer, peer)
        self.session = sess
        self._idle_expired = False
        if self.idle_handle is not None:
            self.idle_handle.cancel()
            self.idle_handle = None
        self._note(f"平台已连接 {peer}")
        self._publish_state()
        try:
            await self._client_loop(sess)
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError) as exc:
            self._note(f"平台连接异常：{exc}", "warn")
        except Exception as exc:                       # 防御：任何异常都不能拖垮进程
            self.log.exception("平台连接处理异常")
            self._note(f"平台连接处理异常：{exc}", "error")
        finally:
            was_current = self.session is sess
            if was_current:
                self.session = None
                self._cancel_pending("平台断开")
            with contextlib.suppress(Exception):
                writer.close()
            self._note(f"平台已断开 {peer}")
            self._publish_state()
            if was_current and self.running:
                self._schedule_idle_close()

    async def _client_loop(self, sess: Session) -> None:
        while True:
            data = await sess.reader.read(65536)
            if not data:
                return
            sess.bytes_in += len(data)
            self.stats["bytes_up"] += len(data)
            sess.buf += data
            if self.mode is None:
                if len(sess.buf) < 8:
                    continue
                self.mode = "mbap" if mbap.looks_like_mbap(sess.buf) else "raw"
                self._note("链路识别为 Modbus TCP (MBAP)" if self.mode == "mbap"
                           else "未识别到 MBAP 头，按原始字节流转发")
            if self.mode == "raw":
                chunk, sess.buf = sess.buf, b""
                await self._from_platform_raw(sess, chunk)
            else:
                frames, sess.buf, dropped = mbap.split_frames(sess.buf)
                if dropped:
                    self._note(f"帧头异常，跳过 {dropped} 字节重新同步", "warn")
                await self._from_platform(sess, frames)

    async def _from_platform(self, sess: Session, frames: list[bytes]) -> None:
        if self.session is not sess:
            return
        for frame in frames:
            ts = now_ts()
            self.rec.record(ts, UP, frame)
            info = mbap.decode(frame, "req", offset=self.cfg.address_offset)
            sess.frames_in += 1
            self.stats["frames_up"] += 1
            self._mark()
            self._publish_frame("up", frame, info, ts=clock())
            await self._enqueue(frame, info)

    async def _from_platform_raw(self, sess: Session, chunk: bytes) -> None:
        if self.session is not sess:
            return
        ts = now_ts()
        self.rec.record(ts, UP, chunk)
        sess.frames_in += 1
        self.stats["frames_up"] += 1
        self._mark()
        self._publish_raw("up", chunk, ts=clock())
        await self._enqueue(chunk, None)

    async def _enqueue(self, frame: bytes, info: dict | None) -> None:
        if not self.running:
            return
        self._ensure_up()
        try:
            self.queue.put_nowait((frame, info))
        except asyncio.QueueFull:
            self.stats["errors"] += 1
            self._note("转发队列已满，丢弃一帧", "error")

    # ------------------------------------------------------------------ #
    # 设备侧
    # ------------------------------------------------------------------ #

    def _ensure_up(self) -> None:
        if self.up_task is not None and not self.up_task.done():
            return
        self._idle_expired = False
        self.up_task = asyncio.create_task(self._upstream_loop(), name="device-link")

    async def _upstream_loop(self) -> None:
        backoff = 0.5
        while self.running and not self._idle_expired:
            host, port = self.cfg.device_host, self.cfg.device_port
            self.up_state = "连接中"
            self.up_error = None
            self._publish_state()
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=float(self.cfg.connect_timeout)
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.up_state = "连接失败"
                self.up_error = _err_text(exc)
                self.up_reconnects += 1
                self._publish_state()
                self._note(f"设备连接失败：{self.up_error}", "warn")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 5.0)
                continue

            backoff = 0.5
            _tune(writer)
            self.up_reader, self.up_writer = reader, writer
            self.up_state = "已连接"
            self.up_error = None
            self.up_since = time.time()
            self._publish_state()
            self._note(f"设备已连接 {host}:{port}")
            reader_task = asyncio.create_task(self._device_reader(reader), name="device-reader")
            try:
                while True:
                    if reader_task.done():
                        break
                    try:
                        frame, info = await asyncio.wait_for(self.queue.get(), timeout=0.25)
                    except asyncio.TimeoutError:
                        continue
                    if reader_task.done():
                        with contextlib.suppress(asyncio.QueueFull):
                            self.queue.put_nowait((frame, info))
                        break
                    writer.write(frame)
                    try:
                        await asyncio.wait_for(writer.drain(), timeout=5.0)
                    except asyncio.TimeoutError:
                        self._note("向设备写入超时，重建链路", "warn")
                        break
                    self._register_pending(frame, info)
            except asyncio.CancelledError:
                raise
            except (ConnectionError, OSError) as exc:
                self.up_error = _err_text(exc)
                self._note(f"设备连接中断：{self.up_error}", "warn")
            finally:
                reader_task.cancel()
                with contextlib.suppress(BaseException):
                    await reader_task
                self.up_reader = None
                self.up_writer = None
                with contextlib.suppress(Exception):
                    writer.close()
                self.up_state = "已断开"
                self._fail_pending("设备连接断开")
                self._publish_state()

            if not self.running or self._idle_expired:
                break
            if self.session is None and self.queue.empty():
                break
            await asyncio.sleep(0.5)

        self.up_task = None
        if self.up_state != "未连接":
            self.up_state = "未连接"
            self._publish_state()

    async def _device_reader(self, reader: asyncio.StreamReader) -> None:
        buf = b""
        while True:
            data = await reader.read(65536)
            if not data:
                return
            self.stats["bytes_down"] += len(data)
            if self.mode == "raw":
                self.rec.record(now_ts(), DOWN, data)
                self.stats["frames_down"] += 1
                self._mark()
                self._publish_raw("down", data, ts=clock())
                self._write_client(data)
                continue
            buf += data
            frames, rest, dropped = mbap.split_frames(buf)
            buf = rest
            if dropped:
                self._note(f"设备侧帧头异常，跳过 {dropped} 字节", "warn")
            for frame in frames:
                self._handle_device_frame(frame)

    def _handle_device_frame(self, frame: bytes) -> None:
        ts = now_ts()
        self.rec.record(ts, DOWN, frame)
        tid = int.from_bytes(frame[0:2], "big")
        pending = self.pending.pop(tid, None)
        rtt = None
        ref = None
        if pending is not None:
            pending["task"].cancel()
            rtt = (time.monotonic() - pending["ts"]) * 1000.0
            ref = pending["ref"]
        info = mbap.decode(frame, "resp", ref=ref, offset=self.cfg.address_offset)
        self.stats["frames_down"] += 1
        self._mark()
        sess = self.session
        if sess is not None:
            sess.frames_out += 1
        self._publish_frame("down", frame, info, rtt=rtt, ts=clock())
        self._write_client(frame)

    def _write_client(self, data: bytes) -> None:
        sess = self.session
        if sess is None:
            return
        try:
            transport = getattr(sess.writer, "transport", None)
            if transport is not None and transport.get_write_buffer_size() > 4 * 1024 * 1024:
                self._note("平台接收缓慢，发送缓冲超限，断开该连接", "warn")
                sess.writer.close()
                return
            sess.writer.write(data)
            sess.bytes_out += len(data)
        except Exception:
            self.log.debug("向平台写入失败", exc_info=True)

    def _register_pending(self, frame: bytes, info: dict | None) -> None:
        if info is None:
            return
        tid = int.from_bytes(frame[0:2], "big")
        old = self.pending.pop(tid, None)
        if old is not None:
            old["task"].cancel()
            self._note(f"TID 0x{tid:04X} 被重复使用，覆盖前一次请求", "warn")
        p = {"ts": time.monotonic(), "ref": info, "frame": frame, "task": None}
        p["task"] = asyncio.create_task(self._expire(tid, p), name=f"timeout-{tid:04x}")
        self.pending[tid] = p

    async def _expire(self, tid: int, p: dict) -> None:
        await asyncio.sleep(float(self.cfg.response_timeout))
        if self.pending.get(tid) is not p:
            return
        del self.pending[tid]
        self.stats["timeouts"] += 1
        self._note(f"设备应答超时 TID=0x{tid:04X}", "warn")
        if self.cfg.timeout_policy == "exception" and self.session is not None:
            frame = mbap.make_exception_frame(p["frame"], 11)
            self.rec.record(now_ts(), GW, frame)
            self._write_client(frame)
            info = mbap.decode(frame, "resp", offset=self.cfg.address_offset)
            info["summary"] = "设备应答超时，网关代回异常 0x0B"
            self._publish_frame("gw", frame, info, ts=clock())

    def _fail_pending(self, reason: str) -> None:
        if not self.pending:
            return
        for tid, p in list(self.pending.items()):
            p["task"].cancel()
            self._note(f"请求未完成 TID=0x{tid:04X}（{reason}）", "warn")
        self.pending.clear()

    def _cancel_pending(self, reason: str) -> None:
        if not self.pending:
            return
        for tid, p in list(self.pending.items()):
            p["task"].cancel()
            self.log.info("未完成请求 TID=0x%04X：%s", tid, reason)
        self.pending.clear()

    # ------------------------------------------------------------------ #
    # 空闲与关闭
    # ------------------------------------------------------------------ #

    def _schedule_idle_close(self) -> None:
        if self.idle_handle is not None:
            self.idle_handle.cancel()
            self.idle_handle = None
        delay = float(self.cfg.idle_close)
        if delay <= 0:
            asyncio.create_task(self._stop_upstream())
            return
        loop = asyncio.get_running_loop()
        self.idle_handle = loop.call_later(delay, self._idle_timer)

    def _idle_timer(self) -> None:
        self.idle_handle = None
        if self.session is None:
            asyncio.create_task(self._stop_upstream())

    async def _stop_upstream(self) -> None:
        self._idle_expired = True
        task, self.up_task = self.up_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        writer, self.up_writer = self.up_writer, None
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()
        self.up_reader = None
        self.up_state = "未连接"
        self._cancel_pending("设备链路空闲关闭")
        self._publish_state()

    async def _close_session(self, reason: str) -> None:
        sess, self.session = self.session, None
        if sess is None:
            return
        self._cancel_pending(reason)
        with contextlib.suppress(Exception):
            sess.writer.close()
            await sess.writer.wait_closed()
        self._note(f"平台连接关闭（{reason}）")

    # ------------------------------------------------------------------ #
    # 配置热更新
    # ------------------------------------------------------------------ #

    async def apply(self, cfg: Config) -> dict:
        old_listen = (self.cfg.listen_host, self.cfg.listen_port)
        old_dev = (self.cfg.device_host, self.cfg.device_port)
        old_rec = (self.cfg.record.enabled, self.cfg.record.dir)
        self.cfg = cfg
        self.rec.cfg = cfg
        result = {"restarted": False, "device_reconnect": False}
        rec_changed = old_rec != (cfg.record.enabled, cfg.record.dir)
        if rec_changed:
            self.rec.close()
            self.rec.start()
        if not self.running:
            return result
        if old_listen != (cfg.listen_host, cfg.listen_port):
            await self.start()
            result["restarted"] = True
        elif old_dev != (cfg.device_host, cfg.device_port):
            await self._stop_upstream()
            if self.session is not None:
                self._ensure_up()
            result["device_reconnect"] = True
        return result

    # ------------------------------------------------------------------ #
    # 状态与事件
    # ------------------------------------------------------------------ #

    def _mark(self) -> None:
        self._marks.append(time.monotonic())

    def fps(self) -> float:
        now = time.monotonic()
        while self._marks and now - self._marks[0] > 5.0:
            self._marks.popleft()
        return round(len(self._marks) / 5.0, 1)

    def status(self) -> dict:
        sess = self.session
        client = None
        if sess is not None:
            client = {
                "peer": sess.peer,
                "since": _iso(sess.opened),
                "frames_in": sess.frames_in,
                "frames_out": sess.frames_out,
                "bytes_in": sess.bytes_in,
                "bytes_out": sess.bytes_out,
            }
        return {
            "running": self.running,
            "listen": f"{self.cfg.listen_host}:{self.cfg.listen_port}",
            "listen_error": self.listen_error,
            "mode": self.mode,
            "client": client,
            "upstream": {
                "state": self.up_state,
                "peer": f"{self.cfg.device_host}:{self.cfg.device_port}",
                "since": _iso(self.up_since) if self.up_since else None,
                "error": self.up_error,
                "reconnects": self.up_reconnects,
            },
            "stats": dict(self.stats, pending=len(self.pending), fps=self.fps()),
            "recording": self.rec.status(),
            "uptime": int(time.time() - self.started_at) if self.started_at else 0,
        }

    def _publish_state(self) -> None:
        with contextlib.suppress(Exception):
            self.bus.publish({"type": "state", "state": self.status()})

    def _publish_frame(self, direction: str, frame: bytes, info: dict, rtt: float | None = None,
                       ts: str | None = None) -> None:
        event = {
            "type": "frame",
            "ts": ts or clock(),
            "dir": direction,
            "tid": f"0x{info['tid']:04X}",
            "fc": info["fc"],
            "uid": info["uid"],
            "hex": frame.hex().upper(),
            "sum": info["summary"],
            "bad": bool(info["bad"]),
            "vals": info["vals"],
            "bits": info["bits"],
            "fields": [[k, v] for k, v in info["fields"]],
            "addr": info["addr"],
            "qty": info["qty"],
            "vals_addr": info.get("vals_addr"),
            "rtt": round(rtt, 2) if rtt is not None else None,
        }
        with contextlib.suppress(Exception):
            self.bus.publish(event)

    def _publish_raw(self, direction: str, chunk: bytes, ts: str | None = None) -> None:
        event = {
            "type": "frame", "ts": ts or clock(), "dir": direction,
            "tid": "", "fc": -1, "uid": -1, "hex": chunk.hex().upper(),
            "sum": f"原始数据 {len(chunk)} 字节（未识别为 Modbus TCP）",
            "bad": False, "vals": None, "bits": None, "fields": [],
            "addr": None, "qty": None, "rtt": None,
        }
        with contextlib.suppress(Exception):
            self.bus.publish(event)

    def _note(self, text: str, level: str = "info") -> None:
        self.log.info(text) if level == "info" else self.log.warning(text)
        with contextlib.suppress(Exception):
            self.rec.note(text)
        with contextlib.suppress(Exception):
            self.bus.publish({"type": "sys", "ts": clock(), "level": level, "text": text})


def _err_text(exc: BaseException) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")
