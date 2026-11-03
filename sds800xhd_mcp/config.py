"""Persistent connection parameters for the oscilloscope (JSON file)."""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path

DEFAULT_SCPI_PORT = 5025

# TCPIP[board]::host::port::SOCKET
_SOCKET_RESOURCE = re.compile(r"^TCPIP(\d*)::([^:]+)::(\d+)::SOCKET$", re.IGNORECASE)


@dataclass
class ScopeConfig:
    host: str = ""
    port: int = DEFAULT_SCPI_PORT
    # Empty = build from host/port, otherwise this VISA string is used
    resource_override: str = ""
    # "@py" = PyVISA-py (no driver needed), "" = system VISA (NI-VISA, Keysight IO Libraries, ...)
    visa_backend: str = "@py"
    timeout_ms: int = 5000
    auto_connect: bool = True

    @property
    def resource(self) -> str:
        if self.resource_override.strip():
            return self.resource_override.strip()
        if not self.host:
            return ""
        return f"TCPIP0::{self.host}::{self.port}::SOCKET"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["resource"] = self.resource
        return d


def parse_socket_resource(resource: str) -> tuple[str, int] | None:
    """Splits 'TCPIP0::192.168.1.10::5025::SOCKET' into (host, port)."""
    m = _SOCKET_RESOURCE.match(resource.strip())
    if not m:
        return None
    return m.group(2), int(m.group(3))


def validate(data: dict, base: ScopeConfig | None = None) -> ScopeConfig:
    """Builds a validated configuration from (partial) input data."""
    cfg = ScopeConfig(**asdict(base)) if base else ScopeConfig()
    known = {f.name for f in fields(ScopeConfig)}
    for key, value in data.items():
        if key in known:
            setattr(cfg, key, value)

    cfg.host = str(cfg.host).strip()
    if cfg.host and not re.fullmatch(r"[A-Za-z0-9.\-_\[\]:]+", cfg.host):
        raise ValueError(f"Invalid host: {cfg.host!r}")
    try:
        cfg.port = int(cfg.port)
        cfg.timeout_ms = int(cfg.timeout_ms)
    except (TypeError, ValueError) as exc:
        raise ValueError("Port and timeout must be numbers") from exc
    if not 1 <= cfg.port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    if not 500 <= cfg.timeout_ms <= 120_000:
        raise ValueError("Timeout must be between 500 and 120000 ms")
    cfg.auto_connect = bool(cfg.auto_connect)
    cfg.visa_backend = str(cfg.visa_backend or "").strip()
    if cfg.visa_backend not in ("@py", ""):
        raise ValueError("VISA backend must be '@py' or '' (system VISA)")

    cfg.resource_override = str(cfg.resource_override or "").strip()
    if cfg.resource_override:
        parsed = parse_socket_resource(cfg.resource_override)
        if parsed is None:
            raise ValueError("VISA resource must have the form TCPIP0::<host>::<port>::SOCKET")
        # Keep host/port in sync so the UI shows consistent values
        cfg.host, cfg.port = parsed
        if cfg.resource_override.upper() == f"TCPIP0::{cfg.host}::{cfg.port}::SOCKET".upper():
            cfg.resource_override = ""
    return cfg


class ConfigStore:
    """Thread-safe access to the configuration file."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._config = self._load()

    def _load(self) -> ScopeConfig:
        cfg = ScopeConfig()
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                cfg = validate(data)
            except (OSError, ValueError, TypeError) as exc:
                print(f"Warning: configuration {self.path} unreadable ({exc}), using defaults")
        # Environment variables only as initial values if nothing has been saved yet
        elif os.environ.get("SIGLENT_IP"):
            cfg = validate({
                "host": os.environ["SIGLENT_IP"],
                "port": os.environ.get("SIGLENT_PORT", DEFAULT_SCPI_PORT),
            })
        return cfg

    def get(self) -> ScopeConfig:
        with self._lock:
            return ScopeConfig(**asdict(self._config))

    def update(self, data: dict) -> ScopeConfig:
        with self._lock:
            cfg = validate(data, self._config)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            payload = {k: v for k, v in asdict(cfg).items()}
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
            self._config = cfg
            return ScopeConfig(**asdict(cfg))
