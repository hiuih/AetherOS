#!/usr/bin/env python3
"""
AetherOS — first-run setup wizard.
A friendly GTK4/libadwaita dialog that connects the user's Claude API key so
Aether activates. The daemon (claused) polls for the key and turns on within
seconds — no restart or password prompt needed.

  setup.py             always show the wizard
  setup.py --autostart show only if no key is configured yet (used at first login)
"""
import os
import socket
import sys
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gdk, GLib

CONFIG_DIR = Path(os.path.expanduser("~/.config/aetheros"))
KEY_FILE = CONFIG_DIR / "api_key"
TOKEN_FILE = Path("/etc/aetheros/web_token")


def local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


def web_token() -> str:
    try:
        return TOKEN_FILE.read_text().strip()
    except Exception:
        return "(generated on first daemon start)"


class SetupWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Welcome to AetherOS")
        self.set_default_size(560, 600)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.SLIDE_LEFT)
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)
        toolbar.set_content(self.stack)
        self.set_content(toolbar)

        self.stack.add_named(self._welcome_page(), "welcome")
        self.stack.add_named(self._done_page(), "done")

    # ── Page 1: paste key ────────────────────────────────────────────────────
    def _welcome_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_top(36); box.set_margin_bottom(36)
        box.set_margin_start(40); box.set_margin_end(40)
        box.set_valign(Gtk.Align.CENTER)

        glyph = Gtk.Label(label="◈")
        glyph.add_css_class("title-1")
        glyph.set_markup('<span size="48000" foreground="#cba6f7">◈</span>')
        box.append(glyph)

        title = Gtk.Label(label="Welcome to AetherOS")
        title.add_css_class("title-1")
        box.append(title)

        sub = Gtk.Label(label="Your AI, Aether, lives inside this system — always running,\n"
                              "ready to chat, code, and act on your behalf.\n\n"
                              "Paste your Claude API key to bring Aether to life.")
        sub.set_justify(Gtk.Justification.CENTER)
        sub.add_css_class("dim-label")
        box.append(sub)

        self.entry = Gtk.PasswordEntry()
        self.entry.set_show_peek_icon(True)
        self.entry.set_property("placeholder-text", "sk-ant-…")
        self.entry.set_margin_top(8)
        self.entry.connect("activate", lambda *_: self._save())
        box.append(self.entry)

        link = Gtk.LinkButton(uri="https://console.anthropic.com/settings/keys",
                              label="Get a key from console.anthropic.com →")
        box.append(link)

        self.error = Gtk.Label()
        self.error.add_css_class("error")
        box.append(self.error)

        activate = Gtk.Button(label="Activate Aether")
        activate.add_css_class("suggested-action")
        activate.add_css_class("pill")
        activate.set_halign(Gtk.Align.CENTER)
        activate.set_margin_top(8)
        activate.connect("clicked", lambda *_: self._save())
        box.append(activate)

        skip = Gtk.Button(label="Skip for now")
        skip.add_css_class("flat")
        skip.set_halign(Gtk.Align.CENTER)
        skip.connect("clicked", lambda *_: self.close())
        box.append(skip)
        return box

    # ── Page 2: success ──────────────────────────────────────────────────────
    def _done_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_margin_top(40); box.set_margin_bottom(40)
        box.set_margin_start(40); box.set_margin_end(40)
        box.set_valign(Gtk.Align.CENTER)

        box.append(Gtk.Label(label="✓", css_classes=["title-1"]))
        t = Gtk.Label(label="Aether is activated")
        t.add_css_class("title-1"); box.append(t)
        box.append(Gtk.Label(
            label="Aether is now running in the background.\n"
                  "Press Super+A or click ◈ in the top bar to chat.",
            justify=Gtk.Justification.CENTER, css_classes=["dim-label"]))

        grp = Adw.PreferencesGroup(title="Use Aether from your phone or another device")
        row1 = Adw.ActionRow(title="Remote address", subtitle=f"http://{local_ip()}:7474")
        row1.add_suffix(self._copy_btn(f"http://{local_ip()}:7474"))
        grp.add(row1)
        row2 = Adw.ActionRow(title="Access token", subtitle=web_token())
        row2.add_suffix(self._copy_btn(web_token()))
        grp.add(row2)
        box.append(grp)

        tips = Adw.PreferencesGroup(title="Try these")
        for cmd, desc in [
            ("ask how do I update my system", "one-shot question"),
            ("aether-do set up a python project in ~/proj", "full agentic task"),
            ("cat error.log | ask", "pipe anything to Aether"),
        ]:
            tips.add(Adw.ActionRow(title=cmd, subtitle=desc))
        box.append(tips)

        done = Gtk.Button(label="Start using AetherOS")
        done.add_css_class("suggested-action"); done.add_css_class("pill")
        done.set_halign(Gtk.Align.CENTER); done.set_margin_top(8)
        done.connect("clicked", lambda *_: self.close())
        box.append(done)
        return box

    def _copy_btn(self, text):
        b = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"])
        b.connect("clicked", lambda *_: Gdk.Display.get_default().get_clipboard().set(text))
        return b

    def _save(self):
        key = self.entry.get_text().strip()
        if not key.startswith("sk-ant-"):
            self.error.set_text("That doesn't look like a Claude key (expected sk-ant-…).")
            return
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        KEY_FILE.write_text(key)
        KEY_FILE.chmod(0o600)
        # Remove the first-run autostart so the wizard won't nag again.
        try:
            (Path(os.path.expanduser("~/.config/autostart")) / "aether-firstrun.desktop").unlink()
        except Exception:
            pass
        self.stack.set_visible_child_name("done")


class SetupApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="os.aetheros.Setup")

    def do_activate(self):
        SetupWindow(self).present()


def main():
    if "--autostart" in sys.argv and KEY_FILE.exists() and KEY_FILE.read_text().strip():
        return  # already configured — don't nag
    Adw.init()
    SetupApp().run([])


if __name__ == "__main__":
    main()
