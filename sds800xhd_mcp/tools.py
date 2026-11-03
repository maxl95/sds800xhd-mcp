"""MCP tools for the Siglent SDS800X HD (new SCPI command set)."""

from __future__ import annotations

import json
import math
import re
import time
from typing import Annotated, Any, Callable, Literal, TypeVar

import anyio.to_thread
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from . import waveform as wf
from .config import validate
from .image import bmp_to_png
from .scope import Scope, ScopeError

T = TypeVar("T")

Channel = Literal["C1", "C2", "C3", "C4"]

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

COMMON_MEASUREMENTS = (
    "PKPK, MAX, MIN, AMPL, TOP, BASE, MEAN, CMEAN, STDEV, RMS, CRMS, MEDIAN, OVSN, OVSP, "
    "FPRE, RPRE, PER, FREQ, TMAX, TMIN, PWID, NWID, DUTY, NDUTY, RISE, FALL, "
    "RISE20T80, FALL80T20, PAREA, NAREA, AREA, ABSAREA, CYCLES, REDGES, FEDGES, EDGES, "
    "PPULSES, NPULSES, DELAY (requires source2), PHA (requires source2)"
)


def _num(value: str) -> float | None:
    """Siglent returns numbers like '1.00E+00' or '****' (invalid)."""
    m = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", value)
    if not m or "*" in value:
        return None
    return float(m.group(0))


def _fmt(x: float) -> str:
    return f"{x:.6E}"


def capture_png(scope: Scope) -> bytes:
    """Screenshot as PNG. :PRINt? PNG, falling back to the older SCDP (BMP)."""
    try:
        with scope.session() as s:
            data = s.query_binary(":PRINt? PNG", timeout_ms=30_000)
    except ScopeError as first:
        try:
            with scope.session() as s:
                data = s.query_binary("SCDP", timeout_ms=30_000)
        except ScopeError:
            raise first from None
    return bmp_to_png(data) if data[:2] == b"BM" else data


def register_tools(mcp: MCPServer, scope: Scope) -> None:
    async def run(fn: Callable[[], T]) -> T:
        def guarded() -> T:
            try:
                return fn()
            except (ScopeError, ValueError) as exc:
                raise ToolError(str(exc)) from exc

        return await anyio.to_thread.run_sync(guarded)

    def dump(obj: Any) -> str:
        return json.dumps(obj, indent=2, ensure_ascii=False)

    def send_all(commands: list[str]) -> None:
        with scope.session() as s:
            for cmd in commands:
                s.write(cmd)
            s.opc()

    # ------------------------------------------------------------------ Connection

    @mcp.tool(annotations=WRITES, structured_output=False)
    async def connect(
        host: Annotated[str | None, Field(description="IP/hostname of the oscilloscope. Empty = saved setting")] = None,
        port: Annotated[int | None, Field(description="SCPI socket port (default 5025)")] = None,
        save: Annotated[bool, Field(description="Save the given values permanently")] = False,
    ) -> str:
        """Connects to the oscilloscope via VISA (TCPIP::<host>::<port>::SOCKET). Without parameters the configuration saved in the web UI is used."""
        overrides: dict[str, Any] = {}
        if host:
            overrides["host"] = host
            overrides["resource_override"] = ""
        if port:
            overrides["port"] = port
        try:
            cfg = scope.store.update(overrides) if save and overrides else validate(overrides, scope.store.get())
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        idn = await run(lambda: scope.connect(cfg))
        return f"Connected to {cfg.resource}\nDevice: {idn}"

    @mcp.tool(annotations=WRITES, structured_output=False)
    async def disconnect() -> str:
        """Disconnects from the oscilloscope."""
        await run(scope.disconnect)
        return "Disconnected"

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def connection_status() -> str:
        """Shows the connection status and the saved configuration."""
        return dump({"status": scope.status(), "config": scope.store.get().to_dict()})

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def identify() -> str:
        """Queries *IDN? (manufacturer, model, serial number, firmware)."""

        def do() -> dict:
            with scope.session() as s:
                parts = [p.strip() for p in s.query("*IDN?").split(",")]
            keys = ("manufacturer", "model", "serial", "firmware")
            return {k: parts[i] if i < len(parts) else "" for i, k in enumerate(keys)}

        return dump(await run(do))

    # ------------------------------------------------------------------ Channels

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def get_channel(channel: Channel) -> str:
        """Reads the settings of an analog channel (on/off, V/div, offset, coupling, bandwidth limit, probe, invert, unit, label)."""
        n = channel[1]

        def do() -> dict:
            result: dict[str, Any] = {"channel": channel}
            items = {
                "enabled": f":CHANnel{n}:SWITch?",
                "scale_v_per_div": f":CHANnel{n}:SCALe?",
                "offset_v": f":CHANnel{n}:OFFSet?",
                "coupling": f":CHANnel{n}:COUPling?",
                "impedance": f":CHANnel{n}:IMPedance?",
                "bandwidth_limit": f":CHANnel{n}:BWLimit?",
                "probe": f":CHANnel{n}:PROBe?",
                "invert": f":CHANnel{n}:INVert?",
                "unit": f":CHANnel{n}:UNIT?",
                "label": f":CHANnel{n}:LABel:TEXT?",
            }
            with scope.session() as s:
                for key, cmd in items.items():
                    result[key] = s.query(cmd)
            return result

        return dump(await run(do))

    @mcp.tool(annotations=WRITES, structured_output=False)
    async def configure_channel(
        channel: Channel,
        enabled: Annotated[bool | None, Field(description="Turn the channel on/off")] = None,
        scale_v_per_div: Annotated[float | None, Field(description="Vertical scale in V/div (e.g. 0.5)")] = None,
        offset_v: Annotated[float | None, Field(description="Vertical offset in V")] = None,
        coupling: Literal["DC", "AC", "GND"] | None = None,
        bandwidth_limit: Annotated[Literal["FULL", "20M"] | None, Field(description="Bandwidth limit")] = None,
        probe: Annotated[float | None, Field(description="Probe attenuation factor, e.g. 1, 10, 100")] = None,
        invert: bool | None = None,
        unit: Literal["V", "A"] | None = None,
        label: Annotated[str | None, Field(description="Channel label (empty = label off)")] = None,
    ) -> str:
        """Configures an analog channel. Only the given parameters are changed."""
        n = channel[1]
        cmds: list[str] = []
        if enabled is not None:
            cmds.append(f":CHANnel{n}:SWITch {'ON' if enabled else 'OFF'}")
        if probe is not None:
            cmds.append(f":CHANnel{n}:PROBe VALue,{_fmt(probe)}")
        if coupling is not None:
            cmds.append(f":CHANnel{n}:COUPling {coupling}")
        if bandwidth_limit is not None:
            cmds.append(f":CHANnel{n}:BWLimit {bandwidth_limit}")
        if unit is not None:
            cmds.append(f":CHANnel{n}:UNIT {unit}")
        if scale_v_per_div is not None:
            cmds.append(f":CHANnel{n}:SCALe {_fmt(scale_v_per_div)}")
        if offset_v is not None:
            cmds.append(f":CHANnel{n}:OFFSet {_fmt(offset_v)}")
        if invert is not None:
            cmds.append(f":CHANnel{n}:INVert {'ON' if invert else 'OFF'}")
        if label is not None:
            if label:
                safe = label.replace('"', "'")[:20]
                cmds += [f':CHANnel{n}:LABel:TEXT "{safe}"', f":CHANnel{n}:LABel ON"]
            else:
                cmds.append(f":CHANnel{n}:LABel OFF")
        if not cmds:
            return "No parameters given."
        await run(lambda: send_all(cmds))
        return f"Channel {channel} configured:\n" + "\n".join(cmds)

    # ------------------------------------------------------------------ Acquisition / trigger

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def get_acquisition_status() -> str:
        """Reads the acquisition and trigger state: status, sample rate, memory depth, timebase, trigger mode/type/source/level/slope."""
        items = {
            "trigger_status": ":TRIGger:STATus?",
            "sample_rate_sa_s": ":ACQuire:SRATe?",
            "memory_depth": ":ACQuire:MDEPth?",
            "points": ":ACQuire:POINts?",
            "acquire_type": ":ACQuire:TYPE?",
            "timebase_s_per_div": ":TIMebase:SCALe?",
            "timebase_delay_s": ":TIMebase:DELay?",
            "trigger_mode": ":TRIGger:MODE?",
            "trigger_type": ":TRIGger:TYPE?",
            "edge_source": ":TRIGger:EDGE:SOURce?",
            "edge_level_v": ":TRIGger:EDGE:LEVel?",
            "edge_slope": ":TRIGger:EDGE:SLOPe?",
            "edge_coupling": ":TRIGger:EDGE:COUPling?",
        }

        def do() -> dict:
            with scope.session() as s:
                return {k: s.query(cmd) for k, cmd in items.items()}

        return dump(await run(do))

    @mcp.tool(annotations=WRITES, structured_output=False)
    async def configure_acquisition(
        action: Annotated[
            Literal["run", "stop", "single", "force_trigger"] | None,
            Field(description="run = start acquisition, stop = halt, single = single acquisition, force_trigger = force a trigger"),
        ] = None,
        timebase_s_per_div: Annotated[float | None, Field(description="Timebase in s/div, e.g. 1e-6")] = None,
        timebase_delay_s: Annotated[float | None, Field(description="Horizontal delay in s")] = None,
        memory_depth: Annotated[str | None, Field(description="Memory depth, e.g. '10k', '100k', '1M', '10M'")] = None,
        acquire_type: Annotated[
            str | None,
            Field(description="NORMal, PEAK, AVERage,<16|32|...|1024> or ERES,<0.5|1.0|...|3.0>"),
        ] = None,
        trigger_mode: Literal["AUTO", "NORMal", "SINGle"] | None = None,
        trigger_source: Literal["C1", "C2", "C3", "C4", "EX", "EX5", "LINE"] | None = None,
        trigger_level_v: Annotated[float | None, Field(description="Edge trigger level in V")] = None,
        trigger_slope: Literal["RISing", "FALLing", "ALTernate"] | None = None,
        trigger_coupling: Literal["DC", "AC", "LFREJect", "HFREJect"] | None = None,
    ) -> str:
        """Controls run/stop/single and sets timebase, memory depth, acquisition type and the edge trigger. Only the given parameters are changed."""
        cmds: list[str] = []
        if memory_depth is not None:
            if not re.fullmatch(r"\d+(\.\d+)?[kKmM]?", memory_depth):
                raise ToolError("Give memory_depth as e.g. '10k' or '1M'")
            cmds.append(f":ACQuire:MDEPth {memory_depth}")
        if acquire_type is not None:
            if not re.fullmatch(r"[A-Za-z]+(,[\d.]+)?", acquire_type):
                raise ToolError("Invalid acquire_type")
            cmds.append(f":ACQuire:TYPE {acquire_type}")
        if timebase_s_per_div is not None:
            cmds.append(f":TIMebase:SCALe {_fmt(timebase_s_per_div)}")
        if timebase_delay_s is not None:
            cmds.append(f":TIMebase:DELay {_fmt(timebase_delay_s)}")
        if any(v is not None for v in (trigger_source, trigger_level_v, trigger_slope, trigger_coupling)):
            cmds.append(":TRIGger:TYPE EDGE")
        if trigger_source is not None:
            cmds.append(f":TRIGger:EDGE:SOURce {trigger_source}")
        if trigger_coupling is not None:
            cmds.append(f":TRIGger:EDGE:COUPling {trigger_coupling}")
        if trigger_slope is not None:
            cmds.append(f":TRIGger:EDGE:SLOPe {trigger_slope}")
        if trigger_level_v is not None:
            cmds.append(f":TRIGger:EDGE:LEVel {_fmt(trigger_level_v)}")
        if trigger_mode is not None:
            cmds.append(f":TRIGger:MODE {trigger_mode}")
        if action == "run":
            cmds.append(":TRIGger:RUN")
        elif action == "stop":
            cmds.append(":TRIGger:STOP")
        elif action == "single":
            cmds.append(":TRIGger:MODE SINGle")
        elif action == "force_trigger":
            cmds.append(":TRIGger:MODE FTRIG")
        if not cmds:
            return "No parameters given."
        await run(lambda: send_all(cmds))
        return "Sent:\n" + "\n".join(cmds)

    @mcp.tool(annotations=WRITES, structured_output=False)
    async def autoset() -> str:
        """Runs AUTO SETUP on the oscilloscope (adjusts vertical, timebase and trigger to the signal)."""

        def do() -> None:
            with scope.session() as s:
                s.write(":AUToset")
                s.query("*OPC?", timeout_ms=20_000)

        await run(do)
        return "Autoset done"

    # ------------------------------------------------------------------ Measurements

    def _setup_advanced(s: Scope, slot: int, channel: str, param: str, source2: str | None) -> None:
        s.write(":MEASure ON")
        s.write(":MEASure:MODE ADVanced")
        lines = _num(s.query(":MEASure:ADVanced:LINenumber?")) or 0
        if lines < slot:
            s.write(f":MEASure:ADVanced:LINenumber {slot}")
        s.write(f":MEASure:ADVanced:P{slot} ON")
        s.write(f":MEASure:ADVanced:P{slot}:SOURce1 {channel}")
        if source2:
            s.write(f":MEASure:ADVanced:P{slot}:SOURce2 {source2}")
        s.write(f":MEASure:ADVanced:P{slot}:TYPE {param}")

    def _check_param(p: str) -> str:
        p = p.strip().upper()
        if not re.fullmatch(r"[A-Z0-9]+", p):
            raise ToolError(f"Invalid measurement parameter: {p!r}")
        return p

    @mcp.tool(annotations=WRITES, structured_output=False)
    async def measure(
        channel: Channel,
        parameters: Annotated[
            list[str],
            Field(description=f"Measurement parameters, e.g. ['PKPK','FREQ']. 'ALL' returns all values of the simple measurement mode. Typical: {COMMON_MEASUREMENTS}"),
        ],
        source2: Annotated[Channel | None, Field(description="Second source for DELAY/PHA etc.")] = None,
    ) -> str:
        """Measures one or more parameters on a channel. Uses the advanced measurement slots P1..Pn on the oscilloscope and returns the raw and numeric value."""
        params = [_check_param(p) for p in parameters]
        if not params:
            raise ToolError("Give at least one parameter")
        if len(params) > 12:
            raise ToolError("At most 12 parameters at a time")

        def do() -> dict:
            with scope.session() as s:
                if params == ["ALL"]:
                    s.write(":MEASure ON")
                    s.write(":MEASure:MODE SIMPle")
                    s.write(f":MEASure:SIMPle:SOURce {channel}")
                    s.opc()
                    time.sleep(0.5)
                    return {"channel": channel, "all": s.query(":MEASure:SIMPle:VALue? ALL")}
                for i, p in enumerate(params, start=1):
                    _setup_advanced(s, i, channel, p, source2)
                s.opc()
                time.sleep(0.5)
                values = {}
                for i, p in enumerate(params, start=1):
                    raw = s.query(f":MEASure:ADVanced:P{i}:VALue?")
                    values[p] = {"raw": raw, "value": _num(raw), "slot": f"P{i}"}
                return {"channel": channel, "measurements": values}

        return dump(await run(do))

    @mcp.tool(annotations=WRITES, structured_output=False)
    async def measure_statistics(
        action: Annotated[Literal["on", "off", "reset", "read"], Field(description="Turn statistics on/off, reset or read them")],
        slot: Annotated[int, Field(ge=1, le=12, description="Measurement slot P1..P12 (see measure)")] = 1,
        channel: Annotated[Channel | None, Field(description="Optional: set the slot to this channel first")] = None,
        parameter: Annotated[str | None, Field(description="Optional: measurement parameter for the slot")] = None,
    ) -> str:
        """Controls the statistics of the advanced measurement mode and reads current/mean/min/max/std dev/count of a slot."""

        def do() -> Any:
            with scope.session() as s:
                if channel and parameter:
                    _setup_advanced(s, slot, channel, _check_param(parameter), None)
                if action == "on":
                    s.write(":MEASure:ADVanced:STATistics ON")
                elif action == "off":
                    s.write(":MEASure:ADVanced:STATistics OFF")
                elif action == "reset":
                    s.write(":MEASure:ADVanced:STATistics:RESet")
                s.opc()
                if action != "read":
                    return f"Statistics: {action}"
                s.write(":MEASure:ADVanced:STATistics ON")
                time.sleep(0.3)
                result = {"slot": f"P{slot}", "type": s.query(f":MEASure:ADVanced:P{slot}:TYPE?")}
                for key in ("CURRent", "MEAN", "MAXimum", "MINimum", "STDev", "COUNt"):
                    raw = s.query(f":MEASure:ADVanced:P{slot}:STATistics? {key}")
                    result[key.lower()] = {"raw": raw, "value": _num(raw)}
                return result

        res = await run(do)
        return res if isinstance(res, str) else dump(res)

    # ------------------------------------------------------------------ Waveform / screenshot

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def get_waveform(
        channel: Channel,
        max_points: Annotated[int, Field(ge=10, le=100_000, description="Maximum number of returned points (decimated on the device)")] = 1000,
        include_samples: Annotated[bool, Field(description="false = statistics only (min/max/mean/RMS) without the data array")] = True,
    ) -> str:
        """Downloads the waveform data of a channel (12-bit, WORD transfer) and converts it to volts. Returns start time t0, sample interval dt and the voltage values."""

        def do() -> dict:
            with scope.session() as s:
                s.write(f":WAVeform:SOURce {channel}")
                s.write(":WAVeform:WIDTh BYTE")
                pre = wf.parse_preamble(s.query_binary(":WAVeform:PREamble?"))
                if pre.adc_bit > 8:
                    s.write(":WAVeform:WIDTh WORD")
                    pre = wf.parse_preamble(s.query_binary(":WAVeform:PREamble?"))
                total = pre.points
                if total <= 0:
                    raise ScopeError(f"No data on {channel} (channel off or no acquisition?)")
                tdiv = _num(s.query(":TIMebase:SCALe?")) or 0.0
                max_chunk = int(_num(s.query(":WAVeform:MAXPoint?")) or 1_000_000)

                step = max(1, math.ceil(total / max_points))
                wanted = math.ceil(total / step)
                s.write(f":WAVeform:INTerval {step}")
                raw = bytearray()
                bpp = pre.bytes_per_point
                if step > 1 or wanted <= max_chunk:
                    s.write(":WAVeform:STARt 0")
                    s.write(f":WAVeform:POINt {min(wanted, max_chunk)}")
                    raw += s.query_binary(":WAVeform:DATA?", timeout_ms=60_000)
                else:
                    got = 0
                    while got < wanted:
                        s.write(f":WAVeform:STARt {got}")
                        s.write(f":WAVeform:POINt {min(max_chunk, wanted - got)}")
                        chunk = s.query_binary(":WAVeform:DATA?", timeout_ms=60_000)
                        if not chunk:
                            break
                        raw += chunk
                        got += len(chunk) // bpp
                s.write(":WAVeform:INTerval 1")
                s.write(":WAVeform:POINt 0")

            volts = wf.codes_to_volts(wf.decode_samples(bytes(raw), pre), pre)
            n = len(volts)
            mean = sum(volts) / n if n else 0.0
            result: dict[str, Any] = {
                "channel": channel,
                "total_points_on_scope": total,
                "returned_points": n,
                "decimation": step,
                "adc_bits": pre.adc_bit,
                "volts_per_div": pre.vdiv,
                "offset_v": pre.offset,
                "timebase_s_per_div": tdiv,
                "sample_interval_s": pre.interval,
                "t0_s": wf.time_of_first_point(pre, tdiv),
                "dt_s": pre.interval * step,
                "stats": {
                    "min_v": min(volts) if n else None,
                    "max_v": max(volts) if n else None,
                    "pkpk_v": (max(volts) - min(volts)) if n else None,
                    "mean_v": mean,
                    "rms_v": math.sqrt(sum(v * v for v in volts) / n) if n else None,
                },
            }
            if include_samples:
                result["voltages_v"] = [float(f"{v:.6g}") for v in volts]
            return result

        return json.dumps(await run(do), ensure_ascii=False)

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def screenshot() -> Image:
        """Takes a screenshot of the oscilloscope display (PNG)."""
        return Image(data=await run(lambda: capture_png(scope)), format="png")

    # ------------------------------------------------------------------ Raw SCPI

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def scpi_query(
        command: Annotated[str, Field(description="SCPI query, e.g. ':CHANnel1:SCALe?' or '*IDN?'")],
        timeout_ms: Annotated[int | None, Field(ge=100, le=120_000)] = None,
    ) -> str:
        """Sends an arbitrary SCPI query and returns the text response (escape hatch for functions not covered by other tools)."""
        if not command.strip().endswith("?") and "?" not in command:
            raise ToolError("Queries must contain a '?'. Use scpi_command for commands.")

        def do() -> str:
            with scope.session() as s:
                return s.query(command.strip(), timeout_ms=timeout_ms)

        return await run(do)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True), structured_output=False)
    async def scpi_command(
        command: Annotated[str, Field(description="SCPI command without a response, e.g. ':TRIGger:RUN'")],
    ) -> str:
        """Sends an arbitrary SCPI command (no response) and waits for completion via *OPC?."""
        cmd = command.strip()
        if "?" in cmd:
            raise ToolError("Use scpi_query for queries.")
        await run(lambda: send_all([cmd]))
        return f"Sent: {cmd}"
