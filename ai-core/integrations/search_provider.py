#!/usr/bin/env python3
"""
AetherOS — GNOME Shell search provider.

Makes Aether answer from the Activities overview: start typing a question and an
"Ask Aether" result appears; activating it opens Aether with the query. DBus
auto-activated via os.aetheros.SearchProvider; registered through the
search-providers .ini that ships in /usr/share/gnome-shell/search-providers/.
"""
import sys
import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

BUS_NAME = "os.aetheros.SearchProvider"
OBJECT_PATH = "/os/aetheros/SearchProvider"
AETHER_UI = ["/opt/aether/venv/bin/python3", "/opt/aether/gui/aether-ui.py"]

IFACE_XML = """
<node>
  <interface name="org.gnome.Shell.SearchProvider2">
    <method name="GetInitialResultSet">
      <arg type="as" name="terms" direction="in"/>
      <arg type="as" name="results" direction="out"/>
    </method>
    <method name="GetSubsearchResultSet">
      <arg type="as" name="previous_results" direction="in"/>
      <arg type="as" name="terms" direction="in"/>
      <arg type="as" name="results" direction="out"/>
    </method>
    <method name="GetResultMetas">
      <arg type="as" name="identifiers" direction="in"/>
      <arg type="aa{sv}" name="metas" direction="out"/>
    </method>
    <method name="ActivateResult">
      <arg type="s" name="identifier" direction="in"/>
      <arg type="as" name="terms" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="LaunchSearch">
      <arg type="as" name="terms" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
  </interface>
</node>
"""


class SearchProvider:
    def __init__(self):
        self.node = Gio.DBusNodeInfo.new_for_xml(IFACE_XML)

    def on_bus_acquired(self, conn, name, *user_data):
        conn.register_object(OBJECT_PATH, self.node.interfaces[0], self.handle_call, None, None)

    def _ask(self, terms):
        return " ".join(terms).strip()

    def handle_call(self, conn, sender, path, iface, method, params, invocation):
        try:
            if method in ("GetInitialResultSet", "GetSubsearchResultSet"):
                terms = params.unpack()[-1]
                query = self._ask(terms)
                results = ["aether-ask"] if len(query) >= 3 else []
                invocation.return_value(GLib.Variant("(as)", (results,)))

            elif method == "GetResultMetas":
                ids = params.unpack()[0]
                metas = []
                if "aether-ask" in ids:
                    metas.append({
                        "id": GLib.Variant("s", "aether-ask"),
                        "name": GLib.Variant("s", "Ask Aether"),
                        "description": GLib.Variant("s", "Get an answer from your system's AI"),
                        # 'icon' (plain icon-name string) — NOT 'gicon' (which needs a
                        # serialized GIcon variant); GNOME Shell supports both keys.
                        "icon": GLib.Variant("s", "system-run-symbolic"),
                    })
                invocation.return_value(GLib.Variant("(aa{sv})", (metas,)))

            elif method == "ActivateResult":
                _id, terms, _ts = params.unpack()
                self._launch(self._ask(terms))
                invocation.return_value(None)

            elif method == "LaunchSearch":
                terms, _ts = params.unpack()
                self._launch(self._ask(terms))
                invocation.return_value(None)
            else:
                invocation.return_value(None)
        except Exception as e:
            invocation.return_value(GLib.Variant("(as)", ([],)) if "ResultSet" in method else None)
            sys.stderr.write(f"search_provider error in {method}: {e}\n")

    def _launch(self, query):
        try:
            import os
            argv = AETHER_UI + (["--ask", query] if query else [])
            launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.NONE)
            launcher.set_environ(GLib.get_environ())
            # The DBus-activated provider usually inherits DISPLAY/WAYLAND_DISPLAY
            # from the session; ensure a sane fallback so the GTK GUI can start.
            if not os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY"):
                launcher.setenv("DISPLAY", ":0", True)
            launcher.spawnv(argv)
        except Exception as e:
            sys.stderr.write(f"launch failed: {e}\n")


def main():
    provider = SearchProvider()
    loop = GLib.MainLoop()
    Gio.bus_own_name(
        Gio.BusType.SESSION, BUS_NAME, Gio.BusNameOwnerFlags.NONE,
        provider.on_bus_acquired, None,
        lambda *a: (sys.stderr.write("could not own bus name\n"), loop.quit()),
    )
    loop.run()


if __name__ == "__main__":
    main()
