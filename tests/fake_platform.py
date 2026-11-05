"""假平台：向代理发一串 Modbus 请求并校验应答。python tests/fake_platform.py [port]"""

import asyncio
import sys

HOST = "127.0.0.1"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 15002


def build(tid: int, uid: int, fc: int, payload: bytes) -> bytes:
    pdu = bytes([fc]) + payload
    return tid.to_bytes(2, "big") + b"\x00\x00" + (len(pdu) + 1).to_bytes(2, "big") + bytes([uid]) + pdu


async def main() -> int:
    reader = writer = None
    for _ in range(30):
        try:
            reader, writer = await asyncio.open_connection(HOST, PORT)
            break
        except OSError:
            await asyncio.sleep(0.3)
    if reader is None:
        print("连接代理失败")
        return 1

    failures = 0

    async def request(frame: bytes, timeout: float = 2.0) -> bytes | None:
        writer.write(frame)
        await writer.drain()
        try:
            return await asyncio.wait_for(reader.read(4096), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal failures
        print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  {detail}"))
        if not cond:
            failures += 1

    tid = 0
    for _ in range(6):
        tid += 1
        resp = await request(build(tid, 1, 3, b"\x00\x10\x00\x04"))
        check(f"读 4 个保持寄存器 #{tid}",
              resp is not None and resp[7:] == bytes([3, 8, 0x11, 0x11, 0x22, 0x22, 0x33, 0x33, 0x44, 0x44]),
              repr(resp))

    tid += 1
    resp = await request(build(tid, 1, 16, b"\x00\x20\x00\x03\x06\xaa\xaa\xbb\xbb\x00\x01"))
    check("写多寄存器 0x20-0x22",
          resp is not None and resp[7:] == bytes([16, 0x00, 0x20, 0x00, 0x03]),
          repr(resp))

    tid += 1
    resp = await request(build(tid, 1, 3, b"\x00\x20\x00\x03"))
    check("读回写入值",
          resp is not None and resp[7:] == bytes([3, 6, 0xAA, 0xAA, 0xBB, 0xBB, 0x00, 0x01]),
          repr(resp))

    tid += 1
    resp = await request(build(tid, 1, 6, b"\x00\x30\x12\x34"))
    check("写单寄存器", resp is not None and resp[7:] == bytes([6, 0x00, 0x30, 0x12, 0x34]), repr(resp))

    tid += 1
    resp = await request(build(tid, 1, 3, b"\x00\xfe\x00\x02"))
    check("异常应答透传", resp is not None and resp[7:] == bytes([0x83, 0x02]), repr(resp))

    tid += 1
    resp = await request(build(tid, 1, 3, b"\x00\xff\x00\x01"), timeout=1.5)
    check("设备不响应时平台收不到数据", resp is None, repr(resp))

    tid += 1
    resp = await request(build(tid, 1, 3, b"\x00\x10\x00\x01"))
    check("超时后链路仍可用", resp is not None and resp[7:] == bytes([3, 2, 0x11, 0x11]), repr(resp))

    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    print(f"\n{'全部通过' if failures == 0 else f'失败 {failures} 项'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
