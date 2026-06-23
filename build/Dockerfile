# AetherOS ISO Builder — ARM64 (native on Apple Silicon)
# docker build --platform linux/arm64 -t aetheros-builder -f build/Dockerfile .
FROM --platform=linux/arm64 debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \
    live-build \
    debootstrap \
    squashfs-tools \
    xorriso \
    grub-efi-arm64-bin \
    grub-efi-arm64 \
    mtools \
    dosfstools \
    btrfs-progs \
    python3 \
    python3-pip \
    python3-venv \
    git \
    curl \
    wget \
    rsync \
    cpio \
    gzip \
    bzip2 \
    xz-utils \
    ca-certificates \
    gnupg \
    procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY . /build/

RUN chmod +x /build/scripts/*.sh 2>/dev/null || true
RUN chmod +x /build/build/auto/config 2>/dev/null || true
RUN chmod +x /build/build/config/hooks/live/*.hook.chroot 2>/dev/null || true

VOLUME ["/output"]

CMD ["/build/scripts/build-iso.sh"]
