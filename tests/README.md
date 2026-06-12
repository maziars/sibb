# SIBB tests

Four-layer test pyramid sized after a 2026-05-14 critic review. Full
rationale + the multi-app episode lifecycle that L3 e2e tests cover
described in design notes
under "Testing strategy."

## Layers

| Layer | Dir | Marker | Sim required? | Budget |
|---|---|---|---|---|
| L1 unit | `unit/` | `fast` | no | <2 s |
| L1.5 fake-reader | `handler/` | `fake_reader` | no | <5 s |
| L2 sim integration | `integration/` | `sim` | yes | <60 s |
| L3 multi-app e2e | `e2e/` | `sim` | yes | <90 s |
| L4 Swift contracts | `contract/` | `contract` | yes (nightly) | <60 s |

## Install (one-time)

The system Python 3.9 at `/Library/Developer/CommandLineTools/usr/bin/python3`
does NOT ship pytest. Install user-scope:

```bash
/Library/Developer/CommandLineTools/usr/bin/python3 -m pip install --user \
    pytest pytest-asyncio pytest-rerunfailures
```

Always invoke via `python3 -m pytest`, never bare `pytest` — bare picks
up Anaconda 3.7 first on this Mac.

## Run

```bash
# L1 only (default in pre-commit; sub-2 s)
python3 -m pytest -m fast

# L1 + L1.5 (no simulator)
python3 -m pytest -m "fast or fake_reader"

# Everything (sim-dependent tests skip-with-reason if SIBB_UDID unset)
python3 -m pytest

# Sim-backed only
SIBB_UDID=<your-simulator-UDID> \
    python3 -m pytest -m sim
```

## Add a fake-reader fixture for a new handler

When implementing a new app handler (e.g. Calendar), record a fixture
file from a real sim before writing L1.5 tests:

```bash
SIBB_UDID=<UDID> python3 sibb/tests/scripts/record_socket_fixture.py \
    "$SIBB_UDID" --scenario reminders_basic
```

Add a scenario function for your handler in
`sibb/tests/scripts/record_socket_fixture.py`. Commit the resulting
JSON under `sibb/tests/fixtures/swift_socket/<command>.json`.

## Quarantine

Tests can be marked `@pytest.mark.quarantined` if they're known-flaky.
PR CI excludes them (`-m "not quarantined"`); nightly runs them
separately and reports without blocking merge. Use sparingly — every
quarantined test is implicit tech debt.

## Why no `__init__.py` in test directories

Pytest uses rootless test collection here. Test files import siblings
directly (`from fakes.fake_reader import FakeXCUITestReader`); the
`sibb/tests/` directory is added to `sys.path` by
[`conftest.py`](./conftest.py). Adding `__init__.py` causes
import-mode conflicts with pytest's collection. The `fakes/`
subpackage keeps its `__init__.py` because it IS a Python package
imported by tests; the test-layer directories are not.
