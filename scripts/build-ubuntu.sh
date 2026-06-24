#!/bin/bash
# ════════════════════════════════════════════════════════════════════════════
# AetherOS — Ubuntu 26.04 remaster pipeline (runs INSIDE the builder container)
#
#   Inputs (bind-mounted):
#     /cache/ubuntu-26.04-desktop-arm64.iso   source ISO (read-only)
#     /payload                                 repo root (read-only): ai-core, desktop, build
#     /output                                  where the finished ISO is written
#
#   Strategy: extract ISO → unpack the non-live squashfs base layers (stacked)
#   → chroot + customize (inject Aether) → repack into the TOP base layer →
#   rebuild ISO preserving EFI boot via `xorriso ... -boot_image any replay`.
# ════════════════════════════════════════════════════════════════════════════
set -uo pipefail

ISO_SRC="${ISO_SRC:-/cache/ubuntu-26.04-desktop-arm64.iso}"
PAYLOAD="${PAYLOAD:-/payload}"
OUTPUT="${OUTPUT:-/output}"
WORK="/work"
ISO_X="$WORK/iso"          # extracted ISO tree
ROOTFS="$WORK/rootfs"      # stacked, editable root filesystem
TS=$(date +%Y%m%d-%H%M)
OUT_ISO="$OUTPUT/AetherOS-1.0-arm64-${TS}.iso"

die() { echo "ERROR: $*" >&2; exit 1; }
step() { echo ""; echo "╔══ $* ══"; }

[ -f "$ISO_SRC" ] || die "source ISO not found at $ISO_SRC"
mkdir -p "$WORK" "$OUTPUT"
rm -rf "$ISO_X" "$ROOTFS"
mkdir -p "$ISO_X" "$ROOTFS"

# ── 1. Extract the ISO filesystem tree ───────────────────────────────────────
step "Extracting ISO contents"
xorriso -osirrox on -indev "$ISO_SRC" -extract / "$ISO_X" 2>/dev/null \
    || die "xorriso extract failed"
chmod -R u+w "$ISO_X"
echo "  extracted $(du -sh "$ISO_X" | cut -f1) to $ISO_X"

# ── 2. Identify the injection target layer ───────────────────────────────────
# Ubuntu 26.04 desktop uses type:fsimage-layered. install-sources.yaml lists the
# install options; minimal.squashfs is the LOWEST/base layer shared by every
# install option (minimal + full) AND the live session. Injecting Aether here
# guarantees it is present everywhere, regardless of which option the user picks.
step "Identifying base squashfs layer"
CASPER="$ISO_X/casper"
[ -d "$CASPER" ] || die "no casper/ dir — unexpected ISO layout"
TOP_BASE="$CASPER/minimal.squashfs"
if [ ! -f "$TOP_BASE" ]; then
    # Fallback: smallest non-live, non-language squashfs is the base.
    TOP_BASE=$(find "$CASPER" -maxdepth 1 -name '*.squashfs' \
        ! -name '*live*' ! -name '*.??.squashfs' \
        -printf '%s %p\n' 2>/dev/null | sort -n | head -1 | cut -d' ' -f2-)
fi
[ -f "$TOP_BASE" ] || die "could not locate base squashfs layer"
echo "  base layer: $(basename "$TOP_BASE") ($(du -h "$TOP_BASE" | cut -f1))"
echo "  all layers:"; find "$CASPER" -maxdepth 1 -name '*.squashfs' -printf '    %f\n' | sort

# ── 3. Unpack the base layer into an editable rootfs ─────────────────────────
step "Unpacking $(basename "$TOP_BASE") into rootfs"
unsquashfs -f -d "$ROOTFS" "$TOP_BASE" >/dev/null 2>&1 || die "unsquashfs failed"
echo "  rootfs: $(du -sh "$ROOTFS" | cut -f1)"

# ── 4. Stage payload + chroot customization ──────────────────────────────────
step "Customizing rootfs (chroot)"
mkdir -p "$ROOTFS/aether-payload/desktop"
cp -r "$PAYLOAD/ai-core" "$ROOTFS/aether-payload/ai-core"
# Desktop assets: wallpaper gen, dconf, shell extension, plymouth, and the
# primary Catppuccin theme (skip the hdpi/xhdpi/cinnamon variants we don't use).
cp "$PAYLOAD/desktop/make-wallpaper.py" "$PAYLOAD/desktop/01-aetheros-dconf" "$ROOTFS/aether-payload/desktop/"
cp -r "$PAYLOAD/desktop/gnome-extension" "$ROOTFS/aether-payload/desktop/gnome-extension"
cp -r "$PAYLOAD/desktop/plymouth"        "$ROOTFS/aether-payload/desktop/plymouth" 2>/dev/null || true
cp -r "$PAYLOAD/desktop/themes/catppuccin-mocha-mauve-standard+default" \
      "$ROOTFS/aether-payload/desktop/theme" 2>/dev/null || true
cp -r "$PAYLOAD/build/boot-agent" "$ROOTFS/aether-payload/boot-agent"
cp "$PAYLOAD/build/customize.sh" "$ROOTFS/customize.sh"
chmod +x "$ROOTFS/customize.sh"

# Network + pseudo-filesystems for apt/pip inside chroot.
# The stock rootfs ships /etc/resolv.conf as a dangling symlink to systemd-resolved,
# which breaks DNS (and therefore apt/pip) inside the chroot — replace with a real file.
rm -f "$ROOTFS/etc/resolv.conf"
printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' > "$ROOTFS/etc/resolv.conf"
for fs in proc sys dev dev/pts run; do mkdir -p "$ROOTFS/$fs"; done
mount -t proc proc       "$ROOTFS/proc"     2>/dev/null || mount --bind /proc    "$ROOTFS/proc"
mount --rbind /sys       "$ROOTFS/sys"
mount --rbind /dev       "$ROOTFS/dev"
mount -t tmpfs tmpfs      "$ROOTFS/run"      2>/dev/null || true
cleanup_mounts() {
    umount -lf "$ROOTFS/run"     2>/dev/null || true
    umount -lf "$ROOTFS/dev"     2>/dev/null || true
    umount -lf "$ROOTFS/sys"     2>/dev/null || true
    umount -lf "$ROOTFS/proc"    2>/dev/null || true
}
trap cleanup_mounts EXIT

chroot "$ROOTFS" /bin/bash /customize.sh || die "chroot customization failed"

# Cleanup payload + temp from the rootfs so it doesn't ship in the image
rm -rf "$ROOTFS/aether-payload" "$ROOTFS/customize.sh"
rm -f  "$ROOTFS/etc/resolv.conf"
cleanup_mounts
trap - EXIT

# Track exactly which files we change, so the ISO rebuild updates only those
# (surgical) rather than diffing the whole tree.
CHANGED=()
iso_rel() { printf '%s' "${1#$ISO_X}"; }   # /work/iso/casper/x -> /casper/x

# ── 5. Repack the top base layer ─────────────────────────────────────────────
step "Repacking squashfs: $(basename "$TOP_BASE")"
NEW_SQ="$WORK/new.squashfs"
rm -f "$NEW_SQ"
mksquashfs "$ROOTFS" "$NEW_SQ" -comp zstd -b 1M -noappend -no-progress \
    || die "mksquashfs failed"
mv -f "$NEW_SQ" "$TOP_BASE"
CHANGED+=("$TOP_BASE")
echo "  new layer: $(du -h "$TOP_BASE" | cut -f1)"

# Update size sidecar if present (installer progress + free-space check)
SIZE_FILE="${TOP_BASE%.squashfs}.size"
if [ -f "$SIZE_FILE" ]; then
    du -sx --block-size=1 "$ROOTFS" | cut -f1 > "$SIZE_FILE"
    CHANGED+=("$SIZE_FILE")
    echo "  updated $(basename "$SIZE_FILE")"
fi
# Refresh manifest if present
MAN="${TOP_BASE%.squashfs}.manifest"
if [ -f "$MAN" ]; then
    chroot_dpkg=$(find "$ROOTFS/var/lib/dpkg" -name status 2>/dev/null | head -1)
    if [ -n "$chroot_dpkg" ] && awk '/^Package: / {p=$2} /^Version: / {print p" "$2}' "$chroot_dpkg" > "$MAN" 2>/dev/null; then
        CHANGED+=("$MAN")
    fi
fi

# Bump install-sources.yaml sizes so the installer's free-space estimate accounts
# for the injected Aether layer (it injects into the base, so EVERY source grows).
YAML="$CASPER/install-sources.yaml"
if [ -f "$YAML" ]; then
    NEW_MIN=$(du -sbx "$ROOTFS" 2>/dev/null | cut -f1)
    mapfile -t SZ < <(grep -oE '^\s*size:\s*[0-9]+' "$YAML" | grep -oE '[0-9]+')
    if [ -n "${NEW_MIN:-}" ] && [ "${#SZ[@]}" -ge 1 ]; then
        OLD_MIN="${SZ[0]}"
        DELTA=$(( NEW_MIN - OLD_MIN ))
        sed -i "s/size: ${OLD_MIN}/size: ${NEW_MIN}/" "$YAML"
        if [ "${#SZ[@]}" -ge 2 ]; then
            OLD_FULL="${SZ[1]}"
            sed -i "s/size: ${OLD_FULL}/size: $(( OLD_FULL + (DELTA > 0 ? DELTA : 0) ))/" "$YAML"
        fi
        CHANGED+=("$YAML")
        echo "  install-sources.yaml sizes bumped (minimal: $OLD_MIN → $NEW_MIN)"
    fi
fi

# ── 6. Regenerate md5sum.txt (skip boot catalog) ─────────────────────────────
step "Regenerating md5sum.txt"
if [ -f "$ISO_X/md5sum.txt" ]; then
    ( cd "$ISO_X" && find . -type f \
        ! -name md5sum.txt \
        ! -path './boot.catalog' \
        ! -path './isolinux/*' \
        -print0 | xargs -0 md5sum > md5sum.txt 2>/dev/null ) || true
    CHANGED+=("$ISO_X/md5sum.txt")
    echo "  md5sum.txt refreshed"
fi

# ── 7. Light ISO-level branding (GRUB menu titles) ───────────────────────────
step "Branding boot menu"
for cfg in "$ISO_X/boot/grub/grub.cfg" "$ISO_X/EFI/boot/grub.cfg" "$ISO_X"/boot/grub/*/grub.cfg; do
    [ -f "$cfg" ] || continue
    if sed -i -e 's/Try or Install Ubuntu/◈  Try or Install AetherOS/g' \
              -e 's/Ubuntu (safe graphics)/AetherOS (safe graphics)/g' \
              -e 's/Try Ubuntu/Try AetherOS/g' \
              -e 's/Install Ubuntu/Install AetherOS/g' "$cfg" 2>/dev/null; then
        CHANGED+=("$cfg")
    fi
done

# ── 8. Rebuild the ISO, preserving the original EFI boot records ─────────────
# Keep the original El Torito boot records verbatim (`-boot_image any keep`) and
# surgically -update only the files we actually changed. This is more robust than
# a recursive tree diff for a boot-critical image.
step "Building final ISO (preserving boot)"
mkdir -p "$OUTPUT"
XARGS=(-indev "$ISO_SRC" -outdev "$OUT_ISO" -boot_image any keep -volid "AetherOS")
for f in "${CHANGED[@]}"; do
    XARGS+=(-update "$f" "$(iso_rel "$f")")
    echo "  -update $(iso_rel "$f")"
done
XARGS+=(-commit)
xorriso "${XARGS[@]}" 2>&1 | tail -10 || die "xorriso rebuild failed"

[ -f "$OUT_ISO" ] || die "output ISO missing after build"
echo ""
echo "════════════════════════════════════════════════════════════"
echo "✓ AetherOS ISO: $OUT_ISO ($(du -h "$OUT_ISO" | cut -f1))"
echo "════════════════════════════════════════════════════════════"
