#!/usr/bin/env bash
# Build AetherOS ISO from macOS using Docker
# Prerequisites: Docker Desktop with buildx support
# Usage: ./scripts/docker-build.sh [amd64|arm64|both]

set -euo pipefail

ARCH="${1:-amd64}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "╔══════════════════════════════════════════════════╗"
echo "║  AetherOS Docker Build Launcher                 ║"
echo "║  Building for: ${ARCH}                               ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# Check Docker
if ! docker info >/dev/null 2>&1; then
    echo "ERROR: Docker is not running. Start Docker Desktop first."
    exit 1
fi

# Enable QEMU emulation for cross-arch builds
if [ "$ARCH" = "arm64" ] || [ "$ARCH" = "both" ]; then
    echo "→ Setting up cross-arch emulation..."
    docker run --privileged --rm tonistiigi/binfmt --install arm64 2>/dev/null || true
fi

# Build the builder image
echo "→ Building Docker image..."
docker build \
    --platform linux/amd64 \
    -t aetheros-builder \
    -f "$ROOT/build/Dockerfile" \
    "$ROOT"

mkdir -p "$ROOT/iso"

# Run the build
echo "→ Running ISO build inside Docker (15-40 min)..."
docker run \
    --rm \
    --privileged \
    -v "$ROOT:/build:ro" \
    -v "$ROOT/iso:/output" \
    -e ARCH="$ARCH" \
    aetheros-builder \
    bash -c "cp -r /build /buildcopy && cd /buildcopy && ./scripts/build-iso.sh $ARCH && cp iso/* /output/ 2>/dev/null || true"

echo ""
echo "Done! Check $ROOT/iso/"
ls -lh "$ROOT/iso/"*.iso 2>/dev/null || echo "(no ISOs yet)"
