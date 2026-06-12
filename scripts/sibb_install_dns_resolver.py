#!/usr/bin/env python3
"""One-time installer: enable .test hostname resolution for the SIBB
Phase 4 harness.

What it does
============
Writes `/etc/resolver/test` with the following two lines:

    nameserver 127.0.0.1
    port 35353

After this install, macOS' resolver (used by both the host AND the
iOS simulator's Safari) will route any `*.test` DNS query to a tiny
Python DNS server SIBB lazily spawns on `127.0.0.1:35353`. That
server answers every query with `127.0.0.1`, so URLs like
`http://events.test:<port>/` transparently reach the local MockSite
HTTP fixture.

Why this is needed
==================
iOS 26's simulator doesn't ship with `/etc/hosts`, so hostname-based
URLs like `http://events.test/...` otherwise fail with "Can't Open
Page" in Safari. Without this install, SIBB tasks still work — they
just fall back to the IP form `http://127.0.0.1:<port>/...` (less
realistic in the agent's prompt).

Safety
======
* The override is scoped to ONE TLD (`.test` — RFC 6761 reserves it
  for testing, so it CANNOT collide with a real public domain).
* Idempotent: re-running checks the file contents and skips the
  rewrite if everything's already correct.
* Cleanly reversible: `sudo rm /etc/resolver/test` removes it.

Usage
=====
    python3 scripts/sibb_install_dns_resolver.py
    # or
    sudo python3 scripts/sibb_install_dns_resolver.py

The script will invoke `sudo` for the file write if it isn't already
running as root. CI runners with passwordless sudo (GitHub Actions
macOS runners do by default) work without prompting.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

RESOLVER_DIR = "/etc/resolver"
RESOLVER_FILE = "/etc/resolver/test"
EXPECTED = "nameserver 127.0.0.1\nport 35353\n"


def main() -> int:
    # Idempotency check FIRST — re-running should be a clean no-op.
    if os.path.exists(RESOLVER_FILE):
        try:
            with open(RESOLVER_FILE, "r", encoding="utf-8") as fh:
                current = fh.read()
        except PermissionError:
            current = None
        if current == EXPECTED:
            print(f"✓ {RESOLVER_FILE} already correct — nothing to do.")
            return 0
        if current is not None:
            print(f"⚠ {RESOLVER_FILE} exists but has unexpected "
                  f"contents. Replacing:\n"
                  f"    current:  {current!r}\n"
                  f"    expected: {EXPECTED!r}")

    print(f"This will write {RESOLVER_FILE}:\n")
    for line in EXPECTED.splitlines():
        print(f"    {line}")
    print()

    # If we're not root and stdin is a tty, confirm before sudo
    # prompts the user.
    needs_sudo = os.geteuid() != 0
    if needs_sudo and sys.stdin.isatty():
        ans = input(
            "macOS will prompt for your sudo password. Continue? "
            "[y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("Aborted (no changes made).")
            return 1

    # Write to a temp file first, then sudo-cp into place. Avoids
    # opening a sudo shell that lingers, and works in non-interactive
    # contexts (CI passwordless sudo).
    with tempfile.NamedTemporaryFile(
            mode="w", suffix=".sibb-resolver",
            delete=False) as fh:
        fh.write(EXPECTED)
        tmp_path = fh.name

    try:
        if needs_sudo:
            cmd_mkdir = ["sudo", "mkdir", "-p", RESOLVER_DIR]
            cmd_cp = ["sudo", "cp", tmp_path, RESOLVER_FILE]
            cmd_chmod = ["sudo", "chmod", "644", RESOLVER_FILE]
        else:
            cmd_mkdir = ["mkdir", "-p", RESOLVER_DIR]
            cmd_cp = ["cp", tmp_path, RESOLVER_FILE]
            cmd_chmod = ["chmod", "644", RESOLVER_FILE]
        for cmd in (cmd_mkdir, cmd_cp, cmd_chmod):
            result = subprocess.run(cmd)
            if result.returncode != 0:
                print(f"\n✗ Command failed: {' '.join(cmd)}",
                      file=sys.stderr)
                return result.returncode
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    print(f"\n✓ Wrote {RESOLVER_FILE}.")
    print("  macOS will now route *.test DNS queries to "
          "127.0.0.1:35353.")
    print("  iOS sim Safari inherits this — no sim-side config.")
    print()
    print("Next: run any SIBB harness task; the DNS server starts "
          "lazily on the first MockSite. To undo:")
    print(f"    sudo rm {RESOLVER_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
