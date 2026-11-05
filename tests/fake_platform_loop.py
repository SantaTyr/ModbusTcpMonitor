"""持续轮询的假平台，用于演示/截图：python tests/fake_platform_loop.py [port]"""

import asyncio
import sys

HOST = "127.0.0.1"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 15002


def build(tid: int, uid: int, fc: int, payload: bytes) -> bytes:
    pdu = bytes([fc]) + payload
    return tid.to_bytes(2, "big") + b"\x00\x00" + (len(pdu) + 1).to_bytes(2, "big") + bytes([uid]) + pdu


async def main() -> None:
    i = 0
    reader = writer = None
    while True:
        if writer is None or writer.is_closing():
            try:
                reader, writer = await asyncio.open_connection(HOST, PORT)
            except OSError:
                await asyncio.sleep(0.5)
                continue
        i += 1
        tid = i & 0xFFFF
        try:
            if i % 5 == 0:
                frame = build(tid, 1, 16, b"\x00\x20\x00\x02\x04\x12\x34\x56\x78")
            elif i % 5 == 1:
                frame = build(tid, 1, 3, b"\x00\x10\x00\x04")
            elif i % 5 == 2:
                frame = build(tid, 1, 6, b"\x00\x30" + (i & 0xFFFF).to_bytes(2, "big"))
            elif i % 5 == 3:
                frame = build(tid, 1, 3, b"\x00\x20\x00\x02")
            else:
                frame = build(tid, 1, 1, b"\x00\x10\x00\x02")
            writer.write(frame)
            await writer.drain()
            await asyncio.wait_for(reader.read(4096), timeout=1.0)
        except (ConnectionError, OSError):
            writer = None
        except asyncio.TimeoutError:
            pass
        await asyncio.sleep(0.35)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
