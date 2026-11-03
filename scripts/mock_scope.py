"""Simple SCPI simulator of an SDS800X HD for testing without hardware.

    python scripts/mock_scope.py [--port 5025]

Answers *IDN?, *OPC?, channel/trigger queries, :WAVeform:PREamble?/DATA?
(1 kHz sine, 12 bit) and :PRINt? PNG. Unknown queries return "1.00E+00".
"""

from __future__ import annotations

import argparse
import asyncio
import math
import struct
import zlib

POINTS = 20_000
VDIV, OFFSET, ADC_BIT = 0.5, 0.0, 12
INTERVAL = 1e-7


def preamble(width: str) -> bytes:
    d = bytearray(346)
    d[0:8] = b"WAVEDESC"
    struct.pack_into("<h", d, 32, 1 if width == "WORD" else 0)
    struct.pack_into("<i", d, 116, POINTS)
    struct.pack_into("<f", d, 156, VDIV)
    struct.pack_into("<f", d, 160, OFFSET)
    struct.pack_into("<f", d, 164, 256.0 if width == "WORD" else 25.0)
    struct.pack_into("<h", d, 172, ADC_BIT)
    struct.pack_into("<f", d, 176, INTERVAL)
    struct.pack_into("<d", d, 180, 0.0)
    struct.pack_into("<f", d, 328, 1.0)
    return bytes(d)


def samples(width: str, start: int, count: int, step: int) -> bytes:
    out = bytearray()
    for i in range(start, min(POINTS, start + count * step), step):
        v = 1.2 * math.sin(2 * math.pi * 1000 * i * INTERVAL)  # 2.4 Vpp
        if width == "WORD":
            code = round(v / VDIV * 256.0)
            out += struct.pack("<H", code & 0x0FFF)  # 12-Bit-Zweierkomplement
        else:
            out += struct.pack("<b", round(v / VDIV * 25.0))
    return bytes(out)


def png() -> bytes:
    w, h = 80, 48
    rows = b"".join(b"\x00" + bytes((20, 20 + y * 4, 60)) * w for y in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def block(data: bytes) -> bytes:
    n = str(len(data)).zfill(9)
    return b"#9" + n.encode() + data + b"\n\n"


class State:
    def __init__(self):
        self.width, self.start, self.points, self.interval = "BYTE", 0, 0, 1
        self.values = {
            ":TRIGGER:STATUS?": "Auto", ":ACQUIRE:SRATE?": "1.00E+07", ":ACQUIRE:MDEPTH?": "20k",
            ":TIMEBASE:SCALE?": "2.00E-04", ":TRIGGER:MODE?": "AUTO", ":TRIGGER:TYPE?": "EDGE",
            ":MEASURE:ADVANCED:LINENUMBER?": "5", ":WAVEFORM:MAXPOINT?": "5000000",
        }


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    st = State()
    while line := await reader.readline():
        cmd = line.decode().strip()
        if not cmd:
            continue
        up = cmd.upper()
        print("<<", cmd)
        resp: bytes | None = None
        if up == "*IDN?":
            resp = b"Siglent Technologies,SDS824X HD,MOCK000001,3.8.12.1.1.3.8\n"
        elif up == "*OPC?":
            resp = b"1\n"
        elif up.startswith(":WAVEFORM:WIDTH "):
            st.width = up.split()[1]
        elif up.startswith(":WAVEFORM:START "):
            st.start = int(up.split()[1])
        elif up.startswith(":WAVEFORM:POINT "):
            st.points = int(up.split()[1])
        elif up.startswith(":WAVEFORM:INTERVAL "):
            st.interval = int(up.split()[1])
        elif up == ":WAVEFORM:PREAMBLE?":
            resp = block(preamble(st.width))
        elif up == ":WAVEFORM:DATA?":
            count = st.points or POINTS
            resp = block(samples(st.width, st.start, count, st.interval))
        elif up == ":PRINT? PNG":
            resp = png() + b"\n"
        elif up.endswith("?") or "?" in up:
            resp = (st.values.get(up, "1.00E+00") + "\n").encode()
        elif " " in up:
            key, val = cmd.split(" ", 1)
            st.values[key.upper() + "?"] = val
        if resp is not None:
            writer.write(resp)
            await writer.drain()
    writer.close()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5025)
    args = parser.parse_args()
    server = await asyncio.start_server(handle, "127.0.0.1", args.port)
    print(f"Mock SDS800X HD on 127.0.0.1:{args.port}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
