"""
Generic verifier framework: check kinds operating on per-app resources.

A task declares its success conditions as `verify_checks: List[Dict]`.
Each check has:

  - `kind`     : one of CHECK_KINDS (exists / absent / count /
                 attribute_eq / subset)
  - `resource` : one of RESOURCE_FETCHERS (e.g. "reminders.lists",
                 "reminders.items"); fetches a list of records
                 from the live sim via the XCUITest socket
  - `selector` : dict of attribute equalities used to filter the
                 fetched records before the check runs
  - `severity` : "blocking" (default) | "informational"
  - kind-specific params (`attr`/`value` for attribute_eq, `op`/`n`
    for count, etc.)
  - `label`    : optional human-readable description for the report

The dispatcher (`run_checks`) routes each check through its fetcher
and kind handler, returning a structured `CheckResult` per check.
`blocking_pass` aggregates: True iff every blocking check passed.

Why structured results: the legacy `(label, bool|None)` tuple shape
collapses error vs fail (a Swift socket failure and a real
verification miss become indistinguishable). `CheckResult.status`
keeps them separate; `severity="informational"` carries the old
`None` semantics (logged but not gating).

Compatibility: `legacy_format(results)` translates back to the
tuple shape so `verify_reminders_list_task_async` keeps its
existing (passed, checks) return contract.

String matching in selectors is case-insensitive by default — list
names and reminder titles are typically used by humans and the
Swift side is already case-insensitive on its list filter (see
`sibb_xcuitest_setup.sh:list_reminders`).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple


# ────────────────────────── exceptions + types ────────────────────────

class ResourceFetchError(RuntimeError):
    """Raised by a resource fetcher when the underlying socket call fails."""


@dataclass(frozen=True)
class CheckResult:
    kind: str
    label: str
    status: str       # "pass" | "fail" | "info" | "error"
    severity: str     # "blocking" | "informational"
    evidence: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BaselineSnapshot:
    """Per-resource state captured at a moment in time.

    Created via `BaselineSnapshot.capture(reader, [resource_keys])`
    at the start of an episode; passed to `run_check` / `run_checks`
    via the `baseline=` kwarg so the `identity` check kind can diff
    current state against the captured state.

    The captured records are stored unfiltered; check-time selectors
    apply equally to baseline and current snapshots so the diff is
    over the same logical slice.
    """

    captured_at: float
    resources: Dict[str, List[Dict[str, Any]]]

    @classmethod
    async def capture(cls, reader, resource_keys: List[str]) -> "BaselineSnapshot":
        snap: Dict[str, List[Dict[str, Any]]] = {}
        for key in resource_keys:
            fetcher = RESOURCE_FETCHERS.get(key)
            if fetcher is None:
                raise ValueError(
                    f"unknown resource {key!r}; "
                    f"valid: {sorted(RESOURCE_FETCHERS)}"
                )
            snap[key] = await fetcher(reader, {})
        return cls(captured_at=time.time(), resources=snap)


# ─────────────────────────── selector matcher ─────────────────────────

def _matches(record: Dict[str, Any], selector: Dict[str, Any]) -> bool:
    """Case-insensitive equality match on every key in selector."""
    for k, v in selector.items():
        actual = record.get(k)
        if isinstance(actual, str) and isinstance(v, str):
            if actual.lower() != v.lower():
                return False
        else:
            if actual != v:
                return False
    return True


def _filter_records(records: List[Dict[str, Any]],
                    selector: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not selector:
        return list(records)
    return [r for r in records if _matches(r, selector)]


# ─────────────────────── baseline-side filters ────────────────────────
#
# Resource fetchers handle selector pushdown to the live reader using
# resource-specific semantics (e.g. start_iso/end_iso are window bounds
# for calendar.events, not exact-match). The baseline-side filter in
# `_check_identity` needs to mirror those semantics — otherwise an
# identity check scoped by date window will succeed on the live side
# (Swift returns overlapping events) and fail on the baseline side
# (`_matches` requires exact equality), producing spurious diffs.
#
# Each registered filter takes `(records, selector)` and returns the
# filtered list using the resource's pushdown semantics. Resources
# whose live fetchers do plain exact-match (reminders.lists,
# contacts.all, etc.) don't need a registered filter — `_check_identity`
# falls back to `_filter_records`.


def _normalize_event_iso_for_compare(iso: str, *, is_end: bool) -> str:
    """Normalize a date-only ISO to a timed ISO for lexical comparison
    against windowed bounds. iOS returns all-day events as
    "YYYY-MM-DD" (date-only) — and `"2026-05-22"` is LEXICALLY LESS
    than `"2026-05-22T00:00:00"`, so a naïve lex compare drops all-day
    events from any windowed query. Pad to the appropriate boundary:
      • date-only start_iso  → "YYYY-MM-DDT00:00:00"
      • date-only end_iso    → "YYYY-MM-DDT23:59:59"
    Already-timed strings pass through unchanged."""
    if "T" in iso:
        return iso
    if not iso:
        return iso
    return iso + ("T23:59:59" if is_end else "T00:00:00")


def _filter_calendar_events_baseline(records: List[Dict[str, Any]],
                                      selector: Dict[str, Any]
                                      ) -> List[Dict[str, Any]]:
    """Apply `_fetch_calendar_events` semantics on a baseline list:
    start_iso/end_iso are WINDOW bounds (records overlap the window),
    writable_only is a no-op (baseline already excludes read-only
    calendars at capture time), other keys are exact-match via
    `_filter_records`.

    All-day events round-trip as date-only ISO ("YYYY-MM-DD"); these
    must be padded to T00:00:00 / T23:59:59 before lexical compare
    or windowed filtering silently drops them (Tier 2/3 critic 1
    flagged this 2026-05-21 as latent — breaks Tier 4 all-day corpora).
    """
    inner = dict(selector)
    window_start = inner.pop("start_iso", None)
    window_end   = inner.pop("end_iso", None)
    inner.pop("writable_only", None)
    # master_only is a server-side dedup hint, not a baseline filter.
    # If the baseline was captured with master_only=True, it already
    # has one row per series. If captured with master_only=False, the
    # baseline filter still treats all rows equally — the selector
    # consumer is responsible for matching capture/query consistency.
    inner.pop("master_only", None)
    out: List[Dict[str, Any]] = []
    for r in records:
        # Window-overlap semantics: event's [start, end] intersects
        # [window_start, window_end]. Normalize date-only forms first.
        r_end = _normalize_event_iso_for_compare(
            r.get("end_iso", "") or "", is_end=True)
        r_start = _normalize_event_iso_for_compare(
            r.get("start_iso", "") or "", is_end=False)
        if window_start is not None and r_end < window_start:
            continue
        if window_end is not None and r_start > window_end:
            continue
        out.append(r)
    return _filter_records(out, inner)


_BASELINE_FILTERS = {
    "calendar.events": _filter_calendar_events_baseline,
}


def _filter_baseline(resource: str,
                      records: List[Dict[str, Any]],
                      selector: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Resource-aware baseline filter. Routes to a registered
    resource-specific filter if one exists; otherwise falls back to
    plain exact-match via `_filter_records`."""
    fn = _BASELINE_FILTERS.get(resource)
    if fn is not None:
        return fn(records, selector)
    return _filter_records(records, selector)


# ─────────────────────────── resource fetchers ────────────────────────

async def _fetch_reminders_lists(reader, selector: Dict[str, Any]
                                  ) -> List[Dict[str, Any]]:
    resp = await reader._send({"type": "list_lists"})
    if not resp.get("ok"):
        raise ResourceFetchError(
            f"list_lists failed: {resp.get('error')}")
    return _filter_records(resp.get("lists", []), selector)


async def _fetch_reminders_items(reader, selector: Dict[str, Any]
                                  ) -> List[Dict[str, Any]]:
    cmd: Dict[str, Any] = {"type": "list_reminders",
                            "include_completed": True}
    # Push list filter down to the socket (Swift handles it
    # case-insensitively already). Other selector fields filter
    # client-side after fetch.
    if "list" in selector:
        cmd["list"] = selector["list"]
    resp = await reader._send(cmd)
    if not resp.get("ok"):
        raise ResourceFetchError(
            f"list_reminders failed: {resp.get('error')}")
    return _filter_records(resp.get("reminders", []), selector)


async def _fetch_calendar_events(reader, selector: Dict[str, Any]
                                  ) -> List[Dict[str, Any]]:
    cmd: Dict[str, Any] = {"type": "list_events"}
    # `start_iso` / `end_iso` are WINDOW BOUNDS, not exact-match
    # selector fields — they tell the server which time range to
    # return. Pop them before the client-side `_filter_records`
    # sees them, or it'd reject every event whose start/end isn't
    # exactly equal to the window bound.
    inner = dict(selector)
    for window_key in ("start_iso", "end_iso"):
        if window_key in inner:
            cmd[window_key] = inner.pop(window_key)
    if "calendar" in inner:
        cmd["calendar"] = inner["calendar"]
    # writable_only pushdown — default ON in Swift (excludes US Holidays
    # and other read-only subscribed calendars; see IOS_SIM_QUIRKS §16).
    # Selector can set writable_only=False to include read-only calendars.
    if "writable_only" in inner:
        cmd["writable_only"] = inner.pop("writable_only")
    # master_only pushdown — default ON in Swift (one row per recurring
    # series, NOT N expanded occurrences; see IOS_SIM_QUIRKS §16 and
    # probe Q6.2). Selector can set master_only=False to see every
    # expanded occurrence (rare; mostly useful for debugging/probes).
    if "master_only" in inner:
        cmd["master_only"] = inner.pop("master_only")
    resp = await reader._send(cmd)
    if not resp.get("ok"):
        raise ResourceFetchError(
            f"list_events failed: {resp.get('error')}")
    return _filter_records(resp.get("events", []), inner)


async def _fetch_calendar_calendars(reader, selector: Dict[str, Any]
                                     ) -> List[Dict[str, Any]]:
    """List writable user-event calendars (EKCalendar of type .event,
    allowsContentModifications=true). Excludes subscribed calendars
    like US Holidays / Birthdays / Siri Suggestions automatically.
    The default `"Calendar"` calendar is always present on a fresh
    sim AND survives `wipe_calendars`; user-created ones come and go."""
    resp = await reader._send({"type": "list_calendars"})
    if not resp.get("ok"):
        raise ResourceFetchError(
            f"list_calendars failed: {resp.get('error')}")
    return _filter_records(resp.get("calendars", []), selector)


async def _fetch_contacts_all(reader, selector: Dict[str, Any]
                                ) -> List[Dict[str, Any]]:
    cmd: Dict[str, Any] = {"type": "list_contacts"}
    # name_filter is pushed down to the socket — Swift handles it
    # case-insensitively (matches against given, family, or
    # "given family" concatenation). Other selector fields filter
    # client-side after fetch.
    if "name_filter" in selector:
        cmd["name_filter"] = selector["name_filter"]
    resp = await reader._send(cmd)
    if not resp.get("ok"):
        raise ResourceFetchError(
            f"list_contacts failed: {resp.get('error')}")
    # Strip the name_filter from client-side filtering — it's the
    # server-side push-down equivalent, not an exact-match selector.
    inner = {k: v for k, v in selector.items() if k != "name_filter"}
    return _filter_records(resp.get("contacts", []), inner)


async def _fetch_settings_defaults(reader, selector: Dict[str, Any]
                                    ) -> List[Dict[str, Any]]:
    """Read a single (domain, key) via host-side `simctl spawn defaults
    read`. Returns one row if the key exists, [] otherwise.

    Selector must specify `domain` and `key`. Whole-domain reads are
    intentionally not supported: `defaults read <domain>` returns a
    plist-like format that's painful to parse robustly, and most
    verifier checks target a single key. If a future task needs
    multi-key reads, add a `keys: List[str]` selector and loop.
    """
    domain = selector.get("domain")
    key = selector.get("key")
    if not domain or not key:
        raise ResourceFetchError(
            "settings.defaults requires selector with both "
            "`domain` and `key`")
    udid = getattr(reader, "udid", None)
    if not udid:
        raise ResourceFetchError(
            "settings.defaults fetcher needs a reader with a .udid "
            "attribute (host-side simctl, not socket)")
    # Lazy import to avoid a sibb_verify → sibb_state cycle at module
    # load (state imports verify for RESOURCE_FETCHERS).
    from sibb_state import _simctl_defaults_read
    value = await _simctl_defaults_read(udid, domain, key)
    if not value:
        return []
    # _filter_records will compare the row's `value` field to
    # whatever the selector says — but the selector has `domain`/`key`
    # only (those are pushdown fields), so strip them before filtering.
    inner = {k: v for k, v in selector.items()
              if k not in ("domain", "key")}
    return _filter_records(
        [{"domain": domain, "key": key, "value": value}],
        inner,
    )


async def _fetch_maps_user_defaults(reader, selector: Dict[str, Any]
                                      ) -> List[Dict[str, Any]]:
    """Read the labeled user-default keys from com.apple.Maps.plist.

    Returns a single-row list with one dict containing every Maps
    user-preference field SIBB cares about — driving / cycling /
    walking avoid keys, transit disabled-modes bitfield (decoded into
    a `transit_disabled_modes` list AND surfaced raw as
    `transit_disabled_modes_bits`), transit fare display, and sort.

    See `sibb_maps_reader.read_maps_user_defaults` for the field list.

    Phase A check kinds (driving/cycling/walking/transit prefs) use
    this fetcher with `attribute_eq` against a single field, OR with
    `attribute_set_equals` against `transit_disabled_modes` for
    set-valued comparison.

    The selector accepts the same key-equality fields as other
    fetchers — if you want to assert "no key matches this filter"
    use `absent`; if you want to assert one key has a value, use
    `attribute_eq` with `attr=<field>` and `value=<expected>`.
    """
    udid = getattr(reader, "udid", None)
    if not udid:
        raise ResourceFetchError(
            "maps.user_defaults fetcher needs a reader with a .udid "
            "attribute (host-side plist read)")
    from sibb_maps_reader import read_maps_user_defaults
    defaults = read_maps_user_defaults(udid)
    if not defaults:
        # No plist or unreadable — return empty so `exists` checks
        # correctly report "no row" rather than passing on a None.
        return []
    return _filter_records([defaults], selector)


async def _fetch_maps_active_route(reader, selector: Dict[str, Any]
                                     ) -> List[Dict[str, Any]]:
    """Read the user's currently-active navigation route — UNIFIED
    plist + rstorage source.

    Returns one row (or empty) with both plist-level fields
    (destination, origin, arrive_by_iso, created_iso, response_uuid)
    AND labeled rstorage-level fields (mode, distance_m,
    expected_time_s, avoids_highways, avoids_tolls, notices,
    violations) — sourced from the chosen alternative the user
    activated via Maps' UI.

    Selectors (all optional):
      min_mtime_epoch: float — epoch-seconds floor for rstorage +
        GraphDirections file scan. Pass baseline.captured_at to
        scope to the current episode (defends against stale state).
      max_age_seconds: float — ceiling for rstorage scan; default
        1800s (30 min).
      require_activated: bool — if true, only return rows where
        `is_activated=True` (i.e. GraphDirections file confirms the
        user actually tapped Go, not just previewed). Default false
        so debugging can still see preview-only state.
      (any other selector key is matched against the row dict by the
       generic `_filter_records` filter.)

    Returned dict carries `disambiguation_reason` for observability:
      single | mtime_winner_with_graphdirs | preview_only |
      no_matching_rstorage | no_response_uuid

    Returns [] when no active route present in plist.

    Critics addressed:
      - cfprefsd freshness: the caller should poll (mirrors maps.history's
        retry pattern). Future improvement: prefer `defaults read`
        via simctl over direct file read.
      - schema drift: UUIDv1 regex fallback in _extract_response_uuid
        handles tag-position changes in iOS 27+.
      - stale state: min_mtime_epoch selector scopes per-episode.
      - mode enum drift: unknown _transportType → "unknown" string.
      - never raises — None/[] on absent/broken state.
    """
    udid = getattr(reader, "udid", None)
    if not udid:
        raise ResourceFetchError(
            "maps.active_route fetcher needs a reader with a .udid "
            "attribute (host-side plist read)")
    from sibb_maps_reader import read_active_route_full

    min_mtime = selector.get("min_mtime_epoch")
    max_age = selector.get("max_age_seconds", 1800.0)
    require_activated = bool(selector.get("require_activated", False))
    if min_mtime is not None and not isinstance(min_mtime, (int, float)):
        raise ResourceFetchError(
            "maps.active_route.min_mtime_epoch must be numeric")
    if not isinstance(max_age, (int, float)):
        raise ResourceFetchError(
            "maps.active_route.max_age_seconds must be numeric")

    route = read_active_route_full(
        udid, min_mtime=min_mtime, max_age_seconds=float(max_age))
    if not route:
        return []
    if require_activated and not route.get("is_activated"):
        return []
    # Strip our internal selector keys before record-filtering.
    inner = {k: v for k, v in selector.items()
              if k not in ("min_mtime_epoch", "max_age_seconds",
                            "require_activated")}
    return _filter_records([route], inner)


async def _fetch_files_all(reader, selector: Dict[str, Any]
                            ) -> List[Dict[str, Any]]:
    cmd: Dict[str, Any] = {"type": "list_files"}
    # `directory` is the Swift-side scope (where to start walking),
    # not a selector field — pull it off the selector before
    # `_filter_records` sees it. recursive defaults to True.
    inner = dict(selector)
    if "directory" in inner:
        cmd["directory"] = inner.pop("directory")
    if "recursive" in inner:
        cmd["recursive"] = inner.pop("recursive")
    resp = await reader._send(cmd)
    if not resp.get("ok"):
        raise ResourceFetchError(
            f"list_files failed: {resp.get('error')}")
    return _filter_records(resp.get("files", []), inner)


async def _fetch_photos_assets(reader, selector: Dict[str, Any]
                                ) -> List[Dict[str, Any]]:
    """List every PHAsset in the sim photo library, optionally
    filtered by `media_type` (image|video|audio) via socket pushdown.

    No pushdown for individual asset attributes (identifier,
    creationDate, etc.) — those go through client-side
    `_filter_records`. Pushing media_type matters because a sim with
    a video corpus + thousands of images would otherwise pay the
    full enumeration cost.
    """
    cmd: Dict[str, Any] = {"type": "list_photos"}
    inner = dict(selector)
    # `list_photos` doesn't currently support a media_type filter
    # pushdown — keep the API surface tight on the Swift side. Filter
    # client-side via _filter_records on `media_type`.
    resp = await reader._send(cmd)
    if not resp.get("ok"):
        raise ResourceFetchError(
            f"list_photos failed: {resp.get('error')}")
    return _filter_records(resp.get("photos", []), inner)


async def _fetch_health_samples(reader, selector: Dict[str, Any]
                                  ) -> List[Dict[str, Any]]:
    """Fetch HealthKit samples, optionally narrowed by sample_type
    and a start/end ISO window. Window pushdown is socket-side
    (HealthKit's NSPredicate is faster than client-side filtering
    over potentially thousands of samples).
    """
    cmd: Dict[str, Any] = {"type": "list_health_samples"}
    inner = dict(selector)
    for pushdown_key in ("sample_type", "start_iso", "end_iso"):
        if pushdown_key in inner:
            cmd[pushdown_key] = inner.pop(pushdown_key)
    # When sample_type pushdown was used we may still want to filter
    # client-side on it (the row carries sample_type) for the same-key
    # repeated-selector case. Keep it in `inner` too if the caller put
    # it there originally — pop above already removed it, so we'd lose
    # client-side filtering. Re-add on the inner dict only if needed.
    resp = await reader._send(cmd)
    if not resp.get("ok"):
        raise ResourceFetchError(
            f"list_health_samples failed: {resp.get('error')}")
    return _filter_records(resp.get("samples", []), inner)


async def _fetch_fitness_activity_summary(
    reader, selector: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Read Activity-ring data (Move/Exercise/Stand) from the
    healthdb on the host filesystem.

    The Fitness app renders one row per day on its Summary tab,
    pulling from the `activity_caches` table. Each row carries the
    Move ring (`energy_burned` / `energy_burned_goal`), the Exercise
    ring (`brisk_minutes` / `brisk_minutes_goal`) and the Stand
    ring (`active_hours` / `active_hours_goal`), plus the day's
    `steps`. Exercise / Stand columns are NULL on iPhone-only sims
    (those rings need Apple Watch input).

    Selector keys:
        date     ISO date (YYYY-MM-DD) — narrows to rows where the
                 `start_iso` falls on this date
        latest   if truthy, return only the most recent matching row
        Any other key passes through to `_filter_records` for
        exact-match filtering on the row dict.

    Verification idioms:
        # Did the Move ring close today (≥100%)?
        rows = fetch; rows[0]["energy_burned"] >= rows[0]["energy_burned_goal"]
        # How many steps yesterday?
        select date=YESTERDAY; row["steps"]
    """
    udid = getattr(reader, "udid", None)
    if not udid:
        raise ResourceFetchError(
            "fitness.activity_summary fetcher needs a reader with a "
            ".udid attribute (host-side sqlite, not socket)")
    from sibb_state import _fitness_activity_summary
    rows = _fitness_activity_summary(udid)
    date_filter = selector.get("date")
    if date_filter is not None:
        if not isinstance(date_filter, str):
            raise ResourceFetchError(
                "fitness.activity_summary.date must be a YYYY-MM-DD "
                f"string; got {type(date_filter).__name__}")
        rows = [r for r in rows
                 if (r.get("start_iso") or "").startswith(date_filter)]
    if selector.get("latest"):
        rows = rows[:1]
    inner = {k: v for k, v in selector.items()
              if k not in {"date", "latest"}}
    return _filter_records(rows, inner)


_MAPS_HISTORY_KNOWN_SELECTORS = frozenset({
    "query_contains", "location_contains",
    "z_ent", "z_ent_in", "include_directions",
    "lat_near", "lon_near", "radius_deg",
    "latest", "limit",
    "min_create_iso",
    # Row-level exact-match keys also accepted via _filter_records:
    "query", "location_display", "type", "latitude", "longitude",
    "muid", "create_iso", "modification_iso",
})


async def _fetch_maps_history(reader, selector: Dict[str, Any]
                                ) -> List[Dict[str, Any]]:
    """Read Maps' ZHISTORYITEM table — the agent's search / route
    history. Verifier oracle for "did the agent navigate to X?".

    Z_ENT (Core Data entity id) is the row-class discriminator:
      - 20 = HistoryPlaceItem  — agent tapped a search result, OR
             tapped Directions on a place card. Populates lat/lon/muid.
      - 22 = HistorySearchItem — agent committed a `?q=…` openurl
             without picking a result. Populates query string only.
      - 16 = HistoryDirectionsItem — agent committed a route (GO).

    Selector pushdowns (client-side, applied after the host-side read):
        query_contains    substring match on ZQUERY (lowercased)
        location_contains substring match on ZLOCATIONDISPLAY
                          (iOS region-dependent; prefer query_contains
                          or lat/lon for stable matching)
        z_ent             exact match on Z_ENT (single int)
        z_ent_in          list[int]; row matches if Z_ENT is in the set
                          (defaults to 20 + 22 = "search-flavored" if
                          neither z_ent nor z_ent_in given AND the
                          selector key `include_directions` is falsy)
        include_directions if truthy, default scope is widened to include
                          Z_ENT=16 (HistoryDirectionsItem)
        lat_near          float; centerpoint latitude for radius match
        lon_near          float; centerpoint longitude for radius match
        radius_deg        float; tolerance for lat_near/lon_near
                          (default 0.005° ≈ 500m). Both lat_near and
                          lon_near required to activate.
        min_create_iso    str; ISO timestamp. Rows with create_iso
                          earlier than this are dropped. Use with the
                          BaselineSnapshot.capture timestamp to scope
                          the verifier to ONLY rows the agent created
                          this episode (vs. residual rows from prior
                          runs — ZHISTORYITEM grows monotonically).
        latest            if truthy, return only the most recent row
        limit             int; cap on rows read from the DB (default
                          1000, the same default as `_maps_history`)

    Unknown selector keys raise ResourceFetchError to catch typos
    (e.g. `latnear` instead of `lat_near` previously failed silently
    by passing through `_filter_records` as an exact-match on a
    non-existent column → always empty).
    """
    unknown = set(selector.keys()) - _MAPS_HISTORY_KNOWN_SELECTORS
    if unknown:
        raise ResourceFetchError(
            f"maps.history: unknown selector key(s) {sorted(unknown)!r}. "
            f"Known keys: {sorted(_MAPS_HISTORY_KNOWN_SELECTORS)!r}")
    udid = getattr(reader, "udid", None)
    if not udid:
        raise ResourceFetchError(
            "maps.history fetcher needs a reader with a .udid "
            "attribute (host-side sqlite, not socket)")
    from sibb_state import _maps_history, MAPS_Z_ENT_SEARCH_FLAVORED
    limit = selector.get("limit", 1000)
    if not isinstance(limit, int):
        raise ResourceFetchError(
            "maps.history.limit must be an int")

    def _read_and_filter() -> List[Dict[str, Any]]:
        """Single-shot read + filter — extracted so the async-write
        retry below can call it repeatedly."""
        return _filter_maps_history(
            _maps_history(udid, limit=limit), selector,
            MAPS_Z_ENT_SEARCH_FLAVORED)

    rows = _read_and_filter()

    # Async-write retry. iOS Maps writes ZHISTORYITEM asynchronously
    # after the user commits an action (place tap, route start) —
    # empirically a 2-3s delay between the UI action and the row
    # appearing on disk. The verifier runs immediately on episode
    # end; on a fast-completing task the row may not be flushed yet,
    # producing a false-negative.
    #
    # Mitigation: when the check has `min_create_iso` (signaling
    # "did something new happen since baseline?") AND we got 0
    # matching rows, poll up to 5s at 500ms intervals before giving
    # up. Bounded so legitimate-failures still fail fast-ish.
    #
    # Gated on `min_create_iso` so static-state checks (e.g. "does
    # the agent have search history from before the episode?") don't
    # pay the latency penalty.
    if not rows and "min_create_iso" in selector:
        import time as _time
        import asyncio as _asyncio
        # 10s budget (was 5s). Variant D 2026-05-28 trial showed iOS
        # Maps can delay the ZHISTORYITEM write 10+ seconds after the
        # UI action when other Maps subsystems are busy (route
        # rendering, tile cache). 5s wasn't enough; the verifier
        # gave up before pk=29 appeared on disk. 10s gives Maps a
        # generous flush window.
        deadline = _time.monotonic() + 10.0
        while _time.monotonic() < deadline:
            await _asyncio.sleep(0.5)
            rows = _read_and_filter()
            if rows:
                break
    return rows


def _filter_maps_history(rows: List[Dict[str, Any]],
                          selector: Dict[str, Any],
                          search_flavored: set) -> List[Dict[str, Any]]:
    """Apply the maps.history selector to a list of rows. Extracted
    from `_fetch_maps_history` so the async-write retry loop can
    re-filter the freshly-read rows on each retry.

    No I/O — pure dispatch over the rows from `_maps_history`."""
    # Z_ENT scoping: explicit z_ent / z_ent_in wins; otherwise default
    # to search-flavored unless include_directions is truthy.
    z_ent = selector.get("z_ent")
    z_ent_in = selector.get("z_ent_in")
    if z_ent is not None:
        if not isinstance(z_ent, int):
            raise ResourceFetchError(
                "maps.history.z_ent must be an int")
        rows = [r for r in rows if r.get("z_ent") == z_ent]
    elif z_ent_in is not None:
        if not isinstance(z_ent_in, list) or not all(
                isinstance(x, int) for x in z_ent_in):
            raise ResourceFetchError(
                "maps.history.z_ent_in must be a list[int]")
        ent_set = set(z_ent_in)
        rows = [r for r in rows if r.get("z_ent") in ent_set]
    elif not selector.get("include_directions"):
        rows = [r for r in rows
                 if r.get("z_ent") in search_flavored]

    qc = selector.get("query_contains")
    if qc is not None:
        if not isinstance(qc, str):
            raise ResourceFetchError(
                "maps.history.query_contains must be a string")
        ql = qc.lower()
        rows = [r for r in rows if ql in (r.get("query") or "").lower()]
    loc = selector.get("location_contains")
    if loc is not None:
        if not isinstance(loc, str):
            raise ResourceFetchError(
                "maps.history.location_contains must be a string")
        ll = loc.lower()
        rows = [r for r in rows
                 if ll in (r.get("location_display") or "").lower()]
    lat_near = selector.get("lat_near")
    lon_near = selector.get("lon_near")
    if lat_near is not None and lon_near is not None:
        if not isinstance(lat_near, (int, float)) or not isinstance(
                lon_near, (int, float)):
            raise ResourceFetchError(
                "maps.history.lat_near/lon_near must be numeric")
        radius = selector.get("radius_deg", 0.005)
        if not isinstance(radius, (int, float)):
            raise ResourceFetchError(
                "maps.history.radius_deg must be numeric")
        rows = [r for r in rows
                 if r.get("latitude") is not None
                 and r.get("longitude") is not None
                 and abs(r["latitude"] - lat_near) <= radius
                 and abs(r["longitude"] - lon_near) <= radius]
    min_create_iso = selector.get("min_create_iso")
    if min_create_iso is not None:
        if not isinstance(min_create_iso, str):
            raise ResourceFetchError(
                "maps.history.min_create_iso must be an ISO timestamp "
                "string (e.g. '2026-05-26T20:30:00Z')")
        rows = [r for r in rows
                 if (r.get("create_iso") or "") >= min_create_iso]
    if selector.get("latest"):
        rows = rows[:1]
    inner = {k: v for k, v in selector.items()
              if k not in {"query_contains", "location_contains",
                            "latest", "limit", "z_ent", "z_ent_in",
                            "include_directions", "lat_near", "lon_near",
                            "radius_deg", "min_create_iso"}}
    return _filter_records(rows, inner)


async def _fetch_passwords_entry_count(reader, selector: Dict[str, Any]
                                         ) -> List[Dict[str, Any]]:
    """Query keychain `inet` table for password entry counts by
    access group. Returns rows like
    `{"access_group": "com.apple.password-manager", "count": 3}`.

    The Passwords-app-visible entries land under access group
    `com.apple.password-manager`. The selector can pass
    `access_group=<grp>` to narrow; default returns all known
    password-related groups.

    Doesn't decrypt the password data — counts only. Verification
    pattern: take a baseline count before the agent acts, take
    another after, compare delta.
    """
    udid = getattr(reader, "udid", None)
    if not udid:
        raise ResourceFetchError(
            "passwords.entry_count fetcher needs a reader with a "
            ".udid attribute (host-side sqlite, not socket)")
    from sibb_state import _passwords_entry_count, _keychain_db_path
    target_groups = [
        "com.apple.password-manager",
        "com.apple.password-manager.password-evaluations",
        "com.apple.cfnetwork",
    ]
    if selector.get("access_group"):
        target_groups = [selector["access_group"]]
    rows = []
    for g in target_groups:
        rows.append({
            "access_group": g,
            "count": _passwords_entry_count(udid, g),
        })
    inner = {k: v for k, v in selector.items()
              if k != "access_group"}
    return _filter_records(rows, inner)


async def _fetch_passwords_entry_exists(reader, selector: Dict[str, Any]
                                          ) -> List[Dict[str, Any]]:
    """Check if a keychain row exists matching `service` + `account`.

    Returns a single row like
    `{"service": "...", "account": "...", "access_group": "...", "exists": True/False}`.
    The lookup uses SHA-1 hash equality against the `acct`/`srvr`
    columns (which store SHA-1 of the plaintext as a lookup index;
    see IOS_SIM_QUIRKS.md §13). No decryption — the encrypted `data`
    BLOB is untouched. tomb=0 excludes soft-deleted rows.

    Required selector keys: `service`, `account`.
    Optional: `access_group` (default `com.apple.password-manager`,
    the Passwords-app-visible group).
    """
    udid = getattr(reader, "udid", None)
    if not udid:
        raise ResourceFetchError(
            "passwords.entry_exists fetcher needs a reader with a "
            ".udid attribute (host-side sqlite, not socket)")
    svc = selector.get("service")
    acc = selector.get("account")
    if not isinstance(svc, str) or not isinstance(acc, str):
        raise ResourceFetchError(
            "passwords.entry_exists selector requires string "
            "`service` and `account` keys")
    agrp = selector.get("access_group", "com.apple.password-manager")
    from sibb_state import _passwords_entry_exists
    exists = _passwords_entry_exists(udid, svc, acc, agrp)
    return [{
        "service": svc,
        "account": acc,
        "access_group": agrp,
        "exists": exists,
    }]


async def _fetch_shortcuts_installed(
    reader, selector: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """List user-installed shortcuts visible on the Library tab.

    Drives the Shortcuts UI: terminates Shortcuts (clean state),
    attaches a fresh AXReader, reads the Library AX tree, parses
    user-shortcut cells. Each row is `{"name": str, "action_count":
    int}`. Apple's app-grouped suggestion cells (Scan Document,
    Recents, Places, …) are skipped — they lack the `, N action[s]`
    suffix that user shortcuts have.

    Selector keys: `name`, `action_count` (exact match via
    _filter_records). Any other selector key passes through.

    ⚠ Slow (~3-5s) — launches Shortcuts, waits for the Library to
    render, then disconnects. Cache the result at the verifier
    level if a single check needs to compare two states.
    """
    udid = getattr(reader, "udid", None)
    if not udid:
        raise ResourceFetchError(
            "shortcuts.installed fetcher needs a reader with a "
            ".udid attribute")

    from sibb_scaffold import AXReader  # local: keeps test imports light
    from sibb_state import _parse_shortcuts_library_tree

    # Terminate Shortcuts first so we land on a fresh Library tab,
    # not whatever screen the agent last navigated to (edit view,
    # Gallery, an open automation, etc.).
    proc = await asyncio.create_subprocess_exec(
        "xcrun", "simctl", "terminate", udid, "com.apple.shortcuts",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    try:
        await asyncio.wait_for(proc.communicate(), timeout=5.0)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
    await asyncio.sleep(0.5)

    sr = AXReader(udid)
    await sr.start(bundle_id="com.apple.shortcuts")
    try:
        await asyncio.sleep(1.5)  # let Library render
        tree = await sr.read()
    finally:
        await sr.stop()

    rows = _parse_shortcuts_library_tree(tree.elements)
    return _filter_records(rows, selector)


async def _fetch_mock_site_submissions(
    reader, selector: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Read POST submissions from a running `MockSite` fixture.

    The mock site is the source of truth for **password-value**
    verification (the keychain encrypts the password column; we
    can only hash-match the username/server). When the agent
    autofills + submits a login form, the password arrives at the
    fixture's server in plaintext, and the submission is recorded
    alongside whether it matched the configured credentials.

    Selector keys (all optional):
        site_id   which fixture to query (default "default")
        username  filter to submissions for this username
        password  filter to submissions for this password
        success   filter to success=True / False
        mode      "signin" | "signup"
        latest    if truthy, return only the most recent matching row

    Returned rows have shape:
        {"mode": "signin"|"signup",
         "username": str,
         "password": str,
         "success": bool,
         "timestamp": float}

    Verification idioms:
        # Agent's autofill carried the right password through to
        # the form post:
        check: count(success=True, username=X, password=P) >= 1
        # Agent typed but never authenticated:
        check: count(success=True) == 0
    """
    from sibb_mock_site import get_site
    site_id = selector.get("site_id", "default")
    site = get_site(site_id)
    if site is None:
        raise ResourceFetchError(
            f"mock_site.submissions: no mock site registered as "
            f"{site_id!r}. Call MockSite(site_id=...).start() in the "
            f"episode setup before invoking the verifier.")

    rows = site.submissions()
    if selector.get("latest"):
        rows = rows[-1:] if rows else []
    inner = {k: v for k, v in selector.items()
              if k not in {"site_id", "latest"}}
    return _filter_records(rows, inner)


async def _fetch_mock_site_visited(
    reader, selector: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Read GET visits to a running `MockSite` fixture. The keystone
    for reverse-direction generators (Calendar.url → Safari,
    Reminders.url → Safari, Messages link → Safari, etc.) — whenever
    Safari is the DESTINATION rather than the source, the harness
    records the GET and this fetcher exposes it for verification.

    Selector keys (all optional):
        site_id          which fixture to query (default "default")
        path             filter to visits whose path == this value
        path_contains    filter to visits whose path contains this
                         substring (use for permalink-like URLs)
        query_contains   filter to visits whose query contains this
                         substring
        min_epoch        filter to visits with epoch ≥ this (use to
                         restrict to "after the episode started")
        latest           if truthy, return only the most recent
                         matching row

    Returned rows have shape:
        {"path": str, "query": str, "epoch": float, "user_agent": str}

    Verification idioms:
        # Agent navigated to /events/42 after baseline_epoch:
        check: exists(path="/events/42", min_epoch="$baseline_epoch")
        # Agent didn't open the link at all:
        check: count(path="/events/42") == 0
    """
    from sibb_mock_site import get_site
    site_id = selector.get("site_id", "default")
    site = get_site(site_id)
    if site is None:
        raise ResourceFetchError(
            f"mock_site.visited: no mock site registered as "
            f"{site_id!r}. Call MockSite(site_id=...).start() in the "
            f"episode setup before invoking the verifier.")

    rows = site.visits()
    path_eq = selector.get("path")
    path_sub = selector.get("path_contains")
    query_sub = selector.get("query_contains")
    min_epoch = selector.get("min_epoch")
    if path_eq is not None:
        rows = [r for r in rows if r.get("path") == path_eq]
    if path_sub is not None:
        rows = [r for r in rows if path_sub in (r.get("path") or "")]
    if query_sub is not None:
        rows = [r for r in rows if query_sub in (r.get("query") or "")]
    if min_epoch is not None:
        try:
            min_e = float(min_epoch)
            rows = [r for r in rows
                    if (r.get("epoch") or 0.0) >= min_e]
        except (TypeError, ValueError):
            pass
    if selector.get("latest"):
        rows = rows[-1:] if rows else []
    inner = {k: v for k, v in selector.items()
              if k not in {"site_id", "path", "path_contains",
                           "query_contains", "min_epoch", "latest"}}
    return _filter_records(rows, inner)


async def _fetch_news_recipes(reader, selector: Dict[str, Any]
                                ) -> List[Dict[str, Any]]:
    """Scrape the Recipe Catalog from News.app.

    Path to reach: Search tab → Food category → first cell (Recipe
    Catalog landing). Recipes appear as `Other` elements with labels
    of the form:
      - `"<source>, RECIPE, <duration>, <title>"` (with prep time)
      - `"<source>, RECIPE, <title>"` (no duration listed)

    Returns rows of `{"source": str, "duration": str, "title": str}`.
    `duration` is empty when not provided by the source.

    This fetcher does NOT navigate to the Recipe Catalog — caller
    must drive UI navigation first (Search → Food → Recipe Catalog
    landing). Otherwise the AX tree it observes won't have recipe
    rows.

    Verified 2026-05-16: works against fresh baseline clone. Real
    Simple / Epicurious / Simply Recipes / etc. recipes appear with
    full structured metadata.
    """
    cmd = {"type": "observe", "bundleId": "com.apple.news"}
    resp = await reader._send(cmd)
    if not resp.get("ok"):
        raise ResourceFetchError(
            f"news.recipes: observe(com.apple.news) failed: "
            f"{resp.get('error')}")
    rows: List[Dict[str, Any]] = []
    for el in resp.get("elements", []):
        role = el.get("role") or ""
        label = el.get("label") or ""
        if role != "Other" or ", RECIPE, " not in label:
            continue
        # Parse "<source>, RECIPE, [<duration>, ]<title>".
        # Split on ", RECIPE, " first to isolate the trailing
        # "<duration>, <title>" or "<title>" segment.
        before, _, after = label.partition(", RECIPE, ")
        source = before.strip()
        # The after part may have duration prefix (e.g. "35m, ...",
        # "3h 5m, ...") or just the title. Heuristic: a duration
        # is short (<= 10 chars) and matches m/h pattern.
        parts = after.split(", ", 1)
        if (len(parts) == 2 and len(parts[0]) <= 10
                and any(unit in parts[0].lower()
                         for unit in ("m", "h", "min", "hr"))):
            duration = parts[0].strip()
            title = parts[1].strip()
        else:
            duration = ""
            title = after.strip()
        if title:
            rows.append({"source": source,
                          "duration": duration,
                          "title": title})
    return _filter_records(rows, selector)


async def _fetch_news_headlines(reader, selector: Dict[str, Any]
                                 ) -> List[Dict[str, Any]]:
    """Scrape Today-feed headlines from News.app's AX tree.

    News on iOS sim renders headlines in `Other` AX elements
    whose label has the form `"<source>, <title>"`. The Today-feed
    ScrollArea wraps them. This fetcher launches the News app,
    waits for the feed to settle, and returns rows of
    `{"source": str, "title": str}` parsed from the matching
    labels.

    The reader must be an XCUITestReader (or similar) with a
    `_send` method that routes through the socket. This fetcher
    does its own attach/observe; the caller's reader bundle
    doesn't need to be News beforehand.

    Limitations (documented in sibb_state.py NewsHandler):
    - Article DETAIL view often fails ("Cannot Connect"); this
      fetcher reads headlines only.
    - The set of visible headlines depends on what News has
      cached at baseline build time + during clone-boot.
    """
    # `observe` with explicit bundleId switches the runner's target
    # without restarting the AXReader. We rely on the runner being
    # already attached + News being launchable.
    cmd = {"type": "observe", "bundleId": "com.apple.news"}
    resp = await reader._send(cmd)
    if not resp.get("ok"):
        raise ResourceFetchError(
            f"news.headlines: observe(com.apple.news) failed: "
            f"{resp.get('error')}")
    rows: List[Dict[str, Any]] = []
    for el in resp.get("elements", []):
        # Look for `Other` role elements whose label looks like
        # "<source>, <title>" — that's how News encodes article
        # cells. The role string comes from sibb_scaffold's
        # ElementRole.value mapping.
        role = el.get("role") or ""
        label = el.get("label") or ""
        if role == "Other" and ", " in label:
            # Skip non-article Other elements (scroll bars, etc.)
            # by requiring a substring that suggests an article
            # title — i.e. multiple words after the comma.
            parts = label.split(", ", 1)
            if len(parts) != 2:
                continue
            source, title = parts[0].strip(), parts[1].strip()
            # Filter out scrollbar / system labels. Real News
            # source attributions don't contain "scroll bar"; iOS's
            # scroll-bar AX elements have labels like "Vertical
            # scroll bar, 4 pages" which round-trips to
            # source="Vertical scroll bar" / title="4 pages".
            combined = (source + " " + title).lower()
            if "scroll bar" in combined:
                continue
            if not title:
                continue
            rows.append({"source": source, "title": title})
    return _filter_records(rows, selector)


async def _fetch_safari_bookmarks(reader, selector: Dict[str, Any]
                                    ) -> List[Dict[str, Any]]:
    """Read bookmarks from Safari's Bookmarks.db. The reader must
    have a .udid attribute (host-side SQLite access, not socket).

    Selector keys understood by the fetcher *before* generic filtering:
      - `folder` (str): restrict to bookmarks under a folder with this
        title (case-insensitive). Walks the path, so "Favorites" matches
        BookmarksBar leaves AND BookmarksBar/Sub leaves.
      - `include_subfolders` (bool, default True): if False, only
        BookmarksBar root leaves are returned.
      - `include_reading_list` (bool, default False): include Reading
        List entries. Reading-List rows carry `kind="reading_list"`;
        regular bookmarks carry `kind="bookmark"`.
    All other selector keys are passed to the generic `_filter_records`
    record matcher (e.g. `url`, `title`, `kind`).
    """
    udid = getattr(reader, "udid", None)
    if not udid:
        raise ResourceFetchError(
            "safari.bookmarks fetcher needs a reader with a .udid "
            "attribute (host-side sqlite, not socket)")
    sel = dict(selector or {})
    folder = sel.pop("folder", None)
    include_subfolders = bool(sel.pop("include_subfolders", True))
    include_reading_list = bool(sel.pop("include_reading_list", False))
    canonicalize = bool(sel.pop("url_canonicalize", False))
    from sibb_state import _safari_list_bookmarks
    rows = await _safari_list_bookmarks(
        udid,
        parent_filter=folder,
        include_subfolders=include_subfolders,
        include_reading_list=include_reading_list,
    )
    if canonicalize:
        # Safari may rewrite URLs on bookmark create (drop trailing
        # slash on root, lowercase host, scheme upgrade). When the
        # generator asks for canonicalization, match on the canonical
        # form. The selector's `url` (if any) was already canonicalized
        # by the generator on the way in.
        for r in rows:
            r["url"] = _canonicalize_url(r.get("url"))
        if "url" in sel:
            sel["url"] = _canonicalize_url(sel["url"])
    return _filter_records(rows, sel)


# ─────────────────────── agent.answer pseudo-resource ─────────────────
#
# Reads the agent's parsed ANSWER payload from the verifier context
# instead of from the live sim. The dispatcher passes `context={"agent_answer": payload}`
# to run_check / run_checks, which forwards it here. Empty list ⇒ the
# agent emitted DONE/FAIL instead of ANSWER (or the JSON failed to
# parse, in which case sibb_scaffold.parse_action sets answer_payload
# to None and parse_error explains why). The `agent_answer` check kind
# converts that empty list into a structured failure with
# `failure_kind=no_answer`.
#
# `agent.answer` is the first non-state resource — the pattern
# generalizes to future `agent.tool_calls`, `agent.transcript`, etc.
# Selector is ignored (the payload is whole).

async def _fetch_agent_answer(reader, selector: Dict[str, Any], *,
                               context: Optional[Dict[str, Any]] = None
                               ) -> List[Dict[str, Any]]:
    if not context:
        return []
    payload = context.get("agent_answer")
    if payload is None:
        return []
    return [payload]


RESOURCE_FETCHERS: Dict[
    str,
    Callable[..., Awaitable[List[Dict[str, Any]]]],
] = {
    "reminders.lists":   _fetch_reminders_lists,
    "reminders.items":   _fetch_reminders_items,
    "calendar.events":   _fetch_calendar_events,
    "calendar.calendars": _fetch_calendar_calendars,
    "contacts.all":      _fetch_contacts_all,
    "files.all":         _fetch_files_all,
    "settings.defaults": _fetch_settings_defaults,
    "photos.assets":     _fetch_photos_assets,
    "health.samples":            _fetch_health_samples,
    "fitness.activity_summary":  _fetch_fitness_activity_summary,
    "maps.history":      _fetch_maps_history,
    "maps.user_defaults":  _fetch_maps_user_defaults,
    "maps.active_route":   _fetch_maps_active_route,
    "safari.bookmarks":  _fetch_safari_bookmarks,
    "news.headlines":    _fetch_news_headlines,
    "news.recipes":      _fetch_news_recipes,
    "passwords.entry_count":  _fetch_passwords_entry_count,
    "passwords.entry_exists": _fetch_passwords_entry_exists,
    "shortcuts.installed":    _fetch_shortcuts_installed,
    "mock_site.submissions":  _fetch_mock_site_submissions,
    "mock_site.visited":      _fetch_mock_site_visited,
    "agent.answer":           _fetch_agent_answer,
}


# ─────────────────────────── check kind handlers ──────────────────────
#
# Each handler takes (filtered_records, check_dict) and returns
# (status, evidence). Status is one of "pass" | "fail" | "error".
# Handlers raise no exceptions for failing checks — only for
# malformed check params (caught as "error" status by the dispatcher).

def _check_exists(records: List[Dict[str, Any]], check: Dict[str, Any]
                  ) -> Tuple[str, Dict[str, Any]]:
    return (
        ("pass", {"count": len(records)}) if records
        else ("fail", {"count": 0, "selector": check.get("selector", {})})
    )


def _check_absent(records: List[Dict[str, Any]], check: Dict[str, Any]
                  ) -> Tuple[str, Dict[str, Any]]:
    if not records:
        return "pass", {"count": 0}
    return "fail", {
        "count": len(records),
        "found": [
            {k: r.get(k) for k in ("name", "title", "identifier") if k in r}
            for r in records[:3]
        ],
    }


def _check_count(records: List[Dict[str, Any]], check: Dict[str, Any]
                 ) -> Tuple[str, Dict[str, Any]]:
    op = check.get("op", "eq")
    n = check.get("n")
    if n is None or not isinstance(n, int):
        raise ValueError("count check requires integer `n`")
    actual = len(records)
    ok = {"eq": actual == n,
          "ge": actual >= n,
          "le": actual <= n}.get(op)
    if ok is None:
        raise ValueError(
            f"count check `op` must be eq|ge|le, got {op!r}")
    return ("pass" if ok else "fail",
            {"expected": f"{op} {n}", "actual": actual})


def _walk_attr(record: Dict[str, Any], attr: str) -> Any:
    """Resolve a dot-path attribute against a record. Used by
    `attribute_eq` / `attribute_exists` / `attribute_absent` so the
    check can target a sub-field of a dict-valued column (e.g.
    `recurrence.frequency` on the `reminders.items` resource).

    A missing intermediate dict OR a non-dict intermediate returns
    None; the caller decides whether that is a fail or a pass.
    """
    cur: Any = record
    for seg in attr.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(seg)
    return cur


def _check_attribute_eq(records: List[Dict[str, Any]],
                         check: Dict[str, Any]
                         ) -> Tuple[str, Dict[str, Any]]:
    attr = check.get("attr")
    if attr is None:
        raise ValueError("attribute_eq check requires `attr`")
    if "value" not in check:
        raise ValueError("attribute_eq check requires `value`")
    expected = check["value"]
    if not records:
        return "fail", {"error": "no records match selector"}
    mismatches: List[Dict[str, Any]] = []
    for r in records:
        actual = _walk_attr(r, attr) if "." in attr else r.get(attr)
        if actual != expected:
            mismatches.append(
                {"actual": actual,
                 "identity": r.get("identifier") or r.get("title")
                             or r.get("name")}
            )
    if mismatches:
        return "fail", {"attr": attr, "expected": expected,
                         "mismatches": mismatches[:3],
                         "checked_count": len(records)}
    return "pass", {"attr": attr, "expected": expected,
                     "checked_count": len(records)}


def _check_attribute_set_equals(records: List[Dict[str, Any]],
                                  check: Dict[str, Any]
                                  ) -> Tuple[str, Dict[str, Any]]:
    """Per-record check that `record[attr]` (a list) matches `expected`
    as a multiset. Supports `item_keys`, `time_keys`, `digits_only_keys`,
    `case_sensitive`, `trim_strings` (forwarded to `_cmp_collection`).

    Use for Contacts multi-value fields (phones, emails,
    postal_addresses, urls, dates) where:
      - the order doesn't matter (set, not sequence)
      - each item is a dict with a known schema (item_keys)
      - some field values need canonicalization (phone digits, time
        format) before comparison

    For ordered comparison use `_check_attribute_ordered_match` (not yet
    implemented; add when a generator needs it)."""
    attr = check.get("attr")
    if attr is None:
        raise ValueError("attribute_set_equals check requires `attr`")
    if "expected" not in check:
        raise ValueError("attribute_set_equals check requires `expected`")
    expected = check["expected"]
    if not isinstance(expected, list):
        raise ValueError("attribute_set_equals expected must be a list")
    if not records:
        return "fail", {"error": "no records match selector"}
    item_keys = check.get("item_keys")
    time_keys = check.get("time_keys")
    digits_only_keys = check.get("digits_only_keys")
    street_norm_keys = check.get("street_norm_keys")
    if time_keys and digits_only_keys:
        overlap = set(time_keys) & set(digits_only_keys)
        if overlap:
            raise ValueError(
                "attribute_set_equals: time_keys and digits_only_keys "
                f"must not overlap (both target {sorted(overlap)!r}); "
                "double-canonicalization would silently corrupt the value")
    # Same exclusivity contract for street_norm_keys vs time/digit:
    # the canonicalizers run sequentially and would corrupt the value
    # if applied to the same key in different orders.
    for other_name, other in (("time_keys", time_keys),
                                ("digits_only_keys", digits_only_keys)):
        if street_norm_keys and other:
            overlap = set(street_norm_keys) & set(other)
            if overlap:
                raise ValueError(
                    "attribute_set_equals: street_norm_keys and "
                    f"{other_name} must not overlap (both target "
                    f"{sorted(overlap)!r}); double-canonicalization "
                    "would silently corrupt the value")
    case_sensitive = bool(check.get("case_sensitive", True))
    trim_strings = bool(check.get("trim_strings", False))
    mismatches: List[Dict[str, Any]] = []
    for r in records:
        actual = _walk_attr(r, attr) if "." in attr else r.get(attr)
        if actual is None:
            actual = []
        if not isinstance(actual, list):
            mismatches.append({
                "identity": r.get("identifier") or r.get("title")
                            or r.get("given_name"),
                "failure": "attr_not_list",
                "got_type": type(actual).__name__,
            })
            continue
        status, detail = _cmp_collection(
            actual, expected, ordered=False,
            item_keys=item_keys,
            time_keys=time_keys,
            digits_only_keys=digits_only_keys,
            street_norm_keys=street_norm_keys,
            case_sensitive=case_sensitive,
            trim_strings=trim_strings)
        if status != "pass":
            mismatches.append({
                "identity": r.get("identifier") or r.get("title")
                            or r.get("given_name"),
                "actual": actual,
                "detail": detail,
            })
    if mismatches:
        return "fail", {"attr": attr, "expected": expected,
                         "mismatches": mismatches[:3],
                         "checked_count": len(records)}
    return "pass", {"attr": attr, "checked_count": len(records)}


def _check_attribute_set_contains(records: List[Dict[str, Any]],
                                    check: Dict[str, Any]
                                    ) -> Tuple[str, Dict[str, Any]]:
    """Per-record check that `record[attr]` (a list) contains AT LEAST
    one item matching each partial-dict in `expected`.

    "Partial-dict matching": only the keys present in the expected
    entry are checked; other keys in the actual item are ignored. So
    if expected = [{"label": "home", "city": "Cupertino"}], the
    actual postal_addresses list must contain at least one entry
    whose `label` is "home" AND whose `city` is "Cupertino" — the
    street, state, postal_code, country are not constrained.

    Use for: "agent added an address with the right label and city,
    but the exact street text from Maps might vary." Compare to
    `attribute_set_equals` (exact multiset match, no partial)."""
    attr = check.get("attr")
    if attr is None:
        raise ValueError("attribute_set_contains check requires `attr`")
    if "expected" not in check:
        raise ValueError("attribute_set_contains check requires `expected`")
    expected = check["expected"]
    if not isinstance(expected, list):
        raise ValueError(
            "attribute_set_contains expected must be a list of partial dicts")
    if not records:
        return "fail", {"error": "no records match selector"}
    case_sensitive = bool(check.get("case_sensitive", True))
    trim_strings = bool(check.get("trim_strings", False))
    digit_keys = set(check.get("digits_only_keys") or [])
    street_keys = set(check.get("street_norm_keys") or [])

    def _norm_for_key(k: str, v: Any) -> Any:
        if k in digit_keys and isinstance(v, str):
            return _canonicalize_phone_digits(v)
        if k in street_keys and isinstance(v, str):
            return _canonicalize_street(v)
        return _norm_scalar(v, case_sensitive=case_sensitive,
                              trim_strings=trim_strings)

    mismatches: List[Dict[str, Any]] = []
    for r in records:
        actual = _walk_attr(r, attr) if "." in attr else r.get(attr)
        if actual is None:
            actual = []
        if not isinstance(actual, list):
            mismatches.append({
                "identity": r.get("identifier") or r.get("title")
                            or r.get("given_name"),
                "failure": "attr_not_list",
                "got_type": type(actual).__name__,
            })
            continue
        for exp_partial in expected:
            if not isinstance(exp_partial, dict):
                raise ValueError(
                    "attribute_set_contains expected entries must be dicts")
            matched = False
            for item in actual:
                if not isinstance(item, dict):
                    continue
                if all(_norm_for_key(k, item.get(k))
                         == _norm_for_key(k, v)
                         for k, v in exp_partial.items()):
                    matched = True
                    break
            if not matched:
                mismatches.append({
                    "identity": r.get("identifier") or r.get("title")
                                or r.get("given_name"),
                    "missing_partial": exp_partial,
                    "actual_items": actual[:5],
                })
    if mismatches:
        return "fail", {"attr": attr, "expected": expected,
                         "mismatches": mismatches[:3],
                         "checked_count": len(records)}
    return "pass", {"attr": attr, "checked_count": len(records)}


def _check_attribute_list_length(records: List[Dict[str, Any]],
                                    check: Dict[str, Any]
                                    ) -> Tuple[str, Dict[str, Any]]:
    """Every filtered record's `record[attr]` (a list) must have length
    matching the operator (`op` + `n`).

    `op` defaults to "eq". Supported: "eq", "ne", "ge", "gt", "le", "lt".
    `n` is the integer comparand.

    Use alongside `attribute_set_contains` when the count of items
    matters too — e.g. "exactly ONE phone, and it's the body number"
    (catches leftover auto-prefilled phones from iOS Messages-shortcut
    create-contact flows)."""
    attr = check.get("attr")
    if attr is None:
        raise ValueError("attribute_list_length check requires `attr`")
    if "n" not in check:
        raise ValueError("attribute_list_length check requires `n`")
    n = check["n"]
    op = check.get("op", "eq")
    ops: Dict[str, Any] = {
        "eq": lambda a, b: a == b,
        "ne": lambda a, b: a != b,
        "ge": lambda a, b: a >= b,
        "gt": lambda a, b: a > b,
        "le": lambda a, b: a <= b,
        "lt": lambda a, b: a < b,
    }
    if op not in ops:
        raise ValueError(
            f"attribute_list_length: unsupported op {op!r}; "
            f"valid: {sorted(ops)}")
    if not records:
        return "fail", {"error": "no records match selector"}
    cmp = ops[op]
    mismatches: List[Dict[str, Any]] = []
    for r in records:
        val = _walk_attr(r, attr) if "." in attr else r.get(attr)
        if val is None:
            val = []
        if not isinstance(val, list):
            mismatches.append({
                "identity": r.get("identifier") or r.get("title")
                            or r.get("given_name"),
                "failure": "attr_not_list",
                "got_type": type(val).__name__,
            })
            continue
        actual_len = len(val)
        if not cmp(actual_len, n):
            mismatches.append({
                "identity": r.get("identifier") or r.get("title")
                            or r.get("given_name"),
                "expected_op": op, "expected_n": n,
                "actual_len": actual_len,
                "actual_items": val[:5],
            })
    if mismatches:
        return "fail", {"attr": attr, "op": op, "n": n,
                         "mismatches": mismatches[:3],
                         "checked_count": len(records)}
    return "pass", {"attr": attr, "op": op, "n": n,
                     "checked_count": len(records)}


def _check_attribute_exists(records: List[Dict[str, Any]],
                              check: Dict[str, Any]
                              ) -> Tuple[str, Dict[str, Any]]:
    """Every filtered record must have the attribute present (i.e.
    `record[attr]` is not None and, for dot-paths, every intermediate
    dict exists). Useful for "this reminder has SOME recurrence
    rule, shape doesn't matter" assertions where dict equality on
    the rule would be brittle to fetcher-shape drift."""
    attr = check.get("attr")
    if attr is None:
        raise ValueError("attribute_exists check requires `attr`")
    if not records:
        return "fail", {"error": "no records match selector"}
    missing: List[Dict[str, Any]] = []
    for r in records:
        val = _walk_attr(r, attr) if "." in attr else r.get(attr)
        if val is None:
            missing.append({
                "identity": r.get("identifier") or r.get("title")
                            or r.get("name"),
            })
    if missing:
        return "fail", {"attr": attr,
                         "missing_on": missing[:3],
                         "checked_count": len(records)}
    return "pass", {"attr": attr, "checked_count": len(records)}


def _check_attribute_absent(records: List[Dict[str, Any]],
                              check: Dict[str, Any]
                              ) -> Tuple[str, Dict[str, Any]]:
    """Mirror of `attribute_exists` — every filtered record must NOT
    have the attribute set. Used for "recurrence cleared" or
    "no notes" without resorting to `attribute_eq(value=None)` (which
    accidentally matches missing-key records too, mixing two
    different invariants)."""
    attr = check.get("attr")
    if attr is None:
        raise ValueError("attribute_absent check requires `attr`")
    if not records:
        # No records to check — vacuously satisfied. Callers wanting
        # "record must exist with attribute absent" should pair with
        # `exists`.
        return "pass", {"attr": attr, "checked_count": 0}
    present: List[Dict[str, Any]] = []
    for r in records:
        val = _walk_attr(r, attr) if "." in attr else r.get(attr)
        if val is not None:
            present.append({
                "identity": r.get("identifier") or r.get("title")
                            or r.get("name"),
                "value": val,
            })
    if present:
        return "fail", {"attr": attr,
                         "present_on": present[:3],
                         "checked_count": len(records)}
    return "pass", {"attr": attr, "checked_count": len(records)}


def _haversine_m(lat1: float, lon1: float,
                  lat2: float, lon2: float) -> float:
    """Great-circle distance in meters between two lat/lon points.
    Uses the haversine formula on a spherical Earth (R = 6_371_000 m).
    Accurate to ~0.5 % across the globe — well within the verifier's
    decision threshold (50 m for variant D / E)."""
    from math import radians, sin, cos, asin, sqrt
    R = 6_371_000.0
    p1, p2 = radians(lat1), radians(lat2)
    dp = radians(lat2 - lat1)
    dl = radians(lon2 - lon1)
    a = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 2 * R * asin(sqrt(a))


def _check_geo_within_m(records: List[Dict[str, Any]],
                         check: Dict[str, Any]
                         ) -> Tuple[str, Dict[str, Any]]:
    """Asserts the resource record's nested `destination.lat`/
    `destination.lon` is within `radius_m` meters of the expected
    coord. Used by variant D / E to check the agent navigated to
    the right address, not just *some* address.

    Required check keys: `lat`, `lon`, `radius_m`.
    The selector's resource fetch must return ONE record (or zero;
    we treat zero as fail with a clear evidence trail). The record
    must expose `destination` as a dict with float `lat` / `lon`.
    Both shapes are what `maps.active_route` produces in Phase A++.
    """
    expected_lat = check.get("lat")
    expected_lon = check.get("lon")
    radius_m = check.get("radius_m")
    if (expected_lat is None or expected_lon is None
            or radius_m is None):
        raise ValueError(
            "geo_within_m requires `lat`, `lon`, and `radius_m`")
    if not isinstance(expected_lat, (int, float)) \
            or not isinstance(expected_lon, (int, float)) \
            or not isinstance(radius_m, (int, float)):
        raise ValueError(
            "geo_within_m `lat`/`lon`/`radius_m` must be numeric")
    if not records:
        return "fail", {
            "error": "no record matched selector — nothing to "
                     "compare against",
            "selector": check.get("selector", {}),
            "expected_lat": expected_lat,
            "expected_lon": expected_lon,
            "radius_m": radius_m,
        }
    if len(records) > 1:
        return "fail", {
            "error": (f"geo_within_m expects exactly one record, "
                       f"got {len(records)} — refine the selector"),
        }
    record = records[0]
    dest = record.get("destination") or {}
    lat = dest.get("lat")
    lon = dest.get("lon")
    if not isinstance(lat, (int, float)) \
            or not isinstance(lon, (int, float)):
        return "fail", {
            "error": ("record has no usable destination.lat / "
                       "destination.lon"),
            "destination": dest,
        }
    distance_m = _haversine_m(float(lat), float(lon),
                                float(expected_lat),
                                float(expected_lon))
    evidence = {
        "expected": {"lat": float(expected_lat),
                       "lon": float(expected_lon)},
        "actual": {"lat": float(lat), "lon": float(lon)},
        "distance_m": round(distance_m, 2),
        "radius_m": float(radius_m),
    }
    return ("pass" if distance_m <= float(radius_m) else "fail",
            evidence)


def _check_subset(records: List[Dict[str, Any]], check: Dict[str, Any]
                  ) -> Tuple[str, Dict[str, Any]]:
    expected = check.get("expected")
    key = check.get("key")
    if expected is None or key is None:
        raise ValueError("subset check requires `expected` and `key`")
    expected_set = {str(v).lower() for v in expected}
    actual_set = {str(r.get(key)).lower() for r in records}
    missing = sorted(expected_set - actual_set)
    return ("pass" if not missing else "fail",
            {"missing": missing,
             "found_count": len(actual_set & expected_set),
             "expected_count": len(expected_set)})


def _identifier_set(records: List[Dict[str, Any]]) -> Tuple[List[str], bool]:
    """Extract sorted identifier list, plus flag indicating completeness."""
    ids: List[str] = []
    complete = True
    for r in records:
        ident = r.get("identifier")
        if ident is None:
            complete = False
            continue
        ids.append(str(ident))
    ids.sort()
    return ids, complete


_MISSING = object()


def _hashable_field_value(v: Any) -> Any:
    """Convert list/dict values to a hashable form for tuple-set
    comparison. JSON-serialize with sort_keys=True for dicts AND
    sort list items by their canonical-string form so set-equal lists
    always serialize identically.

    The list-sorting is critical for multi-value Contacts fields
    (phones, emails, postal_addresses, urls, dates). CN does NOT
    guarantee stable CNLabeledValue ordering across update_contact
    writes — the agent legitimately editing a sibling could reorder
    the array without changing the set. Without item-sorting,
    `_signature_set` would flag the reordered distractor as MUTATED.

    Sets are folded to sorted lists (their hash-order repr is unstable
    across PYTHONHASHSEED).

    The Calendar implementation explicitly excluded list/dict fields
    from `_CAL_DISTRACTOR_FIELDS` (recurrence handled via per-distractor
    attribute_absent sentinels). For Contacts the multi-value fields
    ARE core to identity, so we serialize instead of excluding."""
    if isinstance(v, set):
        try:
            v = sorted(v, key=lambda x: json.dumps(x, sort_keys=True,
                                                     default=str))
        except (TypeError, ValueError):
            return repr(v)
    if isinstance(v, list):
        try:
            sorted_items = sorted(
                v, key=lambda x: json.dumps(x, sort_keys=True, default=str))
            return json.dumps(sorted_items, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return repr(v)
    if isinstance(v, dict):
        try:
            return json.dumps(v, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return repr(v)
    return v


def _signature_set(records: List[Dict[str, Any]],
                    fields: List[str]) -> List[Tuple[Any, ...]]:
    """Build sorted list of (field1_value, field2_value, ...) tuples.
    Used for `identity` checks with `compare_fields` — catches edits that
    leave identifier intact but mutate fields (rename, time-shift,
    location set, all-day toggle). See `_check_identity` for the
    composition with `exclude_match`.

    Missing fields use a unique sentinel (not `None`) so a record
    lacking a `compare_fields` key compares unequal to one with that
    key set to `None`. Without this guard, fetchers that omit optional
    fields (vs returning them as None) would silently equate to fetchers
    that include them — masking real schema drift between the live
    Swift reader and the L1.5 fake.

    List/dict field values are JSON-serialized (via `_hashable_field_value`)
    so multi-value Contacts fields (phones, postal_addresses, ...) can
    participate in identity comparisons without crashing the set() that
    `_check_identity` builds downstream."""
    sigs: List[Tuple[Any, ...]] = []
    for r in records:
        sigs.append(tuple(
            _hashable_field_value(r[f]) if f in r else _MISSING
            for f in fields))
    sigs.sort(key=lambda t: tuple(str(v) for v in t))
    return sigs


def _check_identity(records: List[Dict[str, Any]], check: Dict[str, Any],
                     baseline: Optional[BaselineSnapshot] = None
                     ) -> Tuple[str, Dict[str, Any]]:
    """Identity check: assert (current ∩ scope) equals (baseline ∩ scope).

    Default: compare identifier sets — catches added/removed records but
    nothing about field edits.

    Optional `compare_fields` (List[str]): compare field-tuple sets
    instead. Catches per-record mutations even when identifiers are
    intact. Use this when the resource exposes mutable fields the agent
    might edit (Calendar events: title/start_iso/end_iso/all_day/
    location/notes; not Reminders, where Phase 2 used count(field=None)
    sentinels because most fields default to None).

    Optional `exclude_match` (Dict): post-filter that removes records
    matching every key-value pair from BOTH current and baseline sides
    before comparison. Use this to scope the "no irrelevant edits"
    guard to "every record EXCEPT the agent's target." For Calendar
    Tier 1: exclude_match={"title": target_title} drops the target so
    the check verifies the distractors are byte-equal to baseline.
    """
    if baseline is None:
        return "error", {
            "error": "identity check requires baseline; "
                     "pass `baseline=` to run_checks",
        }
    resource = check.get("resource")
    if resource is None or resource not in baseline.resources:
        return "error", {
            "error": f"baseline has no snapshot for resource {resource!r}",
            "available": sorted(baseline.resources),
        }
    selector = check.get("selector") or {}
    # Use resource-aware filter so windowed selectors (e.g.,
    # calendar.events with start_iso/end_iso as window bounds) match
    # the same records on baseline that the live fetcher returns.
    baseline_records = _filter_baseline(
        resource, baseline.resources[resource], selector)
    current_records = list(records)

    # Post-filter both sides by exclude_match before any comparison.
    # Uses STRICT (case-sensitive) equality, not _matches: the identity
    # check's contract is "every record except this one is byte-equal
    # to baseline." A case-insensitive exclude would let an agent rename
    # a target from "Lunch" to "LUNCH" and have the new record silently
    # dropped from the comparison.
    exclude_match = check.get("exclude_match")
    if exclude_match:
        def _strict_match(r: Dict[str, Any]) -> bool:
            return all(r.get(k) == v for k, v in exclude_match.items())
        current_records = [r for r in current_records
                            if not _strict_match(r)]
        baseline_records = [r for r in baseline_records
                             if not _strict_match(r)]

    compare_fields = check.get("compare_fields")
    if compare_fields:
        cur_sigs = _signature_set(current_records, compare_fields)
        base_sigs = _signature_set(baseline_records, compare_fields)
        if cur_sigs == base_sigs:
            return "pass", {
                "resource": resource, "method": "signatures",
                "compare_fields": list(compare_fields),
                "count": len(cur_sigs),
            }
        cur_set = set(cur_sigs)
        base_set = set(base_sigs)
        return "fail", {
            "resource": resource, "method": "signatures",
            "compare_fields": list(compare_fields),
            "added": [list(t) for t in sorted(cur_set - base_set,
                                              key=lambda x: tuple(str(v) for v in x))][:5],
            "removed": [list(t) for t in sorted(base_set - cur_set,
                                                key=lambda x: tuple(str(v) for v in x))][:5],
            "current_count": len(cur_sigs),
            "baseline_count": len(base_sigs),
        }

    current_ids, current_complete = _identifier_set(current_records)
    baseline_ids, baseline_complete = _identifier_set(baseline_records)

    if not (current_complete and baseline_complete):
        # Identifier-less resource — fall back to count-based identity
        # (best-effort). Document in evidence so callers know it's weak.
        if len(current_records) == len(baseline_records):
            return "pass", {
                "resource": resource, "method": "count-only",
                "count": len(current_records),
            }
        return "fail", {
            "resource": resource, "method": "count-only",
            "current_count": len(current_records),
            "baseline_count": len(baseline_records),
        }

    if current_ids == baseline_ids:
        return "pass", {
            "resource": resource, "method": "identifiers",
            "count": len(current_ids),
        }
    cur_set = set(current_ids)
    base_set = set(baseline_ids)
    return "fail", {
        "resource": resource, "method": "identifiers",
        "added": sorted(cur_set - base_set)[:5],
        "removed": sorted(base_set - cur_set)[:5],
        "current_count": len(current_ids),
        "baseline_count": len(baseline_ids),
    }


# ───────────────────────── agent_answer check kind ────────────────────
#
# Verifies the JSON object the agent emitted via the terminal ANSWER
# action. `records` for this kind is either `[payload]` (single-element
# list, payload is a dict) or `[]` (no answer).
#
# Strict-by-default design choices (see CLAUDE.md item B design notes):
#   • case_sensitive=True       — string comparisons are exact
#   • trim_strings=False        — whitespace matters
#   • extra keys in items fail  — when item_keys is declared, items
#                                  must contain EXACTLY those keys
#   • no numeric str coercion   — "5" != 5
#   • non-dict top-level payloads are rejected at parse time
#
# Path navigation is a dot-walker only: "$", "$.items", "$.user.name".
# No wildcards / filters — those go through `match=` instead. Keys
# containing literal dots aren't supportable today (re-evaluate when a
# real task needs them).

def _walk_path(payload: Any, path: str) -> Any:
    """Dot-walker. Raises KeyError on miss, TypeError on bad descent."""
    if path in ("", "$"):
        return payload
    p = path
    if p.startswith("$."):
        p = p[2:]
    elif p.startswith("$"):
        p = p[1:]
    if not p:
        return payload
    cur: Any = payload
    for part in p.split("."):
        if not isinstance(cur, dict):
            raise TypeError(
                f"cannot descend into {type(cur).__name__} at {part!r} "
                f"(path={path!r})")
        if part not in cur:
            raise KeyError(part)
        cur = cur[part]
    return cur


def _norm_scalar(v: Any, *, case_sensitive: bool, trim_strings: bool) -> Any:
    if isinstance(v, str):
        if trim_strings:
            v = v.strip()
        if not case_sensitive:
            v = v.lower()
    return v


def _canonicalize_url(u: Any) -> Any:
    """Normalize a URL for verifier comparison.

    Safari sometimes rewrites URLs on bookmark / share (lowercasing the
    host, dropping default ports, normalizing the trailing slash on
    bare-host URLs, upgrading http→https for HSTS sites). Without
    canonicalization, exact-string compare will mismatch on cosmetic
    differences. Returns the input unchanged if it doesn't look like a
    URL — string comparators downstream will fall back to plain compare.

    Idempotent: canonicalize twice → same result.
    """
    if not isinstance(u, str):
        return u
    s = u.strip()
    if not s:
        return u
    # Add a scheme if missing so urlparse can split cleanly.
    has_scheme = re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", s) is not None
    if not has_scheme:
        if "/" in s or "." in s:
            s_with = "http://" + s
        else:
            return u  # doesn't look like a URL
    else:
        s_with = s
    try:
        from urllib.parse import urlparse, urlunparse
        p = urlparse(s_with)
    except Exception:
        return u
    if not p.netloc:
        return u
    scheme = (p.scheme or "http").lower()
    host = p.hostname.lower() if p.hostname else ""
    if not host:
        return u
    port = ""
    default_ports = {"http": 80, "https": 443}
    if p.port and p.port != default_ports.get(scheme):
        port = f":{p.port}"
    # Trailing-slash on root paths is meaningful only for clarity —
    # collapse "/" to "" for the host-only case; otherwise keep.
    path = p.path
    if path == "/":
        path = ""
    netloc = host + port
    return urlunparse((scheme, netloc, path, p.params, p.query, p.fragment))


_TIME_PATTERNS = [
    # "2:00 PM", "12:30 AM", "2:00 P.M.", "2:00pm" (case-insensitive)
    (re.compile(r"^(\d{1,2})\s*[:.]?\s*(\d{2})?\s*([ap])\.?m\.?$"),
     "12h_with_minute"),
    # "2 PM"
    (re.compile(r"^(\d{1,2})\s*([ap])\.?m\.?$"), "12h_no_minute"),
    # "14:00", "14:00:00"
    (re.compile(r"^(\d{1,2}):(\d{2})(?::\d{2})?$"), "24h"),
]


def _canonicalize_time_string(s: str) -> str:
    """Normalize 12h/24h time strings to canonical 24-hour 'HH:MM'.

    Accepts "2:00 PM", "14:00", "2pm", "12:30 AM", "14:00:00", etc.
    Strips Unicode narrow no-break space (\\u202f) and regular
    non-breaking space (\\u00a0) that iOS Calendar emits in AX
    labels (per probe 2026-05-21 — `'10:00\\u202fAM'`).

    Returns the canonical form on match; returns the input unchanged
    on no match (downstream string compare will then fail strictly).
    Used by `_cmp_collection` for keys listed in `time_keys`."""
    if not isinstance(s, str):
        return s
    raw = s.replace(" ", " ").replace(" ", " ").strip()
    # Collapse internal whitespace runs.
    raw = " ".join(raw.split())
    raw_lc = raw.lower()
    for pattern, kind in _TIME_PATTERNS:
        m = pattern.match(raw_lc)
        if not m:
            continue
        if kind == "12h_with_minute":
            h = int(m.group(1))
            mm = int(m.group(2)) if m.group(2) else 0
            ampm = m.group(3)
            if ampm == "p" and h != 12:
                h += 12
            elif ampm == "a" and h == 12:
                h = 0
            return f"{h:02d}:{mm:02d}"
        if kind == "12h_no_minute":
            h = int(m.group(1))
            ampm = m.group(2)
            if ampm == "p" and h != 12:
                h += 12
            elif ampm == "a" and h == 12:
                h = 0
            return f"{h:02d}:00"
        if kind == "24h":
            h = int(m.group(1))
            mm = int(m.group(2))
            return f"{h:02d}:{mm:02d}"
    return s  # unrecognized — return as-is for strict-compare to fail


# US street-suffix abbreviations + directional + ordinal-word
# normalization. Goal: fuzzy-equal `"350 5th Avenue"` ≡ `"350 5th Ave"`
# ≡ `"350 Fifth Avenue"` ≡ `"350 fifth ave."` etc. iOS Contacts often
# saves what the user typed verbatim, but Maps reverse-geocoding will
# rewrite a hand-typed street to the standard format. Without this
# normalization, the verifier would false-fail on cosmetic differences.
#
# Map keys are the LONG form / variant; values are the SHORT canonical.
# Both forms canonicalize to the short form, so the verifier accepts
# either input shape.
_STREET_NORM_MAP: Dict[str, str] = {
    # Suffixes
    "street": "st",      "st.": "st",
    "avenue": "ave",     "ave.": "ave",     "av": "ave",
    "boulevard": "blvd", "blvd.": "blvd",
    "road": "rd",        "rd.": "rd",
    "drive": "dr",       "dr.": "dr",
    "parkway": "pkwy",   "pkwy.": "pkwy",
    "lane": "ln",        "ln.": "ln",
    "court": "ct",       "ct.": "ct",
    "place": "pl",       "pl.": "pl",
    "terrace": "ter",    "ter.": "ter",
    "highway": "hwy",    "hwy.": "hwy",
    "circle": "cir",     "cir.": "cir",
    "square": "sq",      "sq.": "sq",
    "trail": "trl",      "trl.": "trl",
    "expressway": "expy", "expy.": "expy",
    "way": "way",
    # Directionals
    "north": "n",   "n.": "n",
    "south": "s",   "s.": "s",
    "east": "e",    "e.": "e",
    "west": "w",    "w.": "w",
    "northeast": "ne", "northwest": "nw",
    "southeast": "se", "southwest": "sw",
    # Ordinal words common in NYC-style addresses ("Fifth Avenue"→"5th Ave")
    "first": "1st",   "second": "2nd",  "third": "3rd",
    "fourth": "4th",  "fifth": "5th",   "sixth": "6th",
    "seventh": "7th", "eighth": "8th",  "ninth": "9th",
    "tenth": "10th",  "eleventh": "11th", "twelfth": "12th",
}


def _canonicalize_street(s: Any) -> Any:
    """Normalize a US-style street string for fuzzy equality.
      - lowercase
      - strip punctuation (keeps digits, letters, hyphen, slash, space)
      - collapse whitespace
      - apply `_STREET_NORM_MAP` word-by-word (Avenue→Ave, Fifth→5th,
        North→N, etc.)
    Non-string inputs pass through unchanged.

    Used by `_cmp_collection` and `_check_attribute_set_contains` for
    keys listed in `street_norm_keys` — the postal-address counterpart
    of `digits_only_keys`."""
    if not isinstance(s, str):
        return s
    out = s.lower()
    # Strip punctuation that doesn't carry identity. Keep word chars,
    # hyphen, slash, whitespace. (Periods/commas/apostrophes go.)
    out = re.sub(r"[^\w\s\-/]", " ", out)
    tokens = out.split()
    return " ".join(_STREET_NORM_MAP.get(t, t) for t in tokens)


def _canonicalize_phone_digits(s: Any) -> Any:
    """Strip non-digits from a phone-shaped string. US country-code
    normalization: if the result is exactly 11 digits and starts with
    "1", drop the leading "1". This makes `+1-650-555-0001` /
    `(650) 555-0001` / `650-555-0001` all canonicalize to `6505550001`.

    Non-US numbers stay as-is (e.g. `+44 20 7946 0958` → `442079460958`).

    Extension handling: truncates at the first non-phone-punctuation
    character — letters, comma (iPhone pause), semicolon (wait), `x`.
    "650-555-1234 ext 999" → "6505551234" (extension discarded).
    "650-555-0001,,123" → "6505550001".

    Unicode digit folding: fullwidth `６`, Arabic-Indic `٦`, Devanagari
    `६`, etc. fold to ASCII digits, so an i18n agent rendering matches
    an ASCII seed.

    Used by `_cmp_collection` for keys listed in `digits_only_keys` —
    the phone-normalization counterpart of `time_keys`."""
    if not isinstance(s, str):
        return s
    # Truncate at first non-phone-punctuation char (letters, comma,
    # semicolon, `x` from "ext"). Allows ASCII digits, common Unicode
    # digit blocks, whitespace, and phone-shaped punctuation.
    head = re.split(
        r"[^0-9\s\-.()/+٠-٩۰-۹０-９]",
        s, 1)[0]
    digits: List[str] = []
    for c in head:
        if "0" <= c <= "9":
            digits.append(c)
        elif c.isdigit():
            try:
                digits.append(str(int(c)))
            except (TypeError, ValueError):
                pass
    out = "".join(digits)
    if len(out) == 11 and out.startswith("1"):
        out = out[1:]
    return out


class _ItemError(Exception):
    def __init__(self, failure_kind: str, detail: Dict[str, Any]):
        super().__init__(failure_kind)
        self.failure_kind = failure_kind
        self.detail = detail


def _norm_item(item: Any, *, item_keys: Optional[List[str]],
                case_sensitive: bool, trim_strings: bool,
                time_keys: Optional[List[str]] = None,
                digits_only_keys: Optional[List[str]] = None,
                street_norm_keys: Optional[List[str]] = None,
                ) -> Tuple:
    """Convert one item to a hashable tuple for multiset compare.

    Raises `_ItemError` if the item violates the strict-keys policy
    (extra key, missing required key, or wrong top-level type).

    `time_keys`: keys whose string values are treated as time-of-day
    and canonicalized to 24-hour HH:MM before tuple-comparison.

    `digits_only_keys`: keys whose string values are treated as
    phone numbers — non-digits stripped, US country-code prefix
    normalized — before tuple-comparison. Use for Contacts `phones`
    value fields where the iOS UI may render "+1 (650) 555-0001"
    but the seed JSON uses "650-555-0001" or vice versa.

    `street_norm_keys`: keys whose string values are treated as US
    street addresses — lowercased, punctuation-stripped, and
    suffix/directional/ordinal-word-normalized before comparison.
    Use for postal_address `street` value fields where iOS Contacts
    or Maps may rewrite "350 5th Avenue" as "350 5th Ave" etc.
    """
    if item_keys is None:
        return ("_scalar", _norm_scalar(item,
                                         case_sensitive=case_sensitive,
                                         trim_strings=trim_strings))
    if not isinstance(item, dict):
        raise _ItemError("type_mismatch", {
            "expected_type": "dict",
            "got": type(item).__name__,
            "item": item,
        })
    required = set(item_keys)
    actual = set(item.keys())
    if actual != required:
        extra = sorted(actual - required)
        missing = sorted(required - actual)
        kind = "extra_key" if extra and not missing else (
               "missing_required_key" if missing and not extra else
               "schema_violation")
        raise _ItemError(kind, {
            "expected_keys": sorted(required),
            "actual_keys": sorted(actual),
            "extra": extra,
            "missing": missing,
            "item": item,
        })
    time_key_set = set(time_keys or [])
    digit_key_set = set(digits_only_keys or [])
    street_key_set = set(street_norm_keys or [])
    out = []
    for k in sorted(item_keys):
        v = item[k]
        if k in time_key_set and isinstance(v, str):
            v = _canonicalize_time_string(v)
        if k in digit_key_set and isinstance(v, str):
            v = _canonicalize_phone_digits(v)
        if k in street_key_set and isinstance(v, str):
            v = _canonicalize_street(v)
        v = _norm_scalar(v,
                          case_sensitive=case_sensitive,
                          trim_strings=trim_strings)
        out.append((k, v))
    return tuple(out)


def _cmp_collection(value: Any, expected: Any, *,
                     ordered: bool,
                     item_keys: Optional[List[str]],
                     case_sensitive: bool, trim_strings: bool,
                     time_keys: Optional[List[str]] = None,
                     digits_only_keys: Optional[List[str]] = None,
                     street_norm_keys: Optional[List[str]] = None,
                     **_) -> Tuple[str, Dict[str, Any]]:
    if not isinstance(value, list):
        return "fail", {
            "failure_kind": "type_mismatch",
            "expected_type": "list",
            "got": type(value).__name__,
        }
    if not isinstance(expected, list):
        raise ValueError("collection match expected must be a list")
    try:
        norm_v = [_norm_item(it, item_keys=item_keys,
                              case_sensitive=case_sensitive,
                              trim_strings=trim_strings,
                              time_keys=time_keys,
                              digits_only_keys=digits_only_keys,
                              street_norm_keys=street_norm_keys)
                  for it in value]
        norm_e = [_norm_item(it, item_keys=item_keys,
                              case_sensitive=case_sensitive,
                              trim_strings=trim_strings,
                              time_keys=time_keys,
                              digits_only_keys=digits_only_keys,
                              street_norm_keys=street_norm_keys)
                  for it in expected]
    except _ItemError as ie:
        return "fail", {"failure_kind": ie.failure_kind, **ie.detail}
    if ordered:
        if norm_v == norm_e:
            return "pass", {"matched": len(norm_v)}
        return "fail", {
            "failure_kind": "value_mismatch",
            "expected": expected,
            "got": value,
            "ordered": True,
        }
    # Multiset compare via sorted tuples.
    if sorted(norm_v) == sorted(norm_e):
        return "pass", {"matched": len(norm_v)}
    cv = list(norm_v); ce = list(norm_e)
    extra: List[Any] = []
    for it in cv:
        if it in ce: ce.remove(it)
        else: extra.append(it)
    missing = ce
    return "fail", {
        "failure_kind": "value_mismatch",
        "expected_count": len(norm_e),
        "got_count": len(norm_v),
        "extra": extra[:5],
        "missing": missing[:5],
    }


def _cmp_number(value: Any, expected: Any, *,
                 tolerance: Optional[float],
                 **_) -> Tuple[str, Dict[str, Any]]:
    # bool is a subclass of int — exclude it; boolean has its own kind.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "fail", {
            "failure_kind": "type_mismatch",
            "expected_type": "number",
            "got": type(value).__name__,
            "value": value,
        }
    if not isinstance(expected, (int, float)) or isinstance(expected, bool):
        raise ValueError("number match expected must be int|float")
    if tolerance is None:
        ok = value == expected
    else:
        ok = abs(value - expected) <= tolerance
    return ("pass", {"expected": expected, "got": value,
                      "tolerance": tolerance}) if ok else (
            "fail", {"failure_kind": "value_mismatch",
                      "expected": expected, "got": value,
                      "tolerance": tolerance})


def _cmp_string(value: Any, expected: Any, *,
                 mode: str,
                 case_sensitive: bool, trim_strings: bool,
                 digits_only: bool = False,
                 **_) -> Tuple[str, Dict[str, Any]]:
    if not isinstance(value, str):
        return "fail", {
            "failure_kind": "type_mismatch",
            "expected_type": "string",
            "got": type(value).__name__,
            "value": value,
        }
    if not isinstance(expected, str):
        raise ValueError("string match expected must be a str")
    v = _norm_scalar(value, case_sensitive=case_sensitive,
                       trim_strings=trim_strings)
    e = _norm_scalar(expected, case_sensitive=case_sensitive,
                       trim_strings=trim_strings)
    if digits_only:
        # Phone-string compare path: strip non-digits + fold Unicode +
        # US-prefix-strip on both sides. Only meaningful for mode="eq".
        v = _canonicalize_phone_digits(v) if isinstance(v, str) else v
        e = _canonicalize_phone_digits(e) if isinstance(e, str) else e
    if mode == "eq":
        ok = v == e
    elif mode == "contains":
        ok = e in v
    elif mode == "regex":
        import re as _re
        try:
            flags = 0 if case_sensitive else _re.IGNORECASE
            ok = _re.search(expected, value, flags) is not None
        except _re.error as re_err:
            raise ValueError(f"string_regex pattern invalid: {re_err}")
    else:
        raise ValueError(f"unknown string match mode {mode!r}")
    return ("pass", {"expected": expected, "got": value, "mode": mode}
            ) if ok else (
            "fail", {"failure_kind": "value_mismatch",
                      "expected": expected, "got": value, "mode": mode})


def _cmp_boolean(value: Any, expected: Any, **_
                  ) -> Tuple[str, Dict[str, Any]]:
    if not isinstance(value, bool):
        return "fail", {
            "failure_kind": "type_mismatch",
            "expected_type": "bool",
            "got": type(value).__name__,
            "value": value,
        }
    if not isinstance(expected, bool):
        raise ValueError("boolean match expected must be a bool")
    return ("pass", {"expected": expected, "got": value}
            ) if value == expected else (
            "fail", {"failure_kind": "value_mismatch",
                      "expected": expected, "got": value})


# match-kind → (comparator, kwargs-bound-at-dispatch)
_AGENT_ANSWER_COMPARATORS: Dict[
    str, Callable[..., Tuple[str, Dict[str, Any]]]
] = {
    "set_equals":      lambda v, e, **kw: _cmp_collection(v, e, ordered=False, **kw),
    "ordered_match":   lambda v, e, **kw: _cmp_collection(v, e, ordered=True, **kw),
    "number_eq":       lambda v, e, **kw: _cmp_number(v, e, **{**kw, "tolerance": None}),
    "number_close":    _cmp_number,
    "string_eq":       lambda v, e, **kw: _cmp_string(v, e, mode="eq", **kw),
    "string_contains": lambda v, e, **kw: _cmp_string(v, e, mode="contains", **kw),
    "string_regex":    lambda v, e, **kw: _cmp_string(v, e, mode="regex", **kw),
    "boolean":         _cmp_boolean,
}


def _check_agent_answer(records: List[Dict[str, Any]],
                         check: Dict[str, Any],
                         *,
                         observed_bundles: Optional[List[str]] = None,
                         ) -> Tuple[str, Dict[str, Any]]:
    # Observation gate: if the check declares `observation_required`,
    # the episode runner must have logged at least one READ touching
    # each of those bundles. Refuses the ANSWER otherwise — kills
    # hardcoded-payload and pretraining-memorization cheats.
    required = check.get("observation_required") or []
    if required:
        if observed_bundles is None:
            return "fail", {
                "failure_kind": "observation_data_missing",
                "error": ("check declares observation_required but the "
                           "verifier got no observed_bundles in context; "
                           "the episode runner must accumulate READ "
                           "bundle ids and pass them through"),
                "observation_required": list(required),
            }
        observed = set(observed_bundles)
        unmet = [b for b in required if b not in observed]
        if unmet:
            return "fail", {
                "failure_kind": "no_evidence",
                "error": ("agent emitted ANSWER without observing the "
                           "required app(s) in this episode"),
                "observation_required": list(required),
                "observed_bundles": sorted(observed),
                "unmet": unmet,
            }
    if not records:
        return "fail", {
            "failure_kind": "no_answer",
            "error": ("agent did not emit ANSWER, or ANSWER JSON was "
                       "malformed (parse_error captured at action-parse "
                       "time, see scaffold.parse_action)"),
        }
    payload = records[0]
    if not isinstance(payload, dict):
        # Should never happen — parse_action rejects non-dict payloads.
        return "fail", {
            "failure_kind": "schema_violation",
            "error": f"payload must be a dict, got {type(payload).__name__}",
        }
    match_kind = check.get("match")
    if not match_kind:
        raise ValueError("agent_answer check requires `match`")
    if "expected" not in check:
        raise ValueError("agent_answer check requires `expected`")
    comparator = _AGENT_ANSWER_COMPARATORS.get(match_kind)
    if comparator is None:
        raise ValueError(
            f"agent_answer `match` must be one of "
            f"{sorted(_AGENT_ANSWER_COMPARATORS)}, got {match_kind!r}")
    path = check.get("path", "$")
    try:
        value = _walk_path(payload, path)
    except KeyError as e:
        return "fail", {
            "failure_kind": "path_miss",
            "path": path,
            "missing_key": str(e).strip("'\""),
        }
    except TypeError as e:
        return "fail", {
            "failure_kind": "path_miss",
            "path": path,
            "error": str(e),
        }
    return comparator(
        value, check["expected"],
        item_keys=check.get("item_keys"),
        time_keys=check.get("time_keys"),
        digits_only_keys=check.get("digits_only_keys"),
        digits_only=bool(check.get("digits_only", False)),
        case_sensitive=bool(check.get("case_sensitive", True)),
        trim_strings=bool(check.get("trim_strings", False)),
        tolerance=check.get("tolerance"),
    )


CHECK_KINDS: Dict[
    str,
    Callable[..., Tuple[str, Dict[str, Any]]],
] = {
    "exists":            _check_exists,
    "absent":            _check_absent,
    "count":             _check_count,
    "attribute_eq":      _check_attribute_eq,
    "attribute_set_equals": _check_attribute_set_equals,
    "attribute_set_contains": _check_attribute_set_contains,
    "attribute_list_length": _check_attribute_list_length,
    "attribute_exists":  _check_attribute_exists,
    "attribute_absent":  _check_attribute_absent,
    "subset":            _check_subset,
    "identity":          _check_identity,
    "agent_answer":      _check_agent_answer,
    "geo_within_m":      _check_geo_within_m,
}


# Check kinds that need access to the baseline. The dispatcher threads
# baseline into these handlers; other kinds get called with the
# 2-arg sync signature for back-compat with the A6 handler interface.
_BASELINE_AWARE_KINDS = {"identity"}


# ─────────────────────────── dispatcher ───────────────────────────────

_DEFAULT_SEVERITY = "blocking"


def _format_label(check: Dict[str, Any]) -> str:
    if "label" in check:
        return str(check["label"])
    parts = [check.get("kind", "?"), check.get("resource", "")]
    sel = check.get("selector")
    if sel:
        parts.append(repr(sel))
    return " ".join(p for p in parts if p)


def _expand_runtime_tokens(selector: Dict[str, Any],
                            baseline: Optional[BaselineSnapshot]
                            ) -> Dict[str, Any]:
    """Resolve generator-time sentinels to runtime values inside a
    check's `selector`. Returns a fresh dict; the input is untouched.

    Supported sentinels (string-valued selector entries only):
        "$baseline_iso"    →  baseline.captured_at formatted as ISO 8601
                              UTC ("2026-05-26T12:34:56Z"). String value.
        "$baseline_epoch"  →  baseline.captured_at as a float (Unix epoch
                              seconds). Used by selector keys that take
                              numeric mtime/timestamp arguments — e.g.
                              `maps.active_route.min_mtime_epoch`.
    Raises ResourceFetchError if baseline is None when either token
    appears.
    """
    if not selector:
        return selector
    out: Dict[str, Any] = {}
    for k, v in selector.items():
        if isinstance(v, str) and v == "$baseline_iso":
            if baseline is None:
                raise ResourceFetchError(
                    f"selector key {k!r} uses '$baseline_iso' but no "
                    f"baseline was captured for this check")
            import datetime as _dt
            iso = _dt.datetime.utcfromtimestamp(
                baseline.captured_at).isoformat(timespec="seconds") + "Z"
            out[k] = iso
        elif isinstance(v, str) and v == "$baseline_epoch":
            if baseline is None:
                raise ResourceFetchError(
                    f"selector key {k!r} uses '$baseline_epoch' but no "
                    f"baseline was captured for this check")
            out[k] = float(baseline.captured_at)
        else:
            out[k] = v
    return out


async def run_check(reader, check: Dict[str, Any],
                     baseline: Optional[BaselineSnapshot] = None,
                     context: Optional[Dict[str, Any]] = None,
                     ) -> CheckResult:
    """Run one check. `context` carries per-episode runtime data that
    isn't on the reader — today only `agent_answer` (the parsed ANSWER
    payload from the terminal action). Future entries: `agent_observations`
    (for the observation-gate check), `agent_transcript`, etc."""
    kind = check.get("kind", "")
    label = _format_label(check)
    severity = check.get("severity", _DEFAULT_SEVERITY)

    handler = CHECK_KINDS.get(kind)
    if handler is None:
        return CheckResult(
            kind=kind, label=label,
            status="error", severity=severity,
            evidence={"error": f"unknown check kind {kind!r}",
                       "valid": sorted(CHECK_KINDS)},
        )

    resource = check.get("resource")
    fetcher = RESOURCE_FETCHERS.get(resource) if resource else None
    if fetcher is None:
        return CheckResult(
            kind=kind, label=label,
            status="error", severity=severity,
            evidence={"error": f"unknown resource {resource!r}",
                       "valid": sorted(RESOURCE_FETCHERS)},
        )

    selector = check.get("selector") or {}
    # Substitute runtime sentinels in selector string values. Today:
    #   "$baseline_iso" → baseline.captured_at as ISO 8601 UTC string.
    # Generators emit "$baseline_iso" at construction time; we resolve
    # it here because the actual baseline.captured_at isn't known until
    # episode setup time. Used by tasks that need to scope a fetcher
    # to "rows the agent created this episode" (e.g. maps.history's
    # min_create_iso selector).
    selector = _expand_runtime_tokens(selector, baseline)
    # `agent.answer` is the only fetcher today that consumes `context`;
    # the rest read state from the live reader. Branch is explicit so we
    # don't have to extend every existing fetcher's signature.
    try:
        if resource == "agent.answer":
            records = await fetcher(reader, selector, context=context)
        else:
            records = await fetcher(reader, selector)
    except ResourceFetchError as e:
        return CheckResult(
            kind=kind, label=label,
            status="error", severity=severity,
            evidence={"error": str(e)},
        )

    try:
        if kind in _BASELINE_AWARE_KINDS:
            status, evidence = handler(records, check, baseline)
        elif kind == "agent_answer":
            status, evidence = handler(
                records, check,
                observed_bundles=(context or {}).get("observed_bundles"),
            )
        else:
            status, evidence = handler(records, check)
    except ValueError as e:
        return CheckResult(
            kind=kind, label=label,
            status="error", severity=severity,
            evidence={"error": str(e)},
        )

    return CheckResult(
        kind=kind, label=label,
        status=status, severity=severity, evidence=evidence,
    )


async def run_checks(reader,
                      checks: List[Dict[str, Any]],
                      baseline: Optional[BaselineSnapshot] = None,
                      context: Optional[Dict[str, Any]] = None,
                      ) -> List[CheckResult]:
    """Run every check sequentially against the live reader.

    `baseline` is forwarded to baseline-aware check kinds (identity).
    `context` is forwarded to context-aware kinds (agent_answer).
    Non-aware kinds ignore both; callers can omit either.
    """
    out: List[CheckResult] = []
    for check in checks:
        out.append(await run_check(reader, check,
                                    baseline=baseline,
                                    context=context))
    return out


# ─────────────────────────── aggregation ──────────────────────────────

def blocking_pass(results: List[CheckResult]) -> bool:
    """True iff every blocking check has status=='pass'."""
    return all(
        r.status == "pass"
        for r in results
        if r.severity == "blocking"
    )


def legacy_format(results: List[CheckResult]
                  ) -> List[Tuple[str, Optional[bool]]]:
    """Translate to the old `(label, bool | None)` tuple shape used
    by `verify_reminders_list_task_async`'s callers (replay/runner)."""
    out: List[Tuple[str, Optional[bool]]] = []
    for r in results:
        if r.severity == "informational":
            out.append((r.label, None))
        elif r.status == "pass":
            out.append((r.label, True))
        else:
            # fail or error → False
            out.append((r.label, False))
    return out


# ─────────────────────── generator-side helpers ───────────────────────
#
# Authoring an agent_answer check has three coupled pieces: the check
# dict (verifier-side), the human-readable answer-schema sentence the
# generator appends to the instruction (agent-facing), and the strict
# round-trip between them. These helpers keep all three in one call
# so an author can't change one and forget the others.
#
#   check, schema_text = make_answer_check(match="set_equals",
#                                            expected=[{"title": t} for t in xs],
#                                            path="$.items",
#                                            item_keys=["title"])
#   instruction = "List reminders due tomorrow. " + schema_text
#
# The lint at the bottom enforces that the instruction string literally
# contains the schema_text and the "ANSWER" token before the task is
# returned — a generator that forgets to splice the schema will fail
# loudly at construction time, not silently at episode time.

_VALID_MATCHES = {
    "set_equals", "ordered_match",
    "number_eq", "number_close",
    "string_eq", "string_contains", "string_regex",
    "boolean",
}


def make_answer_check(*,
                       match: str,
                       expected: Any,
                       path: str = "$.items",
                       item_keys: Optional[List[str]] = None,
                       time_keys: Optional[List[str]] = None,
                       digits_only_keys: Optional[List[str]] = None,
                       digits_only: bool = False,
                       case_sensitive: bool = True,
                       trim_strings: bool = False,
                       tolerance: Optional[float] = None,
                       severity: str = "blocking",
                       label: Optional[str] = None,
                       observation_required: Optional[List[str]] = None,
                       ) -> Tuple[Dict[str, Any], str]:
    """Build an `agent_answer` check dict and the matching answer-schema
    instruction sentence in one call.

    Returns `(check_dict, schema_sentence)`. The generator splices
    `schema_sentence` into its instruction text; `lint_answer_instruction`
    can then verify the two are aligned before the task is shipped.

    `observation_required` is a list of bundle ids the agent must have
    READ at least once before its ANSWER is accepted (the observation
    gate — enforced by the episode runner, not the verifier directly).
    """
    if match not in _VALID_MATCHES:
        raise ValueError(
            f"make_answer_check: match must be one of "
            f"{sorted(_VALID_MATCHES)}, got {match!r}")
    if match in ("number_close",) and tolerance is None:
        raise ValueError("number_close requires `tolerance`")
    if match in ("set_equals", "ordered_match"):
        if not isinstance(expected, list):
            raise ValueError(
                f"{match} expected must be a list, got {type(expected).__name__}")
    if match in ("set_equals", "ordered_match") and item_keys is not None:
        if not isinstance(item_keys, list) or not all(
                isinstance(k, str) for k in item_keys):
            raise ValueError("item_keys must be a list[str]")
    if match == "boolean" and not isinstance(expected, bool):
        raise ValueError("boolean expected must be a bool")

    check: Dict[str, Any] = {
        "kind": "agent_answer",
        "resource": "agent.answer",
        "match": match,
        "expected": expected,
        "path": path,
        "case_sensitive": case_sensitive,
        "trim_strings": trim_strings,
        "severity": severity,
    }
    if item_keys is not None:
        check["item_keys"] = item_keys
    if time_keys is not None:
        if not isinstance(time_keys, list) or not all(
                isinstance(k, str) for k in time_keys):
            raise ValueError("time_keys must be a list[str]")
        check["time_keys"] = time_keys
    if digits_only_keys is not None:
        if not isinstance(digits_only_keys, list) or not all(
                isinstance(k, str) for k in digits_only_keys):
            raise ValueError("digits_only_keys must be a list[str]")
        if time_keys is not None:
            overlap = set(time_keys) & set(digits_only_keys)
            if overlap:
                raise ValueError(
                    "time_keys and digits_only_keys must not overlap "
                    f"(both target {sorted(overlap)!r}); double-canonicalization "
                    "would silently corrupt the value")
        check["digits_only_keys"] = digits_only_keys
    if digits_only:
        if match != "string_eq":
            raise ValueError(
                f"digits_only is only valid for match='string_eq'; "
                f"got match={match!r}")
        check["digits_only"] = True
    if tolerance is not None:
        check["tolerance"] = tolerance
    if observation_required:
        check["observation_required"] = list(observation_required)
    if label:
        check["label"] = label

    schema_sentence = describe_answer_format(check)
    return check, schema_sentence


def describe_answer_format(check: Dict[str, Any]) -> str:
    """Deterministic answer-schema instruction string derived from the
    check. This is the canonical phrasing the agent sees — generators
    splice it into the per-task instruction text.

    Design notes (post 2026-05-20 LLM-parseability critic pass):
      • Examples use valid JSON (double quotes), not Python-style repr.
      • Each check kind gets a concrete worked example, not abstract
        type placeholders — LLMs follow examples 2-3× better than
        type-spec rules.
      • Sensitivity / strictness hints are computed FROM the check's
        actual flags, so the prompt never contradicts the verifier.
      • Wording stays deterministic so the lint can substring-match.
    """
    if check.get("kind") != "agent_answer":
        raise ValueError("describe_answer_format requires kind=agent_answer")
    match = check.get("match")
    path = check.get("path", "$.items")
    # Strip the dollar-prefix to get the leaf key for the example.
    leaf = path.lstrip("$").lstrip(".")
    if not leaf:
        leaf = "value"
    item_keys = check.get("item_keys")

    if match in ("set_equals", "ordered_match"):
        # Build a valid-JSON exemplar item using the declared item_keys.
        if item_keys:
            example_item = "{" + ", ".join(
                f'"{k}": "..."' for k in item_keys) + "}"
            keys_hint = (
                f'each item must be exactly {example_item}'
            )
            keys_rule = (
                f"Use ONLY these keys, nothing else: "
                f"{', '.join(repr(k) for k in item_keys)}. "
                f"DO NOT add extra keys like 'duration' or 'calendar' "
                f"— adding any field not listed above causes the "
                f"answer to fail."
            )
        else:
            example_item = '"..."'
            keys_hint = "each item is a value"
            keys_rule = ""
        order_note = (" The order of items matters."
                       if match == "ordered_match" else
                       " The order of items does NOT matter.")
        # When time_keys is set, mention the format leniency so an
        # agent reading 12-hour iOS UI doesn't try to be too clever.
        time_keys = check.get("time_keys")
        time_hint = ""
        if time_keys:
            time_hint = (
                f" Time fields ({', '.join(repr(k) for k in time_keys)}) "
                f"accept either 12-hour ('2:00 PM') or 24-hour "
                f"('14:00') format — emit whichever you observe.")
        example = (f'ANSWER {{"{leaf}": [{example_item}, {example_item}]}}')
        return (f'Output your final answer as: '
                f'ANSWER {{"{leaf}": [...]}} '
                f'(JSON object with double quotes, single line). '
                f'{keys_hint}.{order_note} '
                f'{keys_rule}{time_hint} '
                f'Example: {example}.').replace("  ", " ").strip()
    if match in ("number_eq", "number_close"):
        return (f'Output your final answer as: '
                f'ANSWER {{"{leaf}": <number>}} '
                f'where <number> is a JSON number (NOT a string in '
                f'quotes). Example: ANSWER {{"{leaf}": 3}}.')
    if match == "boolean":
        return (f'Output your final answer as either '
                f'ANSWER {{"{leaf}": true}} or '
                f'ANSWER {{"{leaf}": false}} '
                f'(lowercase JSON literals, not True/False).')
    if match in ("string_eq", "string_contains", "string_regex"):
        case_sensitive = bool(check.get("case_sensitive", True))
        trim_strings   = bool(check.get("trim_strings", False))
        if case_sensitive and not trim_strings:
            sensitivity_hint = ("Match is case-sensitive; do not change "
                                "the capitalization or trim whitespace.")
        elif not case_sensitive and trim_strings:
            sensitivity_hint = ("Match is case-insensitive and ignores "
                                "leading/trailing whitespace.")
        elif not case_sensitive and not trim_strings:
            sensitivity_hint = ("Match is case-insensitive but "
                                "whitespace is preserved.")
        else:  # case_sensitive and trim_strings
            sensitivity_hint = ("Match is case-sensitive; leading/"
                                "trailing whitespace is trimmed.")
        return (f'Output your final answer as: '
                f'ANSWER {{"{leaf}": "..."}} '
                f'(JSON object, double quotes). '
                f'Example: ANSWER {{"{leaf}": "buy milk"}}. '
                f'{sensitivity_hint}')
    raise ValueError(f"describe_answer_format: unhandled match {match!r}")


def lint_answer_instruction(instruction: str,
                              check: Dict[str, Any]) -> List[str]:
    """Return a list of error strings — empty list means the instruction
    is well-formed for the given agent_answer check. Generators should
    raise on a non-empty result; tests assert this directly."""
    errors: List[str] = []
    if check.get("kind") != "agent_answer":
        return errors  # Not our concern.
    if "ANSWER" not in instruction:
        errors.append(
            "instruction must include the literal token 'ANSWER' so the "
            "agent knows to use the ANSWER terminal action")
    schema_sentence = describe_answer_format(check)
    if schema_sentence not in instruction:
        errors.append(
            "instruction must contain the canonical schema sentence "
            "produced by describe_answer_format(check); splice the "
            "second return value of make_answer_check() into the "
            "instruction verbatim")
    return errors
