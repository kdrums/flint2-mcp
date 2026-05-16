#!/usr/bin/env python3
"""
Flint 2 MCP Server
Exposes GL.iNet Flint 2 (GL-MT6000) router API as MCP tools.
Transport: HTTP/SSE (Claude desktop connects over LAN).
"""

import json
import sys
from pathlib import Path
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp import types
from starlette.applications import Starlette
from starlette.routing import Mount, Route
import uvicorn

# ── Config ─────────────────────────────────────────────────────────────────────

CONFIG_PATH = Path(__file__).parent / "config.json"

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        print(f"[ERROR] config.json not found at {CONFIG_PATH}", file=sys.stderr)
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        return json.load(f)

cfg = load_config()
ROUTER_URL = cfg["router_url"].rstrip("/")   # e.g. http://192.168.8.1
PASSWORD   = cfg["password"]
TIMEOUT    = cfg.get("timeout", 10)

# ── GL.iNet RPC client ─────────────────────────────────────────────────────────

_session_token: str | None = None

def _rpc(sid: str, subsystem: str, method: str, params: dict | None = None) -> Any:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "call",
        "params": [sid, subsystem, method, params or {}],
    }
    r = httpx.post(f"{ROUTER_URL}/rpc", json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"RPC error: {data['error']}")
    return data.get("result")


def get_token() -> str:
    global _session_token
    if _session_token:
        # probe with a cheap call; re-auth if stale
        try:
            result = _rpc(_session_token, "system", "get_hostname", {})
            if result is not None:
                return _session_token
        except Exception:
            pass

    result = _rpc(
        "00000000000000000000000000000000",
        "system", "login",
        {"username": "root", "password": PASSWORD},
    )
    if not result or not result.get("sid"):
        raise RuntimeError("Authentication failed — check password in config.json")
    _session_token = result["sid"]
    return _session_token


def call(subsystem: str, method: str, params: dict | None = None) -> Any:
    """Authenticate (if needed) and make an RPC call."""
    sid = get_token()
    return _rpc(sid, subsystem, method, params or {})

# ── MCP server ─────────────────────────────────────────────────────────────────

app = Server("flint2-mcp")

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="get_router_status",
            description=(
                "Get overall Flint 2 router status: model, firmware version, "
                "uptime, CPU load, memory usage, and temperature."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="get_wan_status",
            description="Get WAN connection status, IP, gateway, DNS, and uptime.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="get_clients",
            description=(
                "List all connected LAN and Wi-Fi clients with hostname, "
                "MAC address, IP, interface, and traffic stats."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="get_interfaces",
            description="List all network interfaces and their current configuration.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="get_vpn_status",
            description=(
                "Get WireGuard and OpenVPN client/server status, "
                "including connected peers and traffic counters."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="get_system_log",
            description="Fetch the last N lines of the router system log (default 100).",
            inputSchema={
                "type": "object",
                "properties": {
                    "lines": {
                        "type": "integer",
                        "description": "Number of log lines to return (default 100).",
                        "default": 100,
                    }
                },
            },
        ),
        types.Tool(
            name="get_wifi_status",
            description=(
                "Get Wi-Fi radio status for 2.4 GHz and 5 GHz bands: "
                "SSID, channel, bandwidth, TX power, and associated client count."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="reboot_router",
            description=(
                "Reboot the Flint 2 router. "
                "Only call this when the user explicitly requests a reboot."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        result = _dispatch(name, arguments)
        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]
    except Exception as exc:
        return [types.TextContent(type="text", text=f"Error: {exc}")]


def _dispatch(name: str, args: dict) -> Any:
    if name == "get_router_status":
        info     = call("system", "get_info")
        hw       = call("system", "get_hardware")
        mem      = call("system", "get_mem")
        cpu      = call("system", "get_cpu")
        temp_raw = call("system", "get_temp")
        return {
            "model":    hw.get("model") if hw else None,
            "firmware": info.get("firmware") if info else None,
            "uptime_s": info.get("uptime") if info else None,
            "load":     cpu if cpu else None,
            "memory":   mem if mem else None,
            "temp_c":   temp_raw if temp_raw else None,
        }

    elif name == "get_wan_status":
        return call("network", "get_wan_status")

    elif name == "get_clients":
        return call("router.clients", "get_list")

    elif name == "get_interfaces":
        return call("network", "get_status")

    elif name == "get_vpn_status":
        wg  = _safe_call("vpn.wireguard.client", "get_status")
        ovpn = _safe_call("vpn.openvpn.client", "get_status")
        wg_srv  = _safe_call("vpn.wireguard.server", "get_status")
        return {
            "wireguard_client": wg,
            "wireguard_server": wg_srv,
            "openvpn_client":   ovpn,
        }

    elif name == "get_system_log":
        lines = int(args.get("lines", 100))
        raw = call("system", "get_log")
        if isinstance(raw, str):
            return {"log": raw.splitlines()[-lines:]}
        return {"log": raw}

    elif name == "get_wifi_status":
        radio24 = _safe_call("network.wireless", "get_status", {"band": "2g"})
        radio5  = _safe_call("network.wireless", "get_status", {"band": "5g"})
        return {"2g": radio24, "5g": radio5}

    elif name == "reboot_router":
        call("system", "reboot")
        return {"status": "reboot initiated"}

    else:
        raise ValueError(f"Unknown tool: {name}")


def _safe_call(subsystem: str, method: str, params: dict | None = None) -> Any:
    try:
        return call(subsystem, method, params)
    except Exception as e:
        return {"error": str(e)}

# ── SSE transport + Starlette app ──────────────────────────────────────────────

sse = SseServerTransport("/messages/")

async def handle_sse(request):
    async with sse.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        await app.run(
            streams[0], streams[1], app.create_initialization_options()
        )

starlette_app = Starlette(
    routes=[
        Route("/sse", endpoint=handle_sse),
        Mount("/messages/", app=sse.handle_post_message),
    ]
)

if __name__ == "__main__":
    host = cfg.get("host", "0.0.0.0")
    port = cfg.get("port", 8080)
    print(f"[flint2-mcp] Listening on {host}:{port}/sse", file=sys.stderr)
    uvicorn.run(starlette_app, host=host, port=port)
