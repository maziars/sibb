"""FilesHandler — L2 sim integration.

Runs against the real iOS simulator with the live XCUITest runner.
Covers what L1 + L1.5 can't:
1. Swift FileManager-backed command shapes match Python expectations
   (paths, response keys, error strings).
2. Workspace directory creation actually happens at
   Documents/SIBBWorkspace/ inside the runner sandbox.
3. Path-traversal rejection is enforced by the real Swift, not just
   the fake's mirror logic.
4. Read-after-write round-trips through the real filesystem (catches
   any utf-8 encoding bug or atomicity issue the fake glosses over).
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio

pytestmark = pytest.mark.sim

_SIM_DIR = Path(__file__).resolve().parents[2] / "simulator"
_BENCHMARK_DIR = Path(__file__).resolve().parents[2] / "benchmark"
for p in (_SIM_DIR, _BENCHMARK_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from sibb_scaffold import AXReader  # noqa: E402
from sibb_state import FilesHandler  # noqa: E402


@pytest_asyncio.fixture(scope="module")
async def reader(sibb_udid: str) -> AsyncIterator[AXReader]:
    r = AXReader(sibb_udid)
    await r.start(bundle_id="com.apple.springboard")
    try:
        # Clean workspace at fixture entry so tests are order-
        # independent within the module.
        await r._xcuitest._send({"type": "wipe_files"})
        yield r
    finally:
        await r.stop()


# ────────────────────── Swift command shapes ────────────────────────

async def test_create_file_round_trip(reader):
    resp = await reader._xcuitest._send({
        "type": "create_file",
        "path": "notes.txt",
        "content": "hello world",
    })
    assert resp.get("ok") is True, f"create_file failed: {resp}"
    assert resp.get("path") == "notes.txt"
    assert resp.get("size") == len("hello world")

    resp = await reader._xcuitest._send({
        "type": "read_file", "path": "notes.txt"})
    assert resp.get("ok") is True
    assert resp.get("content") == "hello world"


async def test_create_file_creates_nested_parent_directories(reader):
    """Swift's create_file does the equivalent of `mkdir -p` on the
    parent before writing. Without that, `notes/work/today.txt`
    would fail because `notes/work/` doesn't exist yet."""
    await reader._xcuitest._send({"type": "wipe_files"})
    resp = await reader._xcuitest._send({
        "type": "create_file",
        "path": "notes/work/today.txt",
        "content": "x",
    })
    assert resp.get("ok") is True
    resp = await reader._xcuitest._send({
        "type": "read_file", "path": "notes/work/today.txt"})
    assert resp.get("ok") is True
    assert resp.get("content") == "x"


async def test_create_file_base64_round_trip(reader):
    """Binary safety: write base64 → read base64 → compare bytes."""
    raw = bytes(range(32))  # 0..31, includes nulls + control chars
    encoded = base64.b64encode(raw).decode("ascii")
    await reader._xcuitest._send({
        "type": "create_file",
        "path": "blob.bin",
        "content": encoded,
        "encoding": "base64",
    })
    resp = await reader._xcuitest._send({
        "type": "read_file", "path": "blob.bin", "encoding": "base64"})
    assert resp.get("ok") is True
    assert base64.b64decode(resp["content"]) == raw


async def test_create_file_rejects_absolute_path(reader):
    resp = await reader._xcuitest._send({
        "type": "create_file",
        "path": "/etc/passwd",
        "content": "evil",
    })
    assert resp.get("ok") is False
    assert "relative" in resp.get("error", "")


async def test_create_file_rejects_parent_traversal(reader):
    resp = await reader._xcuitest._send({
        "type": "create_file",
        "path": "../escape.txt",
        "content": "evil",
    })
    assert resp.get("ok") is False
    assert ".." in resp.get("error", "")


async def test_read_file_missing_returns_not_found(reader):
    resp = await reader._xcuitest._send({
        "type": "read_file", "path": "nonexistent.txt"})
    assert resp.get("ok") is False
    assert "not found" in resp.get("error", "")


async def test_list_files_emits_file_and_dir_rows(reader):
    """The Swift enumerator yields both files and directories. The
    Python verifier framework (RESOURCE_FETCHERS["files.all"]) relies
    on this — selector `{"type": "dir"}` against the real Swift must
    match parent directories of nested files."""
    await reader._xcuitest._send({"type": "wipe_files"})
    await reader._xcuitest._send({
        "type": "create_file",
        "path": "notes/work/today.txt", "content": "x"})
    resp = await reader._xcuitest._send({"type": "list_files"})
    assert resp.get("ok") is True
    rows = resp.get("files", [])
    files = {r["path"] for r in rows if r["type"] == "file"}
    dirs = {r["path"] for r in rows if r["type"] == "dir"}
    assert "notes/work/today.txt" in files
    assert "notes" in dirs
    assert "notes/work" in dirs


async def test_wipe_files_clears_workspace(reader):
    # Seed.
    for path in ("a.txt", "b/c.txt", "d/e/f.txt"):
        await reader._xcuitest._send({
            "type": "create_file", "path": path, "content": "x"})
    resp = await reader._xcuitest._send({"type": "list_files"})
    assert any(r["type"] == "file" for r in resp.get("files", []))

    resp = await reader._xcuitest._send({"type": "wipe_files"})
    assert resp.get("ok") is True
    resp = await reader._xcuitest._send({"type": "list_files"})
    assert resp.get("files") == []


# ────────────────────── FilesHandler integration ───────────────────

async def test_handler_apply_then_verifier_fetcher_round_trip(reader):
    """End-to-end: handler.apply writes file → fetcher returns it.
    Mirrors the loop a verifier-AFTER would do during an episode."""
    from sibb_verify import RESOURCE_FETCHERS

    await reader._xcuitest._send({"type": "wipe_files"})
    handler = FilesHandler(reader=reader._xcuitest)
    await handler.apply({"type": "file",
                          "path": "session/notes.txt",
                          "content": "agent wrote this"})

    fetcher = RESOURCE_FETCHERS["files.all"]
    rows = await fetcher(reader._xcuitest, {})
    matches = [r for r in rows
               if r["type"] == "file"
               and r["path"] == "session/notes.txt"]
    assert len(matches) == 1, f"expected one file, got {rows}"


async def test_handler_reset_clears_via_handler_api(reader):
    handler = FilesHandler(reader=reader._xcuitest)
    await handler.apply({"type": "file", "path": "x.txt"})
    await handler.apply({"type": "file", "path": "y.txt"})
    await handler.reset()
    resp = await reader._xcuitest._send({"type": "list_files"})
    assert resp.get("files") == []
