#!/usr/bin/env python3
"""
AetherOS — Aether Settings

A native GNOME-style preferences window (libadwaita) for configuring Aether:
autonomy level, model, API key, and remote access. Reads/writes through the
daemon's localhost /config API (the daemon, as root, owns /etc/aetheros).
"""
import json
import sys
import threading
import urllib.request

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gdk, Gio, GObject


def copy_to_clipboard(text):
    """GTK4-correct clipboard set: a string GValue via a ContentProvider."""
    provider = Gdk.ContentProvider.new_for_value(GObject.Value(str, text or ""))
    Gdk.Display.get_default().get_clipboard().set_content(provider)

DAEMON = "http://localhost:7474"

AUTONOMY_DESC = {
    "off": "Aether only responds when you ask. No background activity.",
    "observe": "Watches and logs, but never acts on its own.",
    "active": "Reversible autonomy: auto game-mode, threat isolation, safe cleanup.",
    "aggressive": "Same as active, with broader detection heuristics.",
}


def api_get(path):
    with urllib.request.urlopen(f"{DAEMON}{path}", timeout=5) as r:
        return json.loads(r.read())


def api_post(path, payload):
    req = urllib.request.Request(f"{DAEMON}{path}", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


class SettingsWindow(Adw.PreferencesWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self.set_title("Aether Settings")
        self.set_default_size(640, 720)
        self.set_search_enabled(True)
        self._cfg = {}
        self._loading = True

        self.add(self._page_general())
        self.add(self._page_autonomy())
        self.add(self._page_remote())
        self.add(self._page_about())

        self._toast = Adw.ToastOverlay()
        self._load_async()

    # ── data ───────────────────────────────────────────────────────────────────
    def _load_async(self):
        def work():
            try:
                cfg = api_get("/config")
            except Exception as e:
                cfg = {"_error": str(e)}
            GLib.idle_add(self._apply_cfg, cfg)
        threading.Thread(target=work, daemon=True).start()

    def _apply_cfg(self, cfg):
        # Never let a UI-populate error crash the app (apport popup). Degrade.
        try:
            self._populate(cfg)
        except Exception:
            import traceback
            traceback.print_exc()
            try:
                self._ready_row.set_subtitle("Could not load settings — is claused running?")
            except Exception:
                pass
            self._loading = False
        return False

    def _populate(self, cfg):
        self._cfg = cfg
        self._loading = True
        # NOTE: _key_row is an Adw.PasswordEntryRow, which has NO subtitle — all
        # status text goes on _ready_row (an Adw.ActionRow).
        if "_error" in cfg:
            self._ready_row.set_subtitle("Daemon offline — start claused")
            self._loading = False
            return
        if cfg.get("agent_ready"):
            self._ready_row.set_subtitle("Active and ready ✓")
        elif cfg.get("has_key"):
            self._ready_row.set_subtitle("API key set — Aether is starting")
        else:
            self._ready_row.set_subtitle("No API key yet — paste yours below")
        # model
        models = cfg.get("available_models", [])
        self._model_ids = [m["id"] for m in models]
        self._model_combo.set_model(Gtk.StringList.new([f"{m['name']} — {m['hint']}" for m in models]))
        if cfg.get("model") in self._model_ids:
            self._model_combo.set_selected(self._model_ids.index(cfg["model"]))
        # autonomy
        opts = cfg.get("autonomy_options", ["off", "observe", "active", "aggressive"])
        self._auto_opts = opts
        self._auto_combo.set_model(Gtk.StringList.new([o.capitalize() for o in opts]))
        if cfg.get("autonomy_level") in opts:
            self._auto_combo.set_selected(opts.index(cfg["autonomy_level"]))
        self._auto_desc.set_subtitle(AUTONOMY_DESC.get(cfg.get("autonomy_level", "active"), ""))
        # pid1
        self._pid1_row.set_subtitle(cfg.get("pid1_loader") or "Booted with standard init (live session)")
        # remote
        self._url_row.set_subtitle(cfg.get("remote_url", "—"))
        self._token_row.set_subtitle(cfg.get("web_token", "—"))
        self._loading = False

    def _save(self, payload, toast="Saved"):
        def work():
            try:
                api_post("/config", payload)
                GLib.idle_add(lambda: self.add_toast(Adw.Toast.new(toast)))
            except Exception as e:
                GLib.idle_add(lambda: self.add_toast(Adw.Toast.new(f"Failed: {e}")))
        threading.Thread(target=work, daemon=True).start()

    # ── pages ──────────────────────────────────────────────────────────────────
    def _page_general(self):
        page = Adw.PreferencesPage(title="General", icon_name="preferences-system-symbolic")

        g = Adw.PreferencesGroup(title="Connection",
                                 description="Aether is powered by Claude. Paste your API key to activate it.")
        self._ready_row = Adw.ActionRow(title="Status", subtitle="Loading…")
        self._ready_row.add_prefix(Gtk.Image.new_from_icon_name("emblem-default-symbolic"))
        g.add(self._ready_row)

        self._key_row = Adw.PasswordEntryRow(title="Claude API key")
        self._key_row.set_show_apply_button(True)
        self._key_row.connect("apply", self._on_key_apply)
        g.add(self._key_row)

        link = Adw.ActionRow(title="Get an API key", subtitle="console.anthropic.com/settings/keys")
        link.set_activatable(True)
        link.add_suffix(Gtk.Image.new_from_icon_name("adw-external-link-symbolic"))
        link.connect("activated", lambda *_: Gtk.UriLauncher.new(
            "https://console.anthropic.com/settings/keys").launch(self, None))
        g.add(link)
        page.add(g)

        gm = Adw.PreferencesGroup(title="Model")
        self._model_combo = Adw.ComboRow(title="Aether model",
                                         subtitle="Which Claude model powers Aether")
        self._model_combo.connect("notify::selected", self._on_model_changed)
        gm.add(self._model_combo)
        page.add(gm)
        return page

    def _page_autonomy(self):
        page = Adw.PreferencesPage(title="Autonomy", icon_name="applications-science-symbolic")
        g = Adw.PreferencesGroup(
            title="Autonomous behavior",
            description="Aether can act on its own through safe, reversible actions — "
                        "optimizing performance for games, isolating threats, and tidying up.")
        self._auto_combo = Adw.ComboRow(title="Autonomy level")
        self._auto_combo.connect("notify::selected", self._on_auto_changed)
        g.add(self._auto_combo)
        self._auto_desc = Adw.ActionRow(title="What this does", subtitle="")
        self._auto_desc.add_prefix(Gtk.Image.new_from_icon_name("dialog-information-symbolic"))
        g.add(self._auto_desc)
        page.add(g)

        gp = Adw.PreferencesGroup(title="Boot")
        self._pid1_row = Adw.ActionRow(title="Aether as PID 1", subtitle="…")
        self._pid1_row.add_prefix(Gtk.Image.new_from_icon_name("system-run-symbolic"))
        gp.add(self._pid1_row)
        page.add(gp)
        return page

    def _page_remote(self):
        page = Adw.PreferencesPage(title="Remote", icon_name="network-wireless-symbolic")
        g = Adw.PreferencesGroup(
            title="Remote access",
            description="Talk to Aether from your phone or another device on your network.")
        self._url_row = Adw.ActionRow(title="Address", subtitle="…")
        self._url_row.add_suffix(self._copy_btn(lambda: self._cfg.get("remote_url", "")))
        g.add(self._url_row)
        self._token_row = Adw.ActionRow(title="Access token", subtitle="…")
        self._token_row.add_suffix(self._copy_btn(lambda: self._cfg.get("web_token", "")))
        g.add(self._token_row)
        page.add(g)
        return page

    def _page_about(self):
        page = Adw.PreferencesPage(title="About", icon_name="help-about-symbolic")
        g = Adw.PreferencesGroup()
        g.add(Adw.ActionRow(title="AetherOS", subtitle="AI-native Linux · Ubuntu 26.04 base"))
        g.add(Adw.ActionRow(title="Aether", subtitle="Always-on AI agent · Claude-powered"))
        restart = Adw.ActionRow(title="Restart Aether daemon",
                                subtitle="Apply changes that need a restart")
        restart.set_activatable(True)
        restart.add_suffix(Gtk.Image.new_from_icon_name("view-refresh-symbolic"))
        restart.connect("activated", self._restart_daemon)
        g.add(restart)
        page.add(g)
        return page

    # ── handlers ────────────────────────────────────────────────────────────────
    def _copy_btn(self, getter):
        b = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"])
        def do(_):
            copy_to_clipboard(getter())
            self.add_toast(Adw.Toast.new("Copied"))
        b.connect("clicked", do)
        return b

    def _on_key_apply(self, row):
        key = row.get_text().strip()
        if key.startswith("sk-ant-"):
            self._save({"api_key": key}, "API key saved — Aether activating")
            row.set_text("")
            self._ready_row.set_subtitle("API key set — Aether is starting")

    def _on_model_changed(self, combo, _p):
        if self._loading:
            return
        idx = combo.get_selected()
        if 0 <= idx < len(getattr(self, "_model_ids", [])):
            self._save({"model": self._model_ids[idx]}, "Model updated")

    def _on_auto_changed(self, combo, _p):
        if self._loading:
            return
        idx = combo.get_selected()
        opts = getattr(self, "_auto_opts", [])
        if 0 <= idx < len(opts):
            self._auto_desc.set_subtitle(AUTONOMY_DESC.get(opts[idx], ""))
            self._save({"autonomy_level": opts[idx]}, f"Autonomy: {opts[idx]}")

    def _restart_daemon(self, *_):
        GLib.spawn_async(["pkexec", "systemctl", "restart", "claused"],
                         flags=GLib.SpawnFlags.SEARCH_PATH)
        self.add_toast(Adw.Toast.new("Restarting Aether…"))


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="os.aetheros.Settings")

    def do_activate(self):
        (self.props.active_window or SettingsWindow(self)).present()


def main():
    Adw.init()
    return App().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
