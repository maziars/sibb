"""A1 — protocol structure invariants.

Every value in `HANDLERS` must declare the four class attrs that
later Phase 2a items (A2 ensure_runner_permissions, A2 pre_runner
dispatch, A3 canonicalization, A4 topo-sort) will read. Construction
must be uniform — no `try: cls(reader=reader) except TypeError`
introspection.
"""

from __future__ import annotations

import inspect
from typing import List

import pytest

import sibb_state

pytestmark = pytest.mark.fast


_REQUIRED_ATTRS = ("bundle_id", "tcc_services", "pre_runner", "depends_on")


def test_handlers_registry_nonempty():
    assert sibb_state.HANDLERS, "HANDLERS registry is empty — nothing to test"


@pytest.mark.parametrize("name", sorted(sibb_state.HANDLERS))
def test_handler_declares_required_class_attrs(name: str):
    cls = sibb_state.HANDLERS[name]
    for attr in _REQUIRED_ATTRS:
        assert hasattr(cls, attr), (
            f"{cls.__name__} missing required class attribute {attr!r} "
            "(declared on AppStateHandler protocol)"
        )


@pytest.mark.parametrize("name", sorted(sibb_state.HANDLERS))
def test_handler_class_attr_types(name: str):
    cls = sibb_state.HANDLERS[name]
    assert isinstance(cls.bundle_id, str) and cls.bundle_id
    assert isinstance(cls.tcc_services, list)
    assert all(isinstance(s, str) for s in cls.tcc_services)
    assert isinstance(cls.pre_runner, bool)
    assert isinstance(cls.depends_on, list)
    assert all(isinstance(s, str) for s in cls.depends_on)


@pytest.mark.parametrize("name", sorted(sibb_state.HANDLERS))
def test_handler_construction_is_uniform(name: str):
    cls = sibb_state.HANDLERS[name]
    h = cls(reader=None)
    assert h.reader is None
    sentinel = object()
    h2 = cls(reader=sentinel)
    assert h2.reader is sentinel


@pytest.mark.parametrize("name", sorted(sibb_state.HANDLERS))
def test_handler_reset_is_coroutine(name: str):
    cls = sibb_state.HANDLERS[name]
    assert inspect.iscoroutinefunction(cls.reset), (
        f"{cls.__name__}.reset must be `async def`"
    )


@pytest.mark.parametrize("name", sorted(sibb_state.HANDLERS))
def test_handler_apply_is_coroutine(name: str):
    cls = sibb_state.HANDLERS[name]
    assert inspect.iscoroutinefunction(cls.apply), (
        f"{cls.__name__}.apply must be `async def`"
    )


@pytest.mark.parametrize("name", sorted(sibb_state.HANDLERS))
def test_pre_runner_handlers_implement_apply_pre_runner(name: str):
    cls = sibb_state.HANDLERS[name]
    if not cls.pre_runner:
        return
    assert hasattr(cls, "apply_pre_runner"), (
        f"{cls.__name__} declares pre_runner=True but does not implement "
        "apply_pre_runner(self, udid, entry)"
    )
    assert not inspect.iscoroutinefunction(cls.apply_pre_runner), (
        f"{cls.__name__}.apply_pre_runner must be sync — it runs while "
        "the simulator is shut down and there is no async socket"
    )


def test_no_more_async_suffix_methods():
    # Catches accidental reintroduction of `*_async` aliases during
    # future refactors — the protocol is async-first, so the suffix
    # is redundant and creates two-naming drift.
    for name, cls in sibb_state.HANDLERS.items():
        for attr in dir(cls):
            assert not attr.endswith("_async"), (
                f"{cls.__name__}.{attr}: drop the _async suffix; the "
                "protocol is async-first and the name should be just "
                "`reset` / `apply`."
            )


def test_make_handler_no_typeerror_dance_in_source():
    # A1 deleted the `try: cls(reader=reader) except TypeError: cls()`
    # introspection at sibb_state.py:351-358. If anyone reintroduces
    # it (or a moral equivalent) this lint will flag it on the next CI.
    import pathlib
    src = (pathlib.Path(sibb_state.__file__)).read_text()
    assert "except TypeError" not in src, (
        "sibb_state.py should not catch TypeError on handler construction "
        "— construction is uniform after A1. If you need conditional "
        "construction, add a class-level descriptor (e.g. requires_reader) "
        "rather than catching exceptions."
    )
