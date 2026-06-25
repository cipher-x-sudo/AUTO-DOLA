"""Spawn or attach Frida to Nexus server.exe for local lab license bypass."""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import frida

from hook_common import (
    DEFAULT_BACKEND,
    attach_hook,
    ensure_token,
    force_patch,
    hook_source,
    probe_license,
    wait_for_health,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Frida lab hook for Nexus server.exe")
    parser.add_argument("--backend", type=Path, default=DEFAULT_BACKEND, help="Folder containing server.exe")
    parser.add_argument("--attach", type=int, default=0, help="Attach to existing PID instead of spawning")
    parser.add_argument("--probe", action="store_true", help="Probe license endpoints after hook loads")
    parser.add_argument("--keep-alive", action="store_true", help="Keep session open until Ctrl+C")
    args = parser.parse_args()

    server = args.backend / "server.exe"
    if not server.is_file():
        print(f"server.exe not found: {server}", file=sys.stderr)
        return 1

    token = ensure_token()
    source = hook_source()
    device = frida.get_local_device()

    if args.attach:
        print(f"[*] attaching to PID {args.attach}")
        session, script = attach_hook(device, args.attach, source)
        spawn_pid = args.attach
    else:
        env = os.environ.copy()
        env["NEXUS_API_TOKEN"] = token
        print(f"[*] spawning {server}")
        spawn_pid = device.spawn([str(server)], cwd=str(args.backend), env=env)
        session, script = attach_hook(device, spawn_pid, source)
        print(f"[*] spawned PID {spawn_pid}")

    if not args.attach:
        device.resume(spawn_pid)

    print("[*] waiting for backend health...")
    if not wait_for_health(token):
        print("[!] backend did not become healthy in time", file=sys.stderr)
        return 2

    print("[*] forcing in-process license patch...")
    stats = force_patch(script)
    print(f"[*] hook stats: {stats}")

    if args.probe:
        probe_license(token)

    if args.keep_alive:
        print("[*] hook active; press Ctrl+C to detach")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("[*] detaching")
    else:
        time.sleep(2)
        if args.probe:
            probe_license(token)

    session.detach()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
