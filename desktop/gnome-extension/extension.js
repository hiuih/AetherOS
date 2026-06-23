/**
 * AetherOS GNOME Shell Extension
 * A crash-proof top-bar indicator for the Aether AI agent.
 *
 * Design notes (post-incident):
 *  - NO undefined globals. The old version referenced an undefined `Soup`
 *    which threw a ReferenceError on every 5s poll.
 *  - NO in-extension keybinding / Gio.Settings schema dependency. The old
 *    version constructed an org.gnome.shell.extensions.aetheros gschema that
 *    was never installed and threw at enable(). The Super+Space hotkey is now
 *    a dconf custom keybinding, fully decoupled from this extension.
 *  - Every IO path is wrapped in try/catch so a daemon hiccup can never
 *    propagate into the shell.
 */

import GObject from "gi://GObject";
import GLib from "gi://GLib";
import Gio from "gi://Gio";
import St from "gi://St";
import Clutter from "gi://Clutter";
import * as Main from "resource:///org/gnome/shell/ui/main.js";
import * as PanelMenu from "resource:///org/gnome/shell/ui/panelMenu.js";
import * as PopupMenu from "resource:///org/gnome/shell/ui/popupMenu.js";
import { Extension } from "resource:///org/gnome/shell/extensions/extension.js";

const DAEMON_HEALTH = "http://127.0.0.1:7474/health";
const CHECK_INTERVAL = 6; // seconds
const GUI = "/opt/aether/venv/bin/python3 /opt/aether/gui/aether-ui.py";

const AetherIndicator = GObject.registerClass(
  class AetherIndicator extends PanelMenu.Button {
    _init() {
      super._init(0.0, "Aether AI");

      const box = new St.BoxLayout({ style_class: "panel-status-menu-box" });
      this._dot = new St.Label({
        text: "◈",
        y_align: Clutter.ActorAlign.CENTER,
        style: "color: #f59e0b; font-size: 14px; margin-right: 5px; font-weight: bold;",
      });
      box.add_child(this._dot);
      this.add_child(box);

      const open = new PopupMenu.PopupMenuItem("Open Aether  ·  Super+A");
      open.connect("activate", () => this._spawn(GUI));
      this.menu.addMenuItem(open);

      const term = new PopupMenu.PopupMenuItem("Ask in Terminal");
      term.connect("activate", () =>
        this._spawn('gnome-terminal -- bash -lc "ask; exec bash"')
      );
      this.menu.addMenuItem(term);

      this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

      const status = new PopupMenu.PopupMenuItem("Daemon Status");
      status.connect("activate", () =>
        this._spawn('gnome-terminal -- bash -lc "systemctl status claused; read -n1"')
      );
      this.menu.addMenuItem(status);

      const logs = new PopupMenu.PopupMenuItem("View Action Log");
      logs.connect("activate", () =>
        this._spawn('gnome-terminal -- bash -lc "tail -n 100 -f /var/log/aetheros/actions.log; read -n1"')
      );
      this.menu.addMenuItem(logs);
    }

    _spawn(cmd) {
      try {
        GLib.spawn_command_line_async(`bash -c '${cmd.replace(/'/g, "'\\''")}'`);
      } catch (e) {}
    }

    setStatus(status) {
      const colors = {
        active: "#4ade80",
        starting: "#f59e0b",
        error: "#9ca3af",
        busy: "#a78bfa",
      };
      const color = colors[status] || "#9ca3af";
      this._dot.set_style(
        `color: ${color}; font-size: 14px; margin-right: 5px; font-weight: bold;`
      );
    }
  }
);

export default class AetherExtension extends Extension {
  enable() {
    this._indicator = new AetherIndicator();
    Main.panel.addToStatusArea("aether-indicator", this._indicator, 0, "right");
    this._monitorTimer = GLib.timeout_add_seconds(
      GLib.PRIORITY_DEFAULT,
      CHECK_INTERVAL,
      () => {
        this._checkDaemon();
        return GLib.SOURCE_CONTINUE;
      }
    );
    this._checkDaemon();
  }

  disable() {
    if (this._monitorTimer) {
      GLib.source_remove(this._monitorTimer);
      this._monitorTimer = null;
    }
    this._indicator?.destroy();
    this._indicator = null;
  }

  _checkDaemon() {
    try {
      const file = Gio.File.new_for_uri(DAEMON_HEALTH);
      const cancellable = new Gio.Cancellable();
      GLib.timeout_add(GLib.PRIORITY_DEFAULT, 3000, () => {
        try { cancellable.cancel(); } catch (e) {}
        return GLib.SOURCE_REMOVE;
      });
      file.load_contents_async(cancellable, (f, res) => {
        try {
          const [ok, contents] = f.load_contents_finish(res);
          const text = ok ? new TextDecoder().decode(contents) : "";
          this._indicator?.setStatus(text.includes("running") ? "active" : "error");
        } catch (e) {
          this._indicator?.setStatus("error");
        }
      });
    } catch (e) {
      this._indicator?.setStatus("error");
    }
  }
}
