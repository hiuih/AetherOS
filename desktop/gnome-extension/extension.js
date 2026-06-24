/**
 * AetherOS GNOME Shell Extension
 *
 * Two integration points, both crash-proof (every IO path wrapped in try/catch):
 *   1. A top-bar ◈ indicator showing daemon status with a quick menu.
 *   2. A Quick Settings toggle (in the system menu) to turn Aether's autonomy
 *      on/off and open Aether — the most native, "part of the OS" surface.
 *
 * Design rules (post-incident): no undefined globals (no bare `Soup`), no
 * in-extension gschema dependency. Autonomy state is READ from the local
 * /etc/aetheros/autonomy.conf; changes are WRITTEN via the daemon's localhost
 * API using a spawned curl (the daemon, as root, owns /etc/aetheros).
 */

import GObject from "gi://GObject";
import GLib from "gi://GLib";
import Gio from "gi://Gio";
import St from "gi://St";
import Clutter from "gi://Clutter";
import * as Main from "resource:///org/gnome/shell/ui/main.js";
import * as PanelMenu from "resource:///org/gnome/shell/ui/panelMenu.js";
import * as PopupMenu from "resource:///org/gnome/shell/ui/popupMenu.js";
import * as QuickSettings from "resource:///org/gnome/shell/ui/quickSettings.js";
import { Extension } from "resource:///org/gnome/shell/extensions/extension.js";

const DAEMON_HEALTH = "http://127.0.0.1:7474/health";
const CONFIG_URL = "http://127.0.0.1:7474/config";
const AUTONOMY_CONF = "/etc/aetheros/autonomy.conf";
const CHECK_INTERVAL = 6;
const PY = "/opt/aether/venv/bin/python3";
const GUI = `${PY} /opt/aether/gui/aether-ui.py`;
const SETTINGS = `${PY} /opt/aether/gui/aether-settings.py`;

function spawn(cmd) {
  try {
    GLib.spawn_command_line_async(`bash -c ${GLib.shell_quote(cmd)}`);
  } catch (e) {}
}

function readAutonomyLevel() {
  try {
    const [ok, bytes] = GLib.file_get_contents(AUTONOMY_CONF);
    if (ok) {
      const text = new TextDecoder().decode(bytes);
      const m = text.match(/level\s*=\s*(\w+)/);
      if (m) return m[1];
    }
  } catch (e) {}
  return "active";
}

function setAutonomy(level) {
  const payload = JSON.stringify({ autonomy_level: level });
  spawn(`curl -s -m 4 -X POST ${CONFIG_URL} -H 'Content-Type: application/json' -d ${GLib.shell_quote(payload)}`);
}

// ── Quick Settings toggle (system menu) ───────────────────────────────────────
const AetherToggle = GObject.registerClass(
  class AetherToggle extends QuickSettings.QuickMenuToggle {
    _init() {
      super._init({
        title: "Aether",
        subtitle: "AI autopilot",
        iconName: "system-run-symbolic",
        toggleMode: true,
      });

      try {
        this.menu.setHeader("system-run-symbolic", "Aether AI", "Always-on assistant");
        this.menu.addAction("Open Aether", () => spawn(GUI));
        this.menu.addAction("Aether Settings", () => spawn(SETTINGS));
        this.menu.addAction("Open Web UI", () => spawn("xdg-open http://localhost:7474"));
      } catch (e) {}

      this.checked = readAutonomyLevel() !== "off";
      this._sync();

      this.connect("clicked", () => {
        try {
          setAutonomy(this.checked ? "active" : "off");
          this._sync();
          // The write goes through the daemon (fire-and-forget curl). Re-read the
          // config shortly after and reconcile, so the toggle can't get stuck out
          // of sync if the daemon was briefly unreachable.
          GLib.timeout_add(GLib.PRIORITY_DEFAULT, 700, () => {
            try {
              const real = readAutonomyLevel() !== "off";
              if (real !== this.checked) { this.checked = real; this._sync(); }
            } catch (e) {}
            return GLib.SOURCE_REMOVE;
          });
        } catch (e) {}
      });
    }

    _sync() {
      this.subtitle = this.checked ? "Autopilot on" : "Manual only";
    }
  }
);

const AetherSystemIndicator = GObject.registerClass(
  class AetherSystemIndicator extends QuickSettings.SystemIndicator {
    _init() {
      super._init();
      this._toggle = new AetherToggle();
      this.quickSettingsItems.push(this._toggle);
    }
    destroy() {
      this._toggle?.destroy();
      super.destroy();
    }
  }
);

// ── Top-bar indicator ─────────────────────────────────────────────────────────
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
      open.connect("activate", () => spawn(GUI));
      this.menu.addMenuItem(open);

      const settings = new PopupMenu.PopupMenuItem("Aether Settings");
      settings.connect("activate", () => spawn(SETTINGS));
      this.menu.addMenuItem(settings);

      const term = new PopupMenu.PopupMenuItem("Ask in Terminal");
      term.connect("activate", () => spawn('gnome-terminal -- bash -lc "ask; exec bash"'));
      this.menu.addMenuItem(term);

      this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

      const logs = new PopupMenu.PopupMenuItem("View Action Log");
      logs.connect("activate", () =>
        spawn('gnome-terminal -- bash -lc "tail -n 100 -f /var/log/aetheros/actions.log; read -n1"')
      );
      this.menu.addMenuItem(logs);
    }

    setStatus(status) {
      const colors = { active: "#4ade80", starting: "#f59e0b", error: "#9ca3af", busy: "#a78bfa" };
      this._dot.set_style(
        `color: ${colors[status] || "#9ca3af"}; font-size: 14px; margin-right: 5px; font-weight: bold;`
      );
    }
  }
);

export default class AetherExtension extends Extension {
  enable() {
    try {
      this._indicator = new AetherIndicator();
      Main.panel.addToStatusArea("aether-indicator", this._indicator, 0, "right");
    } catch (e) {}

    try {
      this._qs = new AetherSystemIndicator();
      Main.panel.statusArea.quickSettings.addExternalIndicator(this._qs);
    } catch (e) {}

    this._monitorTimer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, CHECK_INTERVAL, () => {
      this._checkDaemon();
      return GLib.SOURCE_CONTINUE;
    });
    this._checkDaemon();
  }

  disable() {
    if (this._monitorTimer) {
      GLib.source_remove(this._monitorTimer);
      this._monitorTimer = null;
    }
    if (this._cancelTimer) {
      GLib.source_remove(this._cancelTimer);
      this._cancelTimer = null;
    }
    this._indicator?.destroy();
    this._indicator = null;
    this._qs?.destroy();
    this._qs = null;
  }

  _checkDaemon() {
    try {
      const file = Gio.File.new_for_uri(DAEMON_HEALTH);
      const cancellable = new Gio.Cancellable();
      if (this._cancelTimer) GLib.source_remove(this._cancelTimer);
      this._cancelTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 3000, () => {
        try { cancellable.cancel(); } catch (e) {}
        this._cancelTimer = null;
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
