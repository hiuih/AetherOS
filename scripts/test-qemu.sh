#!/usr/bin/env bash
# Test AetherOS ISO in QEMU
# macOS: brew install qemu
# Linux: apt install qemu-system-x86

set -euo pipefail

ISO_DIR="$(cd "$(dirname "$0")/.." && pwd)/iso"
ISO="$(ls -t "$ISO_DIR"/AetherOS-*amd64*.iso 2>/dev/null | head -1)"

if [ -z "$ISO" ]; then
    echo "No amd64 ISO found in $ISO_DIR"
    echo "Run ./scripts/build-iso.sh first (or build from Docker)"
    exit 1
fi

echo "Testing: $ISO"

RAM="${1:-4096}"  # MB of RAM
CORES="${2:-2}"

# Create temp disk
DISK="/tmp/aetheros-test.qcow2"
if [ ! -f "$DISK" ]; then
    qemu-img create -f qcow2 "$DISK" 20G
fi

qemu-system-x86_64 \
    -machine q35,accel=hvf \
    -cpu host \
    -smp "$CORES" \
    -m "$RAM" \
    -drive file="$DISK",format=qcow2,if=virtio \
    -cdrom "$ISO" \
    -boot d \
    -vga virtio \
    -display cocoa,show-cursor=on \
    -net nic,model=virtio \
    -net user,hostfwd=tcp::7474-:7474,hostfwd=tcp::2222-:22 \
    -usb \
    -device usb-tablet \
    -audiodev coreaudio,id=audio \
    -device ich9-intel-hda \
    -device hda-duplex,audiodev=audio \
    -snapshot

echo ""
echo "QEMU running. Aether API will be at http://localhost:7474 when booted."
echo "SSH: ssh -p 2222 user@localhost"
