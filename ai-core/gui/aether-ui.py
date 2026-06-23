#!/usr/bin/env python3
"""
AetherOS - aether-ui
GTK4/libadwaita AI overlay application.
Always-on-top panel that shows Claude's activity and accepts input.
Toggle with Super+Space (configured via GNOME extension).
"""

import asyncio
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Gtk, Adw, GLib, Gdk, Pango, GdkPixbuf, Gio

DAEMON_URL = "http://localhost:7474"
WS_URL = "ws://localhost:7474/ws"
APP_ID = "os.aetheros.UI"


class MessageRow(Gtk.Box):
    def __init__(self, role: str, content: str):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.set_margin_start(12)
        self.set_margin_end(12)
        self.set_margin_top(6)
        self.set_margin_bottom(6)

        is_user = role == "user"

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        icon = Gtk.Label(label="You" if is_user else "Aether")
        icon.add_css_class("caption")
        icon.add_css_class("dim-label" if not is_user else "accent")
        header.append(icon)

        ts = Gtk.Label(label=datetime.now().strftime("%H:%M:%S"))
        ts.add_css_class("caption")
        ts.add_css_class("dim-label")
        header.append(ts)

        self.append(header)

        bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bubble.add_css_class("card")
        bubble.set_margin_start(0 if is_user else 24)
        bubble.set_margin_end(24 if is_user else 0)

        self.label = Gtk.Label(label=content)
        self.label.set_wrap(True)
        self.label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.label.set_xalign(0)
        self.label.set_selectable(True)
        self.label.set_margin_start(12)
        self.label.set_margin_end(12)
        self.label.set_margin_top(8)
        self.label.set_margin_bottom(8)

        if not is_user:
            self.label.add_css_class("monospace")

        bubble.append(self.label)
        self.append(bubble)

    def append_text(self, text: str):
        current = self.label.get_text()
        self.label.set_text(current + text)


class ToolEventRow(Gtk.Box):
    def __init__(self, tool_name: str, status: str, detail: str = ""):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.set_margin_start(36)
        self.set_margin_top(2)
        self.set_margin_bottom(2)

        icons = {
            "bash": "⚡", "read_file": "📖", "write_file": "✏️",
            "delete_file": "🗑️", "screenshot": "📸", "mouse_click": "🖱️",
            "keyboard_type": "⌨️", "list_processes": "📊", "kill_process": "⛔",
            "http_request": "🌐", "notify": "🔔", "system_info": "💻",
            "search_files": "🔍",
        }
        icon = Gtk.Label(label=icons.get(tool_name, "🔧"))

        name_label = Gtk.Label(label=f"{tool_name}")
        name_label.add_css_class("caption")
        name_label.add_css_class("accent")

        status_label = Gtk.Label(label=f"→ {status}")
        status_label.add_css_class("caption")
        status_label.add_css_class("dim-label")

        self.append(icon)
        self.append(name_label)
        self.append(status_label)


class AetherWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self.set_title("Aether AI")
        self.set_default_size(480, 700)
        self.set_resizable(True)

        css = Gtk.CssProvider()
        css.load_from_string("""
            window { background: rgba(18, 18, 23, 0.95); }
            .aether-header { background: rgba(30, 30, 40, 0.98); border-bottom: 1px solid rgba(255,255,255,0.08); }
            .aether-input { background: rgba(30, 30, 40, 0.98); border-top: 1px solid rgba(255,255,255,0.08); }
            .status-dot { color: #4ade80; font-size: 10px; }
            .status-dot.busy { color: #f59e0b; }
            textview { background: transparent; color: #e2e8f0; font-size: 14px; }
            textview text { background: transparent; }
            entry { background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.12); border-radius: 12px; color: #e2e8f0; }
            .card { background: rgba(255,255,255,0.04); border-radius: 12px; }
            label { color: #e2e8f0; }
            .dim-label { color: rgba(226,232,240,0.5); }
            .accent { color: #818cf8; }
        """)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        self._build_ui()
        self._current_assistant_row = None
        self._is_busy = False
        self._ws_thread = None
        self._start_ws()

    def _build_ui(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(root)

        # Header
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header.add_css_class("aether-header")
        header.set_margin_start(16)
        header.set_margin_end(16)
        header.set_margin_top(12)
        header.set_margin_bottom(12)

        logo = Gtk.Label(label="◈ Aether")
        logo.add_css_class("title-4")

        self.status_dot = Gtk.Label(label="●")
        self.status_dot.add_css_class("status-dot")

        self.status_label = Gtk.Label(label="Ready")
        self.status_label.add_css_class("caption")
        self.status_label.add_css_class("dim-label")

        header.append(logo)
        header.append(self.status_dot)
        header.append(self.status_label)

        clear_btn = Gtk.Button(label="Clear")
        clear_btn.add_css_class("flat")
        clear_btn.add_css_class("caption")
        clear_btn.connect("clicked", self._on_clear)
        clear_btn.set_hexpand(True)
        clear_btn.set_halign(Gtk.Align.END)
        header.append(clear_btn)

        root.append(header)

        # Scroll area for messages
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.messages_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.messages_box.set_margin_top(8)
        self.messages_box.set_margin_bottom(8)

        scroll.set_child(self.messages_box)
        root.append(scroll)
        self._scroll = scroll

        self._add_welcome()

        # Input area
        input_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        input_area.add_css_class("aether-input")
        input_area.set_margin_start(12)
        input_area.set_margin_end(12)
        input_area.set_margin_top(10)
        input_area.set_margin_bottom(10)

        input_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        self.entry = Gtk.TextView()
        self.entry.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.entry.set_pixels_above_lines(6)
        self.entry.set_pixels_below_lines(6)
        self.entry.set_left_margin(12)
        self.entry.set_right_margin(12)
        self.entry.get_buffer().connect("changed", self._on_text_changed)

        entry_frame = Gtk.Frame()
        entry_frame.set_child(self.entry)
        entry_frame.set_hexpand(True)
        entry_frame.add_css_class("view")

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self._on_key_pressed)
        self.entry.add_controller(key_ctrl)

        self.send_btn = Gtk.Button()
        send_icon = Gtk.Image.new_from_icon_name("go-up-symbolic")
        self.send_btn.set_child(send_icon)
        self.send_btn.add_css_class("suggested-action")
        self.send_btn.add_css_class("circular")
        self.send_btn.connect("clicked", self._on_send)
        self.send_btn.set_valign(Gtk.Align.END)

        input_row.append(entry_frame)
        input_row.append(self.send_btn)

        hint = Gtk.Label(label="Enter to send  •  Shift+Enter for newline  •  Aether has full system access")
        hint.add_css_class("caption")
        hint.add_css_class("dim-label")

        input_area.append(input_row)
        input_area.append(hint)
        root.append(input_area)

    def _add_welcome(self):
        row = MessageRow("assistant", "AetherOS is active. I have full system access and I'm always watching. What do you need?")
        self.messages_box.append(row)

    def _on_text_changed(self, buf):
        pass

    def _on_key_pressed(self, ctrl, keyval, keycode, state):
        if keyval == Gdk.KEY_Return and not (state & Gdk.ModifierType.SHIFT_MASK):
            self._on_send(None)
            return True
        return False

    def _on_send(self, _btn):
        buf = self.entry.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip()
        if not text or self._is_busy:
            return
        buf.set_text("")
        self._send_message(text)

    def _send_message(self, text: str):
        row = MessageRow("user", text)
        self.messages_box.append(row)
        self._scroll_bottom()
        self._set_busy(True, "Thinking...")
        threading.Thread(target=self._fetch_response, args=(text,), daemon=True).start()

    def _fetch_response(self, text: str):
        import urllib.request
        req = urllib.request.Request(
            f"{DAEMON_URL}/ask",
            data=json.dumps({"message": text}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
            response = data.get("response", "")
            GLib.idle_add(self._append_assistant_response, response)
        except Exception as e:
            GLib.idle_add(self._append_assistant_response, f"[Error: {e}]")

    def _append_assistant_response(self, text: str):
        row = MessageRow("assistant", text)
        self.messages_box.append(row)
        self._scroll_bottom()
        self._set_busy(False, "Ready")

    def _set_busy(self, busy: bool, status: str):
        self._is_busy = busy
        self.send_btn.set_sensitive(not busy)
        self.status_label.set_text(status)
        if busy:
            self.status_dot.add_css_class("busy")
        else:
            self.status_dot.remove_css_class("busy")

    def _scroll_bottom(self):
        def do_scroll():
            adj = self._scroll.get_vadjustment()
            adj.set_value(adj.get_upper())
        GLib.idle_add(do_scroll)

    def _on_clear(self, _btn):
        import urllib.request
        req = urllib.request.Request(f"{DAEMON_URL}/history", method="DELETE")
        try:
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass
        while child := self.messages_box.get_first_child():
            self.messages_box.remove(child)
        self._add_welcome()

    def _start_ws(self):
        self._ws_thread = threading.Thread(target=self._ws_loop, daemon=True)
        self._ws_thread.start()

    def _ws_loop(self):
        try:
            import websocket
            def on_message(ws, message):
                try:
                    event = json.loads(message)
                    GLib.idle_add(self._handle_ws_event, event)
                except Exception:
                    pass
            ws = websocket.WebSocketApp(WS_URL, on_message=on_message)
            ws.run_forever(reconnect=5)
        except ImportError:
            pass

    def _handle_ws_event(self, event: dict):
        etype = event.get("type")
        if etype == "tool_start":
            self._set_busy(True, f"Running {event['name']}...")
            row = ToolEventRow(event["name"], "running")
            self.messages_box.append(row)
            self._scroll_bottom()
        elif etype == "chunk":
            if self._current_assistant_row is None:
                self._current_assistant_row = MessageRow("assistant", "")
                self.messages_box.append(self._current_assistant_row)
            self._current_assistant_row.append_text(event["content"])
            self._scroll_bottom()
        elif etype == "done":
            self._current_assistant_row = None
            self._set_busy(False, "Ready")


class AetherApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.connect("activate", self._on_activate)

    def _on_activate(self, app):
        win = AetherWindow(app)
        win.present()


def main():
    app = AetherApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
