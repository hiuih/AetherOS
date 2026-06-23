#!/bin/bash
# AetherOS: Patch running VM over SSH
# Usage: ./scripts/patch-live-vm.sh [VM_IP] [VM_PORT]
# Defaults: localhost:2222 (UTM port-forward)

set -euo pipefail

VM_IP="${1:-localhost}"
VM_PORT="${2:-2222}"
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -p $VM_PORT"
SSH="ssh $SSH_OPTS user@$VM_IP"
SCP="scp $SSH_OPTS"

echo "==> Patching AetherOS VM at $VM_IP:$VM_PORT"
echo ""

# ── 1. Push daemon + web UI ──────────────────────────────────────────────────
echo "  Copying claused.py and web UI..."
$SCP ai-core/daemon/claused.py user@$VM_IP:/tmp/claused.py
$SCP -r ai-core/web/ user@$VM_IP:/tmp/aether-web/
$SSH "sudo cp /tmp/claused.py /opt/aether/daemon/claused.py"
$SSH "sudo mkdir -p /opt/aether/web && sudo cp -r /tmp/aether-web/. /opt/aether/web/"

# ── 2. Push CLI ──────────────────────────────────────────────────────────────
echo "  Copying CLI..."
$SCP ai-core/cli/ask user@$VM_IP:/tmp/ask
$SSH "sudo cp /tmp/ask /opt/aether/cli/ask && sudo chmod +x /opt/aether/cli/ask"

# ── 3. Push GNOME extension ──────────────────────────────────────────────────
echo "  Copying GNOME extension..."
$SCP -r desktop/gnome-extension/ user@$VM_IP:/tmp/gnome-ext/
$SSH "sudo mkdir -p /usr/share/gnome-shell/extensions/aetheros@aetheros.os && sudo cp -r /tmp/gnome-ext/. /usr/share/gnome-shell/extensions/aetheros@aetheros.os/"

# ── 4. Ensure pip packages are installed ─────────────────────────────────────
echo "  Installing Python packages..."
$SSH "sudo /opt/aether/venv/bin/pip install -q anthropic fastapi 'uvicorn[standard]' websockets httpx aiofiles psutil pillow 2>/dev/null || true"

# ── 5. Restart daemon ────────────────────────────────────────────────────────
echo "  Restarting claused..."
$SSH "sudo systemctl restart claused || sudo systemctl start claused"
sleep 3

# ── 6. Check status ──────────────────────────────────────────────────────────
echo ""
echo "  Daemon status:"
$SSH "sudo systemctl status claused --no-pager -l | head -20" || true

echo ""
echo "==> Done! If daemon is running:"

VM_REAL_IP=$($SSH "hostname -I | awk '{print \$1}'" 2>/dev/null || echo "$VM_IP")
echo "   Remote UI: http://$VM_REAL_IP:7474"
echo "   Token:     $($SSH 'sudo cat /etc/aetheros/web_token 2>/dev/null || echo (not created yet)')"
echo ""
echo "   On your Mac, also try: http://localhost:7474 (if UTM port-forward is on)"
