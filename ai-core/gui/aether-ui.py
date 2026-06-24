#!/usr/bin/env python3
"""
AetherOS — Aether chat GUI (GTK4 / libadwaita)

A native-feeling assistant window: live token streaming, tool-call cards,
markdown rendering, quick actions, and a status pill. Toggled with Super+A.
Streams over the daemon WebSocket (ws://localhost:7474/ws).
"""
import asyncio
import json
import threading
import sys
from datetime import datetime

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Gtk, Adw, GLib, Gdk, Pango, Gio, GObject


def copy_text(text):
    provider = Gdk.ContentProvider.new_for_value(GObject.Value(str, text or ""))
    Gdk.Display.get_default().get_clipboard().set_content(provider)

DAEMON = "http://localhost:7474"
WS_URL = "ws://localhost:7474/ws"
APP_ID = "os.aetheros.Chat"

TOOL_ICONS = {
    "bash": "utilities-terminal-symbolic", "read_file": "text-x-generic-symbolic",
    "write_file": "document-edit-symbolic", "edit_file": "document-edit-symbolic",
    "delete_file": "user-trash-symbolic", "screenshot": "camera-photo-symbolic",
    "mouse_click": "input-mouse-symbolic", "keyboard_type": "input-keyboard-symbolic",
    "list_processes": "view-list-symbolic", "kill_process": "process-stop-symbolic",
    "http_request": "network-transmit-receive-symbolic", "notify": "preferences-system-notifications-symbolic",
    "system_info": "computer-symbolic", "search_files": "system-search-symbolic",
    "install_package": "system-software-install-symbolic", "open_application": "application-x-executable-symbolic",
    "get_clipboard": "edit-paste-symbolic", "set_clipboard": "edit-copy-symbolic",
    "manage_service": "applications-system-symbolic", "wake_on_lan": "network-wired-symbolic",
    "quarantine_file": "security-high-symbolic", "set_performance_mode": "power-profile-performance-symbolic",
}

CSS = """
.aether-user { background: alpha(@accent_bg_color, 0.16); border-radius: 16px 16px 4px 16px; }
.aether-assistant { background: alpha(@card_bg_color, 0.55); border-radius: 16px 16px 16px 4px; }
.aether-bubble { padding: 10px 14px; }
.aether-role { font-size: 0.8em; font-weight: 700; }
.aether-code { background: alpha(#000000, 0.32); border-radius: 10px; padding: 6px 10px 10px 12px; }
.code-text { font-family: 'JetBrains Mono', monospace; font-size: 0.92em; }
.tool-card { background: alpha(@card_bg_color, 0.6); border-radius: 12px; padding: 6px 10px;
             border-left: 3px solid @accent_color; }
.tool-name { font-weight: 700; font-size: 0.85em; }
.status-pill { border-radius: 999px; padding: 2px 12px; background: alpha(@accent_bg_color, 0.18);
               font-size: 0.82em; font-weight: 600; }
.chip { border-radius: 999px; padding: 6px 14px; }
.glyph { color: @accent_color; font-weight: 800; }
.dim { opacity: 0.6; }
"""


def md_to_pango(text: str) -> str:
    """Minimal, safe markdown → Pango markup (for non-code text segments)."""
    out = GLib.markup_escape_text(text)
    import re
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", out)
    out = re.sub(r"`([^`]+?)`", r'<tt>\1</tt>', out)
    lines = []
    for ln in out.split("\n"):
        s = ln.lstrip()
        if s.startswith("### "):
            ln = f"<b>{s[4:]}</b>"
        elif s.startswith("## "):
            ln = f"<big><b>{s[3:]}</b></big>"
        elif s.startswith("# "):
            ln = f"<big><b>{s[2:]}</b></big>"
        elif s.startswith(("- ", "* ")):
            ln = "  • " + s[2:]
        lines.append(ln)
    return "\n".join(lines)


class WSClient:
    """Background WebSocket client. Delivers daemon events to the GTK thread."""
    def __init__(self, on_event):
        self.on_event = on_event
        self.loop = None
        self._q = None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._main())

    async def _main(self):
        import websockets
        self._q = asyncio.Queue()
        while True:
            try:
                async with websockets.connect(WS_URL, max_size=8 * 1024 * 1024) as ws:
                    GLib.idle_add(self.on_event, {"type": "_connected"})
                    send_task = asyncio.create_task(self._sender(ws))
                    async for msg in ws:
                        try:
                            GLib.idle_add(self.on_event, json.loads(msg))
                        except Exception:
                            pass
                    send_task.cancel()
            except Exception:
                GLib.idle_add(self.on_event, {"type": "_disconnected"})
                await asyncio.sleep(2)

    async def _sender(self, ws):
        while True:
            text = await self._q.get()
            await ws.send(json.dumps({"type": "chat", "message": text}))

    def send(self, text):
        if self.loop and self._q is not None:
            asyncio.run_coroutine_threadsafe(self._q.put(text), self.loop)


class AetherWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self.set_title("Aether")
        self.set_default_size(560, 760)
        self._busy = False
        self._live_label = None          # the streaming assistant label
        self._live_text = ""
        self._tool_cards = {}

        prov = Gtk.CssProvider()
        prov.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self._pending = getattr(app, "initial_query", None)
        self._build()
        self.ws = WSClient(self._on_ws)
        self._refresh_status()

    # ── UI ───────────────────────────────────────────────────────────────────
    def _build(self):
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        title = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        glyph = Gtk.Label(label="◈"); glyph.add_css_class("glyph")
        name = Gtk.Label(label="Aether"); name.add_css_class("heading")
        title.append(glyph); title.append(name)
        header.set_title_widget(title)

        self.status = Gtk.Label(label="Connecting…")
        self.status.add_css_class("status-pill")
        header.pack_start(self.status)

        menu = Gio.Menu()
        menu.append("New conversation", "win.clear")
        menu.append("Aether Settings", "win.settings")
        menu.append("Open Web UI", "win.webui")
        mbtn = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        header.pack_end(mbtn)
        toolbar.add_top_bar(header)

        for name_, cb in [("clear", self._clear), ("settings", self._open_settings),
                          ("webui", self._open_webui)]:
            act = Gio.SimpleAction.new(name_, None)
            act.connect("activate", cb)
            self.add_action(act)

        # messages
        self.scroller = Gtk.ScrolledWindow(vexpand=True)
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.msgs = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.msgs.set_margin_top(16); self.msgs.set_margin_bottom(16)
        self.msgs.set_margin_start(16); self.msgs.set_margin_end(16)
        clamp = Adw.Clamp(maximum_size=720, child=self.msgs)
        self.scroller.set_child(clamp)

        bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        bottom.append(self._quick_chips())
        bottom.append(self._composer())
        bottom.set_margin_start(12); bottom.set_margin_end(12); bottom.set_margin_bottom(12)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(self.scroller)
        content.append(bottom)
        toolbar.set_content(content)
        self.set_content(toolbar)

        # Esc closes the window (overlay feel) when the composer isn't focused.
        esc = Gtk.EventControllerKey()
        esc.connect("key-pressed", self._on_window_key)
        self.add_controller(esc)

        self._welcome()

    def _on_window_key(self, _c, keyval, _kc, _state):
        if keyval == Gdk.KEY_Escape and not self.entry.has_focus():
            self.close()
            return True
        return False

    def _quick_chips(self):
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        scroll.set_propagate_natural_height(True)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        chips = [
            ("📊 System status", "Give me a concise system status: CPU, memory, disk, and anything notable."),
            ("🧹 Free up space", "Find what's safe to clean and free up disk space, then tell me what you did."),
            ("🔍 What's slow?", "What's using the most CPU and memory right now? Summarize."),
            ("📸 Explain my screen", "Take a screenshot and tell me what's on my screen."),
            ("🔧 Update system", "Update all system packages safely and summarize what changed."),
        ]
        for label, prompt in chips:
            b = Gtk.Button(label=label)
            b.add_css_class("chip"); b.add_css_class("pill")
            b.connect("clicked", lambda _b, p=prompt: self._send(p))
            row.append(b)
        scroll.set_child(row)
        return scroll

    def _composer(self):
        frame = Gtk.Frame(); frame.add_css_class("card")
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_margin_start(6); box.set_margin_end(6)
        box.set_margin_top(6); box.set_margin_bottom(6)

        self.entry = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.entry.set_hexpand(True)
        self.entry.set_top_margin(8); self.entry.set_bottom_margin(8)
        self.entry.set_left_margin(10); self.entry.set_right_margin(10)
        self.entry.add_css_class("aether-input")
        kc = Gtk.EventControllerKey()
        kc.connect("key-pressed", self._on_key)
        self.entry.add_controller(kc)
        sw = Gtk.ScrolledWindow(); sw.set_max_content_height(140)
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.set_child(self.entry); sw.set_hexpand(True)

        self.send_btn = Gtk.Button(icon_name="go-up-symbolic", valign=Gtk.Align.END)
        self.send_btn.add_css_class("suggested-action"); self.send_btn.add_css_class("circular")
        self.send_btn.connect("clicked", lambda *_: self._submit())

        box.append(sw); box.append(self.send_btn)
        frame.set_child(box)
        return frame

    # ── message widgets ───────────────────────────────────────────────────────
    def _bubble(self, role):
        wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        is_user = role == "user"
        wrap.set_halign(Gtk.Align.END if is_user else Gtk.Align.START)
        r = Gtk.Label(label="You" if is_user else "◈ Aether", xalign=1 if is_user else 0)
        r.add_css_class("aether-role"); r.add_css_class("dim")
        bub = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        bub.add_css_class("aether-bubble")
        bub.add_css_class("aether-user" if is_user else "aether-assistant")
        bub.set_size_request(min(120, 480), -1)
        wrap.append(r); wrap.append(bub)
        self.msgs.append(wrap)
        return bub

    def _text_label(self, markup_text, code=False):
        lbl = Gtk.Label(xalign=0, wrap=True, selectable=True)
        lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        if code:
            lbl.set_text(markup_text); lbl.add_css_class("aether-code")
        else:
            try:
                lbl.set_markup(md_to_pango(markup_text))
            except Exception:
                lbl.set_text(markup_text)
        return lbl

    def _render_rich(self, bubble, text):
        """Replace a bubble's content with code-aware rich rendering."""
        while c := bubble.get_first_child():
            bubble.remove(c)
        parts = text.split("```")
        for i, part in enumerate(parts):
            if not part.strip():
                continue
            if i % 2 == 1:  # code block (optional language on the first line)
                lang, body = ("", part)
                if "\n" in part:
                    first, body = part.split("\n", 1)
                    if first.strip() and " " not in first.strip():
                        lang = first.strip()
                bubble.append(self._code_block(body.rstrip("\n"), lang))
            else:
                bubble.append(self._text_label(part.strip()))

    def _code_block(self, code, lang=""):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.add_css_class("aether-code")
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        tag = Gtk.Label(label=lang or "code", xalign=0, hexpand=True)
        tag.add_css_class("caption"); tag.add_css_class("dim")
        copy = Gtk.Button(icon_name="edit-copy-symbolic", css_classes=["flat", "circular"])
        copy.set_tooltip_text("Copy")
        copy.connect("clicked", lambda *_: copy_text(code))
        bar.append(tag); bar.append(copy)
        lbl = Gtk.Label(label=code, xalign=0, selectable=True, wrap=True)
        lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        lbl.add_css_class("code-text")
        box.append(bar); box.append(lbl)
        return box

    # ── events ────────────────────────────────────────────────────────────────
    def _welcome(self):
        b = self._bubble("assistant")
        b.append(self._text_label(
            "Hi, I'm **Aether** — your system's AI. I'm always running and I can do "
            "anything on this machine: answer questions, write & run code, fix things, "
            "or take on a full multi-step task.\n\nWhat do you need?"))

    def _on_key(self, _c, keyval, _kc, state):
        if keyval == Gdk.KEY_Return and not (state & Gdk.ModifierType.SHIFT_MASK):
            self._submit(); return True
        return False

    def _submit(self):
        buf = self.entry.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip()
        if not text or self._busy:
            return
        buf.set_text("")
        self._send(text)

    def _send(self, text):
        b = self._bubble("user")
        b.append(self._text_label(text))
        self._set_busy(True, "Thinking…")
        self._live_label = None
        self._live_text = ""
        self.ws.send(text)
        self._scroll()

    def _on_ws(self, ev):
        t = ev.get("type")
        if t == "_connected":
            self._refresh_status()
            if self._pending:
                q, self._pending = self._pending, None
                GLib.timeout_add(250, lambda: (self._send(q), False)[1])
        elif t == "_disconnected":
            self.status.set_text("Reconnecting…")
        elif t == "user":
            pass  # rendered locally
        elif t == "tool_start":
            self._add_tool(ev.get("name", "tool"), ev.get("input", {}))
        elif t == "tool_result":
            self._finish_tool(ev.get("name", "tool"), ev.get("result", ""))
        elif t == "chunk":
            self._stream(ev.get("content", ""))
        elif t == "done":
            self._finalize()
        return False

    def _stream(self, chunk):
        if self._live_label is None:
            bub = self._bubble("assistant")
            self._live_label = self._text_label("")
            bub.append(self._live_label)
            self._live_bubble = bub
        self._live_text += chunk
        self._live_label.set_text(self._live_text)
        self._set_busy(True, "Writing…")
        self._scroll()

    def _finalize(self):
        if self._live_label is not None and self._live_text:
            self._render_rich(self._live_bubble, self._live_text)
        self._live_label = None
        self._live_text = ""
        self._set_busy(False, "Ready")
        self._scroll()

    def _add_tool(self, name, inp):
        card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        card.add_css_class("tool-card")
        card.append(Gtk.Image.new_from_icon_name(TOOL_ICONS.get(name, "applications-system-symbolic")))
        lbl = Gtk.Label(label=name, xalign=0); lbl.add_css_class("tool-name")
        spin = Gtk.Spinner(spinning=True)
        detail = Gtk.Label(label=self._tool_summary(name, inp), xalign=0)
        detail.add_css_class("dim"); detail.add_css_class("caption")
        detail.set_ellipsize(Pango.EllipsizeMode.END); detail.set_hexpand(True)
        card.append(lbl); card.append(detail); card.append(spin)
        self.msgs.append(card)
        self._tool_cards[name] = (card, spin)
        self._set_busy(True, f"Running {name}…")
        self._scroll()

    def _finish_tool(self, name, result):
        item = self._tool_cards.pop(name, None)
        if item:
            card, spin = item
            spin.set_spinning(False); spin.set_visible(False)
            check = Gtk.Image.new_from_icon_name("emblem-ok-symbolic")
            card.append(check)

    def _tool_summary(self, name, inp):
        if not isinstance(inp, dict):
            return ""
        for k in ("command", "path", "package", "app", "service", "url", "task"):
            if k in inp:
                return str(inp[k])[:80]
        return ""

    # ── status / actions ───────────────────────────────────────────────────────
    def _set_busy(self, busy, status):
        self._busy = busy
        self.send_btn.set_sensitive(not busy)
        self.status.set_text(status)

    def _refresh_status(self):
        def work():
            import urllib.request
            try:
                with urllib.request.urlopen(f"{DAEMON}/health", timeout=4) as r:
                    d = json.loads(r.read())
                txt = "Ready" if d.get("agent_ready") else "Add API key in Settings"
                if d.get("autonomy"):
                    txt += f"  ·  autonomy: {d['autonomy']}"
                GLib.idle_add(self.status.set_text, txt)
            except Exception:
                GLib.idle_add(self.status.set_text, "Daemon offline")
        threading.Thread(target=work, daemon=True).start()

    def _scroll(self):
        def go():
            adj = self.scroller.get_vadjustment()
            adj.set_value(adj.get_upper())
            return False
        GLib.idle_add(go)

    def _clear(self, *_):
        import urllib.request
        try:
            urllib.request.urlopen(urllib.request.Request(f"{DAEMON}/history", method="DELETE"), timeout=4)
        except Exception:
            pass
        while c := self.msgs.get_first_child():
            self.msgs.remove(c)
        self._welcome()

    def _open_settings(self, *_):
        GLib.spawn_async(["/opt/aether/venv/bin/python3", "/opt/aether/gui/aether-settings.py"],
                         flags=GLib.SpawnFlags.SEARCH_PATH)

    def _open_webui(self, *_):
        Gtk.UriLauncher.new(f"{DAEMON}/").launch(self, None)


class AetherApp(Adw.Application):
    def __init__(self, initial_query=None):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.initial_query = initial_query

    def do_activate(self):
        win = self.props.active_window or AetherWindow(self)
        win.present()


def main():
    Adw.init()
    query = None
    if "--ask" in sys.argv:
        i = sys.argv.index("--ask")
        if i + 1 < len(sys.argv):
            query = sys.argv[i + 1]
    return AetherApp(initial_query=query).run([])


if __name__ == "__main__":
    sys.exit(main())
