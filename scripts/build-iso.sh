#!/usr/bin/env bash
# AetherOS ISO Build Script
# Usage:
#   ./scripts/build-iso.sh          — build amd64 (default)
#   ./scripts/build-iso.sh arm64    — build arm64
#   ./scripts/build-iso.sh both     — build both architectures
#
# From macOS, run inside Docker:
#   docker run --rm --privileged -v $(pwd):/build -v $(pwd)/iso:/output aetheros-builder

set -euo pipefail

ARCH="${1:-amd64}"
BUILD_DIR="$(cd "$(dirname "$0")/.." && pwd)/build"
OUTPUT_DIR="$(cd "$(dirname "$0")/.." && pwd)/iso"
TIMESTAMP=$(date +%Y%m%d-%H%M)

mkdir -p "$OUTPUT_DIR"

build_arch() {
    local arch="$1"
    echo ""
    echo "╔══════════════════════════════════════════════════╗"
    echo "║  AetherOS ISO Builder                           ║"
    echo "║  Architecture: ${arch}                               ║"
    echo "║  Base: Debian 13 Trixie                         ║"
    echo "╚══════════════════════════════════════════════════╝"
    echo ""

    local work_dir="/tmp/aetheros-build-${arch}"
    rm -rf "$work_dir"
    mkdir -p "$work_dir"
    cd "$work_dir"

    # Copy build config
    rsync -a "$BUILD_DIR/" "$work_dir/build/"
    rsync -a "$(dirname "$BUILD_DIR")/ai-core/" "$work_dir/ai-core/"
    rsync -a "$(dirname "$BUILD_DIR")/desktop/" "$work_dir/desktop/"

    cd "$work_dir/build"

    # Mark hooks as executable
    chmod +x config/hooks/live/*.hook.chroot 2>/dev/null || true

    # Run lb config
    echo "→ Configuring live-build for ${arch}..."
    bash auto/config "${arch}"

    # Copy additional files into chroot includes
    mkdir -p config/includes.chroot/build
    rsync -a "$work_dir/ai-core/" config/includes.chroot/build/ai-core/
    rsync -a "$work_dir/desktop/" config/includes.chroot/build/desktop/

    # Build
    echo "→ Building ISO (this takes 15-40 minutes)..."
    lb build 2>&1 | tee "$work_dir/build.log"

    # Find and move the ISO
    ISO_FILE=$(find "$work_dir/build" -name "*.iso" | head -1)
    if [ -z "$ISO_FILE" ]; then
        echo "ERROR: ISO not found after build!"
        exit 1
    fi

    OUTPUT_NAME="AetherOS-1.0-${arch}-${TIMESTAMP}.iso"
    cp "$ISO_FILE" "$OUTPUT_DIR/$OUTPUT_NAME"

    # Generate checksum
    sha256sum "$OUTPUT_DIR/$OUTPUT_NAME" > "$OUTPUT_DIR/$OUTPUT_NAME.sha256"

    SIZE=$(du -sh "$OUTPUT_DIR/$OUTPUT_NAME" | cut -f1)
    echo ""
    echo "✓ ISO built: $OUTPUT_DIR/$OUTPUT_NAME ($SIZE)"
    echo "  SHA256: $(cat "$OUTPUT_DIR/$OUTPUT_NAME.sha256")"
}

case "$ARCH" in
    both)
        build_arch amd64
        build_arch arm64
        ;;
    amd64|arm64)
        build_arch "$ARCH"
        ;;
    *)
        echo "Usage: $0 [amd64|arm64|both]"
        exit 1
        ;;
esac

echo ""
echo "Done! ISOs are in: $OUTPUT_DIR"
ls -lh "$OUTPUT_DIR"/*.iso 2>/dev/null || true
