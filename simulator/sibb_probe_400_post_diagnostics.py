"""Probe (#243): what causes MockSite to return HTTP 400 for an
RSVP-form POST?

Background
==========
The original sim-verify of `gen_safari_rsvp_form` (task #242) saw
the agent emit 4 POST requests that returned HTTP 400. Step
5b/5c/5d work (font-size randomization, 8 px margins, 44 pt
buttons) was motivated by zoom-related ghost-button issues — but
that work doesn't *directly* address 400 responses. This probe
makes 400 reproduction (or lack thereof) explicit, so #243 can be
closed with empirical evidence rather than "it didn't happen on
seed=1 after Step 5."

`sibb_mock_site.py` source review (2026-06-07):
* `_send_html(...)` is only ever called with status 200.
* `send_response(302)` is the post-login redirect for the login form.
* `send_error(404)` fires for unknown POST paths AND unknown GET paths.
* NO code path explicitly returns 400. Any 400 in the log must come
  from `BaseHTTPRequestHandler` itself rejecting a malformed request
  line / headers / Content-Length / etc.

Therefore: this probe hits MockSite directly via `urllib` with the
suspect request variants an iOS Safari agent might emit, and
records the response code for each. Anything that returns 400 is a
concrete reproduction; anything else demonstrates the BaseHTTPServer
tolerates it.

Variants tested
===============
A. Normal valid POST → expect 200
B. POST with empty body but valid Content-Length: 0 → ?
C. POST with malformed Content-Type → ?
D. POST with no Content-Type → ?
E. POST with Content-Length larger than body → likely 400 (server
   read-timeout / framing error)
F. POST with Content-Length larger than MockSite's body cap → ?
G. POST with bare text body (no form encoding) → ?
H. POST to a path MockSite doesn't recognize → expect 404
I. GET to a path that expects POST → ?
J. POST with multipart body (without proper boundary) → ?

Output
======
Console table:
  STATUS  VARIANT       PATH       NOTES

Plus the MockSite `request_log()` dump (full record per request),
which is what we'd actually be looking at to investigate a live
agent run.

Usage
=====
    python3 sibb/simulator/sibb_probe_400_post_diagnostics.py

No simulator UDID needed — this is a pure HTTP probe.
"""
from __future__ import annotations

import http.client
import sys
import urllib.parse
import urllib.request
from pathlib import Path

_HERE = Path(__file__).resolve()
_ROOT = _HERE.parents[2]
sys.path.insert(0, str(_ROOT / "sibb" / "benchmark"))
sys.path.insert(0, str(_ROOT / "sibb" / "simulator"))


def _raw_post(host: str, port: int, path: str, body: bytes, *,
               content_type: str = None,
               content_length: int = None,
               headers: dict = None) -> int:
    """Open a low-level HTTP connection and send a raw POST. Returns
    the response status code (None on connection error)."""
    conn = http.client.HTTPConnection(host, port, timeout=3.0)
    try:
        hdrs = {}
        if content_type is not None:
            hdrs["Content-Type"] = content_type
        if content_length is not None:
            hdrs["Content-Length"] = str(content_length)
        if headers:
            hdrs.update(headers)
        conn.request("POST", path, body=body, headers=hdrs)
        resp = conn.getresponse()
        return resp.status
    except http.client.HTTPException as e:
        return f"HTTPException: {e}"
    except OSError as e:
        return f"OSError: {e}"
    finally:
        conn.close()


def _raw_get(host: str, port: int, path: str) -> int:
    conn = http.client.HTTPConnection(host, port, timeout=3.0)
    try:
        conn.request("GET", path)
        return conn.getresponse().status
    except Exception as e:
        return f"err: {e}"
    finally:
        conn.close()


def _raw_request_line(host: str, port: int, line: bytes) -> int:
    """Open a raw socket and send a custom request line — bypasses
    http.client's validation so we can poke BaseHTTPRequestHandler's
    parser directly."""
    import socket
    s = socket.create_connection((host, port), timeout=3.0)
    try:
        s.sendall(line)
        s.settimeout(2.0)
        data = b""
        try:
            while len(data) < 256:
                chunk = s.recv(256)
                if not chunk:
                    break
                data += chunk
        except socket.timeout:
            pass
        # Extract status code from first line ("HTTP/1.0 NNN ...")
        first = data.split(b"\r\n", 1)[0] if data else b""
        parts = first.split(b" ", 2)
        if len(parts) >= 2 and parts[1].isdigit():
            return int(parts[1])
        return f"raw: {first[:80]!r}"
    except Exception as e:
        return f"err: {e}"
    finally:
        s.close()


def main() -> int:
    import harness_pages  # noqa: F401  populate PAGE_REGISTRY
    from harness_layout import PAGE_REGISTRY
    from sibb_mock_site import MockSite

    site = MockSite(
        site_id="post-400-probe",
        static_pages={"/event": PAGE_REGISTRY["rsvp_event"]},
    )
    site.page_seed = 506456970
    site.start()
    print(f"[probe] MockSite on {site.base_url}")
    host = "127.0.0.1"
    port = site.port

    print()
    print("─" * 72)
    print(f"{'STATUS':>8s}  VARIANT")
    print("─" * 72)

    def row(status, label):
        print(f"{str(status):>8s}  {label}")

    # A. Normal valid form POST to /rsvp
    body_a = urllib.parse.urlencode({
        "name": "Test Person",
        "contact": "test@example.com",
        "attending": "yes",
    }).encode("utf-8")
    row(_raw_post(host, port, "/rsvp", body_a,
                    content_type="application/x-www-form-urlencoded",
                    content_length=len(body_a)),
        "A. valid form POST /rsvp")

    # B. Empty body, Content-Length: 0
    row(_raw_post(host, port, "/rsvp", b"",
                    content_type="application/x-www-form-urlencoded",
                    content_length=0),
        "B. empty body, CL:0")

    # C. Wrong Content-Type
    row(_raw_post(host, port, "/rsvp", body_a,
                    content_type="text/plain",
                    content_length=len(body_a)),
        "C. text/plain Content-Type")

    # D. NO Content-Type at all (delete the default urllib adds)
    row(_raw_post(host, port, "/rsvp", body_a,
                    content_length=len(body_a)),
        "D. no Content-Type")

    # E. Content-Length LARGER than body (server reads past EOF)
    row(_raw_post(host, port, "/rsvp", body_a,
                    content_type="application/x-www-form-urlencoded",
                    content_length=len(body_a) + 1000),
        "E. CL > body (over-sized)")

    # F. Very large body (10 MB) — body cap on the server side?
    big_body = b"x=" + b"a" * (10 * 1024 * 1024)
    try:
        status_f = _raw_post(host, port, "/rsvp", big_body,
                              content_type=("application/x-www-form-"
                                            "urlencoded"),
                              content_length=len(big_body))
    except Exception as e:
        status_f = f"err: {e}"
    row(status_f, "F. 10 MB body")

    # G. Bare text body (no form encoding)
    row(_raw_post(host, port, "/rsvp", b"just some plain text",
                    content_type="application/x-www-form-urlencoded",
                    content_length=20),
        "G. unparseable body")

    # H. Unknown path
    row(_raw_post(host, port, "/no-such-path", body_a,
                    content_type="application/x-www-form-urlencoded",
                    content_length=len(body_a)),
        "H. POST to /no-such-path")

    # I. GET to /rsvp (the form's POST endpoint)
    row(_raw_get(host, port, "/rsvp"), "I. GET /rsvp (POST-only path)")

    # J. Malformed multipart body
    bad_multipart = b"--boundary\r\nplain text\r\n--boundary--\r\n"
    row(_raw_post(host, port, "/rsvp", bad_multipart,
                    content_type="multipart/form-data; boundary=boundary",
                    content_length=len(bad_multipart)),
        "J. malformed multipart")

    # K. Bad HTTP method (RAW request line)
    row(_raw_request_line(host, port,
                            b"GIBBERISH /rsvp HTTP/1.1\r\n"
                            b"Host: 127.0.0.1\r\n\r\n"),
        "K. raw: bad method GIBBERISH")

    # L. Truly malformed request line
    row(_raw_request_line(host, port, b"\xff\xfe garbage\r\n\r\n"),
        "L. raw: malformed request line")

    # M. Empty request (just headers, no body, no method)
    row(_raw_request_line(host, port, b"\r\n\r\n"), "M. raw: empty")

    print("─" * 72)
    print()
    print("─" * 72)
    print("MockSite request_log dump:")
    print("─" * 72)
    for i, entry in enumerate(site.request_log()):
        code = entry.get("response_code")
        method = entry.get("method") or "?"
        path = entry.get("path") or "?"
        ct = entry.get("content_type") or "—"
        cl = entry.get("content_length") or "—"
        ua = (entry.get("user_agent") or "")[:30]
        marker = "  ← 400" if code == 400 else ""
        print(f"  [{i:2d}] {code:>4d} {method:7s} {path:20s} "
              f"CT={ct[:25]:25s} CL={cl:>6s}{marker}")
    print()

    fourhundreds = [e for e in site.request_log()
                     if e.get("response_code") == 400]
    print(f"[probe] total 400s observed: {len(fourhundreds)}")
    if fourhundreds:
        print("[probe] VARIANTS THAT TRIGGERED 400:")
        for e in fourhundreds:
            print(f"        {e['method']} {e['path']} "
                  f"CT={e['content_type']!r} "
                  f"CL={e['content_length']!r}")
    else:
        print("[probe] No 400 reproduced from any tested variant.")
        print("        BaseHTTPServer apparently tolerates everything")
        print("        we threw at it; the original task #243 400s")
        print("        likely came from a code path no longer reachable")
        print("        after Step 5 (or were misidentified — could have")
        print("        been 404s from agent typing 'yes' into URL bar).")

    site.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
