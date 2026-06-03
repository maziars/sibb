"""FilesHandler — L1 + L1.5 tests.

Same shape as the Contacts test suite, but with the FileManager-
backed handler. Covers:
- Handler-protocol attribute lints (no TCC service, no pre_runner)
- Registry membership + canonicalization
- File typed spec round-trip
- Reset + apply against the in-memory FakeXCUITestReader
- Path-traversal rejection (.., absolute paths)
- base64 vs utf-8 encoding round-trip
- Resource fetcher: files.all with directory + recursive selectors
"""

from __future__ import annotations

import base64

import pytest

from sibb_spec import File, SPEC_TYPES, validate_entry
from sibb_state import (
    HANDLERS,
    FilesHandler,
    canonicalize_app,
    collect_tcc_services,
)

pytestmark = pytest.mark.fast


# ─────────────────────────── handler-protocol lints ──────────────────

def test_files_handler_registered_by_bundle_id():
    assert FilesHandler.bundle_id == "com.apple.DocumentsApp"
    assert HANDLERS[FilesHandler.bundle_id] is FilesHandler


def test_files_handler_declares_no_tcc_services():
    """FileManager works inside any process — no permission grant
    needed. Adding a TCC service here would surface a no-op grant
    that masks a future regression where a permission gate WAS
    added but the handler missed it.
    """
    assert FilesHandler.tcc_services == []


def test_files_handler_is_not_a_pre_runner():
    """Filesystem state lives in the runner sandbox — no shut-down-
    required apply step."""
    assert FilesHandler.pre_runner is False
    assert FilesHandler.pre_runner_kinds == []


def test_files_handler_does_not_contribute_to_collect_tcc_services():
    """Sanity: adding Files must NOT extend the TCC grants the
    runner performs — otherwise we'd waste a grant call (and confuse
    future contributors looking at simctl privacy logs).
    """
    services = collect_tcc_services()
    # Files's tcc_services is [] — nothing files-specific should be
    # introduced. (Reminders+calendar+contacts still present.)
    for s in services:
        assert "file" not in s.lower(), (
            f"unexpected file-related TCC service: {s}"
        )


def test_canonicalize_files_friendly_name():
    assert canonicalize_app("Files") == "com.apple.DocumentsApp"
    assert canonicalize_app("files") == "com.apple.DocumentsApp"


# ─────────────────────────── File spec dataclass ─────────────────────

def test_file_spec_registered():
    assert ("Files", "file") in SPEC_TYPES
    assert SPEC_TYPES[("Files", "file")] is File


def test_file_minimal_construction():
    f = File(path="notes.txt")
    assert f.path == "notes.txt"
    assert f.content == ""
    assert f.encoding is None


def test_file_to_dict_canonical_shape():
    f = File(path="notes/today.txt", content="hello",
              encoding="utf-8")
    assert f.to_dict() == {
        "app": "Files", "type": "file",
        "path": "notes/today.txt",
        "content": "hello",
        "encoding": "utf-8",
    }


def test_file_round_trip():
    original = File(path="a.txt", content="x" * 100)
    back = File.from_dict(original.to_dict())
    assert back == original


def test_validate_entry_accepts_file():
    typed, err = validate_entry({
        "app": "Files", "type": "file",
        "path": "x.txt", "content": "hi",
    })
    assert err is None
    assert isinstance(typed, File)


# ─────────────────────────── handler reset + apply ───────────────────

async def test_handler_apply_creates_file_via_socket():
    from fakes.fake_reader import FakeXCUITestReader
    r = FakeXCUITestReader()
    h = FilesHandler(reader=r)
    await h.apply({"type": "file", "path": "notes.txt",
                    "content": "hello"})
    last = r.history[-1]
    assert last["request"]["type"] == "create_file"
    assert last["request"]["path"] == "notes.txt"
    assert last["request"]["content"] == "hello"
    assert last["response"]["ok"] is True
    # Read it back via the fake to confirm in-memory persistence.
    resp = await r._send({"type": "read_file", "path": "notes.txt"})
    assert resp["ok"] is True
    assert resp["content"] == "hello"


async def test_handler_apply_passes_through_encoding():
    """base64 encoding is the escape hatch for binary content. The
    handler must forward it; otherwise binary tasks would round-trip
    through utf-8 and corrupt embedded null bytes."""
    from fakes.fake_reader import FakeXCUITestReader
    r = FakeXCUITestReader()
    h = FilesHandler(reader=r)
    raw = b"\x00\x01\x02hello"
    encoded = base64.b64encode(raw).decode("ascii")
    await h.apply({"type": "file", "path": "blob.bin",
                    "content": encoded, "encoding": "base64"})
    req = r.history[-1]["request"]
    assert req["encoding"] == "base64"
    # Verify the fake decoded it correctly: read back as base64.
    resp = await r._send({"type": "read_file", "path": "blob.bin",
                            "encoding": "base64"})
    assert resp["ok"] is True
    assert base64.b64decode(resp["content"]) == raw


async def test_handler_apply_omits_encoding_when_none():
    """encoding defaults to utf-8 on the Swift side; passing
    encoding=None from Python should drop the field entirely rather
    than send `"encoding": null`."""
    from fakes.fake_reader import FakeXCUITestReader
    r = FakeXCUITestReader()
    h = FilesHandler(reader=r)
    await h.apply({"type": "file", "path": "a.txt", "content": "x"})
    req = r.history[-1]["request"]
    assert "encoding" not in req


async def test_handler_reset_wipes_workspace():
    from fakes.fake_reader import FakeXCUITestReader
    r = FakeXCUITestReader()
    h = FilesHandler(reader=r)
    await h.apply({"type": "file", "path": "a.txt", "content": "1"})
    await h.apply({"type": "file", "path": "b/c.txt", "content": "2"})
    await h.reset()
    resp = await r._send({"type": "list_files"})
    assert resp["files"] == []


async def test_handler_apply_raises_on_socket_error():
    class FailingReader:
        async def _send(self, cmd):
            return {"ok": False, "error": "disk full"}
    h = FilesHandler(reader=FailingReader())
    with pytest.raises(RuntimeError, match="disk full"):
        await h.apply({"type": "file", "path": "a.txt"})


async def test_handler_apply_rejects_unknown_entry_kind():
    class _Noop:
        async def _send(self, cmd):
            return {"ok": True}
    h = FilesHandler(reader=_Noop())
    with pytest.raises(ValueError, match="unknown entry type"):
        await h.apply({"type": "directory"})


# ─────────────────────────── fake-reader path safety ─────────────────

async def test_fake_reader_rejects_absolute_path():
    from fakes.fake_reader import FakeXCUITestReader
    r = FakeXCUITestReader()
    resp = await r._send({"type": "create_file",
                            "path": "/etc/passwd", "content": ""})
    assert resp["ok"] is False
    assert "relative" in resp["error"]


async def test_fake_reader_rejects_parent_traversal():
    from fakes.fake_reader import FakeXCUITestReader
    r = FakeXCUITestReader()
    resp = await r._send({"type": "create_file",
                            "path": "../escaped.txt", "content": ""})
    assert resp["ok"] is False
    assert ".." in resp["error"]


async def test_fake_reader_empty_path_rejected():
    from fakes.fake_reader import FakeXCUITestReader
    r = FakeXCUITestReader()
    resp = await r._send({"type": "create_file",
                            "path": "", "content": ""})
    assert resp["ok"] is False


async def test_fake_reader_read_nonexistent_returns_not_found():
    from fakes.fake_reader import FakeXCUITestReader
    r = FakeXCUITestReader()
    resp = await r._send({"type": "read_file", "path": "nope.txt"})
    assert resp["ok"] is False
    assert "not found" in resp["error"]


async def test_fake_reader_list_files_emits_dir_rows_for_parents():
    """Swift's enumerator returns both file AND parent-directory
    rows. The fake must mirror that — otherwise selector
    `{"type": "dir"}` against the fake returns [] while the real
    Swift returns the parent dirs of every nested file."""
    from fakes.fake_reader import FakeXCUITestReader
    r = FakeXCUITestReader()
    await r._send({"type": "create_file",
                    "path": "notes/work/today.txt",
                    "content": "x"})
    resp = await r._send({"type": "list_files"})
    paths_by_type = {(row["type"], row["path"])
                      for row in resp["files"]}
    assert ("file", "notes/work/today.txt") in paths_by_type
    assert ("dir", "notes") in paths_by_type
    assert ("dir", "notes/work") in paths_by_type


async def test_fake_reader_list_files_scoped_directory():
    from fakes.fake_reader import FakeXCUITestReader
    r = FakeXCUITestReader()
    await r._send({"type": "create_file",
                    "path": "notes/a.txt", "content": "1"})
    await r._send({"type": "create_file",
                    "path": "notes/b.txt", "content": "2"})
    await r._send({"type": "create_file",
                    "path": "other/c.txt", "content": "3"})
    resp = await r._send({"type": "list_files",
                            "directory": "notes"})
    files = [r["path"] for r in resp["files"] if r["type"] == "file"]
    assert sorted(files) == ["notes/a.txt", "notes/b.txt"]


# ─────────────────────────── resource fetcher ────────────────────────

def test_files_all_in_resource_fetchers():
    from sibb_verify import RESOURCE_FETCHERS
    assert "files.all" in RESOURCE_FETCHERS


async def test_files_all_fetcher_returns_socket_rows():
    from fakes.fake_reader import FakeXCUITestReader
    from sibb_verify import RESOURCE_FETCHERS
    r = FakeXCUITestReader()
    await r._send({"type": "create_file",
                    "path": "a.txt", "content": "x"})
    fetcher = RESOURCE_FETCHERS["files.all"]
    rows = await fetcher(r, {})
    file_rows = [row for row in rows if row["type"] == "file"]
    assert len(file_rows) == 1
    assert file_rows[0]["path"] == "a.txt"


async def test_files_all_fetcher_strips_directory_and_recursive_from_selector():
    """`directory` and `recursive` are SOCKET pushdown fields, not
    client-side selector fields. If the fetcher leaves them in the
    selector, `_filter_records` would reject every row that doesn't
    have a literal `directory` or `recursive` field — which is every
    real file row."""
    from fakes.fake_reader import FakeXCUITestReader
    from sibb_verify import RESOURCE_FETCHERS
    r = FakeXCUITestReader()
    await r._send({"type": "create_file",
                    "path": "notes/a.txt", "content": "1"})
    fetcher = RESOURCE_FETCHERS["files.all"]
    rows = await fetcher(r, {"directory": "notes"})
    file_paths = [row["path"] for row in rows if row["type"] == "file"]
    assert "notes/a.txt" in file_paths
