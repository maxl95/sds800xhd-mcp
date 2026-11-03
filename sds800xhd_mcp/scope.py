"""SCPI connection to the SDS800X HD via VISA (TCPIP::<host>::<port>::SOCKET)."""

from __future__ import annotations

import logging
import struct
import threading
import time
from contextlib import contextmanager
from typing import Iterator

import pyvisa
from pyvisa.errors import VisaIOError

from .config import ConfigStore, ScopeConfig

log = logging.getLogger(__name__)

_WHITESPACE = b"\r\n\t "
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class ScopeError(RuntimeError):
    pass


class Scope:
    """A single, serialized VISA session to the oscilloscope.

    All access goes through an RLock so that concurrent MCP calls do not steal
    each other's responses on the socket. Multi-step sequences (e.g. waveform
    download) hold the lock throughout via `session()`.
    """

    def __init__(self, store: ConfigStore):
        self.store = store
        self._lock = threading.RLock()
        self._rm: pyvisa.ResourceManager | None = None
        self._rm_backend: str | None = None
        self._inst = None
        self._resource = ""
        self.idn = ""
        self.last_error = ""
        self.connected_since: float | None = None

    # ------------------------------------------------------------------ Connection

    def is_connected(self) -> bool:
        return self._inst is not None

    def status(self) -> dict:
        return {
            "connected": self.is_connected(),
            "resource": self._resource if self.is_connected() else "",
            "idn": self.idn if self.is_connected() else "",
            "connected_since": self.connected_since,
            "last_error": self.last_error,
        }

    def connect(self, cfg: ScopeConfig | None = None) -> str:
        cfg = cfg or self.store.get()
        if not cfg.resource:
            raise ScopeError("No oscilloscope configured. Enter the host/IP in the web UI.")
        with self._lock:
            self._close_locked()
            try:
                rm = self._resource_manager(cfg.visa_backend)
                inst = rm.open_resource(cfg.resource, open_timeout=cfg.timeout_ms)
                inst.timeout = cfg.timeout_ms
                inst.read_termination = "\n"
                inst.write_termination = "\n"
                self._inst = inst
                self._resource = cfg.resource
                # Discard any pending bytes (greeting etc.)
                self._drain(0.2)
                idn = inst.query("*IDN?").strip()
            except Exception as exc:
                self._close_locked()
                self.last_error = f"Connection to {cfg.resource} failed: {exc}"
                raise ScopeError(self.last_error) from exc
            self.idn = idn
            self.last_error = ""
            self.connected_since = time.time()
            log.info("Connected to %s: %s", cfg.resource, idn)
            return idn

    def disconnect(self) -> None:
        with self._lock:
            self._close_locked()

    def _resource_manager(self, backend: str) -> pyvisa.ResourceManager:
        if self._rm is None or self._rm_backend != backend:
            if self._rm is not None:
                try:
                    self._rm.close()
                except Exception:
                    pass
            try:
                self._rm = pyvisa.ResourceManager(backend) if backend else pyvisa.ResourceManager()
            except (OSError, ValueError) as exc:
                raise ScopeError(
                    "System VISA not found. Install NI-VISA or choose backend '@py'."
                    if not backend else f"VISA backend {backend!r} not available: {exc}"
                ) from exc
            self._rm_backend = backend
        return self._rm

    def _close_locked(self) -> None:
        if self._inst is not None:
            try:
                self._inst.close()
            except Exception:
                pass
        self._inst = None
        self.connected_since = None

    @contextmanager
    def session(self) -> Iterator["Scope"]:
        """Hold the lock and (re)connect automatically if needed."""
        with self._lock:
            if self._inst is None:
                cfg = self.store.get()
                if not cfg.auto_connect or not cfg.resource:
                    raise ScopeError(
                        "Not connected to the oscilloscope. Call 'connect' or connect in the web UI."
                    )
                self.connect(cfg)
            try:
                yield self
            except (VisaIOError, OSError) as exc:
                # After a timeout/socket error the response stream is out of sync:
                # close the session, the next call reconnects.
                # (pyvisa-py reports dropped sockets as a raw OSError.)
                self.last_error = f"Connection error: {exc}"
                self._close_locked()
                raise ScopeError(f"{self.last_error} (reconnecting on the next call)") from exc

    # ------------------------------------------------------------------ SCPI I/O
    # Use the following methods only inside session().

    def write(self, cmd: str) -> None:
        log.debug(">> %s", cmd)
        self._inst.write(cmd)

    def query(self, cmd: str, timeout_ms: int | None = None) -> str:
        log.debug(">> %s", cmd)
        with self._timeout(timeout_ms):
            resp = self._inst.query(cmd)
            # Skip leftovers of a previous binary block (single \n)
            while resp.strip() == "":
                resp = self._inst.read()
        log.debug("<< %s", resp.strip()[:200])
        return resp.strip()

    def opc(self) -> None:
        """Waits until all previous commands have been processed."""
        self.query("*OPC?")

    def query_binary(self, cmd: str, timeout_ms: int = 30_000) -> bytes:
        """Sends a query and reads a binary response.

        Supports IEEE 488.2 blocks (#<n><len><data>) as well as raw PNG/BMP data
        as returned by :PRINt? and SCDP.
        """
        log.debug(">> %s (binary)", cmd)
        self._inst.write(cmd)
        with self._timeout(timeout_ms):
            first = self._read(1)
            while first in _WHITESPACE:
                first = self._read(1)

            if first == b"#":
                ndigits = int(self._read(1))
                if ndigits == 0:
                    raise ScopeError("Indefinite block length (#0) is not supported")
                length = int(self._read(ndigits))
                data = self._read(length)
            elif first == b"B":
                head = first + self._read(5)
                if head[:2] != b"BM":
                    raise ScopeError(f"Unexpected response: {head!r}")
                size = struct.unpack_from("<I", head, 2)[0]
                data = head + self._read(size - len(head))
            elif first == _PNG_MAGIC[:1]:
                data = bytearray(first + self._read(7))
                if bytes(data) != _PNG_MAGIC:
                    raise ScopeError("Invalid PNG signature")
                while True:
                    chunk_head = self._read(8)
                    (clen,) = struct.unpack(">I", chunk_head[:4])
                    data += chunk_head + self._read(clen + 4)
                    if chunk_head[4:8] == b"IEND":
                        break
                data = bytes(data)
            else:
                rest = first + self._drain(0.3)
                raise ScopeError(f"Unexpected response to {cmd}: {rest[:80]!r}")
        # Discard trailing line endings
        self._drain(0.1, max_bytes=2)
        return data

    def _read(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            buf += self._inst.read_bytes(n - len(buf), break_on_termchar=False)
        return bytes(buf)

    def _drain(self, seconds: float, max_bytes: int = 1 << 20) -> bytes:
        """Reads pending bytes until nothing arrives for `seconds`."""
        got = bytearray()
        old = self._inst.timeout
        self._inst.timeout = int(seconds * 1000)
        try:
            while len(got) < max_bytes:
                got += self._inst.read_bytes(1, break_on_termchar=False)
        except VisaIOError:
            pass
        finally:
            self._inst.timeout = old
        return bytes(got)

    @contextmanager
    def _timeout(self, timeout_ms: int | None):
        if timeout_ms is None:
            yield
            return
        old = self._inst.timeout
        self._inst.timeout = timeout_ms
        try:
            yield
        finally:
            self._inst.timeout = old
