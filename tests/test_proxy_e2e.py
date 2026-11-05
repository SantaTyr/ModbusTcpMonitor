"""端到端测试：假设备 + 真代理 + 假平台，一个事件循环内跑完。"""

import asyncio
import contextlib
import logging
import socket
import sys
import tempfile
import unittest
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modbus_proxy import mbap  # noqa: E402
from modbus_proxy.config import Config, RecordConfig  # noqa: E402
from modbus_proxy.proxy import ModbusProxy  # noqa: E402
from modbus_proxy.recorder import Recorder  # noqa: E402
from modbus_proxy.webapp import EventBus  # noqa: E402

DEVICE = 15020


def free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def build(tid: int, uid: int, fc: int, payload: bytes) -> bytes:
    pdu = bytes([fc]) + payload
    return tid.to_bytes(2, "big") + b"\x00\x00" + (len(pdu) + 1).to_bytes(2, "big") + bytes([uid]) + pdu


class FakeDevice:
    """只实现测试需要的功能码；地址 0x00FF 不响应，0x00FE 返回异常 2。"""

    def __init__(self, port: int = DEVICE) -> None:
        self.port = port
        self.regs = {0x10: 0x1111, 0x11: 0x2222, 0x12: 0x3333, 0x13: 0x4444}
        self.coils = {0x10: 1, 0x11: 0}
        self.frames = 0
        self.server = None

    async def start(self) -> None:
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", self.port)

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def handle(self, reader, writer) -> None:
        buf = b""
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    return
                buf += data
                frames, buf, _ = mbap.split_frames(buf)
                for frame in frames:
                    self.frames += 1
                    resp = self.process(frame)
                    if resp:
                        writer.write(resp)
                        await writer.drain()
        finally:
            writer.close()

    def process(self, frame: bytes) -> bytes | None:
        tid, uid, pdu = frame[0:2], frame[6:7], frame[7:]
        fc = pdu[0]
        addr = int.from_bytes(pdu[1:3], "big")
        if fc in (3, 4):
            if addr == 0x00FF:
                return None
            if addr == 0x00FE:
                return tid + b"\x00\x00\x00\x03" + uid + bytes([fc | 0x80, 2])
            qty = int.from_bytes(pdu[3:5], "big")
            payload = b"".join(self.regs.get(addr + i, 0).to_bytes(2, "big") for i in range(qty))
            body = bytes([fc, len(payload)]) + payload
            return tid + b"\x00\x00" + (len(body) + 1).to_bytes(2, "big") + uid + body
        if fc == 6:
            value = int.from_bytes(pdu[3:5], "big")
            self.regs[addr] = value
            body = pdu[:5]
            return tid + b"\x00\x00" + (len(body) + 1).to_bytes(2, "big") + uid + body
        if fc == 16:
            qty = int.from_bytes(pdu[3:5], "big")
            count = pdu[5]
            data = pdu[6:6 + count]
            for i in range(qty):
                self.regs[addr + i] = int.from_bytes(data[i * 2:i * 2 + 2], "big")
            body = pdu[:5]
            return tid + b"\x00\x00" + (len(body) + 1).to_bytes(2, "big") + uid + body
        if fc == 1:
            qty = int.from_bytes(pdu[3:5], "big")
            bits = [self.coils.get(addr + i, 0) for i in range(qty)]
            value = 0
            for i, bit in enumerate(bits):
                value |= (1 if bit else 0) << i
            count = (qty + 7) // 8
            body = bytes([fc, count]) + value.to_bytes(count, "little")
            return tid + b"\x00\x00" + (len(body) + 1).to_bytes(2, "big") + uid + body
        if fc == 5:
            value = int.from_bytes(pdu[3:5], "big")
            self.coils[addr] = 1 if value == 0xFF00 else 0
            body = pdu[:5]
            return tid + b"\x00\x00" + (len(body) + 1).to_bytes(2, "big") + uid + body
        body = bytes([fc | 0x80, 1])
        return tid + b"\x00\x00" + (len(body) + 1).to_bytes(2, "big") + uid + body


class ProxyE2ETest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mbmon-"))
        self.device = FakeDevice(free_port())
        await self.device.start()
        self.listen_port = free_port()
        self.cfg = Config(
            listen_host="127.0.0.1", listen_port=self.listen_port,
            device_host="127.0.0.1", device_port=self.device.port,
            connect_timeout=2.0, response_timeout=0.5, idle_close=5.0,
            record=RecordConfig(enabled=True, dir=str(self.tmp / "captures")),
        )
        self.log = logging.getLogger("test")
        self.bus = EventBus(deque(maxlen=2000))
        self.recorder = Recorder(self.cfg, self.tmp, self.log)
        self.recorder.start()
        self.proxy = ModbusProxy(self.cfg, self.bus, self.recorder, self.log)
        self.assertTrue(await self.proxy.start())
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", self.listen_port), timeout=3
        )

    async def asyncTearDown(self) -> None:
        self.writer.close()
        await self.proxy.stop()
        self.recorder.close()
        await self.device.stop()

    async def request(self, frame: bytes, timeout: float = 3.0) -> bytes:
        self.writer.write(frame)
        await self.writer.drain()
        return await asyncio.wait_for(self.reader.read(4096), timeout=timeout)

    async def test_full_flow(self) -> None:
        # 1) 读 4 个保持寄存器
        resp = await self.request(build(1, 1, 3, b"\x00\x10\x00\x04"))
        self.assertEqual(resp, build(1, 1, 3, b"\x08\x11\x11\x22\x22\x33\x33\x44\x44"))

        # 2) 写 3 个寄存器
        write = build(2, 1, 16, b"\x00\x20\x00\x03\x06\xaa\xaa\xbb\xbb\x00\x01")
        resp = await self.request(write)
        self.assertEqual(resp, build(2, 1, 16, b"\x00\x20\x00\x03"))
        self.assertEqual(self.device.regs[0x20], 0xAAAA)
        self.assertEqual(self.device.regs[0x22], 0x0001)

        # 3) 读回刚才写入的值
        resp = await self.request(build(3, 1, 3, b"\x00\x20\x00\x03"))
        self.assertEqual(resp[7:], bytes([3, 6, 0xAA, 0xAA, 0xBB, 0xBB, 0x00, 0x01]))

        # 4) 设备返回异常 0x02，代理原样转发
        resp = await self.request(build(4, 1, 3, b"\x00\xfe\x00\x02"))
        self.assertEqual(resp, build(4, 1, 3, b"\x02")[:7] + bytes([0x83, 0x02]))

        # 5) 写单寄存器
        resp = await self.request(build(5, 1, 6, b"\x00\x30\x12\x34"))
        self.assertEqual(self.device.regs[0x30], 0x1234)
        self.assertEqual(resp[7:], bytes([6, 0x00, 0x30, 0x12, 0x34]))

        # 6) 读线圈
        resp = await self.request(build(6, 1, 1, b"\x00\x10\x00\x02"))
        self.assertEqual(resp[7:], bytes([1, 1, 0b01]))

        # 7) 设备不响应 -> 代理超时，平台侧收不到数据，但链路继续可用
        self.writer.write(build(7, 1, 3, b"\x00\xff\x00\x01"))
        await self.writer.drain()
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(self.reader.read(4096), timeout=1.2)
        resp = await self.request(build(8, 1, 3, b"\x00\x10\x00\x01"))
        self.assertEqual(resp[7:], bytes([3, 2, 0x11, 0x11]))

        # 等待事件/落盘
        await asyncio.sleep(0.2)

        frames = [e for e in list(self.bus.ring) if e.get("type") == "frame"]
        summaries = [e["sum"] for e in frames]
        self.assertTrue(any("写多寄存器 0x0020–0x0022（3 个）" in s for s in summaries), summaries)
        self.assertTrue(any("读保持寄存器 0x0010–0x0013（4 个）" in s for s in summaries), summaries)
        self.assertTrue(any("异常应答" in s for s in summaries), summaries)
        self.assertTrue(any(e.get("rtt") is not None for e in frames))

        events = [e for e in frames if e["dir"] == "up" and e.get("vals")]
        self.assertTrue(any(e["vals"] == [0xAAAA, 0xBBBB, 0x0001] for e in events))

        # 落盘文件
        session = self.recorder.session_dir
        self.assertIsNotNone(session)
        log_path = Path(session) / "data.log"
        csv_path = Path(session) / "data.csv"
        self.assertTrue(log_path.exists())
        lines = [ln for ln in log_path.read_text(encoding="utf-8-sig").splitlines() if ln.strip()]
        self.assertGreaterEqual(len(lines), 14)
        for line in lines:
            parts = line.split("\t")
            self.assertEqual(len(parts), 3, line)
            self.assertIn(parts[1], ("平台→设备", "设备→平台"))
            bytes.fromhex(parts[2])
        self.assertTrue(csv_path.exists())
        self.assertIn("原始数据(HEX)", csv_path.read_text(encoding="utf-8-sig").splitlines()[0])

    async def test_new_client_takes_over(self) -> None:
        resp = await self.request(build(1, 1, 3, b"\x00\x10\x00\x01"))
        self.assertEqual(resp[7:], bytes([3, 2, 0x11, 0x11]))

        reader2, writer2 = await asyncio.open_connection("127.0.0.1", self.listen_port)
        try:
            # 旧连接应被关闭（读到 EOF）
            data = await asyncio.wait_for(self.reader.read(100), timeout=2)
            self.assertEqual(data, b"")
            writer2.write(build(2, 1, 3, b"\x00\x10\x00\x01"))
            await writer2.drain()
            resp = await asyncio.wait_for(reader2.read(100), timeout=3)
            self.assertEqual(resp[7:], bytes([3, 2, 0x11, 0x11]))
        finally:
            writer2.close()
            with contextlib.suppress(Exception):
                await writer2.wait_closed()


if __name__ == "__main__":
    unittest.main()
