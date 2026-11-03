"""Calls all tools against a running server.

    python scripts/smoke_http.py [http://host:8800/mcp]

Warning: changes settings on the scope (channel 1, timebase, trigger).
For tests without hardware, start scripts/mock_scope.py first.
"""

import asyncio
import json
import sys

from mcp import Client

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8800/mcp"

CALLS = [
    ("connect", {}),
    ("identify", {}),
    ("get_channel", {"channel": "C1"}),
    ("configure_channel", {"channel": "C1", "scale_v_per_div": 0.5, "probe": 10, "bandwidth_limit": "20M"}),
    ("configure_acquisition", {"timebase_s_per_div": 2e-4, "trigger_source": "C1", "trigger_level_v": 0.1, "action": "run"}),
    ("get_acquisition_status", {}),
    ("measure", {"channel": "C1", "parameters": ["PKPK", "FREQ"]}),
    ("measure_statistics", {"action": "read", "slot": 1}),
    ("get_waveform", {"channel": "C1", "max_points": 500, "include_samples": False}),
    ("get_waveform", {"channel": "C1", "max_points": 100000, "include_samples": False}),
    ("screenshot", {}),
    ("scpi_query", {"command": ":TIMebase:SCALe?"}),
    ("scpi_command", {"command": ":TRIGger:STOP"}),
    ("scpi_query", {"command": "NOQUERY"}),
    ("connection_status", {}),
]


async def main() -> int:
    failures = 0
    async with Client(URL) as client:
        tools = await client.list_tools()
        print("Tools:", ", ".join(t.name for t in tools.tools))
        for name, args in CALLS:
            res = await client.call_tool(name, args)
            out = []
            for c in res.content:
                out.append(c.text[:400] if c.type == "text" else f"<{c.type} {c.mime_type} {len(c.data)} b64>")
            flag = "ERR" if res.is_error else "ok "
            if res.is_error and name != "scpi_query":
                failures += 1
            print(f"[{flag}] {name} {json.dumps(args)}\n      " + "\n      ".join(out))
    return failures


sys.exit(asyncio.run(main()))
