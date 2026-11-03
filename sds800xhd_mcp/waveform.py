"""Parsing of the Siglent WAVEDESC preamble and conversion of the raw data."""

from __future__ import annotations

import struct
from dataclasses import dataclass

# SDS800X HD: 10 horizontal divisions
HORIZONTAL_DIVS = 10


@dataclass
class Preamble:
    comm_type: int  # 0 = BYTE, 1 = WORD
    points: int
    vdiv: float
    offset: float
    code_per_div: float
    adc_bit: int
    interval: float  # sample interval in s
    delay: float  # horizontal offset in s
    probe: float

    @property
    def bytes_per_point(self) -> int:
        return 2 if self.comm_type == 1 else 1


def parse_preamble(block: bytes) -> Preamble:
    base = block.find(b"WAVEDESC")
    if base < 0 or len(block) - base < 346:
        raise ValueError(f"Invalid waveform preamble ({len(block)} bytes)")

    def i16(off: int) -> int:
        return struct.unpack_from("<h", block, base + off)[0]

    def i32(off: int) -> int:
        return struct.unpack_from("<i", block, base + off)[0]

    def f32(off: int) -> float:
        return struct.unpack_from("<f", block, base + off)[0]

    def f64(off: int) -> float:
        return struct.unpack_from("<d", block, base + off)[0]

    probe = f32(328) or 1.0
    return Preamble(
        comm_type=i16(32),
        points=i32(116),
        # vdiv/offset in the preamble are normalized to the probe
        vdiv=f32(156) * probe,
        offset=f32(160) * probe,
        code_per_div=f32(164),
        adc_bit=i16(172),
        interval=f32(176),
        delay=f64(180),
        probe=probe,
    )


def decode_samples(raw: bytes, pre: Preamble) -> list[int]:
    """Convert raw data to signed ADC codes."""
    if pre.bytes_per_point == 1:
        return list(struct.unpack(f"<{len(raw)}b", raw))

    n = len(raw) // 2
    unsigned = struct.unpack(f"<{n}H", raw[: n * 2])
    full = 1 << pre.adc_bit
    if any(v >= full for v in unsigned):
        # Already transferred as 16-bit two's complement
        return [v - 0x10000 if v >= 0x8000 else v for v in unsigned]
    # adc_bit-wide two's complement in a 16-bit container
    half = full >> 1
    return [v - full if v >= half else v for v in unsigned]


def codes_to_volts(codes: list[int], pre: Preamble) -> list[float]:
    scale = pre.vdiv / pre.code_per_div
    return [c * scale - pre.offset for c in codes]


def time_of_first_point(pre: Preamble, tdiv: float) -> float:
    return -pre.delay - tdiv * HORIZONTAL_DIVS / 2
