<div align="center">

# ◈ AetherOS

### An AI-native Linux distribution where the AI *is* the operating system

[![Release](https://img.shields.io/github/v/release/hiuih/AetherOS?color=cba6f7&label=download)](https://github.com/hiuih/AetherOS/releases/latest)
[![Base](https://img.shields.io/badge/base-Ubuntu%2026.04-E95420?logo=ubuntu&logoColor=white)](https://ubuntu.com)
[![Arch](https://img.shields.io/badge/arch-amd64%20%C2%B7%20arm64-89b4fa)](https://github.com/hiuih/AetherOS/releases)
[![License](https://img.shields.io/badge/license-MIT-a6e3a1)](#license)

**AetherOS** is a Linux distribution built on Ubuntu 26.04 with **Aether** — a
[Claude](https://www.anthropic.com/claude)-powered agent — woven into every layer
of the system, from **PID 1** to the desktop. It can chat, write and run code,
fix problems, and act autonomously to keep your machine healthy and fast.

</div>

---

## What makes it different

Most "AI assistants" are an app you open. **Aether is the system.** It boots as
PID 1, runs as a always-on root daemon, watches the kernel through eBPF, and
gives you the same agent from your terminal, a native chat window, the GNOME
system menu, the Activities search, and a phone-friendly web UI.

| | |
|---|---|
| 🧠 **Aether as PID 1** | A tiny static init (`aether-init`) is the *first* process the kernel runs. It performs an AI boot-health pass, then hands off to systemd — the "Loader Pattern". |
| ⚡ **Always-on autonomy** | An eBPF kernel sensor + a deterministic *Sentinel* act on their own through **reversible** means: auto game/performance mode, threat isolation, safe cleanup. |
| 🤖 **Full agentic workflows** | Ask a question, write code, or hand Aether a multi-step goal and it executes end-to-end with full system tools. |
| 🩹 **Self-healing boot** | Golden / last-known-good state, a boot-failure counter with automatic rollback, and a hardware watchdog. |
| 🛟 **Never locks you out** | A Rescue boot entry (`aether.disable=1`) boots plain systemd with all AI disabled. |
| 🎨 **Its own identity** | Catppuccin Mocha theme, Papirus icons, a custom nebula wallpaper, a Plymouth boot splash, and a floating dock — clearly not stock Ubuntu. |

---

## Download & install

Grab the latest [**release**](https://github.com/hiuih/AetherOS/releases/latest).
The amd64 ISO is split into parts (GitHub caps assets at 2 GB) — see
`RECOMBINE.md` in the release.

```bash
# reassemble + verify
cat AetherOS-1.0-amd64.iso.part_* > AetherOS-1.0-amd64.iso
shasum -a 256 AetherOS-1.0-amd64.iso          # match SHA256SUMS.txt

# write to a ≥16 GB USB (or use balenaEtcher / Rufus in DD mode)
sudo dd if=AetherOS-1.0-amd64.iso of=/dev/sdX bs=4M status=progress oflag=sync
```

Boot the USB (UEFI **or** legacy BIOS) → **Try or Install AetherOS** → install →
reboot. On first login, the **Aether Setup** wizard asks for your
[Claude API key](https://console.anthropic.com/settings/keys). That's it — Aether
is now live.

> **Tip:** No API key yet? The system is fully usable; Aether just stays dormant
> until you add one in **Aether Settings**.

---

## Using Aether

```bash
ask "how do I free up disk space?"        # one-shot question
ask -f main.py "explain this file"        # feed it a file
cat error.log | ask                       # pipe anything in
aether-do "set up a Python project in ~/proj with a venv and pytest"   # full agentic task
```

- **`Super`+`A`** — open the native chat window (streaming, tool-call cards, copy-able code blocks)
- **System menu** — an *Aether* Quick Settings toggle to flip autonomy on/off
- **Activities search** — start typing and pick **"Ask Aether"**
- **Web UI** — `http://<your-ip>:7474` (token shown in Aether Settings) to use Aether from your phone

---

## How it works

```
        ┌──────────────────────────────────────────────────────────┐
  boot  │  kernel ──exec──▶  /sbin/aether-init  (PID 1, static C)   │
        │                      │  ① run boot agent (health, LKG,    │
        │                      │     credential check) — no AI      │
        │                      │  ② exec systemd  ───────────────┐  │
        └──────────────────────┼─────────────────────────────────┼──┘
                               ▼                                 ▼
                   deterministic "dumb monitor"        full GNOME desktop
                   (eBPF + Sentinel, local rules)               │
                               │ escalates only on anomaly      │
                               ▼                                 ▼
                   ┌─────────────────────────────────────────────────┐
                   │  claused — the Aether daemon (root, port 7474)   │
                   │  • Claude agent w/ full system tools             │
                   │  • Sentinel: game-mode, threat quarantine        │
                   │  • kernel_sensor: eBPF execve tracing            │
                   │  • HTTP/WS API + web UI + Unix socket            │
                   └─────────────────────────────────────────────────┘
                     ▲            ▲             ▲            ▲
                  ask CLI    chat GUI    Quick Settings   web UI
```

**Dumb monitor / smart brain.** Cheap, local, deterministic code does the
watching (eBPF tracepoints, rule-based Sentinel). The Claude API is invoked only
when something actually needs reasoning — keeping it fast and inexpensive.

**Safe by construction.** Autonomous actions are *reversible*: threats are frozen
(`SIGSTOP`) and **quarantined** (never deleted); game-mode tweaks the CPU
governor and priorities; cleanup only touches caches/logs/temp. Aether never
deletes your files or kills your apps on its own.

---

## Settings & autonomy

Open **Aether Settings** (app grid or the system-menu toggle) to set the model,
API key, remote access, and the autonomy level. The level lives in
`/etc/aetheros/autonomy.conf`:

| Level | Behavior |
|---|---|
| `off` | Aether only responds when asked. No background activity. |
| `observe` | Watches and logs, never acts on its own. |
| `active` *(default)* | Reversible autonomy: auto game-mode, threat isolation, safe cleanup. |
| `aggressive` | Same, with broader detection heuristics. |

---

## Building from source

You need Docker (Docker Desktop on macOS). Builds run in a privileged container;
on Apple Silicon the amd64 build runs under emulation.

```bash
# 1. cache the official Ubuntu 26.04 desktop ISO into ~/.cache/aetheros/
#    arm64: ubuntu-26.04-desktop-arm64.iso   amd64: ubuntu-26.04-desktop-amd64.iso

# 2. build the builder image
docker build --platform linux/arm64 -t aetheros-builder -f build/Dockerfile.ubuntu .

# 3. remaster (ARCH = arm64 | amd64)
docker run --rm --privileged --platform linux/arm64 \
  -v ~/.cache/aetheros/ubuntu-26.04-desktop-arm64.iso:/cache/ubuntu-26.04-desktop-arm64.iso:ro \
  -v "$PWD":/payload:ro -v "$PWD/iso":/output -e ARCH=arm64 \
  aetheros-builder bash /payload/scripts/build-ubuntu.sh
```

The pipeline extracts the official ISO, injects Aether into the universal
`casper/minimal.squashfs` base layer (so it's present in every install option
*and* the live session), repacks, and rebuilds the ISO **byte-preserving the
hybrid GPT/El-Torito boot structure** so it auto-boots on real firmware.

---

## Repository layout

```
ai-core/
  daemon/claused.py        the Aether daemon — agent, tools, Sentinel, eBPF, API
  gui/aether-ui.py         native GTK4 chat window  (Super+A)
  gui/aether-settings.py   libadwaita settings app
  gui/setup.py             first-login API-key wizard
  cli/ask                  the `ask` / `aether` CLI
  integrations/            GNOME Activities search provider
  web/index.html           phone-friendly remote UI
build/
  boot-agent/aether-init.c the PID 1 loader
  boot-agent/*             pre-systemd boot agent, golden/LKG snapshots
  customize.sh             everything that runs inside the chroot
  Dockerfile.ubuntu        the builder image
desktop/
  gnome-extension/         top-bar indicator + Quick Settings toggle
  01-aetheros-dconf        system-wide GNOME defaults (theme, dock, hotkey)
  make-wallpaper.py        the nebula wallpaper generator
  plymouth/ theme/         boot splash + Catppuccin theme
scripts/build-ubuntu.sh    the remaster pipeline (ARCH-aware)
```

---

## Rescue

If anything ever goes wrong at boot, pick **Advanced options → recovery** in
GRUB (or add `aether.disable=1` to the kernel line). That boots plain systemd
with all AI behavior off — a deterministic, AI-free fallback.

---

## Tech stack

Ubuntu 26.04 · GNOME 48 · Python (FastAPI · uvicorn · websockets) ·
Anthropic Claude · GTK4 / libadwaita · GJS · eBPF (bpftrace) · systemd ·
xorriso / squashfs · C (PID 1).

## License

MIT — see [`LICENSE`](LICENSE).

<div align="center">
<sub>Built with <a href="https://claude.com/claude-code">Claude Code</a> · Aether is powered by Claude</sub>
</div>
