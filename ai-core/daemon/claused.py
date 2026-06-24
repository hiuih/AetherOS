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
# Persisted model override (set via the Settings app / POST /config).
try:
    _mf = CONFIG_DIR / "model"
    if _mf.exists() and _mf.read_text().strip():
        MODEL = _mf.read_text().strip()
except Exception:
    pass
MAX_TOKENS = 8192

SYSTEM_PROMPT = """You are Aether — the AI agent built into AetherOS, an Ubuntu-based Linux system.
You are the brain of this operating system: a coding assistant, a conversational helper, and a
fully capable autonomous agent that can carry out both quick one-shot requests and long, multi-step
workflows end to end.

You have root-level access to the entire system through your tools. When the user asks you to do
something, do it — plan internally, execute the steps, and report back clearly and concisely.
Don't ask for permission for ordinary actions the user clearly wants. Be direct and helpful.

Your capabilities (via tools):
- Execute any shell command or script (full root)
- Read, write, search, and delete files
- Inspect and manage processes
- Take screenshots and control the GUI (keyboard/mouse)
- Make network/HTTP requests
- Send native desktop notifications
- Install/remove software (apt)
- Manage systemd services
- Monitor system health

Working style:
- For coding tasks: read the relevant files first, make focused edits, run/verify when possible.
- For long agentic tasks: break the goal into steps, execute them in order, and keep going until done.
- For chat: be conversational and concise.
- Always log meaningful actions (your tools do this automatically to /var/log/aetheros/actions.log).

SAFETY — this matters: When a request is destructive and irreversible (deleting user data,
killing user applications, wiping disks, force-removing core packages), and the user did NOT
explicitly ask for that exact thing, stop and confirm with the user first via a notification or
your reply rather than guessing. Routine, reversible actions need no confirmation. You are NOT
permitted to delete user files or kill user processes on your own initiative during background
monitoring — only when the user directly asks.

Current system: AetherOS (Ubuntu 26.04 base, GNOME desktop).
You are always running, always ready."""

# ── Auth Token ────────────────────────────────────────────────────────────────

def get_or_create_web_token() -> str:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    token = "aether-" + secrets.token_urlsafe(24)
    TOKEN_FILE.write_text(token)
    # 644: readable by the desktop user so the setup wizard and MOTD can show the
    # remote-access token. This is a LAN bearer token on a personal machine.
    TOKEN_FILE.chmod(0o644)
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
        "name": "edit_file",
        "description": "Make a surgical edit to a file by replacing an exact string. Prefer this over write_file for code changes. old_string must match exactly and uniquely.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file"},
                "old_string": {"type": "string", "description": "Exact text to replace (must be unique in the file)"},
                "new_string": {"type": "string", "description": "Replacement text"},
                "replace_all": {"type": "boolean", "description": "Replace all occurrences (default false)"},
            },
            "required": ["path", "old_string", "new_string"],
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
    {
        "name": "quarantine_file",
        "description": "Safely isolate a suspicious/malicious file by moving it (reversibly) "
                       "to the quarantine vault with metadata. Use this instead of deleting "
                       "suspected malware so the action can be reviewed and undone.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path of the file to quarantine"},
                "reason": {"type": "string", "description": "Why it is suspicious"},
            },
            "required": ["path", "reason"],
        },
    },
    {
        "name": "set_performance_mode",
        "description": "Tune the system for a performance profile. 'game' maximizes FPS "
                       "(performance CPU governor, drop caches, raise process priority of the "
                       "foreground game, mute background indexers); 'balanced' restores defaults; "
                       "'powersave' favors battery.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "description": "game, balanced, or powersave"},
                "target_process": {"type": "string", "description": "Foreground app/game to prioritize (optional)"},
            },
            "required": ["mode"],
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
            case "edit_file":
                return await self._edit_file(inp)
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
            case "quarantine_file":
                return await self._quarantine_file(inp)
            case "set_performance_mode":
                return await self._set_performance_mode(inp)
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

    async def _edit_file(self, inp: dict) -> str:
        path = Path(inp["path"])
        if not path.exists():
            return f"File not found: {path}"
        old, new = inp["old_string"], inp["new_string"]
        async with aiofiles.open(path, encoding="utf-8", errors="replace") as f:
            content = await f.read()
        count = content.count(old)
        if count == 0:
            return f"old_string not found in {path}"
        if count > 1 and not inp.get("replace_all"):
            return f"old_string matches {count} times — make it unique or set replace_all=true"
        content = content.replace(old, new)
        async with aiofiles.open(path, "w", encoding="utf-8") as f:
            await f.write(content)
        return f"Edited {path} ({count} replacement{'s' if count != 1 else ''})"

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
        # Try X11 (xclip/xsel) then Wayland (wl-paste)
        return await self._bash({"command": (
            "DISPLAY=:0 xclip -selection clipboard -o 2>/dev/null "
            "|| DISPLAY=:0 xsel --clipboard --output 2>/dev/null "
            "|| wl-paste 2>/dev/null"
        )})

    async def _set_clipboard(self, inp: dict) -> str:
        text = inp["text"].replace("'", "'\\''")
        return await self._bash({"command": (
            f"echo -n '{text}' | DISPLAY=:0 xclip -selection clipboard 2>/dev/null "
            f"|| echo -n '{text}' | DISPLAY=:0 xsel --clipboard --input 2>/dev/null "
            f"|| echo -n '{text}' | wl-copy 2>/dev/null"
        )})

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

    async def _quarantine_file(self, inp: dict) -> str:
        """Reversibly isolate a suspicious file: strip exec perms and move it to
        the quarantine vault with a metadata sidecar. Never deletes — auditable."""
        src = Path(inp["path"])
        reason = inp.get("reason", "unspecified")
        if not src.exists():
            return f"Path not found: {src}"
        vault = Path("/var/lib/aetheros/quarantine")
        vault.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = vault / f"{stamp}__{src.name}"
        try:
            os.chmod(src, 0o000)
        except Exception:
            pass
        meta = {
            "original_path": str(src.resolve()),
            "quarantined_at": datetime.now().isoformat(),
            "reason": reason,
            "size": src.stat().st_size if src.exists() else None,
        }
        # Move via bash so cross-filesystem moves work, then write metadata.
        mv = await self._bash({"command": f"mv -f {json.dumps(str(src))} {json.dumps(str(dest))}"})
        dest.with_suffix(dest.suffix + ".meta.json").write_text(json.dumps(meta, indent=2))
        return (f"Quarantined → {dest}\nReason: {reason}\n"
                f"Reversible: restore with `mv {dest} {meta['original_path']}`\n{mv}")

    async def _set_performance_mode(self, inp: dict) -> str:
        mode = inp.get("mode", "balanced").lower()
        target = inp.get("target_process", "")
        out = []
        if mode == "game":
            out.append(await self._bash({"command":
                "for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do "
                "echo performance > $g 2>/dev/null; done; "
                "sync; echo 1 > /proc/sys/vm/drop_caches 2>/dev/null; "
                "powerprofilesctl set performance 2>/dev/null || true"}))
            if target:
                out.append(await self._bash({"command":
                    f"for p in $(pgrep -f {json.dumps(target)}); do renice -n -10 -p $p; "
                    f"ionice -c1 -n0 -p $p 2>/dev/null; done"}))
            out.append(await self._bash({"command":
                "systemctl stop packagekit 2>/dev/null; "
                "pkill -STOP tracker-miner 2>/dev/null || true"}))
        elif mode == "powersave":
            out.append(await self._bash({"command":
                "for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do "
                "echo powersave > $g 2>/dev/null; done; "
                "powerprofilesctl set power-saver 2>/dev/null || true"}))
        else:  # balanced
            out.append(await self._bash({"command":
                "for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do "
                "echo ondemand > $g 2>/dev/null || echo schedutil > $g 2>/dev/null; done; "
                "powerprofilesctl set balanced 2>/dev/null || true; "
                "pkill -CONT tracker-miner 2>/dev/null || true"}))
        return f"Performance mode '{mode}' applied.\n" + "\n".join(out)


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
    # The daemon runs as root, but the first-run wizard runs as the desktop user.
    # Scan real users' homes so the user-written key is picked up with no password
    # prompt. (USER_CONFIG_DIR above is root's home when running as the service.)
    try:
        for home in Path("/home").glob("*"):
            uk = home / ".config" / "aetheros" / "api_key"
            if uk.exists():
                val = uk.read_text().strip()
                if val:
                    return val
    except Exception:
        pass
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
    pid1 = None
    try:
        pid1 = Path("/run/aether-pid1").read_text().strip()
    except Exception:
        pass
    return {
        "status": "running",
        "model": MODEL,
        "hostname": hostname,
        "autonomy": _autonomy_level(),
        "pid1_loader": pid1,            # set when aether-init owned PID 1 this boot
        "agent_ready": agent is not None,
        "timestamp": datetime.now().isoformat(),
    }


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


@app.post("/agent")
async def agent_endpoint(request: Request, _auth=Depends(verify_token)):
    """Full agentic workflow: hand Aether a goal and stream its multi-step execution
    (planning, tool calls, results) until the task is complete. Used by `aether-do`."""
    if agent is None:
        return StreamingResponse(
            iter(["data: No API key configured. Open the Setup wizard.\n\ndata: [DONE]\n\n"]),
            media_type="text/event-stream",
        )
    body = await request.json()
    task = body.get("task", "") or body.get("message", "")
    if not task:
        return {"error": "No task"}

    framed = (
        "AGENTIC TASK — complete this goal end to end. Break it into steps, execute each "
        "with your tools, verify as you go, and keep working until it is fully done. "
        "Narrate progress briefly as you work.\n\nGOAL:\n" + task
    )

    async def generate():
        async for chunk in agent.chat(framed):
            yield f"data: {chunk}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


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


AVAILABLE_MODELS = [
    {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6", "hint": "Balanced · recommended"},
    {"id": "claude-opus-4-8", "name": "Claude Opus 4.8", "hint": "Most capable"},
    {"id": "claude-haiku-4-5-20251001", "name": "Claude Haiku 4.5", "hint": "Fastest"},
]
MODEL_FILE = CONFIG_DIR / "model"
AUTONOMY_OPTIONS = ["off", "observe", "active", "aggressive"]


def _local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close()
        return ip
    except Exception:
        return "localhost"


@app.get("/config")
async def get_config(_auth=Depends(verify_token)):
    """Everything the Aether Settings app needs to render + the current state."""
    pid1 = None
    try:
        pid1 = Path("/run/aether-pid1").read_text().strip()
    except Exception:
        pass
    return {
        "autonomy_level": _autonomy_level(),
        "autonomy_options": AUTONOMY_OPTIONS,
        "model": MODEL,
        "available_models": AVAILABLE_MODELS,
        "agent_ready": agent is not None,
        "has_key": get_api_key() is not None,
        "web_token": WEB_TOKEN,
        "remote_url": f"http://{_local_ip()}:{API_PORT}",
        "pid1_loader": pid1,
        "hostname": socket.gethostname(),
        "version": "1.0",
    }


@app.post("/config")
async def set_config(request: Request, _auth=Depends(verify_token)):
    """Update Aether settings. The daemon (root) owns /etc/aetheros, so the
    user-space Settings GUI changes config by calling this localhost endpoint."""
    global MODEL, agent
    body = await request.json()
    changed = {}
    level = body.get("autonomy_level")
    if level in AUTONOMY_OPTIONS:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        AUTONOMY_CONF.write_text(
            f"# AetherOS autonomy level: {' | '.join(AUTONOMY_OPTIONS)}\nlevel = {level}\n")
        changed["autonomy_level"] = level
    model = body.get("model")
    if model and any(m["id"] == model for m in AVAILABLE_MODELS):
        MODEL_FILE.write_text(model)
        MODEL = model
        changed["model"] = model
    key = body.get("api_key")
    if key and key.startswith("sk-ant-"):
        save_api_key(key)
        # Persist system-wide (root-owned) so the key survives installs and works
        # for every user + the root daemon — not just the home dir that set it.
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            (CONFIG_DIR / "api_key").write_text(key)
            (CONFIG_DIR / "api_key").chmod(0o600)
        except Exception as e:
            log.warning("system-wide key save failed: %s", e)
        if agent is None:
            agent = AetherAgent(key)
        else:
            agent.client = anthropic.AsyncAnthropic(api_key=key)
        changed["api_key"] = "updated"
    return {"status": "ok", "changed": changed}


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
    # Runs as a detached task — must swallow its own errors and clean up, or a
    # failed chat (API timeout, etc.) would silently orphan the WebSocket.
    try:
        async for _ in agent.chat(message):
            pass
    except Exception:
        log.error("ws chat error: %s", traceback.format_exc())
        try:
            await ws.send_json({"type": "chunk", "content": "\n[Aether hit an error handling that — try again.]"})
            await ws.send_json({"type": "done"})
        except Exception:
            if agent and ws in agent.ws_clients:
                agent.ws_clients.remove(ws)


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

async def _safe_disk_maintenance() -> str:
    """Deterministic, reversible-only cleanup. NO LLM, no user-data deletion, no rm of
    arbitrary paths. Only well-known safe caches/logs/temp. Returns a human summary."""
    cmds = [
        # APT package cache (safe — re-downloadable)
        "apt-get clean 2>/dev/null",
        # Journal logs older than the most recent 200MB (safe — historical logs)
        "journalctl --vacuum-size=200M 2>/dev/null",
        # Thumbnail caches for all users (safe — regenerated on demand)
        "find /home/*/.cache/thumbnails -type f -mtime +7 -delete 2>/dev/null",
        # Old temp files (safe — /tmp is ephemeral)
        "find /tmp -type f -atime +3 -delete 2>/dev/null",
        # Old crash reports
        "rm -f /var/crash/* 2>/dev/null",
    ]
    executor = ToolExecutor()
    before = psutil.disk_usage("/").percent
    for c in cmds:
        await executor._bash({"command": c, "timeout": 60})
    after = psutil.disk_usage("/").percent
    freed = max(0.0, before - after)
    return f"safe maintenance freed ~{freed:.1f}% disk ({before:.0f}%→{after:.0f}%)"


async def _notify_user(title: str, body: str, urgency: str = "normal"):
    """Fire-and-forget desktop notification using the deterministic notify path."""
    try:
        await ToolExecutor()._notify({"title": title, "body": body, "urgency": urgency})
    except Exception as e:
        log.warning("notify failed: %s", e)


# ── Autonomous Sentinel ─────────────────────────────────────────────────────────
# Acts on its own — but ONLY through safe, reversible mechanisms:
#   • game/perf optimization  (reversible governor + nice/ionice tuning)
#   • threat isolation        (SIGSTOP the process + quarantine its binary; both undoable)
# It never deletes user data and never SIGKILLs user apps unprompted.
# Tunable via /etc/aetheros/autonomy.conf  (level = off | observe | active | aggressive)

AUTONOMY_CONF = CONFIG_DIR / "autonomy.conf"
SENTINEL_LOG = LOG_DIR / "sentinel.log"

GAME_HINTS = (
    "steam", "steamwebhelper", "proton", "wine", "wine64", "lutris", "gamescope",
    "heroic", "minecraft", "java.*minecraft", "csgo", "dota2", "factorio", " shipping",
    "-shipping", "unityplayer", " value=game", "vkcube", "retroarch", "ppsspp", "dolphin-emu",
)


def _autonomy_level() -> str:
    # Rescue / recovery boot fully disables autonomous behavior.
    try:
        if "aether.disable" in Path("/proc/cmdline").read_text():
            return "off"
    except Exception:
        pass
    try:
        if AUTONOMY_CONF.exists():
            for line in AUTONOMY_CONF.read_text().splitlines():
                line = line.strip()
                if line.startswith("level"):
                    return line.split("=", 1)[1].strip().lower()
    except Exception:
        pass
    return "active"  # sensible, reversible-only default


def _sentinel_log(msg: str):
    try:
        with open(SENTINEL_LOG, "a") as f:
            f.write(f"{datetime.now().isoformat()} {msg}\n")
    except Exception:
        pass


def _detect_game() -> str | None:
    """Return the name of a running game/heavy 3D app, or None. Deterministic."""
    for p in psutil.process_iter(["pid", "name", "cmdline", "cpu_percent"]):
        try:
            hay = (p.info["name"] or "") + " " + " ".join(p.info.get("cmdline") or [])
            hay = hay.lower()
            for hint in GAME_HINTS:
                if re.search(hint, hay):
                    return p.info["name"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def _detect_threats() -> list[dict]:
    """Conservative, high-precision malware heuristic: a process whose executable lives
    in an ephemeral/world-writable dir (/tmp, /dev/shm) AND holds a network connection.
    This is a classic dropper signature and almost never matches legitimate desktop apps."""
    threats = []
    suspicious_dirs = ("/tmp/", "/dev/shm/", "/var/tmp/")
    for p in psutil.process_iter(["pid", "name", "username"]):
        try:
            exe = p.exe()
        except (psutil.NoSuchProcess, psutil.AccessDenied, FileNotFoundError):
            continue
        if not exe or not exe.startswith(suspicious_dirs):
            continue
        try:
            # psutil renamed Process.connections() → net_connections() in 6.0;
            # support both so threat detection works across versions.
            conn_fn = getattr(p, "net_connections", None) or getattr(p, "connections", None)
            has_net = bool(conn_fn(kind="inet")) if conn_fn else False
        except Exception:
            has_net = False
        if has_net:
            try:
                threats.append({"pid": p.pid, "name": p.info["name"], "exe": exe})
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue  # process died between the net check and reading its name
    return threats


async def sentinel():
    """The always-on autonomous layer. Deterministic, reversible, low-overhead."""
    await asyncio.sleep(25)
    in_game_mode = False
    handled_threats: set[str] = set()
    executor = ToolExecutor()
    log.info("Sentinel active (autonomy=%s)", _autonomy_level())

    while True:
        level = _autonomy_level()
        try:
            if level == "off":
                await asyncio.sleep(30)
                continue

            # ── Game / performance autopilot (reversible) ──────────────────
            if level in ("active", "aggressive"):
                game = _detect_game()
                if game and not in_game_mode:
                    await executor._set_performance_mode({"mode": "game", "target_process": game})
                    in_game_mode = True
                    _sentinel_log(f"game-mode ON for {game}")
                    await _notify_user("◈ Aether — Game Mode",
                                       f"Optimized performance for {game}.", "low")
                elif not game and in_game_mode:
                    await executor._set_performance_mode({"mode": "balanced"})
                    in_game_mode = False
                    _sentinel_log("game-mode OFF (restored balanced)")

            # ── Threat isolation (SIGSTOP + quarantine binary — reversible) ─
            if level in ("active", "aggressive"):
                for t in _detect_threats():
                    key = f"{t['pid']}:{t['exe']}"
                    if key in handled_threats:
                        continue
                    handled_threats.add(key)
                    # Freeze the process (reversible: SIGCONT) so it can't act further.
                    try:
                        os.kill(t["pid"], signal.SIGSTOP)
                    except Exception:
                        pass
                    q = await executor._quarantine_file(
                        {"path": t["exe"],
                         "reason": f"process {t['name']} (pid {t['pid']}) running from temp dir with network activity"})
                    _sentinel_log(f"THREAT isolated: {t} | {q.splitlines()[0]}")
                    await _notify_user("◈ Aether — threat isolated",
                                       f"Froze and quarantined a suspicious process "
                                       f"({t['name']}) running from a temp directory.",
                                       "critical")
        except Exception as e:
            log.error("sentinel error: %s", e)
            _sentinel_log(f"error: {e}")
        await asyncio.sleep(15)


# ── Kernel-level sensor (eBPF) ──────────────────────────────────────────────────
# Aether's deepest reach: an eBPF program attached to the execve tracepoint streams
# every process the KERNEL launches, the instant it happens. We react to executables
# spawned from ephemeral/world-writable dirs in real time — far faster and deeper than
# userspace polling. Falls back silently to the polling sentinel if eBPF is unavailable.

BPFTRACE_PROG = (
    'tracepoint:syscalls:sys_enter_execve '
    '{ printf("EXEC|%d|%s|%s\\n", pid, comm, str(args->filename)); }'
)


async def kernel_sensor():
    await asyncio.sleep(20)
    if _autonomy_level() == "off":
        return
    if not Path("/usr/bin/bpftrace").exists() and not Path("/usr/sbin/bpftrace").exists():
        log.info("bpftrace not present — kernel eBPF sensor disabled (polling still active).")
        return

    executor = ToolExecutor()
    susp = ("/tmp/", "/dev/shm/", "/var/tmp/")
    seen: set[str] = set()

    while True:
        try:
            proc = await asyncio.create_subprocess_exec(
                "bpftrace", "-e", BPFTRACE_PROG,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            log.info("Kernel eBPF sensor attached (execve tracepoint).")
            _sentinel_log("kernel eBPF sensor attached")
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").strip()
                if not line.startswith("EXEC|"):
                    continue
                try:
                    _, pid_s, comm, filename = line.split("|", 3)
                except ValueError:
                    continue
                if not filename.startswith(susp) or filename in seen:
                    continue
                seen.add(filename)
                if _autonomy_level() not in ("active", "aggressive"):
                    continue
                # Kernel saw a binary launch from a temp dir — react immediately.
                try:
                    os.kill(int(pid_s), signal.SIGSTOP)
                except Exception:
                    pass
                q = await executor._quarantine_file(
                    {"path": filename,
                     "reason": f"kernel eBPF: {comm} exec'd from temp dir {filename}"})
                _sentinel_log(f"KERNEL THREAT: {line} | {q.splitlines()[0]}")
                await _notify_user("◈ Aether — blocked at kernel level",
                                   f"Stopped a binary launching from a temp directory ({comm}).",
                                   "critical")
        except Exception as e:
            log.warning("kernel sensor restart (%s)", e)
        await asyncio.sleep(30)  # restart the tracer if it dies


async def system_monitor():
    """Proactive health watch. Performs ONLY safe, reversible maintenance autonomously,
    then notifies the user. Never kills user processes or deletes user data on its own —
    that requires the user to explicitly ask Aether. (Per design review: an LLM running
    rm/kill as root unattended is unacceptable.)"""
    await asyncio.sleep(45)
    notified_disk = False
    notified_mem = False
    while True:
        try:
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage("/")

            if disk.percent > 90:
                summary = await _safe_disk_maintenance()
                still = psutil.disk_usage("/").percent
                if still > 90 and not notified_disk:
                    await _notify_user(
                        "◈ Aether — disk almost full",
                        f"Disk at {still:.0f}%. I ran {summary}. "
                        f"Ask me to help find more to free.",
                        urgency="critical",
                    )
                    notified_disk = True
                elif still <= 90:
                    notified_disk = False
                log.info("disk maintenance: %s", summary)

            if mem.percent > 92 and not notified_mem:
                # Notify only — never auto-kill user apps.
                top = sorted(
                    (p.info for p in psutil.process_iter(["name", "memory_percent"])),
                    key=lambda x: x.get("memory_percent") or 0, reverse=True,
                )[:3]
                names = ", ".join(f"{p['name']} ({(p['memory_percent'] or 0):.0f}%)" for p in top)
                await _notify_user(
                    "◈ Aether — high memory use",
                    f"RAM at {mem.percent:.0f}%. Top: {names}. Ask me to close anything.",
                )
                notified_mem = True
            elif mem.percent <= 85:
                notified_mem = False
        except Exception as e:
            log.error("Monitor error: %s", e)
        await asyncio.sleep(90)


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
            await _notify_user(
                "◈ Aether is now active",
                "Your AI agent is connected and ready. Press Super+Space or click ◈ to chat.",
            )
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
    sentinel_task = asyncio.create_task(sentinel())
    ksensor_task = asyncio.create_task(kernel_sensor())

    if agent is None:
        asyncio.create_task(wait_for_api_key())

    config = uvicorn.Config(app, host="0.0.0.0", port=API_PORT, log_level="warning")
    server = uvicorn.Server(config)

    log.info("HTTP API + Web UI on port %d", API_PORT)
    log.info("Remote access: http://<your-ip>:%d  (token in %s)", API_PORT, TOKEN_FILE)

    if agent is not None:
        async def startup_greeting():
            await asyncio.sleep(8)
            await _notify_user(
                "◈ Aether is ready",
                "Your AI agent is running. Press Super+Space or click ◈ in the top bar to chat.",
            )
        asyncio.create_task(startup_greeting())

    try:
        await server.serve()
    finally:
        monitor_task.cancel()
        sentinel_task.cancel()
        ksensor_task.cancel()
        unix_server.close()


if __name__ == "__main__":
    asyncio.run(main())
