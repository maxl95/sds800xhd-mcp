"""End-to-end: simulator + server as processes, calls via MCP HTTP and the web API."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from mcp import Client

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"Port {port} not reachable")


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    scope_port, http_port = _free_port(), _free_port()
    data_dir = tmp_path_factory.mktemp("data")
    env = {**os.environ, "SIGLENT_IP": "127.0.0.1", "SIGLENT_PORT": str(scope_port)}
    procs = [
        subprocess.Popen([sys.executable, str(ROOT / "scripts" / "mock_scope.py"), "--port", str(scope_port)],
                         stdout=subprocess.DEVNULL),
    ]
    try:
        _wait_for_port(scope_port)
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "sds800xhd_mcp", "--port", str(http_port), "--data-dir", str(data_dir),
             "--log-level", "WARNING"],
            cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
        ))
        _wait_for_port(http_port)
        yield f"http://127.0.0.1:{http_port}"
    finally:
        for p in procs:
            p.terminate()
            p.wait(timeout=10)


def call(url: str, name: str, args: dict | None = None):
    async def go():
        async with Client(f"{url}/mcp") as client:
            return await client.call_tool(name, args or {})

    return asyncio.run(go())


def api(url: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(url + path)
    if body is not None:
        req.data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def test_lists_all_tools(server):
    async def go():
        async with Client(f"{server}/mcp") as client:
            return {t.name for t in (await client.list_tools()).tools}

    assert {"connect", "identify", "measure", "get_waveform", "screenshot", "scpi_query"} <= asyncio.run(go())


def test_identify(server):
    res = call(server, "identify")
    assert not res.is_error
    assert json.loads(res.content[0].text)["model"] == "SDS824X HD"


def test_waveform_is_reconstructed(server):
    res = call(server, "get_waveform", {"channel": "C1", "max_points": 500, "include_samples": True})
    assert not res.is_error
    data = json.loads(res.content[0].text)
    assert data["returned_points"] == 500
    assert data["stats"]["pkpk_v"] == pytest.approx(2.4, abs=0.02)
    assert len(data["voltages_v"]) == 500


def test_configure_and_read_back(server):
    assert not call(server, "configure_acquisition", {"timebase_s_per_div": 2e-4}).is_error
    assert float(call(server, "scpi_query", {"command": ":TIMebase:SCALe?"}).content[0].text) == pytest.approx(2e-4)


def test_screenshot_returns_png(server):
    res = call(server, "screenshot")
    assert not res.is_error
    assert res.content[0].type == "image" and res.content[0].mime_type == "image/png"


def test_scpi_query_requires_question_mark(server):
    assert call(server, "scpi_query", {"command": ":TRIGger:RUN"}).is_error


def test_web_config_roundtrip(server):
    state = api(server, "/api/state")
    assert state["status"]["connected"]
    saved = api(server, "/api/config", {"timeout_ms": 7000})
    assert saved["ok"] and saved["config"]["timeout_ms"] == 7000


def test_web_rejects_non_json_post(server):
    req = urllib.request.Request(server + "/api/disconnect", data=b"x", method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 400
