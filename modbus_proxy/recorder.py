"""收发数据落盘：data.log / data.csv + events.log + meta.json。"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import IO

from .config import Config


class Recorder:
    """把每一帧原始报文按 时间 + 方向 + HEX 写入本地文件。

    只由 asyncio 事件循环线程调用；status() 可能被 HTTP 线程读取，字段均为简单值。
    """

    def __init__(self, cfg: Config, base_dir: Path, log: logging.Logger) -> None:
        self.cfg = cfg
        self.base_dir = base_dir
        self.log = log
        self.session_dir: Path | None = None
        self.error: str | None = None
        self.frames = 0
        self.part = 0
        self.bytes_written = 0
        self._data_log: IO[str] | None = None
        self._data_csv: IO[str] | None = None
        self._events: IO[str] | None = None
        self._last_disk_check = 0.0

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.record.enabled)

    def _root(self) -> Path:
        root = Path(self.cfg.record.dir)
        if not root.is_absolute():
            root = self.base_dir / root
        return root

    def start(self) -> None:
        if not self.enabled:
            self.session_dir = None
            return
        self.error = None
        self.frames = 0
        self.part = 0
        self.bytes_written = 0
        try:
            root = self._root()
            root.mkdir(parents=True, exist_ok=True)
            self._cleanup(root)
            now = datetime.now()
            folder = root / now.strftime("%Y%m%d") / now.strftime("%H%M%S")
            folder.mkdir(parents=True, exist_ok=True)
            self.session_dir = folder
            self._open_files()
            meta = {
                "started": now.isoformat(timespec="milliseconds"),
                "config": self.cfg.to_dict(),
            }
            (folder / "meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            self._fail(f"创建记录目录失败：{exc}")

    def _open_files(self) -> None:
        assert self.session_dir is not None
        suffix = "" if self.part == 0 else f"_{self.part:03d}"
        data_path = self.session_dir / f"data{suffix}.log"
        csv_path = self.session_dir / f"data{suffix}.csv"
        new_csv = not csv_path.exists() or csv_path.stat().st_size == 0
        self._data_log = open(data_path, "a", encoding="utf-8-sig", newline="")
        self._data_csv = open(csv_path, "a", encoding="utf-8-sig", newline="")
        if new_csv:
            self._data_csv.write("时间,方向,字节数,原始数据(HEX)\r\n")
            self._data_csv.flush()
        if self._events is None:
            self._events = open(self.session_dir / "events.log", "a", encoding="utf-8-sig", newline="")

    def _close_data(self) -> None:
        for fh in (self._data_log, self._data_csv):
            try:
                if fh:
                    fh.flush()
                    fh.close()
            except OSError:
                pass
        self._data_log = None
        self._data_csv = None

    def _rotate(self) -> None:
        self._close_data()
        self.part += 1
        self.bytes_written = 0
        try:
            self._open_files()
        except OSError as exc:
            self._fail(f"日志滚动失败：{exc}")

    def close(self) -> None:
        self._close_data()
        if self._events:
            try:
                self._events.close()
            except OSError:
                pass
            self._events = None

    def _fail(self, message: str) -> None:
        self.error = message
        self.log.error("记录停止：%s", message)
        self._close_data()

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #

    def record(self, ts: str, direction: str, frame: bytes) -> None:
        if not self.enabled or self.error or self._data_log is None or self._data_csv is None:
            return
        raw = frame.hex().upper()
        try:
            self._data_log.write(f"{ts}\t{direction}\t{raw}\r\n")
            self._data_csv.write(f"{ts},{direction},{len(frame)},{raw}\r\n")
            if self.cfg.record.flush:
                self._data_log.flush()
                self._data_csv.flush()
            self.frames += 1
            self.bytes_written += len(raw) * 2 + 40
            limit = max(1, int(self.cfg.record.rotate_mb)) * 1024 * 1024
            if self.bytes_written >= limit:
                self._rotate()
            self._check_disk()
        except OSError as exc:
            self._fail(f"写入失败：{exc}")

    def _check_disk(self) -> None:
        import time

        now = time.monotonic()
        if now - self._last_disk_check < 5.0:
            return
        self._last_disk_check = now
        try:
            free = shutil.disk_usage(self.session_dir or self.base_dir).free
        except OSError:
            return
        if free < 100 * 1024 * 1024:
            self._fail(f"磁盘剩余空间不足 100MB（剩余 {free // 1024 // 1024}MB）")

    def note(self, text: str) -> None:
        if self._events is None:
            return
        try:
            self._events.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {text}\r\n")
            self._events.flush()
        except OSError:
            pass

    def _cleanup(self, root: Path) -> None:
        keep = max(1, int(self.cfg.record.keep_days))
        cutoff = (datetime.now() - timedelta(days=keep)).date()
        for child in root.iterdir():
            if not child.is_dir() or len(child.name) != 8 or not child.name.isdigit():
                continue
            try:
                day = datetime.strptime(child.name, "%Y%m%d").date()
            except ValueError:
                continue
            if day < cutoff:
                shutil.rmtree(child, ignore_errors=True)

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "dir": str(self.session_dir) if self.session_dir else "",
            "frames": self.frames,
            "part": self.part,
            "error": self.error,
        }
