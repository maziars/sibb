"""
Symbolic references for task construction.

A `SymbolicRef` stands in for a value that appears in multiple
structured positions across a single task — spec entries,
verify_checks, params, etc. The reference is created once with its
canonical value; reuse the same SymbolicRef instance wherever the
value needs to be referenced. A pre-dispatch `resolve_refs(structure)`
pass replaces every SymbolicRef in a nested dict/list/dataclass tree
with its `.value`, producing pure-data output the dispatcher consumes.

This kills the "verifier reads params['title'], handler reads
spec[0]['title']" drift class for cross-position references. If you
update the value, you update one place (the SymbolicRef construction
site); every appearance in the structure follows.

Today this is opt-in: generators can adopt SymbolicRef where they
want, dispatchers don't care (they just see pure strings after
`resolve_refs`). When TaskBuilder lands in Phase 2c C2, refs will
become the natural default for cross-position values.

Instruction strings stay regular strings. Generators that need an
instruction templated against a SymbolicRef should use `ref.value`
directly when building the string — SymbolicRef is for STRUCTURED
data only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SymbolicRef:
    name: str
    value: Any

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("SymbolicRef.name must be a non-empty string")


def resolve_refs(obj: Any) -> Any:
    """Recursively replace SymbolicRef instances with their `.value`.

    Walks dicts (by value), lists/tuples (by element), and typed
    spec entries (re-serialized via `.to_dict()` and re-resolved).
    Returns a NEW structure — does not mutate the input.

    Primitives (str, int, bool, None, etc.) pass through unchanged.
    Unknown object types pass through unchanged.
    """
    if isinstance(obj, SymbolicRef):
        return obj.value
    if isinstance(obj, dict):
        return {k: resolve_refs(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve_refs(x) for x in obj]
    if isinstance(obj, tuple):
        return tuple(resolve_refs(x) for x in obj)
    # Typed spec entries from sibb_spec: lazily detect to avoid a
    # hard import dependency (and any future circular import risk).
    try:
        from sibb_spec import _SpecBase
        if isinstance(obj, _SpecBase):
            return resolve_refs(obj.to_dict())
    except ImportError:
        pass
    return obj
