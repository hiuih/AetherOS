#!/usr/bin/env bash
# AetherOS GNOME configuration — runs as the primary user on first login
set -euo pipefail

# Dark theme
gsettings set org.gnome.desktop.interface color-scheme prefer-dark
gsettings set org.gnome.desktop.interface gtk-theme Adwaita-dark
gsettings set org.gnome.desktop.interface icon-theme Papirus-Dark
gsettings set org.gnome.desktop.interface cursor-theme Adwaita

# Font
gsettings set org.gnome.desktop.interface font-name "Inter 11"
gsettings set org.gnome.desktop.interface monospace-font-name "JetBrains Mono 11"
gsettings set org.gnome.desktop.interface document-font-name "Inter 11"

# Workspace and windows
gsettings set org.gnome.shell.overrides dynamic-workspaces true
gsettings set org.gnome.desktop.wm.preferences button-layout ":minimize,maximize,close"
gsettings set org.gnome.desktop.wm.preferences focus-mode click

# Top bar
gsettings set org.gnome.desktop.interface clock-show-seconds true
gsettings set org.gnome.desktop.interface clock-show-weekday true
gsettings set org.gnome.desktop.interface show-battery-percentage true

# Hot corners — top-left for activities
gsettings set org.gnome.desktop.interface enable-hot-corners true

# Night light
gsettings set org.gnome.settings-daemon.plugins.color night-light-enabled true
gsettings set org.gnome.settings-daemon.plugins.color night-light-temperature 3500

# Enable Aether extension
gnome-extensions enable aetheros@aetheros.os 2>/dev/null || true

# Set wallpaper (dark AI-themed)
gsettings set org.gnome.desktop.background picture-uri "file:///usr/share/aetheros/wallpaper.png"
gsettings set org.gnome.desktop.background picture-uri-dark "file:///usr/share/aetheros/wallpaper.png"
gsettings set org.gnome.desktop.background picture-options zoom

# Keyboard shortcuts
gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings \
  "['/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/aether/']"
gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/aether/ \
  name "Open Aether AI"
gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/aether/ \
  command "/usr/bin/python3 /opt/aether/gui/aether-ui.py"
gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/aether/ \
  binding "<Super>space"

# Autostart Aether UI
mkdir -p ~/.config/autostart
cp /usr/share/applications/aether-ui.desktop ~/.config/autostart/

# Terminal: set to use aether shell integration
dconf write /org/gnome/terminal/legacy/profiles:/:$(gsettings get org.gnome.Terminal.ProfilesList default | tr -d "'")/custom-command \
  "'/usr/bin/bash --init-file /etc/profile.d/aether.sh'"

echo "GNOME configured for AetherOS."
