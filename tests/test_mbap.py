import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modbus_proxy import mbap  # noqa: E402


def f(hexstr: str) -> bytes:
    return bytes.fromhex(hexstr.replace(" ", ""))


class SplitTests(unittest.TestCase):
    def test_two_frames(self):
        a = "000100000006010300100004"
        b = "000200000006010300200002"
        frames, rest, dropped = mbap.split_frames(f(a + b))
        self.assertEqual(len(frames), 2)
        self.assertEqual(rest, b"")
        self.assertEqual(dropped, 0)
        self.assertEqual(frames[0], f(a))

    def test_partial_frame_kept(self):
        a = f("000100000006010300100004")
        frames, rest, _ = mbap.split_frames(a[:9])
        self.assertEqual(frames, [])
        self.assertEqual(rest, a[:9])

    def test_resync_after_garbage(self):
        frames, rest, dropped = mbap.split_frames(b"\xaa\xbb" + f("000100000006010300100004"))
        self.assertEqual(len(frames), 1)
        self.assertEqual(dropped, 2)
        self.assertEqual(rest, b"")

    def test_looks_like_mbap(self):
        self.assertTrue(mbap.looks_like_mbap(f("000100000006010300100004")))
        self.assertFalse(mbap.looks_like_mbap(b"\x01\x03\x00\x10\x00\x04"))
        self.assertFalse(mbap.looks_like_mbap(b""))


class DecodeTests(unittest.TestCase):
    def test_read_holding_request(self):
        d = mbap.decode(f("000100000006010300100004"), "req")
        self.assertEqual(d["fc"], 3)
        self.assertEqual(d["addr"], 0x10)
        self.assertEqual(d["qty"], 4)
        self.assertFalse(d["bad"])
        self.assertIn("读保持寄存器 0x0010–0x0013（4 个）", d["summary"])

    def test_read_holding_response_with_ref(self):
        req = mbap.decode(f("000100000006010300100004"), "req")
        resp = mbap.decode(f("00010000000B0103080001000200030004"), "resp", ref=req)
        self.assertEqual(resp["vals"], [1, 2, 3, 4])
        self.assertIn("0x0010–0x0013（4 个）", resp["summary"])
        self.assertIn("0x0001, 0x0002, 0x0003, 0x0004", resp["summary"])

    def test_write_multiple_registers(self):
        d = mbap.decode(f("00020000000D01100010000306123456780001"), "req")
        self.assertEqual(d["fc"], 16)
        self.assertEqual(d["vals"], [0x1234, 0x5678, 0x0001])
        self.assertIn("写多寄存器 0x0010–0x0012（3 个）：0x1234, 0x5678, 0x0001", d["summary"])

    def test_write_multiple_response(self):
        d = mbap.decode(f("000200000006011000100003"), "resp")
        self.assertEqual(d["addr"], 0x10)
        self.assertEqual(d["qty"], 3)
        self.assertIn("写多寄存器应答", d["summary"])

    def test_exception_response(self):
        d = mbap.decode(f("000300000003018302"), "resp")
        self.assertTrue(d["bad"])
        self.assertIn("非法数据地址", d["summary"])

    def test_write_single_coil(self):
        d = mbap.decode(f("00040000000601050010FF00"), "req")
        self.assertIn("写单线圈 0x0010 = 导通(ON)", d["summary"])

    def test_write_multiple_coils_bits(self):
        d = mbap.decode(f("000500000009010F0010000A02CD01"), "req")
        self.assertEqual(d["bits"], [1, 0, 1, 1, 0, 0, 1, 1, 1, 0])
        self.assertIn("写多线圈 0x0010–0x0019（10 个）", d["summary"])

    def test_address_offset(self):
        d = mbap.decode(f("000100000006010300100004"), "req", offset=1)
        self.assertIn("0x0011–0x0014", d["summary"])

    def test_exception_frame_builder(self):
        req = f("000200000006010300100004")
        resp = mbap.make_exception_frame(req, 11)
        self.assertEqual(resp[:2], req[:2])
        self.assertEqual(resp[7], 0x83)
        self.assertEqual(resp[8], 11)
        self.assertEqual(int.from_bytes(resp[4:6], "big"), 3)

    def test_unknown_function(self):
        d = mbap.decode(f("0006000000020141"), "req")
        self.assertEqual(d["fc"], 0x41)
        self.assertIn("未知功能码", d["summary"])


if __name__ == "__main__":
    unittest.main()
