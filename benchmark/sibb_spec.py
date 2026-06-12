"""
Typed entries for `InitialState.spec`.

Each subclass of `_SpecBase` represents one valid `(app, type)`
combination. Instances are frozen dataclasses; `.to_dict()` produces
the canonical dict shape that handlers and the dispatcher consume,
and `.from_dict()` round-trips back to a typed instance.

The registry `SPEC_TYPES` keys every known combination by
`(app, type)` so `validate_entry` / `validate_spec` can answer
"is this dict a well-formed spec entry?" without dispatching it.
Generators stay dict-based today; the validator is the safety net
that catches a typo or a missing field at task-construction time
rather than at episode-runtime.

A future refactor (A5b / TaskBuilder in Phase 2c) will have
generators construct typed entries directly and derive `params`
from `spec`, killing the dual-source-of-truth class entirely.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, ClassVar, Dict, List, Optional, Tuple, Type


class _SpecBase:
    """Mixin giving typed-entry subclasses a uniform serialization.

    Subclasses MUST be `@dataclass(frozen=True)` and declare two
    `ClassVar[str]` overrides: `app` and `type`. Those drive the
    `SPEC_TYPES` registry key and `to_dict()` output. All non-
    `ClassVar` annotations on the subclass become dataclass fields
    and round-trip through `to_dict()` / `from_dict()`.
    """

    app: ClassVar[str] = ""
    type: ClassVar[str] = ""

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"app": self.app, "type": self.type}
        for f in fields(self):
            out[f.name] = getattr(self, f.name)
        return out

    @classmethod
    def from_dict(cls, d: Dict[str, Any]):
        kwargs: Dict[str, Any] = {}
        for f in fields(cls):
            if f.name in d:
                kwargs[f.name] = d[f.name]
        return cls(**kwargs)


# ─────────────────────────────── Reminders ────────────────────────────

@dataclass(frozen=True)
class RemindersList(_SpecBase):
    app: ClassVar[str] = "Reminders"
    type: ClassVar[str] = "list"

    name: str


@dataclass(frozen=True)
class RemindersItem(_SpecBase):
    app: ClassVar[str] = "Reminders"
    type: ClassVar[str] = "item"

    list: str
    title: str
    priority: Optional[str] = None
    completed: bool = False
    # Optional fields lining up with EKReminder.dueDateComponents,
    # .notes, .url. Settable through Swift's create_reminder and
    # readable via the reminders.items fetcher. due_iso accepts
    # `YYYY-MM-DDTHH:MM:SS` (local) or `…Z` (UTC) — same parser as
    # CalendarEvent. Time-of-day component sets a default alarm at
    # that moment; date-only strings get parsed as midnight local.
    due_iso: Optional[str] = None
    notes: Optional[str] = None
    url: Optional[str] = None
    # Recurrence rule for the reminder. Shape:
    #   {"frequency": "daily|weekly|monthly|yearly",
    #    "interval":  int (default 1),
    #    "end_iso":   ISO string (optional, mutually exclusive with end_count),
    #    "end_count": int (optional, mutually exclusive with end_iso)}
    # Maps directly onto EKRecurrenceRule. Reads back through
    # reminders.items as the same dict shape.
    recurrence: Optional[Dict[str, Any]] = None


# ─────────────────────────────── Calendar ────────────────────────────

@dataclass(frozen=True)
class Calendar(_SpecBase):
    """An EKCalendar of type `.event` — a user-visible calendar (e.g.
    "Work", "Personal") that events can be assigned to. The iOS sim
    ships with exactly one writable calendar named `"Calendar"` (see
    IOS_SIM_QUIRKS §16); generators that need multi-calendar tasks
    pre-create extras via this spec entry. The default `"Calendar"`
    survives wipes; only user-created calendars are removed on reset."""
    app: ClassVar[str] = "Calendar"
    type: ClassVar[str] = "calendar"

    # Required: display name. Must not collide with the default
    # `"Calendar"` calendar (Swift will reject the create). Case-
    # sensitive when looking up by name later.
    name: str
    # Optional: hex color string ("#RRGGBB"). Swift accepts and stores
    # via EKCalendar.cgColor; defaults to an iOS-picked color if absent.
    # Not surfaced in `list_calendars` today (no verifier asserts it),
    # but the field exists for forward compatibility.
    color: Optional[str] = None


@dataclass(frozen=True)
class CalendarEvent(_SpecBase):
    app: ClassVar[str] = "Calendar"
    type: ClassVar[str] = "event"

    # Required: title + ISO8601 window (`2026-05-15T14:00:00` local,
    # or `2026-05-15T14:00:00Z` UTC). Swift parses both.
    title: str
    start_iso: str
    end_iso: str
    # Optional fields default to None / False; serialized as-is so
    # Swift can use its own defaults (default calendar, not all-day).
    calendar: Optional[str] = None
    all_day: bool = False
    location: Optional[str] = None
    notes: Optional[str] = None
    # Optional URL on the event (EKEvent.url). Stored as String?, read
    # back as the absoluteString. Mirrors RemindersItem.url. Used by
    # T4 generators (add_event_url) and T5 reporting (event-url lookup).
    url: Optional[str] = None
    # Recurrence rule on the event (EKEvent.recurrenceRules first entry).
    # Same shape as RemindersItem.recurrence:
    #   {"frequency": "daily|weekly|monthly|yearly",
    #    "interval":  int (default 1),
    #    "end_iso":   ISO string (mutually exclusive with end_count),
    #    "end_count": int (mutually exclusive with end_iso)}
    # See IOS_SIM_QUIRKS §16 for iOS expansion semantics — list_events
    # dedups to one row per series with the master's start_iso/end_iso.
    recurrence: Optional[Dict[str, Any]] = None


# ─────────────────────────────── Contacts ────────────────────────────

@dataclass(frozen=True)
class Contact(_SpecBase):
    app: ClassVar[str] = "Contacts"
    type: ClassVar[str] = "contact"

    # At least one of given_name / family_name must be non-empty.
    # Swift enforces this; client-side checks happen in validate_entry.
    given_name: str = ""
    family_name: str = ""

    # Simple scalar optionals.
    middle_name: Optional[str] = None
    nickname: Optional[str] = None
    phonetic_given_name: Optional[str] = None
    phonetic_family_name: Optional[str] = None
    phonetic_middle_name: Optional[str] = None
    organization: Optional[str] = None
    job_title: Optional[str] = None
    department: Optional[str] = None
    birthday: Optional[str] = None  # "YYYY-MM-DD" or "--MM-DD"

    # Legacy single-value paths. Pass either `phone`/`email` OR the
    # multi-value `phones`/`emails` arrays — Swift rejects both.
    phone: Optional[str] = None
    email: Optional[str] = None

    # Multi-value labeled fields. Each is a list of dicts on the wire;
    # see sibb_xcuitest_setup.sh::create_contact for the schemas.
    phones: Optional[List[Dict[str, str]]] = None
    emails: Optional[List[Dict[str, str]]] = None
    postal_addresses: Optional[List[Dict[str, str]]] = None
    urls: Optional[List[Dict[str, str]]] = None
    dates: Optional[List[Dict[str, str]]] = None


@dataclass(frozen=True)
class UpdateContact(_SpecBase):
    """Update an existing contact found by `identifier`.

    Clear-vs-omit semantics (mirrors Swift handler):
      - Scalar fields with empty string CLEAR; omitted keys leave the
        value unchanged.
      - Multi-value arrays: `[]` clears, non-empty REPLACES the array,
        omitted leaves unchanged.
      - Birthday: `""` clears, omitted leaves unchanged.
      - JSON `null` for any field is rejected by the Swift handler.
    """
    app: ClassVar[str] = "Contacts"
    type: ClassVar[str] = "update_contact"

    identifier: str = ""  # required; Swift rejects empty
    given_name: Optional[str] = None
    family_name: Optional[str] = None
    middle_name: Optional[str] = None
    nickname: Optional[str] = None
    phonetic_given_name: Optional[str] = None
    phonetic_family_name: Optional[str] = None
    phonetic_middle_name: Optional[str] = None
    organization: Optional[str] = None
    job_title: Optional[str] = None
    department: Optional[str] = None
    birthday: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    phones: Optional[List[Dict[str, str]]] = None
    emails: Optional[List[Dict[str, str]]] = None
    postal_addresses: Optional[List[Dict[str, str]]] = None
    urls: Optional[List[Dict[str, str]]] = None
    dates: Optional[List[Dict[str, str]]] = None


# ─────────────────────────────── Files ──────────────────────────────

@dataclass(frozen=True)
class File(_SpecBase):
    app: ClassVar[str] = "Files"
    type: ClassVar[str] = "file"

    # Required: path relative to SIBBWorkspace. Absolute paths and
    # `..` segments are rejected on the Swift side.
    path: str
    # Content as utf-8 by default; pass encoding="base64" for binary.
    content: str = ""
    encoding: Optional[str] = None


# ─────────────────────────────── Settings ───────────────────────────

@dataclass(frozen=True)
class DefaultsEntry(_SpecBase):
    """A single `defaults write` operation against a sim preferences
    domain. Type is named DefaultsEntry rather than Setting to mirror
    the `defaults write` CLI vocabulary the handler shells out to.
    """
    app: ClassVar[str] = "Settings"
    type: ClassVar[str] = "default"

    # Required.
    domain: str          # e.g. "com.apple.Preferences"
    key: str             # e.g. "AppleITunesStoreItemKinds"
    # value's runtime type matches value_type:
    #   "bool"   → bool
    #   "int"    → int
    #   "string" → str
    #   "float"  → float
    value: Any
    value_type: str = "string"


# ─────────────────────────────── Photos ─────────────────────────────

@dataclass(frozen=True)
class PhotoMedia(_SpecBase):
    """A media file to inject into the sim photo library via
    `simctl addmedia`. Type is named `media` (not `image` or `photo`)
    because simctl + PhotoKit accept image, video, and live-photo
    containers all through the same write path.
    """
    app: ClassVar[str] = "Photos"
    type: ClassVar[str] = "media"

    # Required: absolute or relative-to-cwd path on the HOST to an
    # image/video file. Relative paths are resolved at apply time
    # against the generator's working directory.
    host_path: str


# ─────────────────────────────── Health ─────────────────────────────

@dataclass(frozen=True)
class HealthSample(_SpecBase):
    """A single HealthKit quantity sample.

    `sample_type` must be one of the keys in `HEALTH_QUANTITY_TYPES`
    in `sibb_xcuitest_setup.sh` (also mirrored as `HEALTH_VALID_TYPES`
    in `sibb_state.py`). `value` is numeric (int or float — both
    serialize to JSON number). `end_iso` defaults to `start_iso` for
    instantaneous samples like body_mass.
    """
    app: ClassVar[str] = "Health"
    type: ClassVar[str] = "sample"

    sample_type: str           # "step_count" | "heart_rate" | "body_mass"
    value: float               # numeric value in the type's canonical unit
    start_iso: str             # ISO8601 start
    end_iso: Optional[str] = None  # defaults to start_iso (instantaneous)


# ─────────────────────────────── Safari ─────────────────────────────

@dataclass(frozen=True)
class Bookmark(_SpecBase):
    """A Safari bookmark.

    By default inserted under BookmarksBar (the iOS folder labeled
    "Favorites" in the UI). If `folder` is set, the bookmark goes into
    that named subfolder under BookmarksBar instead — the SafariHandler
    creates the subfolder on demand if missing.

    Fields:
      title:  user-visible title.
      url:    bookmark URL.
      folder: optional subfolder name. When None (default), the
              bookmark is inserted directly under BookmarksBar root.
              When set, the bookmark goes into BookmarksBar/<folder>.
    """
    app: ClassVar[str] = "Safari"
    type: ClassVar[str] = "bookmark"

    title: str
    url: str
    folder: Optional[str] = None


@dataclass(frozen=True)
class MockSite(_SpecBase):
    """A host-side login-site fixture for end-to-end password tasks.

    Spins up a Python HTTP server on `127.0.0.1:<random>` (the iOS
    sim shares the host's network stack, so Safari reaches it
    directly). Pre-seeded with `credentials` the site accepts;
    every form submission is recorded server-side. The
    `mock_site.submissions` resource fetcher reads that record —
    the *only* surface where we can verify the agent's autofilled
    password *value* (the keychain encrypts the BLOB and won't
    surrender it).

    `open_at_start=True` (default) makes the handler navigate
    Safari to the login URL on apply, so the agent's first
    observation is the form. Disable for tasks that want the
    fixture present but not focused.
    """
    app: ClassVar[str] = "Safari"
    type: ClassVar[str] = "mock_site"

    # Required — must be unique within a SIBB process; the
    # mock-site registry rejects collisions at start().
    site_id: str
    # username → password pairs the site will accept on /login.
    # Empty dict ⇒ signup-only site (agent must POST /signup first).
    credentials: Dict[str, str] = field(default_factory=dict)
    # If True, navigate Safari to login_url after starting.
    open_at_start: bool = True
    # Route paths — override only if a task specifically needs to
    # exercise non-default URLs.
    sign_in_path: str = "/login"
    sign_up_path: str = "/signup"
    # ── Phase 4 harness: static-page templates (2026-06-05) ──────────
    # `static_pages` maps a URL path → harness-page template NAME
    # registered in `harness_layout.PAGE_REGISTRY`. We can't put a
    # callable here directly because this dataclass is frozen and
    # JSON-serializable; registering by name lets the apply path
    # resolve the actual template fn at spawn time. Empty by default
    # — only the signin/signup routes are served then.
    static_pages: Dict[str, str] = field(default_factory=dict)
    # Per-episode page seed. The MockSite renders each static-page
    # template against a `random.Random` derived from this seed XOR'd
    # with a stable digest of the path, so layouts are deterministic-
    # per-(seed, path) and replayable. Default 0 — generators should
    # set this from their episode seed for cross-episode variance.
    page_seed: int = 0
    # When `open_at_start=True`, override the start URL to a static-
    # page path rather than the default `sign_in_path`. Use with
    # `static_pages={"/event": "rsvp_event"}` + `start_path="/event"`
    # for harness-page generators where the agent should land on
    # the form page instead of a login form. None ⇒ open the login
    # URL (legacy behavior).
    start_path: Optional[str] = None
    # Optional friendly hostname mapped to 127.0.0.1 via the sim's
    # /etc/hosts. When set, the agent sees URLs like
    # `http://aurora-conference.example:<port>/event` instead of
    # `http://127.0.0.1:<port>/event`. SafariHandler appends the
    # `127.0.0.1 <hostname>` line on apply and removes it on reset
    # so episodes don't leak hostname mappings. Pick a hostname
    # under a reserved test TLD (`.example`, `.test`, `.localhost`)
    # so it can't collide with a real DNS name. None ⇒ use 127.0.0.1.
    hostname: Optional[str] = None


# ─────────────────────────────── Messages ────────────────────────────

@dataclass(frozen=True)
class MessageSendInThread(_SpecBase):
    """Pre-runner: send a marker iMessage in one of the iOS-sim phantom
    threads (`JA` = +1-888-555-1212 or `KB` = +1-555-564-8583). The
    simulator's no-account IDS-firewall fallback echoes the text back
    as a gray-bubble INBOUND on the OTHER phantom thread — this is
    the only way to seed agent-readable inbound content on iOS sim
    (see IOS_SIM_QUIRKS.md §11).

    Workflow:
      thread="JA" → send to JA; agent reads the loopback in KB.
      thread="KB" → send to KB; agent reads the loopback in JA.

    HARD CONSTRAINT: never `simctl terminate com.apple.MobileSMS`
    between this spec entry being applied and verifier run — the
    bubble is in-memory only. The MessagesHandler does ONE
    terminate-relaunch inside apply() to reset state; no further
    termination is allowed by anything in the reset / cleanup pipeline.
    """
    app: ClassVar[str] = "Messages"
    type: ClassVar[str] = "send_in_thread"

    text: str
    thread: str = "JA"


# ─────────────────────────────── Springboard ──────────────────────────
#
# The three Springboard entry types split across the pre-runner /
# runtime boundary: `layout` and `dock` are applied via the
# sibb_randomize_layout.py script while the sim is shut down;
# `start_page` swipes after boot. The split is a `pre_runner_kinds`
# concern on the handler — these spec dataclasses don't repeat it.

@dataclass(frozen=True)
class RunShortcut(_SpecBase):
    """Run a Library shortcut by name via the
    `shortcuts://run-shortcut?name=...` URL scheme.

    `input` is the payload that becomes the **Shortcut Input** magic
    variable inside the shortcut:
        None  — no input. URL is just `name=`.
        str   — passed verbatim as the `text` query param.
        dict  — JSON-encoded then passed as `text`. The shortcut
                should use `Get Dictionary from Input` to parse
                individual keys (URL scheme has only ONE input slot,
                so multi-parameter shortcuts use this pattern).

    Limitations (Apple constraints, not ours):
      - Cannot create / edit / delete shortcuts. Apple has no public
        API; the Core Data store is opaque. See TODO_DEFERRED §G1.
      - Cannot run trigger-based Automations by name. URL scheme is
        name-addressable for Library shortcuts only. Automations
        need UI drive: open Automation tab → tap automation →
        "Run Immediately".
    """
    app: ClassVar[str] = "Shortcuts"
    type: ClassVar[str] = "run"

    # Exact display name of a Library shortcut. URL-encoding (spaces,
    # special chars) is done by the handler — generators pass the
    # human-readable name verbatim.
    name: str
    # Optional payload; see class docstring for shape.
    input: Optional[Any] = None


@dataclass(frozen=True)
class SpringboardLayout(_SpecBase):
    app: ClassVar[str] = "Springboard"
    type: ClassVar[str] = "layout"

    seed: int = 0
    cross_page: bool = False
    distribute: bool = False
    n_pages: Optional[int] = None


@dataclass(frozen=True)
class SpringboardDock(_SpecBase):
    app: ClassVar[str] = "Springboard"
    type: ClassVar[str] = "dock"

    seed: int = 0
    count: Optional[int] = None


@dataclass(frozen=True)
class SpringboardStartPage(_SpecBase):
    app: ClassVar[str] = "Springboard"
    type: ClassVar[str] = "start_page"

    page: int = 0


# ─────────────────────────────── Registry ─────────────────────────────

SPEC_TYPES: Dict[Tuple[str, str], Type[_SpecBase]] = {
    (cls.app, cls.type): cls
    for cls in (
        RemindersList, RemindersItem,
        Calendar, CalendarEvent,
        Contact, UpdateContact,
        File,
        DefaultsEntry,
        PhotoMedia,
        HealthSample,
        Bookmark, MockSite,
        MessageSendInThread,
        RunShortcut,
        SpringboardLayout, SpringboardDock, SpringboardStartPage,
    )
}


# ─────────────────────────── Validation helpers ───────────────────────

def validate_entry(d: Any) -> Tuple[Optional[_SpecBase], Optional[str]]:
    """Try to interpret `d` as a typed spec entry.

    Returns `(typed_instance, None)` on success, or
    `(None, error_message)` on failure. Failure modes:
      - `d` is not a dict
      - `(app, type)` is missing or not in `SPEC_TYPES`
      - the dataclass constructor rejects the field set
    """
    if not isinstance(d, dict):
        return None, f"entry is not a dict: {type(d).__name__}"
    key = (d.get("app"), d.get("type"))
    cls = SPEC_TYPES.get(key)
    if cls is None:
        return None, (
            f"unknown spec entry kind {key!r}; "
            f"valid kinds: {sorted(SPEC_TYPES)!r}"
        )
    try:
        return cls.from_dict(d), None
    except TypeError as e:
        return None, f"{cls.__name__}.from_dict: {e}"


def validate_spec(spec: List[Dict[str, Any]]) -> List[str]:
    """Return error messages for every malformed entry in `spec`.

    Empty list means the spec is valid. The dispatcher does NOT
    auto-validate today (it tolerates dict-shaped entries from
    legacy generators); this helper is the seam tests / runners
    use to fail fast on a malformed task.
    """
    errors: List[str] = []
    for i, entry in enumerate(spec or []):
        _, err = validate_entry(entry)
        if err is not None:
            errors.append(f"spec[{i}]: {err}")
    return errors
