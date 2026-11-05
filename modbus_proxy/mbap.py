"""Modbus TCP 报文分帧、MBAP/PDU 解码与中文摘要。

本模块只做"读"：把字节解释成人能看懂的字段，绝不修改转发内容。
"""

from __future__ import annotations

from typing import Any

HEADER = 7                 # TID(2) + PID(2) + LEN(2) + UID(1)
MAX_LENGTH = 254           # 长度字段上限：UID(1) + PDU(253)
MAX_ADU = 6 + MAX_LENGTH

FC_NAMES: dict[int, str] = {
    1: "读线圈",
    2: "读离散输入",
    3: "读保持寄存器",
    4: "读输入寄存器",
    5: "写单线圈",
    6: "写单寄存器",
    15: "写多线圈",
    16: "写多寄存器",
    23: "读写多寄存器",
    43: "读设备标识",
}

EXCEPTION_NAMES: dict[int, str] = {
    1: "非法功能码",
    2: "非法数据地址",
    3: "非法数据值",
    4: "从站设备故障",
    5: "确认",
    6: "从站设备忙",
    8: "存储奇偶校验错",
    10: "网关路径不可用",
    11: "网关目标设备无响应",
}


def split_frames(buf: bytes) -> tuple[list[bytes], bytes, int]:
    """从 TCP 字节流缓冲中切出完整 ADU。

    返回 (完整帧列表, 剩余字节, 因帧头异常被丢弃的字节数)。
    帧头非法时会向后寻找下一个可能的帧头，保证不解包错位后一直卡死。
    """
    frames: list[bytes] = []
    pos = 0
    dropped = 0
    size = len(buf)
    while size - pos >= HEADER:
        pid = int.from_bytes(buf[pos + 2:pos + 4], "big")
        length = int.from_bytes(buf[pos + 4:pos + 6], "big")
        if pid != 0 or not (2 <= length <= MAX_LENGTH):
            nxt = _resync(buf, pos + 1)
            if nxt is None:
                if size - pos > 4096:      # 长时间无法同步，丢弃避免缓冲膨胀
                    dropped += size - pos
                    pos = size
                break
            dropped += nxt - pos
            pos = nxt
            continue
        end = pos + 6 + length
        if end > size:
            break
        frames.append(buf[pos:end])
        pos = end
    return frames, buf[pos:], dropped


def _resync(buf: bytes, start: int) -> int | None:
    """寻找下一个形如 (PID=0, 2<=LEN<=254) 的帧头位置。"""
    limit = len(buf) - HEADER
    for i in range(start, limit + 1):
        if buf[i + 2] == 0 and buf[i + 3] == 0:
            length = int.from_bytes(buf[i + 4:i + 6], "big")
            if 2 <= length <= MAX_LENGTH:
                return i
    return None


def looks_like_mbap(buf: bytes) -> bool:
    """判断字节流是否像 Modbus TCP（PID=0 且长度合法）。"""
    if len(buf) < HEADER:
        return False
    pid = int.from_bytes(buf[2:4], "big")
    length = int.from_bytes(buf[4:6], "big")
    return pid == 0 and 2 <= length <= MAX_LENGTH


def u16(data: bytes, pos: int) -> int:
    return (data[pos] << 8) | data[pos + 1]


def hex4(value: int) -> str:
    return f"0x{value:04X}"


def hex2(value: int) -> str:
    return f"0x{value:02X}"


def _range_text(addr: int, qty: int, offset: int) -> str:
    start = addr + offset
    if qty <= 1:
        return hex4(start)
    return f"{hex4(start)}–{hex4(start + qty - 1)}"


def _values_text(values: list[int], limit: int = 8) -> str:
    shown = ", ".join(hex4(v) for v in values[:limit])
    if len(values) > limit:
        shown += f", … 共 {len(values)} 个"
    return shown


def _bits_text(bits: list[int], limit: int = 16) -> str:
    shown = ",".join("1" if b else "0" for b in bits[:limit])
    if len(bits) > limit:
        shown += f", … 共 {len(bits)} 个"
    return shown


def decode(frame: bytes, direction: str, ref: dict | None = None, offset: int = 0) -> dict[str, Any]:
    """解析一帧 ADU。

    direction: "req" 表示平台->设备， "resp" 表示设备->平台。
    ref: 对应的请求解析结果，用于给应答补充起始地址/数量。
    """
    tid = int.from_bytes(frame[0:2], "big")
    pid = int.from_bytes(frame[2:4], "big")
    length = int.from_bytes(frame[4:6], "big")
    uid = frame[6]
    pdu = frame[7:]
    out: dict[str, Any] = {
        "tid": tid, "pid": pid, "uid": uid, "fc": -1, "fc_name": "",
        "summary": "", "fields": [], "vals": None, "bits": None,
        "addr": None, "qty": None, "vals_addr": None, "bad": False, "error": None,
    }
    if not pdu:
        out["summary"] = "空报文"
        out["bad"] = True
        out["error"] = "PDU 为空"
        return out

    fc = pdu[0]
    out["fc"] = fc
    out["fields"].append(("功能码", f"{fc} (0x{fc:02X})"))

    if fc & 0x80:
        base_fc = fc & 0x7F
        out["fc_name"] = FC_NAMES.get(base_fc, "")
        out["bad"] = True
        if len(pdu) >= 2:
            code = pdu[1]
            name = EXCEPTION_NAMES.get(code, "未知异常")
            out["summary"] = f"异常应答(FC {hex2(base_fc)})：{name} ({hex2(code)})"
            out["fields"].append(("异常码", f"{code} (0x{code:02X}) {name}"))
        else:
            out["summary"] = "异常应答（缺少异常码）"
            out["error"] = "异常应答长度不足"
        return out

    name = FC_NAMES.get(fc, "")
    out["fc_name"] = name
    try:
        if direction == "req":
            _decode_request(pdu, fc, out, offset)
        else:
            _decode_response(pdu, fc, out, ref, offset)
    except (IndexError, ValueError) as exc:
        out["bad"] = True
        out["error"] = f"解析失败：{exc}"
        out["summary"] = out["summary"] or f"{name or '未知功能码'}（数据不完整）"
    if out["vals_addr"] is None:
        out["vals_addr"] = out["addr"]
    return out


# --------------------------------------------------------------------------- #
# 请求
# --------------------------------------------------------------------------- #

def _need(pdu: bytes, size: int) -> None:
    if len(pdu) < size:
        raise ValueError(f"长度不足，需要 {size} 字节，实际 {len(pdu)}")


def _decode_request(pdu: bytes, fc: int, out: dict, offset: int) -> None:
    fields = out["fields"]
    if fc in (1, 2):
        _need(pdu, 5)
        addr, qty = u16(pdu, 1), u16(pdu, 3)
        out["addr"], out["qty"] = addr, qty
        kind = "读线圈" if fc == 1 else "读离散输入"
        out["summary"] = f"{kind} {_range_text(addr, qty, offset)}（{qty} 个）"
        fields += [("起始地址", hex4(addr + offset)), ("数量", str(qty))]
        if not (1 <= qty <= 2000):
            out["bad"] = True
            fields.append(("校验", "数量超出 1-2000"))
    elif fc in (3, 4):
        _need(pdu, 5)
        addr, qty = u16(pdu, 1), u16(pdu, 3)
        out["addr"], out["qty"] = addr, qty
        kind = "读保持寄存器" if fc == 3 else "读输入寄存器"
        out["summary"] = f"{kind} {_range_text(addr, qty, offset)}（{qty} 个）"
        fields += [("起始地址", hex4(addr + offset)), ("数量", str(qty))]
        if not (1 <= qty <= 125):
            out["bad"] = True
            fields.append(("校验", "数量超出 1-125"))
    elif fc == 5:
        _need(pdu, 5)
        addr, value = u16(pdu, 1), u16(pdu, 3)
        out["addr"], out["qty"] = addr, 1
        state = "导通(ON)" if value == 0xFF00 else ("断开(OFF)" if value == 0x0000 else f"非法值 0x{value:04X}")
        if value not in (0xFF00, 0x0000):
            out["bad"] = True
        out["summary"] = f"写单线圈 {hex4(addr + offset)} = {state}"
        fields += [("地址", hex4(addr + offset)), ("值", f"0x{value:04X} {state}")]
    elif fc == 6:
        _need(pdu, 5)
        addr, value = u16(pdu, 1), u16(pdu, 3)
        out["addr"], out["qty"] = addr, 1
        out["summary"] = f"写单寄存器 {hex4(addr + offset)} = {hex4(value)}"
        fields += [("地址", hex4(addr + offset)), ("值", f"{hex4(value)} ({value})")]
    elif fc == 15:
        _need(pdu, 6)
        addr, qty, bcnt = u16(pdu, 1), u16(pdu, 3), pdu[5]
        out["addr"], out["qty"] = addr, qty
        need = (qty + 7) // 8
        if bcnt != need or len(pdu) < 6 + bcnt:
            out["bad"] = True
            out["error"] = f"字节数不符：声明 {bcnt}，应为 {need}"
        bits = _unpack_bits(pdu[6:6 + bcnt], qty)
        out["bits"] = bits
        out["summary"] = (f"写多线圈 {_range_text(addr, qty, offset)}（{qty} 个）："
                          f"{_bits_text(bits)}")
        fields += [("起始地址", hex4(addr + offset)), ("数量", str(qty)),
                   ("字节数", str(bcnt)), ("值", _bits_text(bits, 64))]
    elif fc == 16:
        _need(pdu, 6)
        addr, qty, bcnt = u16(pdu, 1), u16(pdu, 3), pdu[5]
        out["addr"], out["qty"] = addr, qty
        if bcnt != qty * 2 or len(pdu) < 6 + bcnt:
            out["bad"] = True
            out["error"] = f"字节数不符：声明 {bcnt}，应为 {qty * 2}"
        values = [_u16_at(pdu, 6 + i * 2) for i in range(min(qty, max(0, (len(pdu) - 6) // 2)))]
        out["vals"] = values
        out["summary"] = (f"写多寄存器 {_range_text(addr, qty, offset)}（{qty} 个）："
                          f"{_values_text(values)}")
        fields += [("起始地址", hex4(addr + offset)), ("数量", str(qty)),
                   ("字节数", str(bcnt)), ("写入值", _values_text(values, 64))]
    elif fc == 23:
        _need(pdu, 10)
        raddr, rqty, waddr, wqty, bcnt = u16(pdu, 1), u16(pdu, 3), u16(pdu, 5), u16(pdu, 7), pdu[9]
        out["addr"], out["qty"] = raddr, rqty
        out["vals_addr"] = waddr
        if bcnt != wqty * 2 or len(pdu) < 10 + bcnt:
            out["bad"] = True
            out["error"] = f"字节数不符：声明 {bcnt}，应为 {wqty * 2}"
        values = [_u16_at(pdu, 10 + i * 2) for i in range(min(wqty, max(0, (len(pdu) - 10) // 2)))]
        out["vals"] = values
        out["summary"] = (f"读写寄存器：读 {_range_text(raddr, rqty, offset)}（{rqty} 个），"
                          f"写 {_range_text(waddr, wqty, offset)}（{wqty} 个）= {_values_text(values)}")
        fields += [("读起始地址", hex4(raddr + offset)), ("读数量", str(rqty)),
                   ("写起始地址", hex4(waddr + offset)), ("写数量", str(wqty)),
                   ("写入值", _values_text(values, 64))]
    elif fc == 43:
        out["summary"] = "读设备标识"
        fields.append(("说明", "设备标识读取请求"))
        if len(pdu) >= 2:
            fields.append(("MEI 类型", hex2(pdu[1])))
    else:
        out["summary"] = f"未知功能码 {hex2(fc)}"
        out["bad"] = True


# --------------------------------------------------------------------------- #
# 应答
# --------------------------------------------------------------------------- #

def _decode_response(pdu: bytes, fc: int, out: dict, ref: dict | None, offset: int) -> None:
    fields = out["fields"]
    name = FC_NAMES.get(fc, "")
    if fc in (1, 2, 3, 4):
        _need(pdu, 2)
        bcnt = pdu[1]
        payload = pdu[2:2 + bcnt]
        if len(payload) < bcnt:
            out["bad"] = True
            out["error"] = f"数据不足：声明 {bcnt} 字节，实际 {len(payload)}"
        addr = ref.get("addr") if ref else None
        qty = ref.get("qty") if ref else None
        if fc in (3, 4):
            values = [_u16_at(payload, i * 2) for i in range(len(payload) // 2)]
            out["vals"] = values
            if addr is not None and qty is not None:
                out["addr"], out["qty"] = addr, qty
                head = f"读{'保持' if fc == 3 else '输入'}寄存器应答 {_range_text(addr, qty, offset)}（{qty} 个）"
            else:
                head = f"读{'保持' if fc == 3 else '输入'}寄存器应答（{len(values)} 个值）"
            out["summary"] = f"{head}：{_values_text(values)}"
            fields += [("字节数", str(bcnt)), ("数值", _values_text(values, 64))]
        else:
            bits = _unpack_bits(payload, qty if qty else len(payload) * 8)
            out["bits"] = bits
            if addr is not None and qty is not None:
                out["addr"], out["qty"] = addr, qty
                head = f"读{'线圈' if fc == 1 else '离散输入'}应答 {_range_text(addr, qty, offset)}"
            else:
                head = f"读{'线圈' if fc == 1 else '离散输入'}应答"
            out["summary"] = f"{head}：{_bits_text(bits)}"
            fields += [("字节数", str(bcnt)), ("位值", _bits_text(bits, 64))]
    elif fc == 5:
        _need(pdu, 5)
        addr, value = u16(pdu, 1), u16(pdu, 3)
        out["addr"], out["qty"] = addr, 1
        state = "导通(ON)" if value == 0xFF00 else ("断开(OFF)" if value == 0x0000 else f"0x{value:04X}")
        out["summary"] = f"写单线圈应答 {hex4(addr + offset)}：{state}"
        fields.append(("值", state))
    elif fc == 6:
        _need(pdu, 5)
        addr, value = u16(pdu, 1), u16(pdu, 3)
        out["addr"], out["qty"] = addr, 1
        out["summary"] = f"写单寄存器应答 {hex4(addr + offset)} = {hex4(value)}"
        fields.append(("值", f"{hex4(value)} ({value})"))
    elif fc == 15:
        _need(pdu, 5)
        addr, qty = u16(pdu, 1), u16(pdu, 3)
        out["addr"], out["qty"] = addr, qty
        out["summary"] = f"写多线圈应答：成功 {_range_text(addr, qty, offset)}（{qty} 个）"
        fields += [("起始地址", hex4(addr + offset)), ("数量", str(qty))]
    elif fc == 16:
        _need(pdu, 5)
        addr, qty = u16(pdu, 1), u16(pdu, 3)
        out["addr"], out["qty"] = addr, qty
        out["summary"] = f"写多寄存器应答：成功 {_range_text(addr, qty, offset)}（{qty} 个）"
        fields += [("起始地址", hex4(addr + offset)), ("数量", str(qty))]
    elif fc == 23:
        _need(pdu, 2)
        bcnt = pdu[1]
        payload = pdu[2:2 + bcnt]
        values = [_u16_at(payload, i * 2) for i in range(len(payload) // 2)]
        out["vals"] = values
        raddr = ref.get("raddr") if ref else None
        rqty = ref.get("rqty") if ref else None
        out["vals_addr"] = raddr
        prefix = ""
        if raddr is not None and rqty is not None:
            prefix = _range_text(raddr, rqty, offset) + " "
        out["summary"] = f"读写寄存器应答 读 {prefix}：{_values_text(values)}"
        fields += [("字节数", str(bcnt)), ("读出值", _values_text(values, 64))]
    elif fc == 43:
        out["summary"] = "读设备标识应答"
        fields.append(("数据", pdu[2:].hex(" ").upper()))
    else:
        out["summary"] = f"{name or '未知功能码'} 应答"
        out["bad"] = True


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

def _u16_at(data: bytes, pos: int) -> int:
    if pos + 1 < len(data):
        return (data[pos] << 8) | data[pos + 1]
    return 0


def _unpack_bits(data: bytes, qty: int) -> list[int]:
    bits: list[int] = []
    for byte in data:
        for bit in range(8):
            if len(bits) >= qty:
                break
            bits.append((byte >> bit) & 1)
    return bits


def make_exception_frame(request: bytes, code: int) -> bytes:
    """按请求头生成 Modbus 异常应答（用于设备超时策略 exception）。"""
    tid = request[0:2]
    uid = request[6:7]
    fc = request[7] | 0x80
    return tid + b"\x00\x00" + (3).to_bytes(2, "big") + uid + bytes([fc, code])
