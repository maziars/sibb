"""Tiny UDP DNS resolver for the SIBB harness.

What it does
------------
Binds to `127.0.0.1:35353` and answers any A-record query with
`127.0.0.1` (and AAAA with `::1`). Hand-rolled; no third-party
dependencies. Paired with a one-time `/etc/resolver/test` install
(see `scripts/sibb_install_dns_resolver.py`), this lets the agent
see `http://events.test:<port>/event` URLs in MockSite tasks while
all traffic transparently routes to our loopback HTTP server.

How it's wired
--------------
* `start_if_needed()` — public entry point. Lazily spawns the
  resolver on first call (process-shared singleton). Subsequent
  calls return the existing port. Daemon thread → dies when the
  Python process exits; no explicit teardown needed.
* If port 35353 is already bound (another runner is alive on the
  same box, or mDNS misconfiguration), we catch `EADDRINUSE` and
  return `None` — callers fall back to `127.0.0.1` URLs and log a
  one-line warning.

Why a custom resolver instead of `dnslib`
-----------------------------------------
SIBB has zero hard third-party deps in the benchmark path. The DNS
wire format we need is ~50 lines of `struct.pack` and label parsing,
so a hand-rolled responder is cheaper than the dependency.

Why port 35353 (not 5353)
-------------------------
macOS' mDNSResponder binds `127.0.0.1:5353` for Bonjour. Picking a
different high port avoids the collision. `/etc/resolver/test` lists
this same port so the macOS resolver knows where to query.
"""

from __future__ import annotations

import socket
import struct
import sys
import threading
from typing import Optional, Tuple

DEFAULT_PORT = 35353
LOOPBACK_V4 = bytes([127, 0, 0, 1])
LOOPBACK_V6 = b"\x00" * 15 + b"\x01"  # ::1

# DNS type / class constants we care about.
_TYPE_A = 1
_TYPE_AAAA = 28
_CLASS_IN = 1

# Module-level singleton state. The first caller to `start_if_needed`
# spawns the socket + thread; everyone else reuses them.
_state_lock = threading.Lock()
_socket: Optional[socket.socket] = None
_thread: Optional[threading.Thread] = None
_bound_port: Optional[int] = None


def start_if_needed(port: int = DEFAULT_PORT) -> Optional[int]:
    """Lazily spawn the DNS resolver. Returns the bound port on
    success or `None` if the port is already in use (caller should
    fall back to numeric URLs).

    Thread-safe; idempotent within a process.
    """
    global _socket, _thread, _bound_port
    with _state_lock:
        if _bound_port is not None:
            return _bound_port
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(
                socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
        except OSError as e:
            # EADDRINUSE — another runner already owns the port. Tell
            # the caller so they can fall back to numeric URLs.
            print(f"[sibb_dns] could not bind 127.0.0.1:{port}: "
                  f"{e!r} — falling back to numeric URLs",
                  file=sys.stderr)
            return None
        sock.settimeout(0.5)
        _socket = sock
        _bound_port = port
        _thread = threading.Thread(
            target=_serve, args=(sock,), daemon=True,
            name=f"sibb-dns:{port}")
        _thread.start()
        return _bound_port


def _serve(sock: socket.socket) -> None:
    """UDP serve loop. Runs until the socket is closed (which only
    happens at process exit — daemon thread)."""
    while True:
        try:
            data, addr = sock.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:
            # Socket closed (process shutting down).
            return
        try:
            response = _build_response(data)
            if response is not None:
                sock.sendto(response, addr)
        except Exception:
            # A malformed query shouldn't kill the resolver.
            continue


# ─────────────────────────── DNS wire format ──────────────────────────


def _parse_question(query: bytes) -> Optional[Tuple[int, int, int]]:
    """Return `(question_start, question_end, qtype)` or None if the
    packet is malformed. `question_end` is the offset right after the
    QTYPE+QCLASS — i.e. the start of any additional sections.
    """
    if len(query) < 12:
        return None
    # qdcount must be ≥1 — we only answer the first question.
    qdcount = struct.unpack(">H", query[4:6])[0]
    if qdcount == 0:
        return None
    i = 12
    while i < len(query) and query[i] != 0:
        length = query[i]
        # Pointer (RFC 1035 §4.1.4) — high two bits set. Clients
        # don't typically use pointers in their questions; if they
        # do, bail.
        if length & 0xC0:
            return None
        i += 1 + length
        if i >= len(query):
            return None
    name_end = i + 1  # past the terminating zero label
    if name_end + 4 > len(query):
        return None
    qtype = struct.unpack(">H", query[name_end:name_end + 2])[0]
    qclass = struct.unpack(">H", query[name_end + 2:name_end + 4])[0]
    if qclass != _CLASS_IN:
        return None
    return (12, name_end + 4, qtype)


def _build_response(query: bytes) -> Optional[bytes]:
    parsed = _parse_question(query)
    if parsed is None:
        return None
    q_start, q_end, qtype = parsed
    txn_id = query[:2]
    # 0x8180 = standard response, recursion available, no error.
    if qtype == _TYPE_A:
        an_count = 1
        rdata = LOOPBACK_V4
        ans_type = _TYPE_A
        rdlength = 4
    elif qtype == _TYPE_AAAA:
        an_count = 1
        rdata = LOOPBACK_V6
        ans_type = _TYPE_AAAA
        rdlength = 16
    else:
        # Type we don't synthesize for — respond NOERROR with zero
        # answers (clients fall through cleanly).
        an_count = 0
        rdata = b""
        ans_type = 0
        rdlength = 0
    header = txn_id + struct.pack(
        ">HHHHH", 0x8180, 1, an_count, 0, 0)
    question = query[q_start:q_end]
    body = header + question
    if an_count == 0:
        return body
    # Answer: name pointer (0xC0 0x0C → offset 12), type, class, ttl,
    # rdlength, rdata.
    answer = (
        struct.pack(">HHHIH", 0xC00C, ans_type, _CLASS_IN, 60,
                     rdlength)
        + rdata
    )
    return body + answer


# ─────────────────────────── /etc/resolver detection ─────────────────


RESOLVER_FILE = "/etc/resolver/test"
EXPECTED_RESOLVER_CONTENT = "nameserver 127.0.0.1\nport 35353\n"


def resolver_is_installed() -> bool:
    """True iff `/etc/resolver/test` exists with the expected
    contents. False otherwise — caller falls back to numeric URLs.
    """
    try:
        with open(RESOLVER_FILE, "r", encoding="utf-8") as fh:
            content = fh.read()
    except (FileNotFoundError, PermissionError):
        return False
    return content.strip() == EXPECTED_RESOLVER_CONTENT.strip()
