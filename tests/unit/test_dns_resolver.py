"""sibb_dns_resolver — L1 unit tests for the DNS wire format and the
singleton/lazy-start lifecycle.

We don't exercise `/etc/resolver/test` (that requires sudo on the
host); we do exercise the actual UDP server end-to-end by sending it
a real A/AAAA query and parsing the response.
"""

from __future__ import annotations

import socket
import struct

import pytest

import sibb_dns_resolver

pytestmark = pytest.mark.fast


def _build_query(name: str, qtype: int = 1) -> bytes:
    """Build a minimal DNS query packet for `name`."""
    txn_id = b"\x12\x34"
    flags = 0x0100  # standard query, RD=1
    counts = struct.pack(">HHHH", 1, 0, 0, 0)
    header = txn_id + struct.pack(">H", flags) + counts
    qname = b""
    for label in name.split("."):
        qname += bytes([len(label)]) + label.encode("ascii")
    qname += b"\x00"
    return header + qname + struct.pack(">HH", qtype, 1)  # qtype, class=IN


def _parse_response(packet: bytes) -> dict:
    """Pick out the answer rdata as a python value (IPv4 string for
    A, IPv6 string for AAAA). Returns `{"an_count": ..., "rdata":
    ...}`."""
    an_count = struct.unpack(">H", packet[6:8])[0]
    # Skip header + question — for our simple queries the question
    # ends just before the answer section. Easier: scan to the first
    # 0xc0 0x0c name pointer to find the answer.
    if an_count == 0:
        return {"an_count": 0, "rdata": None}
    idx = packet.find(b"\xc0\x0c")
    assert idx >= 0
    ans_type = struct.unpack(">H", packet[idx + 2:idx + 4])[0]
    rdlength = struct.unpack(">H", packet[idx + 10:idx + 12])[0]
    rdata = packet[idx + 12:idx + 12 + rdlength]
    if ans_type == 1:  # A
        return {"an_count": an_count,
                "rdata": ".".join(str(b) for b in rdata)}
    elif ans_type == 28:  # AAAA
        return {"an_count": an_count, "rdata": rdata.hex()}
    return {"an_count": an_count, "rdata": rdata}


def test_build_response_a_record():
    q = _build_query("aurora-conference.test", qtype=1)
    response = sibb_dns_resolver._build_response(q)
    parsed = _parse_response(response)
    assert parsed["an_count"] == 1
    assert parsed["rdata"] == "127.0.0.1"


def test_build_response_aaaa_record():
    q = _build_query("aurora-conference.test", qtype=28)
    response = sibb_dns_resolver._build_response(q)
    parsed = _parse_response(response)
    assert parsed["an_count"] == 1
    # ::1 = 15 zero bytes + 0x01 → 30 hex zeros + "01".
    assert parsed["rdata"] == "00" * 15 + "01"


def test_build_response_unknown_type_returns_noerror_zero_answers():
    """An unsupported qtype should produce a NOERROR response with
    zero answers — a clean fall-through, not a hard error."""
    q = _build_query("example.test", qtype=16)  # TXT, unsupported
    response = sibb_dns_resolver._build_response(q)
    parsed = _parse_response(response)
    assert parsed["an_count"] == 0


def test_malformed_packet_returns_none():
    """Truncated header → None. Used by the serve loop to drop bad
    packets without crashing."""
    assert sibb_dns_resolver._build_response(b"\x00\x00") is None


def test_resolver_is_installed_negative_when_file_absent(monkeypatch):
    monkeypatch.setattr(sibb_dns_resolver, "RESOLVER_FILE",
                        "/tmp/sibb-nonexistent-resolver-file")
    assert sibb_dns_resolver.resolver_is_installed() is False


def test_end_to_end_udp_query(tmp_path):
    """Real socket round-trip — confirms the daemon thread answers a
    query over UDP. Uses a port other than the default to avoid
    colliding with a running benchmark."""
    # Reset the singleton so this test can pick its own port.
    sibb_dns_resolver._bound_port = None
    sibb_dns_resolver._socket = None
    sibb_dns_resolver._thread = None
    test_port = 35364  # arbitrary high port unlikely to collide
    bound = sibb_dns_resolver.start_if_needed(port=test_port)
    assert bound == test_port

    q = _build_query("aurora-conference.test", qtype=1)
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.settimeout(2.0)
    try:
        client.sendto(q, ("127.0.0.1", test_port))
        response, _ = client.recvfrom(4096)
    finally:
        client.close()

    parsed = _parse_response(response)
    assert parsed["an_count"] == 1
    assert parsed["rdata"] == "127.0.0.1"
