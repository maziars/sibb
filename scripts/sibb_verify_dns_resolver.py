#!/usr/bin/env python3
"""Verify that macOS' resolver actually routes `.test` lookups to the
SIBB DNS server on 127.0.0.1:35353.

Runs four checks:
  1. `/etc/resolver/test` exists with the expected contents.
  2. The SIBB DNS server (sibb_dns_resolver) can be started in this
     process and answers an A query for `aurora-conference.test`
     with 127.0.0.1.
  3. macOS' resolver (via `dscacheutil`) resolves a `.test` name to
     127.0.0.1 — proves the macOS-side wiring is correct AND the
     iOS sim (which inherits the host resolver) will see the same
     answer.
  4. A loopback HTTP GET to `http://aurora-conference.test:<port>/`
     reaches a temporary local HTTP server.

Exit 0 on full success, 1 otherwise.
"""

from __future__ import annotations

import http.server
import socket
import subprocess
import sys
import threading
import time
import urllib.request


def step(msg: str) -> None:
    print(f"\n── {msg}")


def ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def fail(msg: str) -> None:
    print(f"  ✗ {msg}")


def check_resolver_file() -> bool:
    step("1) /etc/resolver/test")
    sys.path.insert(0, "sibb/benchmark")
    try:
        import sibb_dns_resolver
    except ImportError as e:
        fail(f"could not import sibb_dns_resolver: {e}")
        return False
    if sibb_dns_resolver.resolver_is_installed():
        ok("file present and contents match expected")
        return True
    fail("not installed or wrong contents. Run:")
    print("      python3 scripts/sibb_install_dns_resolver.py")
    return False


def check_dns_server_answers() -> bool:
    step("2) SIBB DNS server answers .test A queries")
    import sibb_dns_resolver
    port = sibb_dns_resolver.start_if_needed()
    if port is None:
        fail("could not bind 127.0.0.1:35353 — another process owns "
             "the port already")
        return False
    ok(f"bound to 127.0.0.1:{port}")
    # Send a query directly to our server.
    import struct
    qname = b""
    for label in "aurora-conference.test".split("."):
        qname += bytes([len(label)]) + label.encode()
    qname += b"\x00"
    query = (b"\x12\x34" + struct.pack(">H", 0x0100)
              + struct.pack(">HHHH", 1, 0, 0, 0)
              + qname + struct.pack(">HH", 1, 1))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(2)
    try:
        sock.sendto(query, ("127.0.0.1", port))
        response, _ = sock.recvfrom(4096)
    except socket.timeout:
        fail("no response from DNS server within 2s")
        return False
    finally:
        sock.close()
    # The answer rdata for an A record is 4 bytes; find via name
    # pointer 0xc0 0x0c.
    idx = response.find(b"\xc0\x0c")
    if idx < 0:
        fail("response has no answer section")
        return False
    rdata = response[idx + 12:idx + 16]
    if rdata == bytes([127, 0, 0, 1]):
        ok("resolved aurora-conference.test → 127.0.0.1")
        return True
    fail(f"unexpected rdata: {rdata.hex()}")
    return False


def check_macos_resolver_routes_test() -> bool:
    step("3) macOS resolver routes .test → 127.0.0.1")
    try:
        out = subprocess.run(
            ["dscacheutil", "-q", "host", "-a", "name",
             "aurora-conference.test"],
            capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        fail(f"dscacheutil failed: {e}")
        return False
    if "127.0.0.1" in out.stdout:
        ok("dscacheutil resolved to 127.0.0.1")
        return True
    fail("dscacheutil did NOT return 127.0.0.1.")
    print(f"      stdout: {out.stdout!r}")
    print(f"      stderr: {out.stderr!r}")
    print("      Likely cause: /etc/resolver/test missing or DNS "
          "server not running.")
    return False


def check_http_via_hostname() -> bool:
    step("4) http://aurora-conference.test:<port>/ reaches loopback")

    # Spawn a one-shot local HTTP server.
    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"sibb-verify-ok")

        def log_message(self, *_):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _H)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.1)
    url = f"http://aurora-conference.test:{port}/"
    try:
        body = urllib.request.urlopen(url, timeout=3).read().decode()
    except Exception as e:
        fail(f"GET {url} failed: {e}")
        server.shutdown()
        return False
    server.shutdown()
    if body == "sibb-verify-ok":
        ok(f"GET {url} returned 200 with the expected body")
        return True
    fail(f"GET {url} returned unexpected body: {body!r}")
    return False


def main() -> int:
    print("SIBB DNS resolver verification")
    print("==============================")
    results = [
        check_resolver_file(),
        check_dns_server_answers(),
        check_macos_resolver_routes_test(),
        check_http_via_hostname(),
    ]
    print()
    if all(results):
        print("✓ All checks passed — friendly hostnames are wired.")
        return 0
    print("✗ Some checks failed. See messages above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
