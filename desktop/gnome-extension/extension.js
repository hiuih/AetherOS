/**
 * AetherOS GNOME Shell Extension
 * Adds AI status indicator to top bar + Super+Space hotkey to toggle Aether UI
 */

import GLib from "gi://GLib";
import Gio from "gi://Gio";
import St from "gi://St";
import Clutter from "gi://Clutter";
import * as Main from "resource:///org/gnome/shell/ui/main.js";
import * as PanelMenu from "resource:///org/gnome/shell/ui/panelMenu.js";
import * as PopupMenu from "resource:///org/gnome/shell/ui/popupMenu.js";
import { Extension } from "resource:///org/gnome/shell/extensions/extension.js";

const DAEMON_URL = "http://localhost:7474";
const CHECK_INTERVAL = 5; // seconds

export default class AetherExtension extends Extension {
  enable() {
    this._indicator = new AetherIndicator(this);
    Main.panel.addToStatusArea("aether-indicator", this._indicator, 1, "right");
    this._setupHotkey();
    this._startMonitor();
  }

  disable() {
    this._indicator?.destroy();
    this._indicator = null;
    this._removeHotkey();
    if (this._monitorTimer) {
      GLib.source_remove(this._monitorTimer);
      this._monitorTimer = null;
    }
  }

  _setupHotkey() {
    Main.wm.addKeybinding(
      "toggle-aether",
      new Gio.Settings({ schema: "org.gnome.shell.extensions.aetheros" }),
      0,
      0,
      () => this._toggleAetherUI()
    );
  }

  _removeHotkey() {
    try {
      Main.wm.removeKeybinding("toggle-aether");
    } catch {}
  }

  _toggleAetherUI() {
    GLib.spawn_command_line_async(
      "bash -c 'if pgrep -f aether-ui.py > /dev/null; then pkill -f aether-ui.py; else /opt/aether/venv/bin/python3 /opt/aether/gui/aether-ui.py &; fi'"
    );
  }

  _startMonitor() {
    this._checkDaemon();
    this._monitorTimer = GLib.timeout_add_seconds(
      GLib.PRIORITY_DEFAULT,
      CHECK_INTERVAL,
      () => {
        this._checkDaemon();
        return GLib.SOURCE_CONTINUE;
      }
    );
  }

  _checkDaemon() {
    const msg = Soup
      ? this._checkSoup()
      : this._checkGio();
    return msg;
  }

  _checkGio() {
    const file = Gio.File.new_for_uri(`${DAEMON_URL}/health`);
    const cancellable = new Gio.Cancellable();
    GLib.timeout_add(GLib.PRIORITY_DEFAULT, 3000, () => {
      cancellable.cancel();
      return GLib.SOURCE_REMOVE;
    });
    file.load_contents_async(cancellable, (f, res) => {
      try {
        const [ok] = f.load_contents_finish(res);
        this._indicator?.setStatus(ok ? "active" : "error");
      } catch {
        this._indicator?.setStatus("error");
      }
    });
  }
}

class AetherIndicator extends PanelMenu.Button {
  constructor(ext) {
    super(0.0, "Aether AI");
    this._ext = ext;
    this._status = "starting";
    this._buildUI();
  }

  _buildUI() {
    const box = new St.BoxLayout({ style_class: "panel-status-menu-box" });

    this._dot = new St.Label({
      text: "◈",
      y_align: Clutter.ActorAlign.CENTER,
      style: "color: #818cf8; font-size: 13px; margin-right: 4px;",
    });

    this._label = new St.Label({
      text: "Aether",
      y_align: Clutter.ActorAlign.CENTER,
      style: "font-size: 12px; color: #c7d2fe;",
    });

    box.add_child(this._dot);
    box.add_child(this._label);
    this.add_child(box);

    // Menu
    const openItem = new PopupMenu.PopupMenuItem("Open Aether UI  (Super+Space)");
    openItem.connect("activate", () => {
      GLib.spawn_command_line_async(
        "/opt/aether/venv/bin/python3 /opt/aether/gui/aether-ui.py"
      );
    });

    const termItem = new PopupMenu.PopupMenuItem("Open Aether Terminal");
    termItem.connect("activate", () => {
      GLib.spawn_command_line_async(
        "bash -c 'gnome-terminal -- bash -c \"source /etc/profile.d/aether.sh; bash\"'"
      );
    });

    const statusItem = new PopupMenu.PopupMenuItem("Daemon Status");
    statusItem.connect("activate", () => {
      GLib.spawn_command_line_async(
        "bash -c 'gnome-terminal -- bash -c \"systemctl status claused; read\"'"
      );
    });

    const logsItem = new PopupMenu.PopupMenuItem("View Action Log");
    logsItem.connect("activate", () => {
      GLib.spawn_command_line_async(
        "bash -c 'gnome-terminal -- bash -c \"tail -f /var/log/aetheros/actions.log | python3 -c \\\"import sys,json; [print(json.dumps(json.loads(l), indent=2)) for l in sys.stdin]\\\"; read\"'"
      );
    });

    this.menu.addMenuItem(openItem);
    this.menu.addMenuItem(termItem);
    this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
    this.menu.addMenuItem(statusItem);
    this.menu.addMenuItem(logsItem);
  }

  setStatus(status) {
    this._status = status;
    const colors = {
      active: "#4ade80",
      starting: "#f59e0b",
      error: "#ef4444",
      busy: "#818cf8",
    };
    const color = colors[status] || "#6b7280";
    this._dot.set_style(`color: ${color}; font-size: 13px; margin-right: 4px;`);
    this._label.set_style(`font-size: 12px; color: ${color};`);
  }
}
