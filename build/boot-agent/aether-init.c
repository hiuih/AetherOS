/* ════════════════════════════════════════════════════════════════════════════
 * aether-init — AetherOS PID 1  (the "Loader Pattern" from the project directive)
 *
 * The kernel executes this as PID 1 (via init=/sbin/aether-init). It is the FIRST
 * userspace process on the system — the ancestor of everything. It:
 *
 *   1. Runs the deterministic, non-AI boot agent (health check + last-known-good
 *      reconciliation + credential check) as a bounded child — the "dumb monitor".
 *   2. Pivots control to systemd via exec() so the full OS/desktop comes up and
 *      the always-on Aether agent (claused) takes over for the rest of uptime —
 *      the "smart brain".
 *
 * SAFETY (this is PID 1 — it must never brick the machine):
 *   • Every failure path still ends in exec(systemd) or, last resort, a shell.
 *   • The boot agent runs with a hard wall-clock cap; a hang can never block boot.
 *   • `aether.disable` on the cmdline skips the agent entirely and pivots straight
 *     to systemd (used by the Rescue boot entry).
 *   • This binary is statically simple and has no external runtime dependencies.
 *
 * Build:  cc -O2 -static -o /sbin/aether-init aether-init.c   (falls back to
 *         dynamic link if -static is unavailable; both work as PID 1).
 * ════════════════════════════════════════════════════════════════════════════ */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <signal.h>
#include <sys/wait.h>
#include <sys/types.h>

static int cmdline_has(const char *needle) {
    int fd = open("/proc/cmdline", O_RDONLY);
    if (fd < 0) return 0;
    char buf[4096];
    ssize_t n = read(fd, buf, sizeof(buf) - 1);
    close(fd);
    if (n <= 0) return 0;
    buf[n] = '\0';
    return strstr(buf, needle) != NULL;
}

static void log_line(const char *msg) {
    int fd = open("/dev/kmsg", O_WRONLY);
    if (fd >= 0) { dprintf(fd, "aether-init: %s\n", msg); close(fd); }
}

/* Run the boot agent as a child, but never let it block boot for more than ~12s. */
static void run_boot_agent(void) {
    const char *agent = "/usr/lib/aether/aether-boot-stage1";
    if (access(agent, X_OK) != 0) return;

    pid_t pid = fork();
    if (pid == 0) {
        /* child: own session, run the agent, then _exit no matter what */
        setsid();
        execl(agent, "aether-boot-stage1", (char *)NULL);
        _exit(127);
    }
    if (pid < 0) return;  /* fork failed — skip, boot continues */

    for (int i = 0; i < 120; i++) {           /* up to ~12s */
        int status;
        pid_t r = waitpid(pid, &status, WNOHANG);
        if (r == pid) return;                 /* finished cleanly */
        usleep(100000);                       /* 100ms */
    }
    kill(pid, SIGKILL);                       /* hung — kill and move on */
    waitpid(pid, NULL, 0);
}

int main(int argc, char **argv) {
    log_line("PID 1 up — AetherOS loader");

    if (!cmdline_has("aether.disable")) {
        run_boot_agent();
    } else {
        log_line("aether.disable present — skipping boot agent");
    }

    /* Pivot to the real init (systemd). exec() makes systemd PID 1. */
    char *const sysd_argv[] = { "/lib/systemd/systemd", NULL };
    const char *candidates[] = {
        "/lib/systemd/systemd",
        "/usr/lib/systemd/systemd",
        "/sbin/init",
        NULL
    };
    for (int i = 0; candidates[i]; i++) {
        log_line("handing off to system manager");
        execv(candidates[i], sysd_argv);      /* returns only on failure */
    }

    /* Absolute last resort — never panic; give the user a shell. */
    log_line("FATAL: could not exec system manager — emergency shell");
    execl("/bin/sh", "sh", (char *)NULL);
    execl("/bin/bash", "bash", (char *)NULL);

    /* If even that failed we are still PID 1. Returning here would terminate
     * PID 1 and trigger a kernel panic, so instead idle forever (and keep
     * reaping orphans) — the system hangs but stays inspectable rather than
     * panicking. The Rescue GRUB entry (no init=) is the real way out. */
    log_line("FATAL: no shell either — idling as PID 1 to avoid kernel panic");
    for (;;) {
        int status;
        while (waitpid(-1, &status, WNOHANG) > 0) { /* reap */ }
        pause();
    }
}
