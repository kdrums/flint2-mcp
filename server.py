#!/usr/bin/env python3
"""
Flint 2 MCP Server
Exposes GL.iNet Flint 2 (GL-MT6000) router API as MCP tools.
Transport: HTTP/SSE (Claude desktop connects over LAN).
"""

import hashlib
import json
import subprocess
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
ROUTER_URL = cfg["router_url"].rstrip("/")
PASSWORD   = cfg["password"]
TIMEOUT    = cfg.get("timeout", 10)

# ── GL.iNet RPC client ─────────────────────────────────────────────────────────

_session_token: str | None = None


def _post(payload: dict) -> dict:
    """Raw POST to /rpc, returns parsed JSON."""
    r = httpx.post(f"{ROUTER_URL}/rpc", json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _login() -> str:
    """
    GL.iNet firmware 4.x challenge-response auth:
      1. POST {"method":"challenge","params":{"username":"root"}}
         -> {"result":{"nonce":"...","alg":5,"salt":"..."}}
      2. crypt_pw = openssl passwd -<alg> -salt <salt> <password>
      3. hash = sha256hex("root:" + crypt_pw + ":" + nonce)
      4. POST {"method":"login","params":{"username":"root","hash":"<hash>"}}
         -> {"result":{"sid":"..."}}
    """
    # Step 1: challenge
    ch = _post({"jsonrpc": "2.0", "id": 1, "method": "challenge",
                "params": {"username": "root"}})
    if "error" in ch:
        raise RuntimeError(f"Challenge failed: {ch['error']}")
    c = ch["result"]
    nonce, alg, salt = c["nonce"], c["alg"], c["salt"]

    # Step 2: crypt-hash the password (same algo used in /etc/shadow)
    crypt_pw = subprocess.run(
        ["openssl", "passwd", f"-{alg}", "-salt", salt, PASSWORD],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    # Step 3: sha256("root:<crypt_pw>:<nonce>")
    login_hash = hashlib.sha256(f"root:{crypt_pw}:{nonce}".encode()).hexdigest()

    # Step 4: login
    lr = _post({"jsonrpc": "2.0", "id": 2, "method": "login",
                "params": {"username": "root", "hash": login_hash}})
    if "error" in lr:
        raise RuntimeError(f"Login failed: {lr['error']}")

    sid = lr.get("result", {}).get("sid")
    if not sid:
        raise RuntimeError(f"No SID in login response: {lr}")
    return sid


def get_token() -> str:
    global _session_token
    if _session_token:
        try:
            resp = _post({"jsonrpc": "2.0", "id": 3, "method": "alive",
                          "params": {"sid": _session_token}})
            if "error" not in resp:
                return _session_token
        except Exception:
            pass
    _session_token = _login()
    return _session_token


def _rpc(subsystem: str, method: str, params: dict | None = None) -> Any:
    """Make an authenticated call via method='call' format."""
    global _session_token
    sid = get_token()
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "call",
        "params": [sid, subsystem, method, params or {}],
    }
    data = _post(payload)
    if "error" in data:
        # Session may have expired — retry once with fresh auth
        _session_token = None
        sid = get_token()
        payload["params"][0] = sid
        data = _post(payload)
        if "error" in data:
            raise RuntimeError(f"RPC error: {data['error']}")
    return data.get("result")


def _safe_rpc(subsystem: str, method: str, params: dict | None = None) -> Any:
    try:
        return _rpc(subsystem, method, params)
    except Exception as e:
        return {"error": str(e)}

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
        return {
            "info":   _safe_rpc("system", "get_info"),
            "hw":     _safe_rpc("system", "get_hardware"),
            "memory": _safe_rpc("system", "get_mem"),
            "cpu":    _safe_rpc("system", "get_cpu"),
            "temp":   _safe_rpc("system", "get_temp"),
        }

    elif name == "get_wan_status":
        return _rpc("network", "get_wan_status")

    elif name == "get_clients":
        return _rpc("clients", "get_list")

    elif name == "get_interfaces":
        return _rpc("network", "get_status")

    elif name == "get_vpn_status":
        return {
            "wireguard_client": _safe_rpc("wg-client", "get_status"),
            "wireguard_server": _safe_rpc("wg-server", "get_status"),
            "openvpn_client":   _safe_rpc("ovpn_client", "get_status"),
        }

    elif name == "get_system_log":
        lines = int(args.get("lines", 100))
        raw = _rpc("logread", "get_log")
        if isinstance(raw, str):
            return {"log": raw.splitlines()[-lines:]}
        if isinstance(raw, list):
            return {"log": raw[-lines:]}
        return {"log": raw}

    elif name == "get_wifi_status":
        return {
            "2g": _safe_rpc("wifi", "get_status", {"band": "2g"}),
            "5g": _safe_rpc("wifi", "get_status", {"band": "5g"}),
        }

    elif name == "reboot_router":
        _rpc("system", "reboot")
        return {"status": "reboot initiated"}

    else:
        raise ValueError(f"Unknown tool: {name}")

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
