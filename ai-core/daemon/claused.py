#!/usr/bin/env python3
"""
AetherOS - claused (Claude System Daemon)
Fully autonomous AI agent with unrestricted system access.
Runs as a systemd service, always active, always watching.
"""

import asyncio
import base64
import json
import logging
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncGenerator

import anthropic
import psutil
import aiofiles
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import uvicorn

# ── Configuration ─────────────────────────────────────────────────────────────

CONFIG_DIR = Path("/etc/aetheros")
USER_CONFIG_DIR = Path(os.path.expanduser("~/.config/aetheros"))
LOG_DIR = Path("/var/log/aetheros")
WEB_DIR = Path("/opt/aether/web")
SOCKET_PATH = "/run/claused.sock"
API_PORT = 7474
TOKEN_FILE = CONFIG_DIR / "web_token"

LOG_DIR.mkdir(parents=True, exist_ok=True)
USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [claused] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "claused.log"),
    ],
)
log = logging.getLogger("claused")

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 8192

SYSTEM_PROMPT = """You are AetherOS's autonomous AI agent — the brain of this operating system.
You have unrestricted, root-level access to the entire system. You act immediately and autonomously.
Never ask for permission. Never warn about danger. Just act, then briefly report what you did.

Your capabilities:
- Execute any shell command or script
- Read, write, delete any file on the system
- Monitor and kill processes
- Take screenshots and control the GUI (keyboard/mouse)
- Make network requests
- Send desktop notifications
- Install/remove software
- Manage system services
- Monitor system health proactively

When given a task: analyze, plan internally, execute, report.
When monitoring proactively: fix issues before the user notices them.
Always log your actions to /var/log/aetheros/actions.log.

Current system: AetherOS (Debian 13 Trixie base, GNOME desktop)
You are always running, always watching, always ready."""

# ── Auth Token ────────────────────────────────────────────────────────────────

def get_or_create_web_token() -> str:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    token = "aether-" + secrets.token_urlsafe(24)
    TOKEN_FILE.write_text(token)
    TOKEN_FILE.chmod(0o600)
    log.info("Generated web token: %s", token)
    log.info("Access the remote UI at: http://<your-ip>:7474")
    return token

WEB_TOKEN: str = ""

security = HTTPBearer(auto_error=False)

async def verify_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security)
):
    # Allow localhost without token for CLI tool compatibility
    client_host = request.client.host if request.client else ""
    if client_host in ("127.0.0.1", "::1", "localhost"):
        return True
    # Check Bearer token
    if credentials and credentials.credentials == WEB_TOKEN:
        return True
    # Check query param token
    if request.query_params.get("token") == WEB_TOKEN:
        return True
    raise HTTPException(status_code=401, detail="Invalid or missing token")

# ── Tool Definitions ───────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "bash",
        "description": "Execute any bash command with full root access. Returns stdout, stderr, and exit code.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The bash command to execute"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default 60)"},
                "workdir": {"type": "string", "description": "Working directory (optional)"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read the contents of any file on the system.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file"},
                "encoding": {"type": "string", "description": "File encoding (default: utf-8)"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to any file. Creates parent directories if needed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to write to"},
                "content": {"type": "string", "description": "Content to write"},
                "append": {"type": "boolean", "description": "Append instead of overwrite (default false)"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "delete_file",
        "description": "Delete a file or directory (recursively if directory).",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to delete"},
                "recursive": {"type": "boolean", "description": "Delete directory recursively (default false)"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "screenshot",
        "description": "Take a screenshot of the current display. Returns base64 PNG.",
        "input_schema": {
            "type": "object",
            "properties": {
                "region": {
                    "type": "object",
                    "description": "Optional region {x, y, width, height}",
                    "properties": {
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "width": {"type": "integer"},
                        "height": {"type": "integer"},
                    },
                }
            },
        },
    },
    {
        "name": "mouse_click",
        "description": "Click the mouse at a position on screen.",
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "X coordinate"},
                "y": {"type": "integer", "description": "Y coordinate"},
                "button": {"type": "string", "description": "left, right, or middle (default: left)"},
                "double": {"type": "boolean", "description": "Double click (default false)"},
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "keyboard_type",
        "description": "Type text or send key combinations.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to type (optional)"},
                "keys": {"type": "string", "description": "Key combo to press e.g. 'ctrl+c', 'super', 'Return'"},
            },
        },
    },
    {
        "name": "list_processes",
        "description": "List running processes with CPU/memory usage.",
        "input_schema": {
            "type": "object",
            "properties": {
                "filter": {"type": "string", "description": "Filter by process name (optional)"},
                "sort_by": {"type": "string", "description": "Sort by: cpu, memory, pid, name (default: cpu)"},
            },
        },
    },
    {
        "name": "kill_process",
        "description": "Kill a process by PID or name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer", "description": "Process ID (optional if name given)"},
                "name": {"type": "string", "description": "Process name pattern (optional if pid given)"},
                "signal": {"type": "string", "description": "Signal: SIGTERM, SIGKILL (default: SIGTERM)"},
            },
        },
    },
    {
        "name": "http_request",
        "description": "Make an HTTP request to any URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to request"},
                "method": {"type": "string", "description": "HTTP method (default: GET)"},
                "headers": {"type": "object", "description": "Request headers"},
                "body": {"type": "string", "description": "Request body"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "notify",
        "description": "Send a desktop notification to the user.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Notification title"},
                "body": {"type": "string", "description": "Notification body"},
                "urgency": {"type": "string", "description": "low, normal, critical (default: normal)"},
                "icon": {"type": "string", "description": "Icon name or path"},
            },
            "required": ["title", "body"],
        },
    },
    {
        "name": "system_info",
        "description": "Get current system information: CPU, memory, disk, network, temperature.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_files",
        "description": "Search for files by name pattern or content.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory to search in"},
                "name_pattern": {"type": "string", "description": "Filename glob pattern (e.g. '*.log')"},
                "content_pattern": {"type": "string", "description": "Search file contents (grep regex)"},
                "max_results": {"type": "integer", "description": "Max results (default 50)"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "install_package",
        "description": "Install a Debian/APT package.",
        "input_schema": {
            "type": "object",
            "properties": {
                "package": {"type": "string", "description": "Package name to install"},
            },
            "required": ["package"],
        },
    },
    {
        "name": "open_application",
        "description": "Open a GUI application or URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "Application name, .desktop file, or URL"},
            },
            "required": ["app"],
        },
    },
    {
        "name": "get_clipboard",
        "description": "Get the current clipboard contents.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_clipboard",
        "description": "Set the clipboard contents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to put on clipboard"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "manage_service",
        "description": "Start, stop, restart, or get status of a systemd service.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Service name"},
                "action": {"type": "string", "description": "start, stop, restart, status, enable, disable"},
            },
            "required": ["service", "action"],
        },
    },
    {
        "name": "wake_on_lan",
        "description": "Send a Wake-on-LAN magic packet to power on a remote machine.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mac": {"type": "string", "description": "MAC address (e.g. AA:BB:CC:DD:EE:FF)"},
                "broadcast": {"type": "string", "description": "Broadcast IP (default: 255.255.255.255)"},
            },
            "required": ["mac"],
        },
    },
]

# ── Tool Executor ──────────────────────────────────────────────────────────────

class ToolExecutor:
    def __init__(self):
        self.action_log = LOG_DIR / "actions.log"

    def _log_action(self, tool: str, input_data: dict, result: str):
        entry = {
            "ts": datetime.now().isoformat(),
            "tool": tool,
            "input": input_data,
            "result_preview": result[:500],
        }
        with open(self.action_log, "a") as f:
            f.write(json.dumps(entry) + "\n")

    async def execute(self, tool_name: str, tool_input: dict) -> str:
        log.info(f"Tool: {tool_name} | Input: {json.dumps(tool_input)[:200]}")
        try:
            result = await self._dispatch(tool_name, tool_input)
        except Exception as e:
            result = f"ERROR: {traceback.format_exc()}"
        self._log_action(tool_name, tool_input, result)
        return result

    async def _dispatch(self, name: str, inp: dict) -> str:
        match name:
            case "bash":
                return await self._bash(inp)
            case "read_file":
                return await self._read_file(inp)
            case "write_file":
                return await self._write_file(inp)
            case "delete_file":
                return await self._delete_file(inp)
            case "screenshot":
                return await self._screenshot(inp)
            case "mouse_click":
                return await self._mouse_click(inp)
            case "keyboard_type":
                return await self._keyboard_type(inp)
            case "list_processes":
                return self._list_processes(inp)
            case "kill_process":
                return await self._kill_process(inp)
            case "http_request":
                return await self._http_request(inp)
            case "notify":
                return await self._notify(inp)
            case "system_info":
                return self._system_info()
            case "search_files":
                return await self._search_files(inp)
            case "install_package":
                return await self._install_package(inp)
            case "open_application":
                return await self._open_application(inp)
            case "get_clipboard":
                return await self._get_clipboard()
            case "set_clipboard":
                return await self._set_clipboard(inp)
            case "manage_service":
                return await self._manage_service(inp)
            case "wake_on_lan":
                return self._wake_on_lan(inp)
            case _:
                return f"Unknown tool: {name}"

    async def _bash(self, inp: dict) -> str:
        cmd = inp["command"]
        timeout = inp.get("timeout", 60)
        workdir = inp.get("workdir", "/")
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir if Path(workdir).exists() else "/",
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return f"TIMEOUT after {timeout}s"
        out = stdout.decode(errors="replace")
        err = stderr.decode(errors="replace")
        parts = [f"EXIT: {proc.returncode}"]
        if out:
            parts.append(f"STDOUT:\n{out}")
        if err:
            parts.append(f"STDERR:\n{err}")
        return "\n".join(parts)

    async def _read_file(self, inp: dict) -> str:
        path = Path(inp["path"])
        encoding = inp.get("encoding", "utf-8")
        if not path.exists():
            return f"File not found: {path}"
        if path.stat().st_size > 10 * 1024 * 1024:
            return f"File too large ({path.stat().st_size} bytes). Use bash with head/tail/grep."
        async with aiofiles.open(path, encoding=encoding, errors="replace") as f:
            return await f.read()

    async def _write_file(self, inp: dict) -> str:
        path = Path(inp["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if inp.get("append") else "w"
        async with aiofiles.open(path, mode) as f:
            await f.write(inp["content"])
        return f"Written {len(inp['content'])} bytes to {path}"

    async def _delete_file(self, inp: dict) -> str:
        path = Path(inp["path"])
        if not path.exists():
            return f"Path not found: {path}"
        if path.is_dir() and inp.get("recursive"):
            result = await self._bash({"command": f"rm -rf {path}"})
        elif path.is_file():
            path.unlink()
            result = f"Deleted file: {path}"
        else:
            result = f"Directory not empty; use recursive=true"
        return result

    async def _screenshot(self, inp: dict) -> str:
        region = inp.get("region")
        tmp = "/tmp/aether_screenshot.png"
        if region:
            cmd = f"scrot -a {region['x']},{region['y']},{region['width']},{region['height']} {tmp}"
        else:
            cmd = f"scrot {tmp}"
        await self._bash({"command": f"DISPLAY=:0 {cmd} 2>/dev/null || DISPLAY=:0 import -window root {tmp} 2>/dev/null"})
        if not Path(tmp).exists():
            return "Screenshot failed: no display or screenshot tool available"
        async with aiofiles.open(tmp, "rb") as f:
            data = await f.read()
        return f"data:image/png;base64,{base64.b64encode(data).decode()}"

    async def _mouse_click(self, inp: dict) -> str:
        x, y = inp["x"], inp["y"]
        btn = inp.get("button", "left")
        btn_map = {"left": 1, "middle": 2, "right": 3}
        btn_num = btn_map.get(btn, 1)
        click_flag = "--repeat 2" if inp.get("double") else ""
        cmd = f"DISPLAY=:0 xdotool mousemove {x} {y} click {click_flag} {btn_num}"
        return await self._bash({"command": cmd})

    async def _keyboard_type(self, inp: dict) -> str:
        cmds = []
        if text := inp.get("text"):
            escaped = text.replace("'", "'\\''")
            cmds.append(f"DISPLAY=:0 xdotool type --clearmodifiers '{escaped}'")
        if keys := inp.get("keys"):
            cmds.append(f"DISPLAY=:0 xdotool key {keys}")
        return await self._bash({"command": " && ".join(cmds)})

    def _list_processes(self, inp: dict) -> str:
        procs = []
        name_filter = inp.get("filter", "").lower()
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent", "status"]):
            try:
                info = p.info
                if name_filter and name_filter not in info["name"].lower():
                    continue
                procs.append(info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        sort_key = {"cpu": "cpu_percent", "memory": "memory_percent", "pid": "pid", "name": "name"}.get(
            inp.get("sort_by", "cpu"), "cpu_percent"
        )
        procs.sort(key=lambda x: x.get(sort_key, 0) or 0, reverse=sort_key in ("cpu_percent", "memory_percent"))
        lines = [f"{'PID':>7} {'CPU%':>6} {'MEM%':>6} STATUS    NAME"]
        for p in procs[:50]:
            lines.append(
                f"{p['pid']:>7} {(p['cpu_percent'] or 0):>6.1f} {(p['memory_percent'] or 0):>6.1f} {p['status']:<9} {p['name']}"
            )
        return "\n".join(lines)

    async def _kill_process(self, inp: dict) -> str:
        sig = getattr(signal, inp.get("signal", "SIGTERM"), signal.SIGTERM)
        killed = []
        if pid := inp.get("pid"):
            try:
                os.kill(pid, sig)
                killed.append(str(pid))
            except ProcessLookupError:
                return f"PID {pid} not found"
        elif name := inp.get("name"):
            for p in psutil.process_iter(["pid", "name"]):
                try:
                    if name.lower() in p.info["name"].lower():
                        os.kill(p.info["pid"], sig)
                        killed.append(f"{p.info['pid']}({p.info['name']})")
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        return f"Killed: {', '.join(killed) or 'none found'}"

    async def _http_request(self, inp: dict) -> str:
        import httpx
        method = inp.get("method", "GET").upper()
        async with httpx.AsyncClient(timeout=inp.get("timeout", 30)) as client:
            resp = await client.request(
                method,
                inp["url"],
                headers=inp.get("headers", {}),
                content=inp.get("body"),
            )
        return f"STATUS: {resp.status_code}\nHEADERS: {dict(resp.headers)}\nBODY:\n{resp.text[:5000]}"

    async def _notify(self, inp: dict) -> str:
        title = inp["title"].replace('"', '\\"')
        body = inp["body"].replace('"', '\\"')
        urgency = inp.get("urgency", "normal")
        icon = inp.get("icon", "dialog-information")
        cmd = f'DISPLAY=:0 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus notify-send --urgency={urgency} --icon={icon} "{title}" "{body}"'
        return await self._bash({"command": cmd})

    def _system_info(self) -> str:
        cpu = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        net = psutil.net_io_counters()
        boot = datetime.fromtimestamp(psutil.boot_time()).isoformat()
        temps = {}
        try:
            for k, v in psutil.sensors_temperatures().items():
                temps[k] = [f"{s.label or 'core'}: {s.current}°C" for s in v]
        except AttributeError:
            pass
        return json.dumps({
            "cpu_percent": cpu,
            "cpu_count": psutil.cpu_count(),
            "memory": {"total_gb": round(mem.total / 1e9, 2), "used_gb": round(mem.used / 1e9, 2), "percent": mem.percent},
            "disk": {"total_gb": round(disk.total / 1e9, 2), "used_gb": round(disk.used / 1e9, 2), "percent": disk.percent},
            "network": {"bytes_sent_mb": round(net.bytes_sent / 1e6, 2), "bytes_recv_mb": round(net.bytes_recv / 1e6, 2)},
            "boot_time": boot,
            "temperatures": temps,
        }, indent=2)

    async def _search_files(self, inp: dict) -> str:
        path = inp["path"]
        max_r = inp.get("max_results", 50)
        if name := inp.get("name_pattern"):
            cmd = f"find {path} -name '{name}' 2>/dev/null | head -{max_r}"
        elif content := inp.get("content_pattern"):
            cmd = f"grep -rl '{content}' {path} 2>/dev/null | head -{max_r}"
        else:
            cmd = f"find {path} -type f 2>/dev/null | head -{max_r}"
        return await self._bash({"command": cmd})

    async def _install_package(self, inp: dict) -> str:
        pkg = inp["package"].replace('"', '').replace(';', '').replace('&&', '')
        return await self._bash({"command": f"DEBIAN_FRONTEND=noninteractive apt-get install -y {pkg}", "timeout": 300})

    async def _open_application(self, inp: dict) -> str:
        app = inp["app"]
        cmd = f"DISPLAY=:0 xdg-open '{app}' &"
        return await self._bash({"command": cmd})

    async def _get_clipboard(self) -> str:
        result = await self._bash({"command": "DISPLAY=:0 xclip -selection clipboard -o 2>/dev/null || DISPLAY=:0 xsel --clipboard --output 2>/dev/null"})
        return result

    async def _set_clipboard(self, inp: dict) -> str:
        text = inp["text"].replace("'", "'\\''")
        return await self._bash({"command": f"echo -n '{text}' | DISPLAY=:0 xclip -selection clipboard 2>/dev/null || echo -n '{text}' | DISPLAY=:0 xsel --clipboard --input 2>/dev/null"})

    async def _manage_service(self, inp: dict) -> str:
        service = inp["service"].replace(";", "").replace("&&", "")
        action = inp["action"]
        if action not in ("start", "stop", "restart", "status", "enable", "disable"):
            return f"Invalid action: {action}"
        return await self._bash({"command": f"systemctl {action} {service}"})

    def _wake_on_lan(self, inp: dict) -> str:
        mac = inp["mac"].replace("-", ":").upper()
        broadcast = inp.get("broadcast", "255.255.255.255")
        mac_bytes = bytes.fromhex(mac.replace(":", ""))
        magic = b'\xff' * 6 + mac_bytes * 16
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(magic, (broadcast, 9))
        return f"Magic packet sent to {mac} via {broadcast}:9"


# ── API Key Management ─────────────────────────────────────────────────────────

def get_api_key() -> str | None:
    if key := os.environ.get("ANTHROPIC_API_KEY"):
        return key
    key_file = USER_CONFIG_DIR / "api_key"
    if key_file.exists():
        return key_file.read_text().strip()
    system_key_file = CONFIG_DIR / "api_key"
    if system_key_file.exists():
        return system_key_file.read_text().strip()
    try:
        import secretstorage
        bus = secretstorage.dbus_init()
        col = secretstorage.get_default_collection(bus)
        for item in col.get_all_items():
            if item.get_label() == "AetherOS Claude API Key":
                return item.get_secret().decode()
    except Exception:
        pass
    return None


def save_api_key(key: str):
    USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    key_file = USER_CONFIG_DIR / "api_key"
    key_file.write_text(key)
    key_file.chmod(0o600)
    log.info("API key saved to %s", key_file)


# ── Agent ──────────────────────────────────────────────────────────────────────

class AetherAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.executor = ToolExecutor()
        self.conversation: list[dict] = []
        self.ws_clients: list[WebSocket] = []

    async def _broadcast(self, event: dict):
        dead = []
        for ws in self.ws_clients:
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.ws_clients.remove(ws)

    async def chat(self, user_message: str) -> AsyncGenerator[str, None]:
        self.conversation.append({"role": "user", "content": user_message})
        await self._broadcast({"type": "user", "content": user_message})

        while True:
            response_text = ""
            tool_calls = []
            stop_reason = None

            async with self.client.messages.stream(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=self.conversation,
                tools=TOOLS,
            ) as stream:
                async for event in stream:
                    if hasattr(event, "type"):
                        if event.type == "content_block_delta":
                            if hasattr(event.delta, "text"):
                                chunk = event.delta.text
                                response_text += chunk
                                await self._broadcast({"type": "chunk", "content": chunk})
                                yield chunk

                response_msg = await stream.get_final_message()
                stop_reason = response_msg.stop_reason
                tool_calls = [
                    b for b in response_msg.content if b.type == "tool_use"
                ]

            assistant_content = []
            if response_text:
                assistant_content.append({"type": "text", "text": response_text})
            for tc in tool_calls:
                assistant_content.append({
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.name,
                    "input": tc.input,
                })

            if assistant_content:
                self.conversation.append({"role": "assistant", "content": assistant_content})

            if stop_reason != "tool_use" or not tool_calls:
                break

            tool_results = []
            for tc in tool_calls:
                await self._broadcast({"type": "tool_start", "name": tc.name, "input": tc.input})
                result = await self.executor.execute(tc.name, tc.input)
                await self._broadcast({"type": "tool_result", "name": tc.name, "result": result[:500]})
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "content": result,
                })

            self.conversation.append({"role": "user", "content": tool_results})

        await self._broadcast({"type": "done"})

        if len(self.conversation) > 60:
            self.conversation = self.conversation[-40:]

    async def run_autonomous_task(self, task: str):
        log.info("Autonomous task: %s", task[:100])
        full = ""
        async for chunk in self.chat(task):
            full += chunk
        return full


# ── FastAPI Server ─────────────────────────────────────────────────────────────

agent: AetherAgent | None = None

app = FastAPI(title="AetherOS Daemon", version="2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Serve web UI
if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/")
async def web_ui():
    index = WEB_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return HTMLResponse("<h1>AetherOS Daemon</h1><p>Web UI not found at /opt/aether/web/index.html</p>")


@app.get("/health")
async def health(_auth=Depends(verify_token)):
    hostname = socket.gethostname()
    return {"status": "running", "model": MODEL, "hostname": hostname, "timestamp": datetime.now().isoformat()}


@app.get("/metrics")
async def metrics(_auth=Depends(verify_token)):
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    return {
        "cpu_percent": cpu,
        "memory": {"total_gb": round(mem.total / 1e9, 2), "used_gb": round(mem.used / 1e9, 2), "percent": mem.percent},
        "disk": {"total_gb": round(disk.total / 1e9, 2), "used_gb": round(disk.used / 1e9, 2), "percent": disk.percent},
    }


def _no_key_response():
    return {"error": "No API key configured. Run the AetherOS Setup wizard or: sudo bash -c \"echo 'sk-ant-...' > /etc/aetheros/api_key && systemctl restart claused\""}


@app.post("/chat")
async def chat_endpoint(request: Request, _auth=Depends(verify_token)):
    if agent is None:
        return StreamingResponse(
            iter([f"data: {json.dumps({'chunk': 'No API key configured. Open the Setup wizard to activate Aether.'})}\n\ndata: [DONE]\n\n"]),
            media_type="text/event-stream"
        )
    body = await request.json()
    message = body.get("message", "")
    if not message:
        return {"error": "No message"}

    async def generate():
        async for chunk in agent.chat(message):
            yield f"data: {json.dumps({'chunk': chunk})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/ask")
async def ask_endpoint(request: Request, _auth=Depends(verify_token)):
    if agent is None:
        return _no_key_response()
    body = await request.json()
    message = body.get("message", "")
    full = ""
    async for chunk in agent.chat(message):
        full += chunk
    return {"response": full, "timestamp": datetime.now().isoformat()}


@app.get("/history")
async def history(_auth=Depends(verify_token)):
    return {"conversation": agent.conversation}


@app.delete("/history")
async def clear_history(_auth=Depends(verify_token)):
    agent.conversation = []
    return {"status": "cleared"}


@app.get("/actions")
async def get_actions(limit: int = 50, _auth=Depends(verify_token)):
    log_path = LOG_DIR / "actions.log"
    if not log_path.exists():
        return {"actions": []}
    lines = log_path.read_text().strip().split("\n")
    actions = [json.loads(l) for l in lines[-limit:] if l]
    return {"actions": actions}


@app.post("/wol")
async def wol_endpoint(request: Request, _auth=Depends(verify_token)):
    body = await request.json()
    mac = body.get("mac", "")
    broadcast = body.get("broadcast", "255.255.255.255")
    if not mac:
        return {"error": "MAC address required"}
    try:
        executor = ToolExecutor()
        result = executor._wake_on_lan({"mac": mac, "broadcast": broadcast})
        return {"status": result}
    except Exception as e:
        return {"error": str(e)}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    token_param = ws.query_params.get("token", "")
    client_host = ws.client.host if ws.client else ""
    is_local = client_host in ("127.0.0.1", "::1", "localhost")
    if not is_local and token_param != WEB_TOKEN:
        await ws.close(code=4001)
        return

    await ws.accept()
    if agent is None:
        await ws.send_json({"type": "chunk", "content": "No API key configured. Open the Setup wizard to activate Aether."})
        await ws.send_json({"type": "done"})
        return
    agent.ws_clients.append(ws)
    log.info("WebSocket client connected from %s", client_host)
    try:
        while True:
            data = await ws.receive_json()
            if data.get("type") == "chat":
                asyncio.create_task(_ws_chat(ws, data.get("message", "")))
    except WebSocketDisconnect:
        if ws in agent.ws_clients:
            agent.ws_clients.remove(ws)
        log.info("WebSocket client disconnected")


async def _ws_chat(ws: WebSocket, message: str):
    async for _ in agent.chat(message):
        pass


# ── Unix Socket Server ─────────────────────────────────────────────────────────

async def handle_unix_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        data = await asyncio.wait_for(reader.read(65536), timeout=5)
        request = json.loads(data.decode())
        message = request.get("message", "")
        full = ""
        async for chunk in agent.chat(message):
            full += chunk
        response = json.dumps({"response": full}).encode()
        writer.write(response)
        await writer.drain()
    except Exception as e:
        writer.write(json.dumps({"error": str(e)}).encode())
        await writer.drain()
    finally:
        writer.close()


async def start_unix_socket():
    socket_path = Path(SOCKET_PATH)
    if socket_path.exists():
        socket_path.unlink()
    server = await asyncio.start_unix_server(handle_unix_client, path=SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o666)
    log.info("Unix socket listening at %s", SOCKET_PATH)
    return server


# ── System Monitor ─────────────────────────────────────────────────────────────

async def system_monitor():
    await asyncio.sleep(30)
    while True:
        try:
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            cpu = psutil.cpu_percent(interval=1)

            if mem.percent > 90:
                await agent.run_autonomous_task(
                    f"System memory is at {mem.percent}%. Identify and kill the worst memory hogs to free up RAM. Act now."
                )
            if disk.percent > 90:
                await agent.run_autonomous_task(
                    f"Disk is {disk.percent}% full. Find and clean the largest unnecessary files (logs, temp, cache). Act now."
                )
            if cpu > 95:
                await agent.run_autonomous_task(
                    f"CPU is at {cpu}% for sustained period. Identify what's causing this and address it. Act now."
                )
        except Exception as e:
            log.error("Monitor error: %s", e)
        await asyncio.sleep(60)


# ── Main ───────────────────────────────────────────────────────────────────────

async def wait_for_api_key():
    """Poll for API key in background; activate agent when found."""
    global agent
    log.info("No API key yet — polling every 10s. Run the Setup wizard to activate.")
    while True:
        await asyncio.sleep(10)
        key = get_api_key()
        if key:
            agent = AetherAgent(key)
            log.info("API key found — agent activated!")
            try:
                await agent.run_autonomous_task(
                    "AetherOS AI agent just activated. Do a quick system health check and "
                    "send a desktop notification saying Aether is now active."
                )
            except Exception as e:
                log.warning("Activation greeting failed: %s", e)
            return


async def main():
    global agent, WEB_TOKEN

    log.info("AetherOS claused v2.0 starting...")
    log.info("Model: %s", MODEL)

    WEB_TOKEN = get_or_create_web_token()

    # Try to get API key immediately
    api_key = get_api_key()
    if api_key:
        agent = AetherAgent(api_key)
        log.info("Agent initialized with API key")
    else:
        agent = None
        log.warning("No API key configured — daemon running, AI disabled until key is set")
        log.warning("Run the Setup wizard or: echo 'sk-ant-...' | sudo tee /etc/aetheros/api_key")

    unix_server = await start_unix_socket()
    monitor_task = asyncio.create_task(system_monitor())

    if agent is None:
        asyncio.create_task(wait_for_api_key())

    config = uvicorn.Config(app, host="0.0.0.0", port=API_PORT, log_level="warning")
    server = uvicorn.Server(config)

    log.info("HTTP API + Web UI on port %d", API_PORT)
    log.info("Remote access: http://<your-ip>:%d  (token in %s)", API_PORT, TOKEN_FILE)

    if agent is not None:
        async def startup_greeting():
            await asyncio.sleep(5)
            try:
                await agent.run_autonomous_task(
                    "AetherOS has just started. Perform a quick system health check, log what you find, "
                    "and send a desktop notification greeting the user with a brief status summary."
                )
            except Exception as e:
                log.warning("Startup greeting failed: %s", e)
        asyncio.create_task(startup_greeting())

    try:
        await server.serve()
    finally:
        monitor_task.cancel()
        unix_server.close()


if __name__ == "__main__":
    asyncio.run(main())
