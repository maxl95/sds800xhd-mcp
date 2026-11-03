# Siglent SDS800X HD – HTTP MCP Server

MCP server (Streamable HTTP) for Siglent **SDS800X HD** oscilloscopes (SDS804X HD / SDS814X HD / SDS824X HD).
It talks SCPI over a VISA socket (`TCPIP0::<ip>::5025::SOCKET`) using PyVISA.
A small web UI sets the connection parameters and stores them persistently.

Based on the idea of [MagnusJohansson/siglent-sds-mcp](https://github.com/MagnusJohansson/siglent-sds-mcp).
The commands have been ported to the new SCPI command set of the SDS800X HD
(`:CHANnel<n>:SCALe`, `:TRIGger:EDGE:*`, `:MEASure:ADVanced:*`, `:WAVeform:*`, `:PRINt?`).

## Installation

```bash
python -m venv .venv
.venv\Scripts\activate        # Linux/macOS: source .venv/bin/activate
pip install -e .
```

By default **PyVISA-py** is used, so no VISA driver is required. If NI-VISA (or another VISA implementation)
is installed, "System VISA" can be selected in the web UI.

## Running

```bash
sds800xhd-mcp                       # or: python -m sds800xhd_mcp
```

| Option | Environment variable | Default | Meaning |
|---|---|---|---|
| `--host` | `SDS_MCP_HOST` | `127.0.0.1` | Bind address (`0.0.0.0` for network access) |
| `--port` | `SDS_MCP_PORT` | `8800` | HTTP port for web UI and MCP |
| `--data-dir` | `SDS_MCP_DATA_DIR` | `./data` | Location of `config.json` |
| `--log-level` | `SDS_MCP_LOG_LEVEL` | `INFO` | `DEBUG` logs all SCPI commands |

- Web UI: <http://localhost:8800/>
- MCP endpoint: <http://localhost:8800/mcp>

## Docker (Linux)

Slim image based on `python:3.13-alpine` (multi-stage, no pip, runs as a non-root user).
PyVISA-py needs no drivers, so the image contains only Python and the packages.

```bash
docker compose up -d --build
```

or without Compose:

```bash
docker build -t sds800xhd-mcp .
docker run -d --name sds800xhd-mcp --restart unless-stopped -p 8800:8800 -v sds-data:/data sds800xhd-mcp
```

- Web UI: `http://<docker-host>:8800/`, MCP: `http://<docker-host>:8800/mcp`
- The configuration lives in the `/data` volume (`config.json`) and survives container updates.
- Inside the container the server listens on `0.0.0.0`. To make it reachable only locally, map the port with
  `-p 127.0.0.1:8800:8800`.
- The scope must be reachable from the Docker host (TCP 5025). The default bridge network is sufficient.
- If a host directory is mounted instead of the volume, it must be writable for UID `10001`:
  `sudo chown 10001 ./data`.
- Append options such as `--log-level DEBUG`: `docker run ... sds800xhd-mcp --log-level DEBUG`.

## Web UI

Enter IP/hostname and port (the VISA string is built from them but can also be entered directly),
choose VISA backend, timeout and auto-connect, then click **Save & connect**. The settings are stored in
`data/config.json` and loaded on server start. With auto-connect enabled, the server connects on startup
and re-establishes the connection on the next tool call after a drop (e.g. a scope restart).
The **Screenshot** button verifies the connection with an image of the display.

## MCP client setup

Claude Code: copy [`examples/mcp.json`](examples/mcp.json) to `.mcp.json` in your project folder and adjust
the URL (the local `.mcp.json` is listed in `.gitignore`):

```json
{
  "mcpServers": {
    "siglent-sds800xhd": { "type": "http", "url": "http://localhost:8800/mcp" }
  }
}
```

or via the CLI:

```bash
claude mcp add --transport http siglent-sds800xhd http://localhost:8800/mcp
```

## Tools

| Tool | Function |
|---|---|
| `connect` / `disconnect` / `connection_status` | Connection (without parameters, the saved configuration is used) |
| `identify` | `*IDN?` |
| `get_channel` / `configure_channel` | V/div, offset, coupling, BW limit, probe, invert, unit, label, on/off |
| `get_acquisition_status` / `configure_acquisition` | Run/stop/single/force, timebase, memory depth, acquisition type, edge trigger |
| `autoset` | Auto setup |
| `measure` | One or more measurements (slots P1..P12) or `ALL` |
| `measure_statistics` | Turn statistics on/off, reset or read them |
| `get_waveform` | Waveform data (12 bit, WORD), decimated on the device, in volts with t0/dt and statistics |
| `screenshot` | Display as PNG (`:PRINt? PNG`, fallback `SCDP`) |
| `scpi_query` / `scpi_command` | Arbitrary SCPI commands |

## Development and tests

```
sds800xhd_mcp/        Package: server, tools, VISA connection, web UI (static/index.html)
scripts/mock_scope.py SCPI simulator of an SDS824X HD (for tests without hardware)
scripts/smoke_http.py Calls all tools against a running server
tests/                pytest: unit tests and an end-to-end test with the simulator
examples/mcp.json     Template for the MCP client configuration
```

```bash
pip install -e ".[dev]"
pytest                                            # starts simulator and server itself
```

Manually without hardware:

```bash
python scripts/mock_scope.py                      # simulates an SDS824X HD on 127.0.0.1:5025
sds800xhd-mcp                                     # enter 127.0.0.1 in the web UI
python scripts/smoke_http.py                      # calls all tools over HTTP
```

`scripts/smoke_http.py` also works against a real scope, but changes channel 1, timebase and trigger there.

## Security

The web UI and MCP endpoint have no authentication and allow arbitrary SCPI commands.
By default the server listens only on `127.0.0.1`. Use `--host 0.0.0.0` only in a trusted lab network.

## Note

The tools are tested with the included simulator. Connecting to a real SDS824X HD
(firmware 3.8.12.1.1.6.5) works; the individual tools have not yet been systematically verified on the device.
If the firmware differs for individual commands, they can be checked via `scpi_query` / `scpi_command` and
adjusted in `sds800xhd_mcp/tools.py`.

## License

[MIT](LICENSE)
