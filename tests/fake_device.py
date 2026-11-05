"""独立假设备，用于联调：python tests/fake_device.py 15020"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_proxy_e2e import FakeDevice  # noqa: E402


async def main(port: int) -> None:
    device = FakeDevice(port)
    await device.start()
    print(f"fake device listening 127.0.0.1:{port}", flush=True)
    async with device.server:
        await device.server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 15020))
