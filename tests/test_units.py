import struct
import zlib

import pytest

from sds800xhd_mcp import waveform as wf
from sds800xhd_mcp.config import ConfigStore, parse_socket_resource, validate
from sds800xhd_mcp.image import bmp_to_png


# ---------------------------------------------------------------- Configuration

def test_resource_is_built_from_host_and_port():
    cfg = validate({"host": "192.0.2.10", "port": 5025})
    assert cfg.resource == "TCPIP0::192.0.2.10::5025::SOCKET"


def test_resource_override_sets_host_and_port():
    cfg = validate({"resource_override": "TCPIP1::198.51.100.5::5555::SOCKET"})
    assert (cfg.host, cfg.port) == ("198.51.100.5", 5555)
    assert cfg.resource == "TCPIP1::198.51.100.5::5555::SOCKET"


def test_default_resource_override_is_normalized_away():
    cfg = validate({"resource_override": "tcpip0::1.2.3.4::5025::socket"})
    assert cfg.resource_override == ""
    assert cfg.resource == "TCPIP0::1.2.3.4::5025::SOCKET"


@pytest.mark.parametrize("data", [
    {"host": "bad host;rm"},
    {"port": 0},
    {"timeout_ms": 10},
    {"visa_backend": "@sim"},
    {"resource_override": "TCPIP0::1.2.3.4::inst0::INSTR"},
])
def test_invalid_config_is_rejected(data):
    with pytest.raises(ValueError):
        validate(data)


def test_parse_socket_resource():
    assert parse_socket_resource("TCPIP0::scope.lab::5025::SOCKET") == ("scope.lab", 5025)
    assert parse_socket_resource("USB0::1::2::INSTR") is None


def test_config_store_persists(tmp_path):
    path = tmp_path / "config.json"
    ConfigStore(path).update({"host": "192.0.2.10", "timeout_ms": 7000})
    cfg = ConfigStore(path).get()
    assert (cfg.host, cfg.timeout_ms) == ("192.0.2.10", 7000)


# ---------------------------------------------------------------- Waveform

def _preamble(comm_type=1, points=4, vdiv=0.5, offset=0.1, code_per_div=256.0, adc_bit=12,
              interval=1e-6, delay=0.0, probe=10.0) -> bytes:
    d = bytearray(b"#9000000346" + bytes(346))
    base = 11
    d[base:base + 8] = b"WAVEDESC"
    struct.pack_into("<h", d, base + 32, comm_type)
    struct.pack_into("<i", d, base + 116, points)
    struct.pack_into("<f", d, base + 156, vdiv)
    struct.pack_into("<f", d, base + 160, offset)
    struct.pack_into("<f", d, base + 164, code_per_div)
    struct.pack_into("<h", d, base + 172, adc_bit)
    struct.pack_into("<f", d, base + 176, interval)
    struct.pack_into("<d", d, base + 180, delay)
    struct.pack_into("<f", d, base + 328, probe)
    return bytes(d)


def test_parse_preamble_applies_probe_factor():
    pre = wf.parse_preamble(_preamble())
    assert pre.points == 4 and pre.adc_bit == 12 and pre.bytes_per_point == 2
    assert pre.vdiv == pytest.approx(5.0)
    assert pre.offset == pytest.approx(1.0)


def test_parse_preamble_rejects_garbage():
    with pytest.raises(ValueError):
        wf.parse_preamble(b"#9000000010" + bytes(10))


def test_decode_12bit_twos_complement_in_word():
    pre = wf.parse_preamble(_preamble())
    raw = struct.pack("<4H", 0x0001, 0x07FF, 0x0800, 0x0FFF)
    assert wf.decode_samples(raw, pre) == [1, 2047, -2048, -1]


def test_decode_sign_extended_word():
    pre = wf.parse_preamble(_preamble())
    raw = struct.pack("<3h", 100, -100, -2048)
    assert wf.decode_samples(raw, pre) == [100, -100, -2048]


def test_decode_byte():
    pre = wf.parse_preamble(_preamble(comm_type=0, adc_bit=8))
    assert wf.decode_samples(bytes([1, 0xFF, 0x80]), pre) == [1, -1, -128]


def test_codes_to_volts_and_time():
    pre = wf.parse_preamble(_preamble(probe=1.0, offset=0.0))
    assert wf.codes_to_volts([256, -512], pre) == pytest.approx([0.5, -1.0])
    assert wf.time_of_first_point(pre, tdiv=1e-3) == pytest.approx(-5e-3)


# ---------------------------------------------------------------- Image

def _bmp_rgb565(width=3, height=2) -> bytes:
    masks = (0xF800, 0x07E0, 0x001F)
    row_bytes = ((width * 16 + 31) // 32) * 4
    pixels = bytearray()
    for y in range(height):
        row = struct.pack(f"<{width}H", *([0xF800] * width))  # red
        pixels += row + bytes(row_bytes - len(row))
    header_size = 14 + 40 + 12
    dib = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 16, 3, len(pixels), 0, 0, 0, 0)
    file_header = b"BM" + struct.pack("<IHHI", header_size + len(pixels), 0, 0, header_size)
    return file_header + dib + struct.pack("<III", *masks) + bytes(pixels)


def test_bmp_rgb565_to_png():
    png = bmp_to_png(_bmp_rgb565())
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", png[16:24])
    assert (width, height) == (3, 2)
    idat_len = struct.unpack(">I", png[33:37])[0]
    rows = zlib.decompress(png[41:41 + idat_len])
    assert rows[:4] == bytes([0, 255, 0, 0])  # filter byte + first pixel red
