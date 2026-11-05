"""手动校验：超时策略 = exception 时，代理是否会替设备回 0x0B。

用法（先让一个"不响应"的设备地址挂在程序上，例如 127.0.0.1:15020 没有服务）：

    python app.py --config tests/dev-config.json
    python tools/check_timeout_policy.py --base http://127.0.0.1:18080 --port 15002

脚本会把运行中实例的 timeout_policy 临时改为 exception / response_timeout=0.8，
发一条读保持寄存器请求（地址 0x00FF），期望在超时后收到 `83 0B`，随后恢复原配置。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import urllib.request


def post(base: str, path: str, obj: dict) -> dict:
    req = urllib.request.Request(
        base + path,
        data=json.dumps(obj).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def request_exception(port: int) -> bytes:
    async def run() -> bytes:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(bytes.fromhex("000100000006010300ff0001"))
            await writer.drain()
            return await asyncio.wait_for(reader.read(100), timeout=3)
        finally:
            writer.close()

    return asyncio.run(run())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8080", help="网页地址")
    parser.add_argument("--port", type=int, default=502, help="代理监听端口")
    args = parser.parse_args()

    res = post(args.base, "/api/config", {"timeout_policy": "exception", "response_timeout": 0.8})
    if not res.get("ok"):
        print("修改配置失败：", res.get("errors"))
        return 1
    print("已临时切换为 timeout_policy=exception")

    try:
        data = request_exception(args.port)
    finally:
        post(args.base, "/api/config", {"timeout_policy": "drop", "response_timeout": 5})
        print("已恢复 timeout_policy=drop")

    print("收到应答：", data.hex().upper())
    ok = len(data) >= 9 and data[7] == 0x83 and data[8] == 0x0B
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
