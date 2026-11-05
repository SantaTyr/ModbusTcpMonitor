"""程序入口：加载配置、启动代理与网页服务。"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
import threading
import urllib.request
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

from modbus_proxy import __version__
from modbus_proxy.config import load_config, validate
from modbus_proxy.proxy import ModbusProxy
from modbus_proxy.recorder import Recorder
from modbus_proxy.webapp import AppContext, EventBus, WebServer


def base_dir() -> Path:
    """exe/脚本所在目录；不可写时退回到 %LOCALAPPDATA%\\ModbusMonitor。"""
    if getattr(sys, "frozen", False):
        candidate = Path(sys.executable).resolve().parent
    else:
        candidate = Path(__file__).resolve().parent
    try:
        probe = candidate / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return candidate
    except OSError:
        fallback = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "ModbusMonitor"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def static_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS")) / "modbus_proxy" / "static"
    return Path(__file__).resolve().parent / "modbus_proxy" / "static"


def setup_logging(base: Path, debug: bool) -> logging.Logger:
    logs = base / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("modbus")
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = RotatingFileHandler(
        logs / "system.log", maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)
    return logger


def set_console_title(title: str) -> None:
    if sys.platform != "win32":
        return
    with contextlib.suppress(Exception):
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW(title)


def running_instance(port: int) -> str | None:
    """探测本机是否已有实例在跑（返回其版本号）。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=0.8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("version")
    except Exception:
        return None


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Modbus TCP 监视器（透明代理 + 网页解析）")
    parser.add_argument("--config", help="配置文件路径，默认程序目录下 config.json")
    parser.add_argument("--web-port", type=int, help="网页端口，默认 8080")
    parser.add_argument("--listen-port", type=int, help="监听平台连接的端口，默认 502")
    parser.add_argument("--device", help="设备地址，格式 ip:port，例如 192.168.1.50:502")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--debug", action="store_true", help="输出调试日志")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if sys.platform == "win32":
        with contextlib.suppress(Exception):
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        with contextlib.suppress(Exception):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    base = base_dir()
    config_path = Path(args.config).expanduser() if args.config else base / "config.json"
    cfg = load_config(config_path)
    if args.web_port:
        cfg.web_port = args.web_port
    if args.listen_port:
        cfg.listen_port = args.listen_port
    if args.device:
        host, _, port = args.device.partition(":")
        cfg.device_host = host.strip()
        if port.strip():
            cfg.device_port = int(port)

    errors = validate(cfg)
    if errors:
        print("配置有误：" + "；".join(errors))
        print(f"配置文件：{config_path}")
        return 1

    log = setup_logging(base, args.debug)
    log.info("Modbus TCP 监视器 v%s 启动，工作目录 %s", __version__, base)

    existing = running_instance(int(cfg.web_port))
    if existing:
        url = f"http://127.0.0.1:{cfg.web_port}/"
        print(f"检测到已有实例在运行（v{existing}）：{url}")
        if not args.no_browser:
            webbrowser.open(url)
        return 0

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    ctx = AppContext(cfg, base, static_dir(), log)
    ctx.config_path = config_path
    bus = EventBus(ctx.frames)
    recorder = Recorder(cfg, base, log)
    proxy = ModbusProxy(cfg, bus, recorder, log)
    ctx.loop = loop
    ctx.proxy = proxy
    ctx.recorder = recorder
    ctx.bus = bus

    web: WebServer | None = None
    last_error: Exception | None = None
    for offset in range(10):
        port = int(cfg.web_port) + offset
        if port > 65535:
            break
        try:
            web = WebServer(ctx, cfg.web_host, port)
            break
        except OSError as exc:
            last_error = exc
    if web is None:
        print(f"网页端口 {cfg.web_port} 启动失败：{last_error}")
        return 1
    ctx.web_port = web.port
    web.start()
    if ctx.web_port != cfg.web_port:
        log.warning("网页端口 %s 被占用，改用 %s", cfg.web_port, ctx.web_port)

    recorder.start()

    async def heartbeat() -> None:
        while not ctx.stopping:
            await asyncio.sleep(1.0)
            with contextlib.suppress(Exception):
                bus.publish({"type": "state", "state": proxy.status()})

    if cfg.auto_start:
        if not loop.run_until_complete(proxy.start()):
            log.error("监听失败：%s", proxy.listen_error)
    loop.create_task(heartbeat())

    url = f"http://127.0.0.1:{ctx.web_port}/"
    record_dir = recorder.session_dir or (base / cfg.record.dir)
    print("=" * 62)
    print(f"  Modbus TCP 监视器  v{__version__}")
    print(f"  网页界面 : {url}")
    print(f"  监听端口 : {cfg.listen_host}:{cfg.listen_port}")
    print(f"  设备地址 : {cfg.device_host}:{cfg.device_port}")
    print(f"  记录目录 : {record_dir}")
    print(f"  配置文件 : {config_path}")
    print("=" * 62)
    print("  把平台的 Modbus 目标地址改成运行本程序的电脑 IP + 监听端口")
    print("  按 Ctrl+C 退出")
    set_console_title(f"Modbus TCP 监视器 - {url}")

    if cfg.auto_open_browser and not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    code = 0
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        print("\n正在退出…")
    except Exception:
        log.exception("主循环异常")
        code = 1
    finally:
        ctx.stopping = True
        with contextlib.suppress(Exception):
            loop.run_until_complete(proxy.stop())
        recorder.close()
        web.stop()
        with contextlib.suppress(Exception):
            loop.close()
        log.info("已退出")
    return code


if __name__ == "__main__":
    sys.exit(main())
