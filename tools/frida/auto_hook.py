"""Auto-attach Frida license hook when Nexus Automator starts server.exe."""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

import frida
import psutil

from hook_common import (
    DEFAULT_BACKEND,
    DEFAULT_NEXUS_EXE,
    attach_hook,
    ensure_token,
    force_patch,
    hook_source,
    probe_license,
    read_api_token,
)

POLL_INTERVAL_SEC = 0.35


def _normalize(path: str | None) -> str:
    if not path:
        return ""
    try:
        return str(Path(path).resolve()).lower()
    except OSError:
        return path.lower()


def find_backend_server_pids(backend: Path) -> list[int]:
    target = _normalize(str(backend / "server.exe"))
    if not target:
        return []

    matches: list[int] = []
    for proc in psutil.process_iter(["pid", "exe", "name"]):
        try:
            info = proc.info
            exe = _normalize(info.get("exe"))
            if exe == target:
                matches.append(int(info["pid"]))
                continue
            if info.get("name", "").lower() == "server.exe" and target in exe:
                matches.append(int(info["pid"]))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return sorted(set(matches))


def launch_nexus(exe: Path) -> subprocess.Popen[bytes] | None:
    if not exe.is_file():
        print(f"[!] Nexus Automator not found: {exe}", file=sys.stderr)
        return None
    print(f"[*] launching {exe}")
    return subprocess.Popen([str(exe)], cwd=str(exe.parent))


class AutoHookWatcher:
    def __init__(self, backend: Path, probe: bool = False):
        self.backend = backend
        self.probe = probe
        self.device = frida.get_local_device()
        self.source = hook_source()
        self.token = ensure_token()
        self.attached: dict[int, tuple[frida.core.Session, frida.core.Script]] = {}
        self.seen_dead: set[int] = set()

    def _cleanup_dead(self) -> None:
        dead = [pid for pid in self.attached if not psutil.pid_exists(pid)]
        for pid in dead:
            session, _script = self.attached.pop(pid)
            try:
                session.detach()
            except Exception:
                pass
            self.seen_dead.add(pid)
            print(f"[*] server.exe exited (PID {pid}); watching for restart")

    def _attach_new(self, pid: int) -> None:
        if pid in self.attached:
            return
        print(f"[*] Nexus backend detected (PID {pid}) — attaching Frida hook")
        try:
            session, script = attach_hook(self.device, pid, self.source)
        except Exception as exc:
            print(f"[!] attach failed for PID {pid}: {exc}", file=sys.stderr)
            return

        self.attached[pid] = (session, script)
        stats = force_patch(script)
        print(f"[*] hook active on PID {pid}: {stats}")
        if self.probe:
            time.sleep(1.0)
            probe_license(read_api_token())

    def tick(self) -> None:
        self._cleanup_dead()
        for pid in find_backend_server_pids(self.backend):
            self._attach_new(pid)

    def run_forever(self) -> int:
        print("[*] auto-hook watcher running (Ctrl+C to stop)")
        print(f"[*] backend: {self.backend / 'server.exe'}")
        try:
            while True:
                self.tick()
                time.sleep(POLL_INTERVAL_SEC)
        except KeyboardInterrupt:
            print("[*] stopping watcher")
            for pid, (session, _script) in list(self.attached.items()):
                try:
                    session.detach()
                except Exception:
                    pass
                print(f"[*] detached PID {pid}")
            return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Auto-hook Nexus Automator server.exe when the app starts (lab use)",
    )
    parser.add_argument("--backend", type=Path, default=DEFAULT_BACKEND)
    parser.add_argument("--nexus-exe", type=Path, default=DEFAULT_NEXUS_EXE)
    parser.add_argument(
        "--launch",
        action="store_true",
        help="Launch Nexus Automator, then auto-hook its backend",
    )
    parser.add_argument("--probe", action="store_true", help="Probe license endpoints after each attach")
    parser.add_argument(
        "--no-watch",
        action="store_true",
        help="Only attach once to an already-running backend, then exit",
    )
    args = parser.parse_args()

    server = args.backend / "server.exe"
    if not server.is_file():
        print(f"server.exe not found: {server}", file=sys.stderr)
        return 1

    if args.launch:
        launch_nexus(args.nexus_exe)
        if args.no_watch:
            deadline = time.time() + 45.0
            while time.time() < deadline:
                if find_backend_server_pids(args.backend):
                    break
                time.sleep(0.5)

    watcher = AutoHookWatcher(args.backend, probe=args.probe)

    if args.no_watch:
        watcher.tick()
        if not watcher.attached:
            print("[!] no Nexus backend process found", file=sys.stderr)
            return 2
        return 0

    return watcher.run_forever()


if __name__ == "__main__":
    raise SystemExit(main())
