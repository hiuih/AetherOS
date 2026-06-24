#!/bin/bash
# ════════════════════════════════════════════════════════════════════════════
# AetherOS — in-chroot customization (runs INSIDE the Ubuntu 26.04 rootfs)
# Invoked by scripts/build-ubuntu.sh after chrooting into the unpacked squashfs.
# Payload (ai-core, desktop assets) is staged at /aether-payload inside chroot.
# ════════════════════════════════════════════════════════════════════════════
set -uo pipefail
export DEBIAN_FRONTEND=noninteractive
export LANG=C.UTF-8
export HOME=/root

PAYLOAD="/aether-payload"
log() { echo "  [aether] $*"; }
section() { echo ""; echo "==> $*"; }

# ── 1. System branding ───────────────────────────────────────────────────────
section "Branding the system as AetherOS"

cat > /etc/os-release << 'EOF'
PRETTY_NAME="AetherOS 1.0"
NAME="AetherOS"
VERSION_ID="26.04"
VERSION="26.04 (AI-Native)"
VERSION_CODENAME=aether
ID=aetheros
ID_LIKE="ubuntu debian"
HOME_URL="https://aetheros.ai"
SUPPORT_URL="https://aetheros.ai/support"
BUG_REPORT_URL="https://aetheros.ai/bugs"
LOGO=aetheros
EOF
# keep lsb-release readable by apps that expect Ubuntu-likeness
cat > /etc/lsb-release << 'EOF'
DISTRIB_ID=AetherOS
DISTRIB_RELEASE=26.04
DISTRIB_CODENAME=aether
DISTRIB_DESCRIPTION="AetherOS 1.0 (AI-Native Linux)"
EOF

cat > /etc/issue << 'EOF'

  \e[38;5;141m◈  AetherOS 1.0\e[0m — AI-Native Linux
     Aether is always running.  Type: ask <anything>

EOF
echo "AetherOS 1.0 — \n \l" > /etc/issue.net

# ── 2. APT packages (tools Aether + the AI GUI/CLI need) ─────────────────────
section "Installing supporting packages via APT"
# The live rootfs points apt at the install CD via a deb822 stanza in
# /etc/apt/sources.list.d/cdrom.sources, which isn't present in our chroot and
# makes `apt update` fail with "Malformed entry". Remove the cdrom source file
# outright (sed corrupts the multi-line deb822 stanza); ubuntu.sources (the real
# network mirror) remains and is used instead.
rm -f /etc/apt/sources.list.d/cdrom.sources /etc/apt/sources.list.d/cdrom.list 2>/dev/null || true
sed -i -e '/cdrom:/d' -e '\#file:/cdrom#d' /etc/apt/sources.list 2>/dev/null || true
apt-get update -y 2>/dev/null || log "apt update failed (continuing, may be offline-cached)"
apt-get install -y --no-install-recommends \
    python3-venv python3-pip python3-gi python3-gi-cairo \
    gir1.2-gtk-4.0 gir1.2-adw-1 libadwaita-1-0 \
    python3-psutil python3-pil \
    xdotool xclip xsel scrot imagemagick wl-clipboard grim \
    notify-osd libnotify-bin \
    jq curl wget git ripgrep fd-find bat \
    fonts-jetbrains-mono fonts-inter \
    papirus-icon-theme gnome-shell-extensions sassc \
    plymouth plymouth-themes \
    bpftrace nftables auditd \
    2>/dev/null || log "some apt packages unavailable (continuing)"

# ── 3. Install the Aether AI core ────────────────────────────────────────────
section "Installing Aether AI core into /opt/aether"
mkdir -p /opt/aether/{daemon,gui,cli,web,integrations}
mkdir -p /etc/aetheros /var/log/aetheros /usr/share/aetheros
mkdir -p /var/lib/aetheros/quarantine

# Autonomy level for the always-on Sentinel + kernel sensor.
#   off       — Aether only responds when asked
#   observe   — watches & logs, never acts on its own
#   active    — (default) reversible autonomy: auto game-mode, threat isolation
#   aggressive— same, with broader heuristics
cat > /etc/aetheros/autonomy.conf << 'EOF'
# AetherOS autonomy level: off | observe | active | aggressive
level = active
EOF

cp -r "$PAYLOAD/ai-core/daemon/." /opt/aether/daemon/
cp -r "$PAYLOAD/ai-core/gui/."    /opt/aether/gui/
cp -r "$PAYLOAD/ai-core/cli/."    /opt/aether/cli/
[ -d "$PAYLOAD/ai-core/web" ] && cp -r "$PAYLOAD/ai-core/web/." /opt/aether/web/
[ -d "$PAYLOAD/ai-core/integrations" ] && cp -r "$PAYLOAD/ai-core/integrations/." /opt/aether/integrations/ 2>/dev/null || true

# Python venv that can see system gi/GTK
python3 -m venv --system-site-packages /opt/aether/venv
/opt/aether/venv/bin/pip install --no-input --upgrade pip 2>/dev/null || true
/opt/aether/venv/bin/pip install --no-input \
    anthropic fastapi "uvicorn[standard]" websockets python-dotenv \
    secretstorage psutil rich httpx aiofiles watchdog pillow \
    2>/dev/null || log "pip install had issues (will retry at first boot)"

# CLI wrappers
for name in ask aether; do
cat > /usr/local/bin/$name << WRAP
#!/bin/bash
exec /opt/aether/venv/bin/python3 /opt/aether/cli/ask "\$@"
WRAP
chmod +x /usr/local/bin/$name
done
chmod +x /opt/aether/cli/ask 2>/dev/null || true

# Agentic long-task launcher
cat > /usr/local/bin/aether-do << 'WRAP'
#!/bin/bash
# Fire a full autonomous agentic task at the daemon (long-running, multi-step).
TASK="$*"
[ -z "$TASK" ] && { echo "usage: aether-do <a goal to accomplish autonomously>"; exit 1; }
curl -s -N -X POST http://localhost:7474/agent \
  -H 'Content-Type: application/json' \
  -d "$(jq -n --arg t "$TASK" '{task:$t}')" 2>/dev/null \
  | sed -u 's/^data: //'
WRAP
chmod +x /usr/local/bin/aether-do

# ── 4. systemd service (daemon, root, always-on) ─────────────────────────────
section "Enabling claused (Aether daemon) service"
cp /opt/aether/daemon/claused.service /etc/systemd/system/claused.service
mkdir -p /etc/systemd/system/graphical.target.wants
ln -sf /etc/systemd/system/claused.service \
   /etc/systemd/system/graphical.target.wants/claused.service
systemctl enable claused 2>/dev/null || true

# ── 4b. Boot architecture: Aether as PID 1 (Loader Pattern) + self-healing ───
section "Installing boot agent — Aether as PID 1"
BA="$PAYLOAD/boot-agent"

# Compile the PID 1 loader (tiny, dependency-free C). Static if possible.
apt-get install -y --no-install-recommends gcc libc6-dev 2>/dev/null || log "gcc install failed"
mkdir -p /usr/lib/aether
AETHER_INIT_OK=0
if cc -O2 -static -o /sbin/aether-init "$BA/aether-init.c" 2>/dev/null \
   || cc -O2 -o /sbin/aether-init "$BA/aether-init.c" 2>/dev/null; then
    chmod 0755 /sbin/aether-init
    cp -f /sbin/aether-init /usr/sbin/aether-init 2>/dev/null || true
    AETHER_INIT_OK=1
    log "aether-init (PID 1 loader) compiled and installed."
else
    log "WARNING: aether-init failed to compile — will boot normal systemd."
fi
# Reclaim space: the compiler was only needed to build aether-init.
apt-get purge -y gcc libc6-dev 2>/dev/null && apt-get autoremove -y 2>/dev/null || true

# Boot agent (deterministic, pre-systemd) + golden/LKG tooling
install -m 0755 "$BA/aether-boot-stage1" /usr/lib/aether/aether-boot-stage1
install -m 0755 "$BA/aether-snapshot"    /usr/local/sbin/aether-snapshot
cp "$BA/aether-lkg.service" /etc/systemd/system/aether-lkg.service
mkdir -p /etc/systemd/system/graphical.target.wants
ln -sf /etc/systemd/system/aether-lkg.service \
   /etc/systemd/system/graphical.target.wants/aether-lkg.service

# Point the bootloader at aether-init as PID 1 — for the INSTALLED system only.
# Use a /etc/default/grub.d drop-in (a NEW file) rather than editing /etc/default/grub,
# so it survives the minimal.standard layer overlay on a full-desktop install.
# Recovery mode uses GRUB_CMDLINE_LINUX (not _DEFAULT) → boots plain systemd = rescue.
# CRITICAL SAFETY: only point PID 1 at aether-init if the binary actually compiled.
# Otherwise the kernel would panic on a missing init — so we leave boot 100% stock.
if [ "$AETHER_INIT_OK" = 1 ] && [ -x /sbin/aether-init ]; then
    mkdir -p /etc/default/grub.d
    cat > /etc/default/grub.d/99-aether.cfg << 'EOF'
# AetherOS: boot the Aether PID 1 loader (Loader Pattern). Recovery entries omit
# GRUB_CMDLINE_LINUX_DEFAULT and therefore boot plain systemd as a safe fallback.
GRUB_CMDLINE_LINUX_DEFAULT="${GRUB_CMDLINE_LINUX_DEFAULT:-quiet splash} init=/sbin/aether-init"
GRUB_DISABLE_RECOVERY="false"
EOF
    update-grub 2>/dev/null || log "update-grub deferred to install time"
    log "Bootloader set: PID 1 = /sbin/aether-init (recovery mode = plain systemd)."
else
    log "aether-init not present — leaving bootloader at stock systemd (safe)."
fi

# Hardware watchdog safety interlock (engages only if a watchdog device exists).
# Drop-in survives layer overlays.
mkdir -p /etc/systemd/system.conf.d
cat > /etc/systemd/system.conf.d/99-aether-watchdog.conf << 'EOF'
[Manager]
RuntimeWatchdogSec=30s
RebootWatchdogSec=2min
EOF

# Capture an initial golden baseline so the very first boot has something to heal to.
/usr/local/sbin/aether-snapshot golden 2>/dev/null || log "golden snapshot deferred to first boot"

# ── 5. Desktop entries (dock app + setup wizard) ─────────────────────────────
section "Installing desktop entries"
cp /opt/aether/gui/aether-ui.desktop /usr/share/applications/aether-ui.desktop 2>/dev/null || true

cat > /usr/share/applications/aether-setup.desktop << 'EOF'
[Desktop Entry]
Name=Aether Setup
Comment=Connect your Claude API key to activate Aether
Exec=/opt/aether/venv/bin/python3 /opt/aether/gui/setup.py
Icon=aetheros
Terminal=false
Type=Application
Categories=System;Settings;
StartupNotify=true
EOF

# Aether chat app (Super+A also launches this)
cat > /usr/share/applications/aether-ui.desktop << 'EOF'
[Desktop Entry]
Name=Aether
GenericName=AI Assistant
Comment=Chat with your system's AI — ask, code, or run a full task
Exec=/opt/aether/venv/bin/python3 /opt/aether/gui/aether-ui.py
Icon=aetheros
Terminal=false
Type=Application
Categories=Utility;System;
Keywords=ai;assistant;aether;claude;chat;
StartupNotify=true
EOF

# Aether Settings — appears in the app grid under Settings/System
cat > /usr/share/applications/aether-settings.desktop << 'EOF'
[Desktop Entry]
Name=Aether Settings
Comment=Configure Aether — autonomy, model, API key, remote access
Exec=/opt/aether/venv/bin/python3 /opt/aether/gui/aether-settings.py
Icon=preferences-system
Terminal=false
Type=Application
Categories=Settings;System;GTK;
Keywords=aether;ai;autonomy;settings;
StartupNotify=true
EOF

# ── GNOME Activities-search integration: "Ask Aether" from the overview ───────
mkdir -p /usr/share/gnome-shell/search-providers /usr/share/dbus-1/services
cat > /usr/share/gnome-shell/search-providers/aether-search.ini << 'EOF'
[Shell Search Provider]
DesktopId=aether-ui.desktop
BusName=os.aetheros.SearchProvider
ObjectPath=/os/aetheros/SearchProvider
Version=2
EOF
cat > /usr/share/dbus-1/services/os.aetheros.SearchProvider.service << 'EOF'
[D-Bus Service]
Name=os.aetheros.SearchProvider
Exec=/opt/aether/venv/bin/python3 /opt/aether/integrations/search_provider.py
EOF

# ── 6. Wallpaper — AetherOS nebula ───────────────────────────────────────────
section "Generating AetherOS wallpaper"
/opt/aether/venv/bin/python3 "$PAYLOAD/desktop/make-wallpaper.py" \
    /usr/share/aetheros/wallpaper.png 2>/dev/null \
  || log "wallpaper generation skipped"

# Logo glyph (simple SVG)
cat > /usr/share/aetheros/logo.svg << 'EOF'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
<polygon points="50,8 92,50 50,92 8,50" fill="none" stroke="#cba6f7" stroke-width="4"/>
<polygon points="50,28 72,50 50,72 28,50" fill="none" stroke="#a684ff" stroke-width="3"/>
</svg>
EOF

# ── 6b. Distinct visual identity: shell extension + GTK/shell theme ──────────
section "Installing AetherOS theme + Aether shell indicator"

# Aether top-bar indicator (crash-proof, just launches the GUI / shows status)
EXT_DIR="/usr/share/gnome-shell/extensions/aetheros@aetheros.os"
mkdir -p "$EXT_DIR"
cp "$PAYLOAD/desktop/gnome-extension/metadata.json" "$EXT_DIR/" 2>/dev/null || true
cp "$PAYLOAD/desktop/gnome-extension/extension.js"  "$EXT_DIR/" 2>/dev/null || true
log "Aether shell indicator installed."

# Catppuccin Mocha GTK + GNOME Shell theme (bundled) → both GTK apps and the shell
if [ -d "$PAYLOAD/desktop/theme" ]; then
    mkdir -p /usr/share/themes/Catppuccin-Mocha
    cp -r "$PAYLOAD/desktop/theme/." /usr/share/themes/Catppuccin-Mocha/
    # The bundled theme's index.theme advertises its long upstream name; the dconf
    # keys reference 'Catppuccin-Mocha' (the install dir). GTK resolves by dir name,
    # but align index.theme too so every lookup path (incl. tweaks tools) matches.
    if [ -f /usr/share/themes/Catppuccin-Mocha/index.theme ]; then
        sed -i \
          -e 's/^Name=.*/Name=Catppuccin-Mocha/' \
          -e 's/^GtkTheme=.*/GtkTheme=Catppuccin-Mocha/' \
          -e 's/^MetacityTheme=.*/MetacityTheme=Catppuccin-Mocha/' \
          /usr/share/themes/Catppuccin-Mocha/index.theme 2>/dev/null || true
    fi
    log "Catppuccin-Mocha theme installed (GTK3/GTK4/shell)."
fi
# GTK4 / libadwaita accent override for skel
mkdir -p /etc/skel/.config/gtk-4.0
cat > /etc/skel/.config/gtk-4.0/gtk.css << 'EOF'
@define-color accent_color #cba6f7;
@define-color accent_bg_color #cba6f7;
@define-color accent_fg_color #1e1e2e;
EOF
if [ -d /usr/share/themes/Catppuccin-Mocha/gtk-4.0 ]; then
    cp -r /usr/share/themes/Catppuccin-Mocha/gtk-4.0/. /etc/skel/.config/gtk-4.0/ 2>/dev/null || true
fi

# Plymouth boot splash (custom AetherOS animation)
if [ -d "$PAYLOAD/desktop/plymouth/aetheros" ]; then
    mkdir -p /usr/share/plymouth/themes/aetheros
    cp -r "$PAYLOAD/desktop/plymouth/aetheros/." /usr/share/plymouth/themes/aetheros/
    plymouth-set-default-theme aetheros 2>/dev/null || \
      update-alternatives --install /usr/share/plymouth/themes/default.plymouth \
        default.plymouth /usr/share/plymouth/themes/aetheros/aetheros.plymouth 100 2>/dev/null || true
    update-initramfs -u 2>/dev/null || log "initramfs update deferred"
    log "Plymouth splash set to aetheros."
fi

# GDM login screen background (purple, matches desktop)
mkdir -p /etc/dconf/db/gdm.d
cat > /etc/dconf/db/gdm.d/01-aether << 'EOF'
[org/gnome/desktop/interface]
color-scheme='prefer-dark'
accent-color='purple'
[org/gnome/login-screen]
logo='/usr/share/aetheros/logo.svg'
EOF

# ── 7. System-wide GNOME defaults via dconf (gentle; Ubuntu is already nice) ──
section "Applying AetherOS GNOME defaults (dconf)"
mkdir -p /etc/dconf/db/local.d /etc/dconf/profile
cat > /etc/dconf/profile/user << 'EOF'
user-db:user
system-db:local
EOF
cp "$PAYLOAD/desktop/01-aetheros-dconf" /etc/dconf/db/local.d/01-aetheros
dconf update 2>/dev/null || log "dconf update deferred to first boot"

# Robustness hedge: the essential look (dark + purple + Catppuccin + Papirus) is
# ALSO set via a uniquely-named gschema override. Unlike the dconf system-db, this
# new file survives if a higher squashfs layer overlays /etc/dconf on a full install.
cat > /usr/share/glib-2.0/schemas/90_aetheros.gschema.override << 'EOF'
[org.gnome.desktop.interface]
color-scheme='prefer-dark'
gtk-theme='Catppuccin-Mocha'
icon-theme='Papirus-Dark'
accent-color='purple'
font-name='Inter 11'
monospace-font-name='JetBrains Mono 11'

[org.gnome.desktop.background]
picture-uri='file:///usr/share/aetheros/wallpaper.png'
picture-uri-dark='file:///usr/share/aetheros/wallpaper.png'
picture-options='zoom'

[org.gnome.shell.extensions.user-theme]
name='Catppuccin-Mocha'
EOF
glib-compile-schemas /usr/share/glib-2.0/schemas/ 2>/dev/null || log "schema compile deferred"

# ── 8. Native AI integration: global hotkey + nautilus + shell niceties ──────
section "Wiring native AI integration"

# command-not-found → Aether hint, exit-code hint, pipe helper
cat > /etc/profile.d/aether.sh << 'EOF'
# AetherOS shell integration
if [ -n "$PS1" ]; then
  command_not_found_handle() {
    echo "aether: '$1' not found — ask Aether? run:  ask \"how do I $*\"" >&2
    return 127
  }
fi
alias a='ask'
EOF

# Nautilus right-click "Ask Aether"
mkdir -p /etc/skel/.local/share/nautilus/scripts
cat > /etc/skel/.local/share/nautilus/scripts/Ask\ Aether << 'EOF'
#!/bin/bash
F="$NAUTILUS_SCRIPT_SELECTED_FILE_PATHS"
ans=$(ask "Look at this file/these files and tell me what they are and anything notable: $F")
notify-send "Aether" "$ans" 2>/dev/null || zenity --info --text="$ans" 2>/dev/null
EOF
chmod +x /etc/skel/.local/share/nautilus/scripts/Ask\ Aether

# First-run autostart: setup wizard (only if no key yet) + aether panel
mkdir -p /etc/skel/.config/autostart
cat > /etc/skel/.config/autostart/aether-firstrun.desktop << 'EOF'
[Desktop Entry]
Type=Application
Name=Aether First Run
Exec=/opt/aether/venv/bin/python3 /opt/aether/gui/setup.py --autostart
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=2
NoDisplay=true
EOF

# ── 9. Remove live-only installer cruft from the INSTALLED system ────────────
# (These exist in the live layer; ensure they don't linger post-install.)
section "Cleaning live-only artifacts"
rm -f /etc/skel/Desktop/*install* 2>/dev/null || true

# ── 10. MOTD ─────────────────────────────────────────────────────────────────
mkdir -p /etc/update-motd.d
cat > /etc/update-motd.d/10-aetheros << 'EOF'
#!/bin/bash
printf "\n  \033[38;5;141m◈  AetherOS\033[0m — your AI is always running\n"
printf "  ─────────────────────────────────────────────\n"
printf "     ask <anything>           talk to Aether\n"
printf "     aether-do <goal>         autonomous agentic task\n"
printf "     cat file | ask           pipe data to Aether\n"
if systemctl is-active --quiet claused; then
  printf "     daemon: \033[32m● running\033[0m   web UI: http://localhost:7474\n\n"
else
  printf "     daemon: \033[33m○ starting\033[0m\n\n"
fi
EOF
chmod +x /etc/update-motd.d/10-aetheros
# de-Ubuntu the motd
rm -f /etc/update-motd.d/10-help-text /etc/update-motd.d/50-motd-news 2>/dev/null || true

log "customization complete."
echo ""
echo "==> AetherOS chroot customization finished successfully."
