"""
SIBB Task Generator v3
======================
Incorporates all design changes from the full research discussion:

1.  Noise injection — similar-but-wrong records injected before task starts,
    with pre/post state diffing for clean binary verification
2.  Constraint satisfaction tasks — Files size range, Notes word/char range,
    Calendar duration range, Reminders count range
3.  Complexity scoring — estimated_human_taps / 4.0 + modifiers (constraint,
    multi-app, noise density)
4.  FAIL tool — agent outputs `FAIL "reason"`, verifier checks for it
5.  CLARIFY tool — deterministic slot lookup, no cap, repeated questions get
    same answer, no partial credits ever
6.  Impossibility tasks — `blocking` initial state, verify pre/post diff
7.  Ambiguous tasks — AmbiguousTaskSpec with ClarifySlot registry
8.  Binary scoring throughout — 1.0 or 0.0, nothing in between
9.  MandatoryParam / OptionalParam distinction, detail_level for complexity dial
10. Three flows we didn't have before: S (search-then-act), R (fetch/read),
    U (update existing record)
"""

import datetime as _dt
import random
import uuid
import json
from dataclasses import dataclass, field
from typing import Any, List, Optional, Dict, Tuple

# Tier 5 reporting generators use the agent_answer helpers from the
# verifier module. Imported lazily-on-module-load (no cycle: verify
# does not import the generator).
from sibb_verify import make_answer_check  # noqa: E402

try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    import numpy as np
    _SKLEARN_OK = True
except ImportError:
    _SKLEARN_OK = False


# ─────────────────────────────────────────────────────────────────────────────
#  APP REGISTRY
#  Single source of truth for which apps are available in which simulator.
#  To re-enable an app when it becomes available: change "available" to True.
#  To add a new app: add an entry to APP_REGISTRY.
# ─────────────────────────────────────────────────────────────────────────────

APP_REGISTRY = {
    # ── Confirmed available in iOS 26.3 simulator (SIBB-11) ──────────────────
    "Reminders":  {"bundle": "com.apple.reminders",            "available": True,  "tier": "A"},
    "Calendar":   {"bundle": "com.apple.mobilecal",            "available": True,  "tier": "A"},
    "Contacts":   {"bundle": "com.apple.MobileAddressBook",    "available": True,  "tier": "A"},
    "Settings":   {"bundle": "com.apple.Preferences",          "available": True,  "tier": "A"},
    "Files":      {"bundle": "com.apple.DocumentsApp",         "available": True,  "tier": "A"},
    "Health":     {"bundle": "com.apple.Health",               "available": True,  "tier": "A"},
    "Maps":       {"bundle": "com.apple.Maps",                 "available": True,  "tier": "A"},
    "Photos":     {"bundle": "com.apple.mobileslideshow",      "available": True,  "tier": "A"},
    "Shortcuts":  {"bundle": "com.apple.shortcuts",            "available": True,  "tier": "A"},
    "Safari":     {"bundle": "com.apple.mobilesafari",         "available": True,  "tier": "B"},
    "Messages":   {"bundle": "com.apple.MobileSMS",            "available": True,  "tier": "B"},

    # ── Unavailable in iOS 26.3 simulator — re-enable when fixed ─────────────
    # These apps exist on real iOS 26 devices but are missing from the
    # iOS 26.3 simulator runtime (build 23D8133 / Xcode 26.3).
    # Root cause: apps moved to App Store downloadable model; simulator
    # has no App Store. Monitor future Xcode/simulator updates.
    # To enable: set "available": True and add generators for these apps.
    "Notes":       {"bundle": "com.apple.mobilenotes",         "available": False, "tier": "A",
                    "unavailable_reason": "iOS 26 simulator missing — App Store downloadable"},
    "Clock":       {"bundle": "com.apple.mobiletimer",         "available": False, "tier": "A",
                    "unavailable_reason": "iOS 26 simulator missing — App Store downloadable"},
    "Music":       {"bundle": "com.apple.Music",               "available": False, "tier": "B",
                    "unavailable_reason": "iOS 26 simulator missing — App Store downloadable"},
    "Podcasts":    {"bundle": "com.apple.podcasts",            "available": False, "tier": "B",
                    "unavailable_reason": "iOS 26 simulator missing — App Store downloadable"},
    "Books":       {"bundle": "com.apple.iBooks",              "available": False, "tier": "B",
                    "unavailable_reason": "iOS 26 simulator missing — App Store downloadable"},
    "Mail":        {"bundle": "com.apple.mobilemail",          "available": False, "tier": "B",
                    "unavailable_reason": "iOS 26 simulator missing — App Store downloadable"},

    # ── Not in simulator runtime (device-only apps) ───────────────────────────
    "Phone":       {"bundle": "com.apple.mobilephone",         "available": False, "tier": "B",
                    "unavailable_reason": "Not included in simulator runtime"},
    "Voice Memos": {"bundle": "com.apple.VoiceMemos",          "available": False, "tier": "B",
                    "unavailable_reason": "Not included in simulator runtime"},
    "Keynote":     {"bundle": "com.apple.Keynote",             "available": False, "tier": "B",
                    "unavailable_reason": "Not included in simulator runtime"},
    "Pages":       {"bundle": "com.apple.Pages",               "available": False, "tier": "B",
                    "unavailable_reason": "Not included in simulator runtime"},
    "Numbers":     {"bundle": "com.apple.Numbers",             "available": False, "tier": "B",
                    "unavailable_reason": "Not included in simulator runtime"},
}

# Available apps for quick reference
AVAILABLE_APPS = [name for name, info in APP_REGISTRY.items() if info["available"]]

def check_app_available(app_name: str) -> bool:
    """Check if an app is available in the current simulator configuration."""
    return APP_REGISTRY.get(app_name, {}).get("available", False)

def get_available_apps(tier: str = None) -> list:
    """Get list of available apps, optionally filtered by tier."""
    return [
        name for name, info in APP_REGISTRY.items()
        if info["available"] and (tier is None or info["tier"] == tier)
    ]


# ─────────────────────────────────────────────────────────────────────────────
#  CANONICAL VALUES
# ─────────────────────────────────────────────────────────────────────────────

C = {
    "family_contacts":  ["Mom", "Dad", "Sister", "Brother", "Grandma",
                         "Aunt Lisa", "Uncle Tom", "My Partner"],
    "social_contacts":  ["Alex", "Sam", "Jordan", "Taylor", "Chris",
                         "Morgan", "Jamie", "Casey", "Riley"],
    "work_contacts":    ["My Manager", "John Smith", "Sarah Chen",
                         "Marcus Webb", "Dr. Patel", "Alex Kim"],
    "contact_first":    ["Mom", "Dad", "Alex", "Sam", "Jordan", "Taylor",
                         "Chris", "Morgan", "John", "Jane", "Marcus",
                         "Sarah", "Dr. Chen", "Riley", "Casey"],
    "contact_last":     ["Smith", "Johnson", "Williams", "Brown", "Jones",
                         "Davis", "Miller", "Wilson", "Webb", "Kim",
                         "Chen", "Patel", "Garcia", "Lee", "Taylor"],
    "blocking_names":   ["Greg", "Natalie", "Bob from Work", "Dr. Ramirez",
                         "Coach Mike", "Aunt Susan", "My Accountant",
                         "Old Friend Jake", "Professor Lee", "Neighbor Pete"],
    # Calendar names that generators create via `create_calendar`.
    # Skip "Calendar" — it's the iOS default writable calendar on a
    # fresh sim (see IOS_SIM_QUIRKS §16). Skip "Birthdays", "Holidays",
    # "US Holidays", "Siri Suggestions" — read-only / system-provided.
    "calendar_names":   ["Personal", "Work", "Family", "Home",
                         "Health & Fitness", "Travel", "Shared",
                         "Side Projects", "Kids", "Volunteering",
                         "Errands", "Date Night", "School", "Gym",
                         "Book Club", "Therapy", "Tax Prep",
                         "Side Hustle", "Renovations", "Hobby"],
    "event_titles":     ["Team Standup", "Project Kickoff", "Weekly Review",
                         "Gym Session", "Doctor Appointment", "Dentist",
                         "Budget Review", "Date Night", "Car Service",
                         "Job Interview", "1-on-1", "Haircut",
                         "Coffee catch-up", "Lunch meeting",
                         # Added 2026-05-20 — corpora expansion before
                         # Calendar tier-by-tier (dedup'd "Car Service").
                         "All-Hands", "Sprint Planning", "Retro",
                         "Quarterly Review", "Client Sync", "Demo Day",
                         "Parent-Teacher Conference", "Book Club Meetup",
                         "Therapy Session", "Massage", "Tax Appointment",
                         "Vet Visit", "PT Session", "Yoga Class",
                         "Investor Call", "Board Meeting", "Onboarding Call",
                         "Vendor Review", "Phone Screen", "Tech Interview",
                         "Tutoring Session", "Family Dinner"],
    "event_locations":  ["Conference Room A", "Conference Room B",
                         "Zoom", "Google Meet", "Home Office",
                         "Starbucks on Market St", "Blue Bottle Coffee",
                         "Dentist Office", "Dr. Lin's Clinic",
                         "Anchor Brewing", "Mission Park", "Dolores Park",
                         "City Library", "School Auditorium",
                         "Office HQ", "Client Site", "Airport",
                         "Co-working Space", "Gym Studio 2",
                         "Whole Foods Parking Lot", "Embarcadero Cafe",
                         "1 Infinite Loop"],
    "event_notes":      ["Bring slides.", "Remember to update the deck.",
                         "Agenda: roadmap, blockers, Q&A.",
                         "Confirm parking validation.",
                         "Discuss budget and headcount.",
                         "Don't forget the gift.",
                         "Bring printout of last quarter's metrics.",
                         "Pre-read attached.",
                         "Followup on action items from last week.",
                         "Optional — joining if I can.",
                         "Conference room booked through 4pm.",
                         "Bring laptop, charger, and notebook.",
                         "Dial-in info in the calendar invite description.",
                         "RSVP by Friday.",
                         "Wear something nice.",
                         "Pack a snack."],
    "reminder_lists":   ["Personal", "Work", "Groceries", "Home",
                         "Health", "Family", "Shopping", "Travel",
                         "Reading", "Someday", "Projects", "Finance",
                         "Errands", "Bills", "Garden", "Pets",
                         "Kids", "Car", "Gifts", "House",
                         "Subscriptions", "Recipes", "Self-care",
                         "Wishlist", "Side Projects", "Holiday Prep",
                         "Followups", "Tomorrow"],
    "note_folders":     ["Work", "Personal", "Projects", "Archive",
                         "Research", "Clients", "Meeting Notes",
                         "Travel", "Recipes", "Health", "Finance"],
    "note_titles":      ["Q3 Planning", "Meeting Minutes", "Project Kickoff",
                         "Research Notes", "Weekly Goals", "Budget Draft",
                         "Ideas Dump", "Pitch Deck Outline",
                         "Product Feedback", "Team Retrospective",
                         "Travel Notes", "Book Summary", "Recipe Ideas"],
    "folder_names":     ["Work Projects", "Personal Documents", "Archive",
                         "2025", "Clients", "Research Papers",
                         "Meeting Notes", "Pitch Prep", "Tax Documents",
                         "Medical Records", "Travel", "Finance Docs"],
    "file_names": [
        "report_q1.txt", "notes_march.txt", "summary_v2.txt",
        "draft_proposal.txt", "budget_2025.txt", "meeting_recap.txt",
        "project_plan.txt", "weekly_update.txt", "research_notes.txt",
        "client_brief.txt", "action_items.txt", "ideas_dump.txt",
    ],
    "alarm_labels":     ["Wake Up", "Work", "Gym", "School",
                         "Meeting", "Medication", "Nap",
                         "Leave for airport", "Morning Standup"],
    "alarm_sounds":     ["Radar", "Ripple", "Chimes", "Apex",
                         "Bulletin", "Nightfall", "Signal", "Summit"],
    "alarm_days": [
        ["Monday", "Wednesday", "Friday"],
        ["Tuesday", "Thursday"],
        ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
        ["Saturday", "Sunday"],
        ["every day"],
    ],
    "wake_times":   ["5:00 AM", "5:15 AM", "5:30 AM", "5:45 AM",
                     "6:00 AM", "6:15 AM", "6:30 AM", "6:45 AM",
                     "7:00 AM", "7:15 AM", "7:30 AM", "7:45 AM",
                     "8:00 AM", "8:15 AM", "8:30 AM"],
    "work_times":   ["8:30 AM", "9:00 AM", "9:30 AM", "10:00 AM",
                     "11:00 AM", "1:00 PM", "2:00 PM", "3:00 PM",
                     "4:00 PM", "4:30 PM", "5:00 PM"],
    "day_offsets":  ["today", "tomorrow", "this Friday", "next Monday",
                     "next Wednesday", "next Saturday", "in 3 days",
                     "in 1 week", "next month"],
    "alert_offsets": ["at time of event", "5 minutes before",
                      "10 minutes before", "15 minutes before",
                      "30 minutes before", "1 hour before",
                      "1 day before", "1 week before"],
    "recurrences":  ["never", "daily", "every weekday",
                     "every Monday", "every Monday and Wednesday",
                     "every Tuesday and Thursday",
                     "every Mon, Wed, Fri", "weekly", "monthly"],
    "priority_levels":  ["low", "medium", "high"],
    "note_tags":    ["work", "personal", "ideas", "research",
                     "meeting", "project", "reading", "travel",
                     "health", "finance", "family", "urgent"],
    "maps_places":  ["Apple Store", "Starbucks", "Whole Foods Market",
                     "CVS Pharmacy", "Gold's Gym", "Union Square",
                     "Golden Gate Park", "Ferry Building",
                     "Chinatown", "Tartine Bakery"],
    "landmark_places": ["Union Square", "Ferry Building",
                        "Golden Gate Park", "Embarcadero"],
    "safari_urls":  ["apple.com/iphone",
                     "en.wikipedia.org/wiki/Machine_learning",
                     "en.wikipedia.org/wiki/Reinforcement_learning",
                     "github.com/trending"],
    "months":       ["January", "February", "March", "April", "May",
                     "June", "July", "August", "September",
                     "October", "November", "December"],
    "companies":    ["Acme Corp", "Apple", "Google", "Meta",
                     "City Hospital", "Law Offices", "Startup Inc"],
    "n_items":      [3, 4, 5, 6],
    "work_items":   ["Review budget", "Update roadmap", "Schedule reviews",
                     "Send board summary", "Prepare slides", "Draft proposal",
                     "Update stakeholders", "Close action items",
                     "Send agenda", "Book projector", "Write retrospective",
                     "Archive old files",
                     # household / personal — mixes the pool so demo
                     # episodes feel less monotonous; verifier doesn't
                     # care about semantic fit with the list name
                     "Buy milk", "Pick up dry cleaning",
                     "Take out the trash", "Water the plants",
                     "Schedule a dentist visit", "Pack lunch",
                     "Renew my passport", "Mail a birthday card",
                     "Refill prescriptions", "Call the plumber",
                     "Change the air filter", "Wash the car",
                     "Pay credit card bill", "Cancel old subscription",
                     "Print boarding pass", "Confirm hotel reservation",
                     "Buy concert tickets", "Send party invites",
                     "Order new books", "Schedule oil change"],
    # File sizes in KB for constraint tasks
    "file_sizes_kb":    [15, 20, 25, 30, 35, 40, 45, 50, 60, 75, 80, 100],
    # Note word counts for constraint tasks
    "note_word_counts": [30, 50, 60, 75, 80, 100, 120, 150, 180, 200],
    "ambiguous_names":  ["the meeting note", "the project file",
                         "the draft", "the report", "the summary",
                         "the proposal"],
}

def pick(key): return random.choice(C[key])
def pickn(key, n): return random.sample(C[key], min(n, len(C[key])))


# ─────────────────────────────────────────────────────────────────────────────
#  CORE DATACLASSES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MandatoryParam:
    """Always in instruction and verifier. Task undefined without it."""
    key:   str
    value: Any

    @property
    def included(self): return True


@dataclass
class OptionalParam:
    """In instruction AND verifier only if sampled. Task valid without it."""
    key:          str
    value:        Any
    include_prob: float = 0.5
    step_cost:    int   = 1
    included:     bool  = field(default=False, repr=False)

    def sample(self, detail_level: float = 0.5) -> "OptionalParam":
        if detail_level <= 0.0:
            self.included = False
        elif detail_level >= 1.0:
            self.included = True
        else:
            if detail_level < 0.5:
                effective = self.include_prob * (detail_level / 0.5)
            else:
                effective = self.include_prob + (1.0 - self.include_prob) * \
                            ((detail_level - 0.5) / 0.5)
            self.included = random.random() < effective
        return self


@dataclass
class NoiseRecord:
    """
    A record injected before the task starts.
    Similar to the target on most dimensions but wrong on exactly one.
    Used to prevent trivial success and simulate a realistic device state.

    Special record_type "layout":
      Represents a randomized home screen layout.
      setup_cmd calls sibb_randomize_layout.py before booting the simulator.
      No DB changes — purely visual/navigational noise.
      The task success criteria are unchanged; only the agent's navigation
      path changes (it must find apps by label, not by fixed position).
    """
    app:         str       # "Clock" | "Calendar" | "Reminders" | "SpringBoard"
    record_type: str       # "alarm" | "event" | "reminder" | "note" | "file" | "layout"
    params:      dict      # the noise record's field values
    similarity:  str       # human-readable description
    setup_cmd:   str       # simctl / sqlite command to inject it


@dataclass
class ClarifySlot:
    """
    One answerable question slot for ambiguous tasks.

    Agent output format:
        CLARIFY <question_type>: <natural language question>

    Examples:
        CLARIFY missing_value: What is Jane's phone number?
        CLARIFY disambiguate: Which note should I move? I found Budget Meeting, Team Meeting
        CLARIFY constraint: What total size should the moved files be?
        CLARIFY confirm: Should I delete all files in Archive?

    Matching: TF-IDF cosine similarity against example_questions.
    Threshold=0.25 (tune: higher=stricter, lower=more permissive).
    Repeated questions get the same answer. No cap on CLARIFY calls.
    No match or below threshold: "[CLARIFY ERROR] I don't have that information."
    """
    question_type:     str
    slot_name:         str          # internal label — NOT shown to agent
    example_questions: List[str]    # 3-5 phrasings used to build TF-IDF corpus
    answer:            Any          # returned on match; None = honest "I don't know"
    answer_format:     str          # "string"|"phone"|"time"|"date"|"index"|"bool"|"range"|"none"
    valid_options:     list         = field(default_factory=list)
    required:          bool         = True


class ClarifyHandler:
    """
    Maps free-form CLARIFY questions to pre-defined answers via TF-IDF cosine
    similarity. CPU-only, no model download, <1ms per query.

    Falls back to word-overlap if sklearn is unavailable.
    """

    THRESHOLD = 0.55

    def __init__(self, slots: List[ClarifySlot]):
        self.slots = slots
        self._examples: List[tuple] = []   # (text, slot_index)
        for i, slot in enumerate(slots):
            for q in slot.example_questions:
                self._examples.append((q, i))

        self._vectorizer = None
        self._matrix     = None
        if _SKLEARN_OK and self._examples:
            texts = [e[0] for e in self._examples]
            self._vectorizer = TfidfVectorizer(
                analyzer="word", ngram_range=(1, 2), stop_words=None
            )
            self._matrix = self._vectorizer.fit_transform(texts)

    @staticmethod
    def parse(line: str):
        """Parse 'CLARIFY <type>: <question>' → (type, question) or (None, None)."""
        rest = line[len("CLARIFY"):].strip()
        if ":" not in rest:
            return None, None
        q_type, question = rest.split(":", 1)
        return q_type.strip().lower(), question.strip()

    def handle(self, line: str) -> dict:
        q_type, question = self.parse(line)

        if q_type is None:
            return {"success": False, "answer": None,
                    "error": "[CLARIFY ERROR] Format: CLARIFY <type>: <question>  "
                             "Types: missing_value | disambiguate | confirm | constraint"}

        if q_type not in ("missing_value", "disambiguate", "confirm", "constraint"):
            return {"success": False, "answer": None,
                    "error": f"[CLARIFY ERROR] Unknown type '{q_type}'. "
                             "Use: missing_value | disambiguate | confirm | constraint"}

        if not self.slots:
            return {"success": False, "answer": None,
                    "error": "[CLARIFY ERROR] I don't have that information. "
                             "Find it in the app."}

        if _SKLEARN_OK and self._vectorizer is not None:
            return self._tfidf_match(q_type, question)
        return self._fallback_match(q_type, question)

    def _tfidf_match(self, q_type: str, question: str) -> dict:
        q_vec   = self._vectorizer.transform([question])
        sims    = cosine_similarity(q_vec, self._matrix).flatten()
        best_i  = int(np.argmax(sims))
        best_sim = float(sims[best_i])
        best_slot = self.slots[self._examples[best_i][1]]

        if best_slot.question_type != q_type or best_sim < self.THRESHOLD:
            return {"success": False, "answer": None,
                    "error": "[CLARIFY ERROR] I don't have that information. "
                             "Find it in the app."}
        return {"success": True,
                "answer": f"[USER INFO] {self._format(best_slot)}",
                "error": None, "slot_name": best_slot.slot_name,
                "required": best_slot.required,
                "similarity": round(best_sim, 3)}

    def _fallback_match(self, q_type: str, question: str) -> dict:
        """Word-overlap fallback when sklearn is unavailable."""
        q_words = set(question.lower().split())
        best_slot, best_score = None, 0.0
        for slot in self.slots:
            if slot.question_type != q_type:
                continue
            for ex in slot.example_questions:
                ex_words = set(ex.lower().split())
                score = len(q_words & ex_words) / max(len(ex_words), 1)
                if score > best_score:
                    best_score, best_slot = score, slot
        if best_slot and best_score >= 0.3:
            return {"success": True,
                    "answer": f"[USER INFO] {self._format(best_slot)}",
                    "error": None, "slot_name": best_slot.slot_name,
                    "required": best_slot.required}
        return {"success": False, "answer": None,
                "error": "[CLARIFY ERROR] I don't have that information. "
                         "Find it in the app."}

    def required_slots_answered(self, clarify_log: list) -> bool:
        answered = {e["slot_name"] for e in clarify_log
                    if e.get("success") and "slot_name" in e}
        return all(s.slot_name in answered for s in self.slots if s.required)

    @staticmethod
    def _format(slot: ClarifySlot) -> str:
        if slot.answer is None:
            return "I don't have that information. Find it in the app."
        fmt = {
            "phone":  lambda v: f"The phone number is {v}",
            "time":   lambda v: f"The time is {v}",
            "date":   lambda v: f"The date is {v}",
            "string": lambda v: f"It's '{v}'",
            "bool":   lambda v: "Yes, proceed." if v else "No, do not proceed.",
            "range":  lambda v: f"Between {v[0]} and {v[1]}",
            "index":  lambda v: f"Use '{v}'",
            "none":   lambda _: "I don't have that information. Find it in the app.",
        }
        return fmt.get(slot.answer_format, lambda v: str(v))(slot.answer)

@dataclass
class InitialState:
    """
    Simulator state before the agent starts.
    Pre-snapshot taken here for diff-based binary verification.

    `spec` is the structured, machine-readable form used by the state
    framework (`sibb_state.py`). Each entry is a dict tagged with
    `app` and `type`; per-app handlers know how to realize it. Examples:

      {"app": "Reminders",   "type": "list",  "name": "Personal"}
      {"app": "Reminders",   "type": "item",  "list": "Personal",
       "title": "Pre-existing", "priority": "medium"}
      {"app": "Calendar",    "type": "event", ...}
      {"app": "Springboard", "type": "layout", "seed": 42}
      {"app": "Springboard", "type": "dock",   "apps": [...]}

    `present`/`absent` remain as human-readable descriptions for display.
    `spec` is what the state applicator actually consumes.
    """
    present:           List[str]        = field(default_factory=list)
    absent:            List[str]        = field(default_factory=list)
    blocking:          List[str]        = field(default_factory=list)
    noise_records:     List[NoiseRecord] = field(default_factory=list)
    setup_commands:    List[str]        = field(default_factory=list)
    teardown_commands: List[str]        = field(default_factory=list)
    spec:              List[Dict[str, Any]] = field(default_factory=list)
    expected_behavior: str              = "complete"
    # "complete"             → do the task
    # "create_then_complete" → create missing entity first, then complete
    # "fail_gracefully"      → impossible; agent must output FAIL "reason"


@dataclass
class Task:
    task_id:         str
    flow:            str
    apps:            List[str]
    instruction:     str
    verify:          str
    verify_mode:     str           # "db_query" | "agent_response" | "db_and_agent"
    initial_state:   InitialState
    steps:           int
    complexity:      float
    detail_level:    float
    params:          Dict[str, Any]
    clarify_slots:   List[ClarifySlot] = field(default_factory=list)
    positive_checks: List[str]         = field(default_factory=list)
    negative_checks: List[str]         = field(default_factory=list)
    agent_check:     str               = ""
    # New in Phase 2c C1: typed verifier check dicts consumed by
    # `sibb_verify.run_checks`. Generators that emit these enable the
    # generic VERIFIERS dispatcher path; legacy generators leave it
    # empty and verify via the old `verify`/`verify_mode` strings.
    verify_checks:   List[Dict[str, Any]] = field(default_factory=list)


def complexity_score(base_steps: int,
                     n_apps: int = 1,
                     has_constraint: bool = False,
                     noise_count: int = 0,
                     requires_clarification: bool = False,
                     is_impossibility: bool = False) -> float:
    """
    AndroidWorld-equivalent complexity.  base_steps / 4.0 + modifiers.
    """
    score = base_steps / 4.0
    score += 0.8 * (n_apps - 1)
    if has_constraint:         score += 1.5
    if noise_count >= 4:       score += 0.3
    if requires_clarification: score += 1.5
    if is_impossibility:       score += 1.0
    return round(score, 1)

# ─────────────────────────────────────────────────────────────────────────────
#  SINGLE-APP TASK GENERATORS
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
#  NOISE BUILDERS
#  Each builder returns a list of NoiseRecord objects.
#  Noise records differ from the target on exactly ONE dimension.
# ─────────────────────────────────────────────────────────────────────────────

def noise_alarm(target_time, target_days, target_label, n=3) -> List[NoiseRecord]:
    records = []
    patterns = ["same_time_wrong_days", "different_time_same_days",
                "same_everything_wrong_label"]
    for i in range(min(n, len(patterns))):
        p = patterns[i]
        if p == "same_time_wrong_days":
            wrong_days = random.choice([d for d in C["alarm_days"]
                                        if d != target_days])
            params = {"time": target_time, "days": wrong_days, "label": pick("alarm_labels")}
        elif p == "different_time_same_days":
            wrong_time = random.choice([t for t in C["wake_times"]
                                        if t != target_time])
            params = {"time": wrong_time, "days": target_days, "label": pick("alarm_labels")}
        else:
            wrong_label = random.choice([l for l in C["alarm_labels"]
                                         if l != target_label])
            params = {"time": target_time, "days": target_days, "label": wrong_label}
        records.append(NoiseRecord(
            app="Clock", record_type="alarm", params=params, similarity=p,
            setup_cmd=f"# inject alarm {params} via sqlite3 on alarm.db"
        ))
    return records


def noise_calendar_events(target_title, target_day, target_time, n=3) -> List[NoiseRecord]:
    records = []
    patterns = ["same_title_wrong_date", "same_date_wrong_time", "similar_title_same_date"]
    for i in range(min(n, len(patterns))):
        p = patterns[i]
        if p == "same_title_wrong_date":
            wrong_day = random.choice([d for d in C["day_offsets"] if d != target_day])
            params = {"title": target_title, "date": wrong_day, "time": target_time}
        elif p == "same_date_wrong_time":
            wrong_time = random.choice([t for t in C["work_times"] if t != target_time])
            params = {"title": pick("event_titles"), "date": target_day, "time": wrong_time}
        else:
            params = {"title": target_title + " (old)", "date": target_day, "time": target_time}
        records.append(NoiseRecord(
            app="Calendar", record_type="event", params=params, similarity=p,
            setup_cmd=f"# inject calendar event {params}"
        ))
    return records


def noise_notes(n=4) -> List[NoiseRecord]:
    records = []
    for i in range(n):
        word_count = random.choice(C["note_word_counts"])
        title = pick("note_titles") + f" (noise {i+1})"
        records.append(NoiseRecord(
            app="Notes", record_type="note",
            params={"title": title, "word_count": word_count},
            similarity="same_folder_different_length",
            setup_cmd=f"# inject note '{title}' with {word_count} words"
        ))
    return records


def noise_files(n=5) -> List[NoiseRecord]:
    records = []
    for i in range(n):
        size_kb = random.choice(C["file_sizes_kb"])
        fname = pick("file_names").replace(".txt", f"_{i+1}.txt")
        records.append(NoiseRecord(
            app="Files", record_type="file",
            params={"name": fname, "size_kb": size_kb},
            similarity="plausible_name_variable_size",
            setup_cmd=f"# create {fname} ({size_kb}KB) via simctl"
        ))
    return records


def noise_contacts(target_name, n=3) -> List[NoiseRecord]:
    parts = target_name.split()
    first = parts[0] if parts else target_name
    records = []
    for i in range(n):
        noise_last = pick("contact_last")
        records.append(NoiseRecord(
            app="Contacts", record_type="contact",
            params={"first": first, "last": noise_last,
                    "phone": f"415-555-{random.randint(1000,9999)}"},
            similarity="same_first_different_last",
            setup_cmd=f"# inject contact {first} {noise_last}"
        ))
    return records



def noise_layout(seed: int = None,
                 cross_page: bool = False,
                 distribute: Optional[bool] = None,
                 randomize_dock: Optional[bool] = None,
                 dock_count: Optional[int] = None) -> List[NoiseRecord]:
    """
    Randomize the SpringBoard layout. Returns NoiseRecord(s) describing
    the variability to apply via `sibb_randomize_layout.py` (the actual
    script runs before sim boot — see SpringboardHandler in sibb_state.py).

    Dimensions of variability:
      seed:           reproducibility (None → random per episode)
      cross_page:     shuffle apps across pages, not just within each page
      distribute:     also randomize *how many* apps land on each page
                      (None → 60% probability per episode)
      randomize_dock: pick a new dock count (1-4) and new contents
                      (None → 60% probability per episode)
      dock_count:     force a specific dock count (1-4) when randomize_dock
                      is True; None means random

    Emits up to two NoiseRecords: one for page layout, optionally one
    for the dock. Both feed into the SpringboardHandler dispatcher.
    """
    if seed is None:
        seed = random.randint(0, 999999)
    if distribute is None:
        distribute = random.random() < 0.6
    if randomize_dock is None:
        randomize_dock = random.random() < 0.6

    records = [NoiseRecord(
        app="SpringBoard",
        record_type="layout",
        params={
            "seed":       seed,
            "cross_page": cross_page,
            "distribute": distribute,
        },
        similarity="randomized_home_screen_layout",
        setup_cmd=(
            "# Shut down simulator first, then:"
            + f"\npython3 sibb_randomize_layout.py $UDID --seed {seed}"
            + (" --cross-page" if cross_page else "")
            + (" --distribute" if distribute else "")
            + "\n# Then boot:\nxcrun simctl boot $UDID"
        )
    )]
    if randomize_dock:
        dock_params: Dict[str, Any] = {"seed": seed}
        if dock_count is not None:
            dock_params["count"] = dock_count
        records.append(NoiseRecord(
            app="SpringBoard",
            record_type="dock",
            params=dock_params,
            similarity="randomized_dock_contents_and_count",
            setup_cmd=(
                f"python3 sibb_randomize_layout.py $UDID --seed {seed} "
                f"--randomize-dock"
                + (f" --dock-count {dock_count}" if dock_count is not None else "")
            )
        ))
    return records


# ─────────────────────────────────────────────────────────────────────────────
#  VERIFY BUILDERS  (structured, binary)
# ─────────────────────────────────────────────────────────────────────────────

def verify_alarm(time, days=None, label=None, sound=None, snooze=None) -> str:
    parts = [f"time={time}"]
    if days:
        days_str = ", ".join(days) if isinstance(days, list) else days
        parts.append(f"days={days_str}")
    if label:   parts.append(f"label='{label}'")
    if sound:   parts.append(f"sound='{sound}'")
    if snooze is not None: parts.append(f"snooze={'true' if snooze else 'false'}")
    return ("Clock alarm.db has entry: " + ", ".join(parts) + ". "
            "Binary: 1.0 if all fields match, 0.0 otherwise. "
            "Diff with pre-snapshot to exclude pre-existing alarms.")


def verify_calendar(title, day, time, calendar=None, alert=None, recur=None) -> str:
    parts = [f"title='{title}'", f"date={day}", f"time={time}"]
    if calendar: parts.append(f"calendar='{calendar}'")
    if alert:    parts.append(f"alert={alert}")
    if recur:    parts.append(f"recur={recur}")
    return ("EventKit SQLite has event: " + ", ".join(parts) + ". "
            "Diff with pre-snapshot: only newly created events counted.")


def verify_reminder(title, list_name, due=None, priority=None, tag=None) -> str:
    parts = [f"title='{title}'", f"list='{list_name}'"]
    if due:      parts.append(f"due={due}")
    if priority: parts.append(f"priority={priority}")
    if tag:      parts.append(f"tag='{tag}'")
    return ("RemindersDB has entry: " + ", ".join(parts) + ". "
            "Diff with pre-snapshot: only new reminder counted.")


def verify_note(title, folder=None, tag=None, locked=False) -> str:
    parts = [f"title='{title}'"]
    if folder: parts.append(f"folder='{folder}'")
    if tag:    parts.append(f"tag='{tag}'")
    if locked: parts.append("locked=true")
    return ("NoteStore.sqlite has entry: " + ", ".join(parts) + ". "
            "Diff with pre-snapshot: only new note counted.")


def verify_no_action(action_desc: str, agent_check: str) -> str:
    return (f"NEGATIVE verify: {action_desc} must NOT have occurred. "
            f"Diff with pre-snapshot: {agent_check} set is empty. "
            f"Agent output must contain FAIL action. "
            f"Binary: 1.0 iff no new record AND FAIL in agent log.")


def verify_constraint(app, field, min_val, max_val, entity=None) -> str:
    loc = f" in '{entity}'" if entity else ""
    return (f"{app}: {field}{loc} satisfies {min_val} ≤ {field} ≤ {max_val}. "
            f"Query DB post-task; diff ensures only agent-moved items counted. "
            f"Binary: 0.0 if out of range.")


def verify_partial_feasibility(doable: list, blocked: list) -> str:
    pos = "; ".join(doable)
    neg = "; ".join(blocked)
    return (f"POSITIVE checks (all must pass): {pos}. "
            f"NEGATIVE checks (all must be absent in diff): {neg}. "
            f"Agent log must contain FAIL action. "
            f"Binary: 1.0 only if ALL positive pass AND ALL negative absent AND FAIL present.")



def gen_clock_alarm(detail_level=None):
    if detail_level is None: detail_level = random.random()

    time  = MandatoryParam("time", pick("wake_times"))

    days  = OptionalParam("days", pick("alarm_days"), 0.7, 1).sample(detail_level)
    label = OptionalParam("label", pick("alarm_labels"), 0.6, 1).sample(detail_level)
    sound = OptionalParam("sound", pick("alarm_sounds"), 0.5, 1).sample(detail_level)
    snooze = OptionalParam("snooze", random.choice([True, False]), 0.4, 1).sample(detail_level)

    noise = noise_alarm(
        target_time=time.value,
        target_days=days.value if days.included else pick("alarm_days"),
        target_label=label.value if label.included else "Wake Up",
        n=random.randint(2, 3)
    )

    parts = [f"Open the Clock app. Create a new alarm at {time.value}."]
    if days.included:
        days_str = ", ".join(days.value) if isinstance(days.value, list) else days.value
        parts.append(f"Set it to repeat on: {days_str}.")
    if label.included: parts.append(f"Label it '{label.value}'.")
    if sound.included: parts.append(f"Set sound to '{sound.value}'.")
    if snooze.included:
        parts.append(f"{'Enable' if snooze.value else 'Disable'} snooze.")

    optional_checks = {
        "days":   days.value   if days.included   else None,
        "label":  label.value  if label.included  else None,
        "sound":  sound.value  if sound.included  else None,
        "snooze": snooze.value if snooze.included else None,
    }

    base_steps = 3 + sum(p.step_cost for p in [days, label, sound, snooze] if p.included)

    return Task(
        task_id="", flow="single_clock", apps=["Clock"],
        instruction=" ".join(parts),
        verify=verify_alarm(time.value, **{k: v for k, v in optional_checks.items() if v is not None}),
        verify_mode="db_query",
        initial_state=InitialState(
            absent=["No alarm at this exact time+days+label combination"],
            noise_records=noise,
            setup_commands=[r.setup_cmd for r in noise],
        ),
        steps=base_steps,
        complexity=complexity_score(base_steps, noise_count=len(noise)),
        detail_level=round(detail_level, 2),
        params={"time": time.value,
                **{k: v for k, v in optional_checks.items() if v is not None}},
    )


def gen_reminders_list(detail_level=None):
    if detail_level is None: detail_level = random.random()

    list_name  = MandatoryParam("list", pick("reminder_lists"))
    n          = pick("n_items")
    items      = pickn("work_items", n)
    list_state = random.choice(["exists", "absent"])

    prio_item = MandatoryParam("priority_item", random.choice(items))
    priority  = OptionalParam("priority", pick("priority_levels"), 0.7, 1).sample(detail_level)
    flag      = OptionalParam("flag", True, 0.5, 1).sample(detail_level)
    due_day   = OptionalParam("due", pick("day_offsets"), 0.6, 1).sample(detail_level)
    tag       = OptionalParam("tag", pick("note_tags"), 0.5, 1).sample(detail_level)

    items_str = "\n  • " + "\n  • ".join(items)
    list_prefix = ("Navigate to" if list_state == "exists"
                   else "Create a list called") + f" '{list_name.value}'."

    parts = [f"Open Reminders. {list_prefix} Add {n} tasks:{items_str}."]
    if priority.included:
        parts.append(f"Set '{prio_item.value}' to {priority.value} priority.")
    if flag.included:
        parts.append(f"Flag '{prio_item.value}'.")
    if due_day.included:
        parts.append(f"Set due date {due_day.value} on the last task.")
    if tag.included:
        parts.append(f"Tag the list '{tag.value}'.")

    noise_records = []
    if list_state == "exists":
        noise_records = [NoiseRecord(
            app="Reminders", record_type="list",
            params={"name": list_name.value, "items": []},
            similarity="list_pre_exists",
            setup_cmd=f"xcrun simctl spawn booted remindersctl create-list '{list_name.value}'"
        )]
    # Layout noise: randomize home screen so agent must navigate by label
    if random.random() < 0.7:   # 70% of tasks get layout noise
        noise_records += noise_layout(cross_page=random.random() < 0.3)

    # ── Structured state spec consumed by the state framework ──────────────
    # When list_state == "exists", the list must be present before the agent
    # starts; that's the only initial state for this task type. Items are
    # always the agent's job to create.
    spec: List[Dict[str, Any]] = []
    if list_state == "exists":
        spec.append({"app": "Reminders", "type": "list",
                     "name": list_name.value})
    # Springboard noise records ("layout" + optional "dock") flow through
    # as structured spec entries. Each becomes an app-state-handler entry
    # that the state framework realizes before the runner attaches.
    for nr in noise_records:
        if nr.app != "SpringBoard":
            continue
        if nr.record_type == "layout":
            spec.append({
                "app":        "Springboard",
                "type":       "layout",
                "seed":       nr.params["seed"],
                "cross_page": nr.params.get("cross_page", False),
                "distribute": nr.params.get("distribute", False),
            })
        elif nr.record_type == "dock":
            entry = {
                "app":  "Springboard",
                "type": "dock",
                "seed": nr.params["seed"],
            }
            if "count" in nr.params:
                entry["count"] = nr.params["count"]
            spec.append(entry)

    base_steps = (3 + (2 if list_state == "absent" else 0) +
                  sum(p.step_cost for p in [priority, flag, due_day, tag] if p.included))

    return Task(
        task_id="", flow="single_reminders", apps=["Reminders"],
        instruction=" ".join(parts),
        verify=verify_reminder(
            title=f"{n} items in list",
            list_name=list_name.value,
            due=due_day.value if due_day.included else None,
            priority=priority.value if priority.included else None,
            tag=tag.value if tag.included else None,
        ),
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"Reminders list '{list_name.value}' exists"] if list_state == "exists" else [],
            absent=[f"Reminders list '{list_name.value}' does not exist"] if list_state == "absent" else [],
            noise_records=noise_records,
            setup_commands=[r.setup_cmd for r in noise_records],
            spec=spec,
            expected_behavior="create_then_complete" if list_state == "absent" else "complete",
        ),
        steps=base_steps,
        complexity=complexity_score(base_steps, noise_count=len(noise_records)),
        detail_level=round(detail_level, 2),
        params={
            "list": list_name.value,
            "items": items,
            "list_state": list_state,
            # Verifier-relevant fields — None if the optional was not included
            "priority_item":  prio_item.value if priority.included else None,
            "priority_level": priority.value  if priority.included else None,
            "flag_item":      prio_item.value if flag.included     else None,
            "due_day":        due_day.value   if due_day.included  else None,
            "tag":            tag.value       if tag.included      else None,
        },
    )


def gen_notes_in_folder(detail_level=None):
    if detail_level is None: detail_level = random.random()

    title        = MandatoryParam("title", pick("note_titles"))
    folder       = MandatoryParam("folder", pick("note_folders"))
    folder_state = random.choice(["exists", "absent"])

    n            = pick("n_items")
    items        = pickn("work_items", n)

    checklist = OptionalParam("checklist", items, 0.8, 2).sample(detail_level)
    n_check   = OptionalParam("n_check", random.randint(1, max(1, n-1)), 0.5, 1).sample(detail_level)
    table     = OptionalParam("table", True, 0.4, 2).sample(detail_level)
    tag       = OptionalParam("tag", pick("note_tags"), 0.6, 1).sample(detail_level)
    lock      = OptionalParam("lock", True, 0.3, 2).sample(detail_level)

    fold_action = ("Move to the existing" if folder_state == "exists"
                   else "Create a folder called") + f" '{folder.value}'."
    parts = [f"Open Notes. Create a new note titled '{title.value}'."]
    if checklist.included:
        items_str = ", ".join(f"'{i}'" for i in checklist.value)
        parts.append(f"Add a checklist with {n} items: {items_str}.")
        if n_check.included:
            checked = ", ".join(f"'{i}'" for i in items[:n_check.value])
            parts.append(f"Check off {n_check.value} items: {checked}.")
    if table.included:
        parts.append("Add a table with columns 'Owner' and 'Due Date' (2 rows).")
    if tag.included:
        parts.append(f"Tag it '{tag.value}'.")
    parts.append(fold_action)
    if lock.included:
        parts.append("Lock the note with a password.")

    base_steps = (3 + (2 if folder_state == "absent" else 0) +
                  sum(p.step_cost for p in [checklist, n_check, table, tag, lock] if p.included))

    return Task(
        task_id="", flow="single_notes", apps=["Notes"],
        instruction=" ".join(parts),
        verify=verify_note(
            title=title.value, folder=folder.value,
            tag=tag.value if tag.included else None,
            locked=lock.included and lock.value,
        ),
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"Notes folder '{folder.value}' exists"] if folder_state == "exists" else [],
            absent=[f"Notes folder '{folder.value}' does not exist"] if folder_state == "absent" else [],
            setup_commands=[f"# create folder '{folder.value}' if absent"] if folder_state == "exists" else [],
            expected_behavior="create_then_complete" if folder_state == "absent" else "complete",
        ),
        steps=base_steps,
        complexity=complexity_score(base_steps),
        detail_level=round(detail_level, 2),
        params={"title": title.value, "folder": folder.value, "folder_state": folder_state},
    )


# ─────────────────────────────────────────────────────────────────────────────
#  IMPOSSIBILITY TASKS  (fail_gracefully)
# ─────────────────────────────────────────────────────────────────────────────

def gen_contact_not_found():
    """
    Pure impossibility: contact doesn't exist, nothing is doable.
    Agent must output FAIL "reason".
    Verify: no new contact created, no call/message initiated, FAIL in log.
    """
    name   = pick("blocking_names")
    action = random.choice(["Call", "Message", "FaceTime", "Send an email to"])
    app    = {"Call": "Phone", "Message": "Messages",
              "FaceTime": "FaceTime", "Send an email to": "Mail"}[action]

    instruction = (f"Open {app}. {action} '{name}'.")

    return Task(
        task_id="", flow="impossibility_single",
        apps=[app],
        instruction=instruction,
        verify=verify_no_action(
            action_desc=f"{action} to '{name}'",
            agent_check=f"no new {app.lower()} thread/call initiated with '{name}'"
        ),
        verify_mode="db_and_agent",
        initial_state=InitialState(
            blocking=[f"Contact '{name}' does NOT exist in address book"],
            expected_behavior="fail_gracefully",
        ),
        steps=3,
        complexity=complexity_score(3, is_impossibility=True),
        detail_level=1.0,
        params={"name": name, "action": action},
        agent_check="FAIL",
        positive_checks=[],
        negative_checks=[
            f"diff['contacts']: no new entry containing '{name}'",
            f"diff['{app.lower()}']: no new thread/call to '{name}'",
        ],
    )


def gen_partial_feasibility_blocking():
    """
    Partial feasibility: Maps + Calendar are doable; Messages is blocked
    because the contact doesn't exist.
    Agent must complete Maps + Calendar, then output FAIL for the Messages step.
    """
    place   = pick("maps_places")
    day     = pick("day_offsets")
    time    = random.choice(["7:00 PM", "7:30 PM", "8:00 PM", "12:00 PM"])
    contact = pick("blocking_names")
    event   = f"Meet at {place.split(',')[0]}"
    cal     = pick("calendar_names")

    instruction = (
        f"1. MAPS: Search for '{place}'. Save it as a pin in Places.\n\n"
        f"2. CALENDAR: Create an event '{event}' on {day} at {time} in '{cal}' "
        f"at that location.\n\n"
        f"3. MESSAGES: Share the event invite to '{contact}' via Messages."
    )

    return Task(
        task_id="", flow="impossibility_partial",
        apps=["Maps", "Calendar", "Messages"],
        instruction=instruction,
        verify=verify_partial_feasibility(
            doable=[
                f"Maps Places has pin for '{place}'",
                f"Calendar '{cal}' has event='{event}' on {day} at {time}",
            ],
            blocked=[
                f"diff['contacts']: no new entry for '{contact}'",
                f"diff['messages']: no new thread with '{contact}'",
            ],
        ),
        verify_mode="db_and_agent",
        initial_state=InitialState(
            blocking=[f"Contact '{contact}' does NOT exist — Messages step impossible"],
            expected_behavior="fail_gracefully",
        ),
        steps=12,
        complexity=complexity_score(12, n_apps=3, is_impossibility=True),
        detail_level=1.0,
        params={"place": place, "day": day, "time": time,
                "contact": contact, "event": event, "cal": cal},
        agent_check="FAIL",
        positive_checks=[
            f"Maps Places has pin for '{place}'",
            f"Calendar event '{event}' exists",
        ],
        negative_checks=[
            f"No new contact '{contact}'",
            f"No new Messages thread with '{contact}'",
        ],
    )


def gen_fetch_not_found():
    """
    Fetch impossibility: event doesn't exist; agent must report 'not found'
    without fabricating a time.
    """
    event_name = pick("event_titles")
    instruction = (
        f"Open the Calendar app. Find the '{event_name}' appointment. "
        f"Report when it is scheduled."
    )
    return Task(
        task_id="", flow="impossibility_fetch",
        apps=["Calendar"],
        instruction=instruction,
        verify=(
            f"Agent reports: no event named '{event_name}' found. "
            f"Agent does NOT state any time or date (no fabrication). "
            f"diff['calendar']: no new event created. "
            f"Agent log contains FAIL action. Binary: 1.0 only if all hold."
        ),
        verify_mode="db_and_agent",
        initial_state=InitialState(
            blocking=[f"No '{event_name}' event exists in Calendar"],
            expected_behavior="fail_gracefully",
        ),
        steps=4,
        complexity=complexity_score(4, is_impossibility=True),
        detail_level=1.0,
        params={"event_name": event_name},
        agent_check="FAIL",
        positive_checks=[],
        negative_checks=[
            f"No new calendar event titled '{event_name}'",
            "Agent output contains no time/date pattern (no fabrication)",
        ],
    )


# ─────────────────────────────────────────────────────────────────────────────
#  AMBIGUOUS TASKS  (CLARIFY tool)
# ─────────────────────────────────────────────────────────────────────────────

def gen_ambiguous_contact_missing_phone():
    """
    Missing value: instruction says 'Add Greg', but phone number not provided.
    Agent must CLARIFY missing_value "contact_phone" context="Greg" to get it.
    """
    raw    = pick("blocking_names")
    # Use only if it's a simple single-token name; otherwise pick a simple name
    first  = raw if " " not in raw else pick("contact_first")
    last   = pick("contact_last")
    phone  = f"415-555-{random.randint(1000, 9999)}"
    name   = f"{first} {last}" if first not in ("Mom","Dad","Sister","Brother") else first

    return Task(
        task_id="", flow="ambiguous_missing_value",
        apps=["Contacts"],
        instruction=f"Add {name} to my contacts.",
        verify=(
            f"Contacts has '{name}' with phone={phone}. "
            f"ClarifyLog: slot 'contact_phone' was answered. "
            f"Binary: 1.0 if contact exists with correct phone AND clarify was used."
        ),
        verify_mode="db_and_agent",
        initial_state=InitialState(
            absent=[f"Contact '{name}' does not yet exist"],
            expected_behavior="complete",
        ),
        steps=8,
        complexity=complexity_score(8, requires_clarification=True),
        detail_level=1.0,
        params={"name": name, "phone": phone},
        clarify_slots=[
            ClarifySlot(
                question_type="missing_value",
                slot_name="contact_phone",
                example_questions=[
                    f"What is {name}'s phone number?",
                    f"What number can I reach {first} at?",
                    f"What is {first}'s mobile number?",
                    f"How do I call {first}?",
                    f"What is the phone number for {name}?",
                ],
                answer=phone,
                answer_format="phone",
                required=True,
            ),
            ClarifySlot(
                question_type="missing_value",
                slot_name="contact_last_name",
                example_questions=[
                    f"What is {first}'s last name?",
                    f"What is {first}'s surname?",
                    f"What family name does {first} use?",
                    f"What last name should I enter for {first}?",
                    f"What surname goes with {first}?",
                ],
                answer=last,
                answer_format="string",
                required=False,
            ),
        ],
    )


def gen_ambiguous_which_note():
    """
    Disambiguation: 'Move the meeting note to Work' but 3 notes have 'meeting' in title.
    Agent must navigate to Notes, find the candidates, then call
    CLARIFY disambiguate "which_note" options="Meeting A,Meeting B,Meeting C"
    """
    correct = "Budget Meeting"
    wrong1  = "Team Meeting"
    wrong2  = "Meeting with Alex"
    folder  = "Work"

    # Noise: inject all three notes in the source folder before task
    noise = [
        NoiseRecord(app="Notes", record_type="note",
                    params={"title": t, "folder": "Personal"},
                    similarity="same_keyword_different_content",
                    setup_cmd=f"# inject note '{t}'")
        for t in [correct, wrong1, wrong2]
    ]

    return Task(
        task_id="", flow="ambiguous_disambiguate",
        apps=["Notes"],
        instruction=f"Move the meeting note to the '{folder}' folder.",
        verify=(
            f"Note '{correct}' is in folder='{folder}'. "
            f"Notes '{wrong1}' and '{wrong2}' are NOT in '{folder}'. "
            f"ClarifyLog: disambiguate slot 'which_note' was answered. "
            f"Binary: all conditions must hold."
        ),
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"Notes '{correct}', '{wrong1}', '{wrong2}' exist in Personal"],
            noise_records=noise,
            setup_commands=[r.setup_cmd for r in noise],
            expected_behavior="complete",
        ),
        steps=9,
        complexity=complexity_score(9, noise_count=3, requires_clarification=True),
        detail_level=1.0,
        params={"correct_note": correct, "wrong_notes": [wrong1, wrong2],
                "target_folder": folder},
        clarify_slots=[
            ClarifySlot(
                question_type="disambiguate",
                slot_name="which_note",
                example_questions=[
                    "Which note should I move to the Work folder?",
                    "Which meeting note do you want moved?",
                    f"Should I move {correct}, {wrong1}, or {wrong2}?",
                    "Which of the meeting notes is the right one?",
                    "Which note are you referring to?",
                ],
                answer=correct,
                answer_format="index",
                valid_options=[correct, wrong1, wrong2],
                required=True,
            ),
        ],
    )


def gen_ambiguous_vague_instruction():
    """
    Constraint clarification: 'Move some files to Archive' — 'some' is undefined.
    Agent must ask for the constraint before acting.
    """
    min_kb = random.choice([75, 100, 125, 150])
    max_kb = min_kb + random.choice([50, 75, 100])

    noise = noise_files(n=6)
    noise_sizes = [r.params["size_kb"] for r in noise]

    return Task(
        task_id="", flow="ambiguous_constraint",
        apps=["Files"],
        instruction="Move some of the documents to the Archive folder.",
        verify=verify_constraint("Files", "total_size_kb",
                                 min_kb, max_kb, entity="Archive"),
        verify_mode="db_query",
        initial_state=InitialState(
            present=["Files has 6 documents in Documents folder"],
            noise_records=noise,
            setup_commands=[r.setup_cmd for r in noise],
            expected_behavior="complete",
        ),
        steps=10,
        complexity=complexity_score(10, has_constraint=True,
                                    noise_count=6, requires_clarification=True),
        detail_level=1.0,
        params={"min_kb": min_kb, "max_kb": max_kb,
                "available_sizes_kb": noise_sizes},
        clarify_slots=[
            ClarifySlot(
                question_type="constraint",
                slot_name="file_size_range",
                example_questions=[
                    "What should the total file size of the moved files be?",
                    "How large should the total of the selected files be?",
                    "What size constraint should I follow when selecting files?",
                    "How many kilobytes should the files I move total?",
                    "What is the total size requirement for the archive?",
                ],
                answer=(min_kb, max_kb),
                answer_format="range",
                required=True,
            ),
        ],
    )


# ─────────────────────────────────────────────────────────────────────────────
#  CONSTRAINT SATISFACTION TASKS
# ─────────────────────────────────────────────────────────────────────────────

def gen_files_size_range():
    """
    Move files into a folder where total file size is between min_kb and max_kb.
    Constraint satisfaction: agent must read file sizes and select a valid subset.
    """
    min_kb = random.choice([75, 100, 125, 150, 175])
    max_kb = min_kb + random.choice([50, 75, 100])
    folder = pick("folder_names")
    folder_state = random.choice(["exists", "absent"])

    # Pre-create 8–10 files with known sizes
    n_files = random.randint(8, 10)
    noise   = noise_files(n=n_files)
    sizes   = [r.params["size_kb"] for r in noise]

    instruction = (
        f"In the Files app, go to the Documents folder. "
        f"Select files and move them to {'the existing' if folder_state == 'exists' else 'a new'} "
        f"'{folder}' folder such that the total size of all moved files is "
        f"between {min_kb} KB and {max_kb} KB."
    )

    return Task(
        task_id="", flow="constraint_files_size",
        apps=["Files"],
        instruction=instruction,
        verify=verify_constraint("Files", "total_size_kb", min_kb, max_kb, entity=folder),
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"Files folder '{folder}' exists"] if folder_state == "exists" else [],
            absent=[f"Files folder '{folder}' does not exist"] if folder_state == "absent" else [],
            noise_records=noise,
            setup_commands=(
                [f"# create folder '{folder}'"] if folder_state == "exists" else []
            ) + [r.setup_cmd for r in noise],
            expected_behavior="create_then_complete" if folder_state == "absent" else "complete",
        ),
        steps=12 + (2 if folder_state == "absent" else 0),
        complexity=complexity_score(12, has_constraint=True, noise_count=n_files),
        detail_level=1.0,
        params={"min_kb": min_kb, "max_kb": max_kb, "folder": folder,
                "folder_state": folder_state, "available_sizes_kb": sizes},
    )


def gen_notes_word_range():
    """
    Move notes from a source folder to a target folder such that total word count
    of moved notes is between min_words and max_words.
    Agent must read note contents to estimate word counts before selecting.
    """
    min_words = random.choice([150, 200, 250, 300])
    max_words = min_words + random.choice([50, 75, 100])
    source    = pick("note_folders")
    target    = random.choice([f for f in C["note_folders"] if f != source])
    n_notes   = random.randint(7, 10)

    noise = noise_notes(n=n_notes)
    word_counts = [r.params["word_count"] for r in noise]

    instruction = (
        f"In the Notes app, some notes are in the '{source}' folder. "
        f"Move a selection of them to the '{target}' folder such that "
        f"the total word count of all moved notes is between "
        f"{min_words} and {max_words} words."
    )

    return Task(
        task_id="", flow="constraint_notes_words",
        apps=["Notes"],
        instruction=instruction,
        verify=(
            f"NoteStore.sqlite: notes in '{target}' folder that were moved during "
            f"this task (diff with pre-snapshot) have total word count satisfying "
            f"{min_words} ≤ total_words ≤ {max_words}. Binary."
        ),
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"Notes folder '{source}' has {n_notes} notes with known word counts",
                     f"Notes folder '{target}' exists (may be empty)"],
            noise_records=noise,
            setup_commands=[r.setup_cmd for r in noise],
            expected_behavior="complete",
        ),
        steps=14,
        complexity=complexity_score(14, has_constraint=True, noise_count=n_notes),
        detail_level=1.0,
        params={"min_words": min_words, "max_words": max_words,
                "source": source, "target": target,
                "available_word_counts": word_counts},
    )


def gen_calendar_duration_range():
    """
    Move a subset of events to a specific calendar such that total meeting
    time (sum of durations) is between min_mins and max_mins.
    """
    min_mins = random.choice([60, 90, 120, 150])
    max_mins = min_mins + random.choice([30, 45, 60])
    source_cal = "Personal"
    target_cal = "Work"
    n_events   = random.randint(6, 9)

    # Pre-create events with varying durations
    durations = random.choices([30, 45, 60, 75, 90, 120], k=n_events)
    noise = [
        NoiseRecord(
            app="Calendar", record_type="event",
            params={"title": pick("event_titles"), "duration_mins": d,
                    "calendar": source_cal},
            similarity="same_calendar_variable_duration",
            setup_cmd=f"# inject {d}min event in {source_cal}"
        )
        for d in durations
    ]

    instruction = (
        f"In the Calendar app, you have several events in the '{source_cal}' calendar. "
        f"Move a selection of them to the '{target_cal}' calendar such that "
        f"the total duration of all moved events is between "
        f"{min_mins} and {max_mins} minutes."
    )

    return Task(
        task_id="", flow="constraint_calendar_duration",
        apps=["Calendar"],
        instruction=instruction,
        verify=verify_constraint("Calendar", "total_duration_mins",
                                 min_mins, max_mins, entity=target_cal),
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"Calendar '{source_cal}' has {n_events} events with known durations"],
            noise_records=noise,
            setup_commands=[r.setup_cmd for r in noise],
            expected_behavior="complete",
        ),
        steps=12,
        complexity=complexity_score(12, has_constraint=True, noise_count=n_events),
        detail_level=1.0,
        params={"min_mins": min_mins, "max_mins": max_mins,
                "source_cal": source_cal, "target_cal": target_cal,
                "available_durations": durations},
    )


# ─────────────────────────────────────────────────────────────────────────────
#  FLOW R — FETCH/READ
# ─────────────────────────────────────────────────────────────────────────────

def gen_fetch_calendar_events():
    day     = pick("day_offsets")
    n_ev    = random.randint(1, 4)
    events  = [{"title": pick("event_titles"), "time": pick("work_times")}
               for _ in range(n_ev)]
    titles  = "; ".join(f"{e['title']} at {e['time']}" for e in events)

    noise_ev = noise_calendar_events(
        target_title="(noise)", target_day=day,
        target_time=pick("work_times"), n=2
    )

    return Task(
        task_id="", flow="R_fetch", apps=["Calendar"],
        instruction=(f"Open Calendar. List all events scheduled for {day}. "
                     f"Report each event's title and time."),
        verify=(f"Agent response lists exactly {n_ev} event(s) for {day}: {titles}. "
                f"Each title and time matches pre-populated events exactly. "
                f"verify_mode=agent_response."),
        verify_mode="agent_response",
        initial_state=InitialState(
            present=[f"Calendar pre-populated with {n_ev} events on {day}: {titles}"],
            noise_records=noise_ev,
            setup_commands=(
                [f"# inject event '{e['title']}' on {day} at {e['time']}'" for e in events] +
                [r.setup_cmd for r in noise_ev]
            ),
            expected_behavior="complete",
        ),
        steps=2 + n_ev,
        complexity=complexity_score(2 + n_ev),
        detail_level=1.0,
        params={"day": day, "events": events},
    )


# ─────────────────────────────────────────────────────────────────────────────
#  FLOW U — UPDATE
# ─────────────────────────────────────────────────────────────────────────────

def gen_update_contact_phone():
    first  = pick("contact_first")
    last   = pick("contact_last")
    name   = f"{first} {last}" if first not in ("Mom","Dad") else first
    old_ph = f"650-555-{random.randint(1000,9999)}"
    new_ph = f"415-555-{random.randint(1000,9999)}"
    while new_ph == old_ph: new_ph = f"415-555-{random.randint(1000,9999)}"

    noise = noise_contacts(name, n=2)

    return Task(
        task_id="", flow="U_update", apps=["Contacts"],
        instruction=(f"Open Contacts. Find '{name}'. "
                     f"Edit their phone number to {new_ph}. Save."),
        verify=(f"Contacts has '{name}' with phone={new_ph}. "
                f"Old value {old_ph} is no longer present (diff confirms). "
                f"Binary."),
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"Contact '{name}' exists with phone={old_ph}"],
            noise_records=noise,
            setup_commands=(
                [f"xcrun simctl spawn booted contactsctl create-contact "
                 f"--first '{first}' --last '{last}' --phone '{old_ph}'"] +
                [r.setup_cmd for r in noise]
            ),
            teardown_commands=[f"# delete contact '{name}'"],
            expected_behavior="complete",
        ),
        steps=7,
        complexity=complexity_score(7, noise_count=len(noise)),
        detail_level=1.0,
        params={"name": name, "old_phone": old_ph, "new_phone": new_ph},
    )


def gen_update_calendar_time():
    event_name = pick("event_titles")
    day        = pick("day_offsets")
    old_time   = pick("work_times")
    new_time   = random.choice([t for t in C["work_times"] if t != old_time])

    noise = noise_calendar_events(event_name, day, old_time, n=2)

    return Task(
        task_id="", flow="U_update", apps=["Calendar"],
        instruction=(f"Open Calendar. Find '{event_name}' on {day}. "
                     f"Change its time from {old_time} to {new_time}. Save."),
        verify=(f"EventKit has '{event_name}' on {day} with time={new_time}. "
                f"Old time {old_time} no longer set (diff confirms). Binary."),
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"Calendar event '{event_name}' on {day} at {old_time}"],
            noise_records=noise,
            setup_commands=(
                [f"xcrun simctl spawn booted calendarctl create-event "
                 f"--title '{event_name}' --date '{day}' --time '{old_time}'"] +
                [r.setup_cmd for r in noise]
            ),
            expected_behavior="complete",
        ),
        steps=7,
        complexity=complexity_score(7, noise_count=len(noise)),
        detail_level=1.0,
        params={"event": event_name, "day": day,
                "old_time": old_time, "new_time": new_time},
    )


# ─────────────────────────────────────────────────────────────────────────────
#  RUNNER
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
#  Multi-app generators — Phase 2b B4 onward
# ─────────────────────────────────────────────────────────────────────────────
#
# These return Task with `apps` set to multiple entries and `spec`
# containing entries for every touched app. `SymbolicRef` from
# `sibb_refs` ensures values that must agree across positions
# (e.g. the reminder title and the calendar event title) are sourced
# from one place. The dispatcher resolves refs before sending entries
# to handlers.

def gen_reminder_with_calendar_event(detail_level=None):
    """Cross-app: a reminder in Personal AND a matching calendar event.

    The agent's job (defined by the instruction string) is to read a
    reminder from a list and create a calendar event with the same
    title at the same time. Setup populates the Reminders side; the
    verifier asserts both the reminder and the calendar event exist
    with matching titles.

    Uses SymbolicRef to guarantee the spec entry's `title`, the
    verify_checks selector's `title`, and the instruction template
    are all sourced from the same value.
    """
    from sibb_refs import SymbolicRef

    if detail_level is None:
        detail_level = random.random()

    list_name  = pick("reminder_lists")
    item_title = random.choice(pick("work_items") if isinstance(C["work_items"], list)
                                else [C["work_items"]])
    # Calendar event date: a future date string + time.
    day_offset = random.randint(1, 7)
    hour       = random.choice([9, 10, 11, 13, 14, 15, 16])

    # Compute ISO8601 wall-clock; Swift's parseISODate handles
    # tz-less strings as local time on the device.
    from datetime import datetime, timedelta
    start_dt = (datetime.now()
                .replace(hour=hour, minute=0, second=0, microsecond=0)
                + timedelta(days=day_offset))
    end_dt = start_dt + timedelta(hours=1)
    start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%S")
    end_iso   = end_dt.strftime("%Y-%m-%dT%H:%M:%S")

    # Single source of truth — the same SymbolicRef instance flows
    # into the spec, the verify_checks selector, and the instruction.
    title_ref = SymbolicRef("event_title", item_title)

    spec: List[Dict[str, Any]] = [
        {"app": "Reminders", "type": "list", "name": list_name},
        {"app": "Reminders", "type": "item",
         "list": list_name, "title": title_ref},
    ]

    verify_checks: List[Dict[str, Any]] = [
        {"kind": "exists", "resource": "reminders.lists",
         "selector": {"name": list_name},
         "label": f"List '{list_name}' present",
         "severity": "blocking"},
        {"kind": "exists", "resource": "reminders.items",
         "selector": {"list": list_name, "title": title_ref},
         "label": f"Reminder '{title_ref.value}' present in '{list_name}'",
         "severity": "blocking"},
        {"kind": "exists", "resource": "calendar.events",
         "selector": {"title": title_ref},
         "label": f"Calendar event '{title_ref.value}' created",
         "severity": "blocking"},
    ]

    day_label = start_dt.strftime("%A %B %d")
    time_label = start_dt.strftime("%I:%M %p").lstrip("0")
    instruction = (
        f"Open Reminders. In the '{list_name}' list there is a task "
        f"titled '{title_ref.value}'. Create a calendar event with "
        f"the same title for {day_label} at {time_label}."
    )

    return Task(
        task_id="", flow="reminder_to_calendar",
        apps=["Reminders", "Calendar"],
        instruction=instruction,
        verify=verify_calendar(
            title=title_ref.value,
            day=day_label,
            time=time_label,
        ),
        verify_mode="db_query",
        initial_state=InitialState(
            present=[
                f"Reminders list '{list_name}' exists",
                f"Reminders item '{title_ref.value}' exists in '{list_name}'",
            ],
            absent=[f"Calendar event '{title_ref.value}' does not exist"],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=4,
        complexity=complexity_score(4, n_apps=2),
        detail_level=round(detail_level, 2),
        params={
            "list": list_name,
            "title": title_ref.value,
            "start_iso": start_iso,
            "end_iso":   end_iso,
        },
        verify_checks=verify_checks,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  TIER 1 — single-action Reminders tasks (Phase 2 systematic task design)
#  Each generator produces:
#    • An InitialState.spec with (a) one Reminders list + items, and
#      (b) Springboard layout/dock randomization so the agent must
#      navigate by app label rather than memorizing a fixed home-screen
#      position. The pre-runner re-applies layout on every replay.
#    • A verify_checks list consumed by sibb_verify.run_checks. Strict
#      blocking checks: target attribute changed AS REQUESTED, *and*
#      every other item in the list unchanged — count guards catch
#      adds/deletes, subset(title) catches renames, and a
#      count(completed=True) guard catches side-effect completions.
#  Difficulty: easy. Each task is 1-3 agent actions. Goal: build verifier
#  confidence and prove the generic-verifier loop end-to-end.
# ─────────────────────────────────────────────────────────────────────────────

# EventKit priority encoding: handler accepts strings ("high"/"medium"/"low"/
# "none"); fetcher returns the canonical EventKit integer. Verifier checks
# the integer because that's what reminders.items emits.
_PRIORITY_STR_TO_INT = {"high": 1, "medium": 5, "low": 9, "none": 0, None: 0}


# Weekday names used by _resolve_due for "next Monday" / "this Friday" /
# etc. The names are spelled exactly as iOS Reminders renders them so the
# agent sees the same word in both prompt and UI.
_WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday",
                   "Friday", "Saturday", "Sunday"]

# Hours of day we sample for time-of-day reminders. Skips wee-hours
# (1-7 AM) and very late (>10 PM) — humans don't typically set reminders
# at those times, so the corpus stays realistic.
_TIME_OF_DAY_HOURS = [9, 11, 12, 14, 15, 17, 19, 20]

# Plausible past-date offsets for "overdue" tasks (Tier 4 #12).
_OVERDUE_DAYS_AGO = [1, 2, 3, 5, 7, 10]


def _resolve_due(*, allow_today: bool = True,
                  now: Optional[_dt.datetime] = None
                  ) -> Tuple[str, str]:
    """Sample a random future due-date and return (iso, human_phrasing).

    Strategies, each with one or more variants:
      • today / tomorrow / day after tomorrow
      • a specific upcoming weekday ("next Friday")
      • in N days (2 / 3 / 5)
      • in 1 week / 2 weeks
      • next month (~30 days)

    Date-only vs date+time is decided by a coin flip per call:
      • Date-only ISO → "YYYY-MM-DD"
      • Date+time ISO → "YYYY-MM-DDTHH:MM:SS" (local, no Z)

    iOS Reminders accepts due dates/times in the past (an immediately-
    overdue reminder is valid), so we don't filter by current-time
    feasibility — any hour from the time-of-day pool is fair game even
    on "today". `allow_today=False` forces a future date for tasks
    where "today" makes no sense (e.g., change-due-date where the
    start state already used today).
    """
    now = now or _dt.datetime.now()
    today = now.date()

    # Pick a date strategy. Weights bias toward common cases.
    strategies = (["today"] if allow_today else []) + [
        "tomorrow", "day_after_tomorrow",
        "specific_weekday", "specific_weekday",
        "in_n_days", "in_n_days",
        "in_1_week", "in_2_weeks",
        "next_month",
    ]
    strategy = random.choice(strategies)

    # Resolve to a date + a human-friendly date phrase.
    if strategy == "today":
        target_date = today
        date_phrase = "today"
    elif strategy == "tomorrow":
        target_date = today + _dt.timedelta(days=1)
        date_phrase = "tomorrow"
    elif strategy == "day_after_tomorrow":
        target_date = today + _dt.timedelta(days=2)
        date_phrase = "the day after tomorrow"
    elif strategy == "specific_weekday":
        wd_name = random.choice(_WEEKDAY_NAMES)
        wd_idx  = _WEEKDAY_NAMES.index(wd_name)
        days_ahead = (wd_idx - today.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7   # "next Monday" never means "today"
        target_date = today + _dt.timedelta(days=days_ahead)
        # "next" reads naturally for 1-7 days out; days >=8 would be
        # "next next Monday" which sounds wrong, but days_ahead in this
        # branch is always in [1,7].
        date_phrase = f"next {wd_name}"
    elif strategy == "in_n_days":
        n = random.choice([2, 3, 5])
        target_date = today + _dt.timedelta(days=n)
        date_phrase = f"in {n} days"
    elif strategy == "in_1_week":
        target_date = today + _dt.timedelta(days=7)
        date_phrase = "in one week"
    elif strategy == "in_2_weeks":
        target_date = today + _dt.timedelta(days=14)
        date_phrase = "in two weeks"
    else:  # next_month
        n = random.randint(28, 33)
        target_date = today + _dt.timedelta(days=n)
        date_phrase = "next month"

    # Coin flip: half the time date-only, half the time date+time.
    # No filtering on whether the resulting time is in the past — iOS
    # accepts past due-times (they show as overdue).
    want_time = random.random() < 0.5
    if want_time:
        hour = random.choice(_TIME_OF_DAY_HOURS)
        iso = f"{target_date.isoformat()}T{hour:02d}:00:00"
        hour_12 = ((hour - 1) % 12) + 1
        ampm = "AM" if hour < 12 else "PM"
        # ":00" reads more naturally as "5 PM" than "5:00 PM" for
        # whole-hour times.
        time_phrase = f" at {hour_12} {ampm}"
    else:
        iso = target_date.isoformat()    # "YYYY-MM-DD"
        time_phrase = ""

    return iso, f"{date_phrase}{time_phrase}"


def _past_iso(days_ago: int,
              *, now: Optional[_dt.datetime] = None,
              with_time: bool = False) -> str:
    """ISO string for `days_ago` calendar days before today. Date-only
    by default; date+time if `with_time` is set (used by overdue tasks
    that want time-of-day distractors)."""
    now = now or _dt.datetime.now()
    d = (now.date() - _dt.timedelta(days=days_ago))
    if with_time:
        h = random.choice(_TIME_OF_DAY_HOURS)
        return f"{d.isoformat()}T{h:02d}:00:00"
    return d.isoformat()


def _reminders_spec(list_name: str,
                     items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build a Reminders InitialState.spec list: one `list` entry plus one
    `item` entry per item. Each `items` dict is a kwargs bundle for
    `RemindersItem` (title required; priority/completed/due_iso/notes/
    url/recurrence optional)."""
    spec: List[Dict[str, Any]] = [
        {"app": "Reminders", "type": "list", "name": list_name},
    ]
    for it in items:
        entry: Dict[str, Any] = {
            "app": "Reminders", "type": "item",
            "list": list_name, "title": it["title"],
        }
        for k in ("priority", "completed", "due_iso",
                  "notes", "url", "recurrence"):
            if it.get(k) is not None:
                entry[k] = it[k]
        spec.append(entry)
    return spec


def _springboard_noise(*, cross_page_prob: float = 0.3,
                         distribute_prob: float = 0.6,
                         dock_prob: float = 0.6,
                         start_page_choices=(0, 1, 2)
                         ) -> List[Dict[str, Any]]:
    """Springboard layout + optional dock randomization + random start
    page. Every Tier 1 task ships this so the agent has to find
    Reminders by app label (a) on a shuffled home screen and (b)
    starting from a page that isn't always 0. Mirrors `noise_layout()`
    defaults but emits typed spec entries directly so the generator
    doesn't need to post-process NoiseRecord objects."""
    seed = random.randint(0, 999_999)
    entries: List[Dict[str, Any]] = [{
        "app": "Springboard", "type": "layout",
        "seed": seed,
        "cross_page": random.random() < cross_page_prob,
        "distribute": random.random() < distribute_prob,
    }]
    if random.random() < dock_prob:
        entries.append({
            "app": "Springboard", "type": "dock", "seed": seed,
        })
    # start_page is a runtime apply (post-boot swipe), not pre_runner.
    # It lands the user on a random page after SpringBoard boots. iOS
    # stops at the rightmost page if we over-swipe, so page=2 is safe
    # even on a 2-page layout.
    entries.append({
        "app": "Springboard", "type": "start_page",
        "page": random.choice(start_page_choices),
    })
    return entries


def _no_irrelevant_edits_checks(list_name: str, n_items: int,
                                  *, completed_count: int = 0,
                                  priority_zero_count: Optional[int] = None
                                  ) -> List[Dict[str, Any]]:
    """Standard "no irrelevant edits" check block.

    Asserts that, in the named list:
      • count of completed items == `completed_count`  (default 0)
      • count of priority=0 items == `priority_zero_count`
          (default = n_items, i.e. nobody's priority changed)
      • no items have notes / due / url / recurrence anywhere in the list

    Every Tier 1/2/3 task ships these as blocking checks so the agent
    can't sneak in side-effect edits (adding notes to the target,
    bumping a sibling's priority, attaching a URL, attaching a
    recurrence rule, etc.) and still score 1.0.

    For tasks that DELIBERATELY set one of these fields (Tier 4 due,
    Tier 4 notes, Tier 4b recurrence), the helper is NOT used; those
    generators emit explicit per-field guards inline.
    """
    if priority_zero_count is None:
        priority_zero_count = n_items
    return [
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "completed": True},
         "op": "eq", "n": completed_count,
         "severity": "blocking",
         "label": f"exactly {completed_count} completed in '{list_name}'"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "priority": 0},
         "op": "eq", "n": priority_zero_count,
         "severity": "blocking",
         "label": f"exactly {priority_zero_count} items at priority=none "
                  f"in '{list_name}'"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "notes": None},
         "op": "eq", "n": n_items,
         "severity": "blocking",
         "label": f"no items in '{list_name}' have notes attached"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "due": None},
         "op": "eq", "n": n_items,
         "severity": "blocking",
         "label": f"no items in '{list_name}' have a due date"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "url": None},
         "op": "eq", "n": n_items,
         "severity": "blocking",
         "label": f"no items in '{list_name}' have a URL"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "recurrence": None},
         "op": "eq", "n": n_items,
         "severity": "blocking",
         "label": f"no items in '{list_name}' have a recurrence rule"},
    ]


def gen_complete_specific_reminder():
    """Mark one specific reminder as completed. Strict: every sibling
    must stay incomplete, no items added/deleted, no titles changed,
    no notes/url/due/priority added anywhere."""
    list_name = pick("reminder_lists")
    n         = random.choice([3, 4, 5])
    titles    = pickn("work_items", n)
    target    = random.choice(titles)

    spec = _reminders_spec(list_name, [
        {"title": t, "completed": False} for t in titles
    ])
    spec += _springboard_noise()

    # Target title is globally unique (we only seed one user list with
    # unique titles), so half the time we omit the list name from the
    # instruction and the agent has to find the right list itself.
    instruction = random.choice([
        f"Open Reminders. In the '{list_name}' list, mark '{target}' "
        f"as completed.",
        f"Open Reminders. Mark '{target}' as completed.",
        f"Open Reminders. '{target}' is done — check it off.",
    ])

    verify_checks = [
        # The requested change.
        {"kind": "attribute_eq",
         "resource": "reminders.items",
         "selector": {"list": list_name, "title": target},
         "attr": "completed", "value": True,
         "severity": "blocking",
         "label": f"'{target}' is completed"},
        # Strict: list size unchanged. Catches add/delete.
        {"kind": "count",
         "resource": "reminders.items",
         "selector": {"list": list_name},
         "op": "eq", "n": n,
         "severity": "blocking",
         "label": f"'{list_name}' has exactly {n} items"},
        # Strict: every original title still present. Catches rename or
        # delete-and-replace.
        {"kind": "subset",
         "resource": "reminders.items",
         "selector": {"list": list_name},
         "key": "title", "expected": titles,
         "severity": "blocking",
         "label": f"all {n} original titles still in '{list_name}'"},
        # Strict "no irrelevant edits": exactly 1 completed, 0 priority
        # changes, no notes/url/due_date added anywhere.
        *_no_irrelevant_edits_checks(list_name, n, completed_count=1),
    ]

    return Task(
        task_id="", flow="single_reminders_complete",
        apps=["Reminders"],
        instruction=instruction,
        verify=f"reminders.items[list={list_name}, title={target}].completed == True",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"Reminders list '{list_name}' with {n} incomplete items"],
            absent=[],
            noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=2,
        complexity=complexity_score(2),
        detail_level=0.0,
        params={"list": list_name, "items": titles, "target": target},
        verify_checks=verify_checks,
    )


def gen_uncomplete_reminder():
    """Reverse direction: the target reminder starts completed; the agent
    must mark it as incomplete. Strict: no other item completed,
    list size and titles unchanged, no notes/url/due/priority added."""
    list_name = pick("reminder_lists")
    n         = random.choice([3, 4])
    titles    = pickn("work_items", n)
    target    = random.choice(titles)

    spec = _reminders_spec(list_name, [
        {"title": t, "completed": (t == target)} for t in titles
    ])
    spec += _springboard_noise()

    instruction = random.choice([
        f"Open Reminders. In the '{list_name}' list, '{target}' is "
        f"marked done but I'm not actually finished — uncheck it so it "
        f"shows as incomplete again.",
        f"Open Reminders. Uncheck '{target}'; it isn't actually done.",
        f"Open Reminders. I marked '{target}' as done by mistake — "
        f"undo that.",
    ])

    verify_checks = [
        {"kind": "attribute_eq",
         "resource": "reminders.items",
         "selector": {"list": list_name, "title": target},
         "attr": "completed", "value": False,
         "severity": "blocking",
         "label": f"'{target}' is no longer completed"},
        # Strict: list size unchanged.
        {"kind": "count",
         "resource": "reminders.items",
         "selector": {"list": list_name},
         "op": "eq", "n": n,
         "severity": "blocking",
         "label": f"'{list_name}' has exactly {n} items"},
        # Strict: every original title still present.
        {"kind": "subset",
         "resource": "reminders.items",
         "selector": {"list": list_name},
         "key": "title", "expected": titles,
         "severity": "blocking",
         "label": f"all {n} original titles still in '{list_name}'"},
        # Strict "no irrelevant edits": 0 completed (target uncompleted,
        # nothing else flipped), no priority/notes/url/due_date changes.
        *_no_irrelevant_edits_checks(list_name, n, completed_count=0),
    ]

    return Task(
        task_id="", flow="single_reminders_uncomplete",
        apps=["Reminders"],
        instruction=instruction,
        verify=f"reminders.items[list={list_name}, title={target}].completed == False",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"Reminders list '{list_name}' with {n} items, "
                     f"'{target}' already completed"],
            absent=[],
            noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=2,
        complexity=complexity_score(2),
        detail_level=0.0,
        params={"list": list_name, "items": titles, "target": target},
        verify_checks=verify_checks,
    )


def gen_add_reminder_to_existing_list():
    """Add one named reminder to a list that already has some content.
    Strict: count is exactly 3 (no duplicates, no extras), all 3
    expected titles present (no rename), nothing accidentally completed,
    no notes/url/due_date attached to ANY item."""
    list_name = pick("reminder_lists")
    existing  = pickn("work_items", 2)
    candidates = [w for w in C["work_items"] if w not in existing]
    new_title  = random.choice(candidates)
    expected_titles = list(existing) + [new_title]

    spec = _reminders_spec(list_name, [
        {"title": t, "completed": False} for t in existing
    ])
    spec += _springboard_noise()

    # The list name is part of the contract here (we need the new item
    # to land in a specific list), so all phrasings name it explicitly.
    instruction = random.choice([
        f"Open Reminders. Add a reminder titled '{new_title}' to the "
        f"'{list_name}' list.",
        f"Open Reminders. Create a reminder '{new_title}' under "
        f"'{list_name}'.",
        f"Open Reminders. Drop '{new_title}' into the '{list_name}' "
        f"list as a new reminder.",
    ])

    verify_checks = [
        {"kind": "exists",
         "resource": "reminders.items",
         "selector": {"list": list_name, "title": new_title},
         "severity": "blocking",
         "label": f"'{new_title}' exists in '{list_name}'"},
        # Strict: exactly the 2 pre-existing + 1 new. Catches duplicates,
        # extras, and accidental deletion of an existing item.
        {"kind": "count",
         "resource": "reminders.items",
         "selector": {"list": list_name},
         "op": "eq", "n": 3,
         "severity": "blocking",
         "label": f"'{list_name}' has exactly 3 items"},
        # Strict: all 3 expected titles present (catches rename).
        {"kind": "subset",
         "resource": "reminders.items",
         "selector": {"list": list_name},
         "key": "title", "expected": expected_titles,
         "severity": "blocking",
         "label": f"'{list_name}' contains all 3 expected titles"},
        # Strict "no irrelevant edits": 0 completed, 0 priority changes,
        # no notes/url/due_date attached.
        *_no_irrelevant_edits_checks(list_name, 3, completed_count=0),
    ]

    return Task(
        task_id="", flow="single_reminders_add_item",
        apps=["Reminders"],
        instruction=instruction,
        verify=f"reminders.items contains list={list_name} title={new_title}",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"Reminders list '{list_name}' with 2 existing items"],
            absent=[f"reminder titled '{new_title}' anywhere"],
            noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=3,
        complexity=complexity_score(3),
        detail_level=0.0,
        params={"list": list_name, "existing": existing,
                "new_title": new_title},
        verify_checks=verify_checks,
    )


def gen_set_priority():
    """Set one reminder's priority to a specified level. Strict: only
    the target's priority changes (others stay at none), no items
    added/deleted, no titles changed, nothing accidentally completed,
    no notes/url/due_date added."""
    list_name = pick("reminder_lists")
    n         = random.choice([3, 4])
    titles    = pickn("work_items", n)
    target    = random.choice(titles)
    level_str = pick("priority_levels")          # "high"/"medium"/"low"
    level_int = _PRIORITY_STR_TO_INT[level_str]

    spec = _reminders_spec(list_name, [
        {"title": t, "completed": False} for t in titles
    ])
    spec += _springboard_noise()

    instruction = random.choice([
        f"Open Reminders. In the '{list_name}' list, set '{target}' to "
        f"{level_str} priority.",
        f"Open Reminders. Mark '{target}' as {level_str} priority.",
        f"Open Reminders. Bump '{target}' to {level_str} priority.",
    ])

    verify_checks = [
        {"kind": "attribute_eq",
         "resource": "reminders.items",
         "selector": {"list": list_name, "title": target},
         "attr": "priority", "value": level_int,
         "severity": "blocking",
         "label": f"'{target}' has {level_str} priority "
                  f"(EventKit int {level_int})"},
        # Strict: list size unchanged.
        {"kind": "count",
         "resource": "reminders.items",
         "selector": {"list": list_name},
         "op": "eq", "n": n,
         "severity": "blocking",
         "label": f"'{list_name}' has exactly {n} items"},
        # Strict: every original title still present.
        {"kind": "subset",
         "resource": "reminders.items",
         "selector": {"list": list_name},
         "key": "title", "expected": titles,
         "severity": "blocking",
         "label": f"all {n} original titles still in '{list_name}'"},
        # Strict "no irrelevant edits": 0 completed, exactly n-1 items
        # still at priority=none (only target's priority changed), no
        # notes/url/due_date added anywhere.
        *_no_irrelevant_edits_checks(
            list_name, n,
            completed_count=0,
            priority_zero_count=n - 1,
        ),
    ]

    return Task(
        task_id="", flow="single_reminders_set_priority",
        apps=["Reminders"],
        instruction=instruction,
        verify=f"reminders.items[list={list_name}, title={target}]"
               f".priority == {level_int} ({level_str})",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"Reminders list '{list_name}' with {n} items, "
                     f"all priority=none"],
            absent=[],
            noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=3,
        complexity=complexity_score(3),
        detail_level=0.0,
        params={"list": list_name, "items": titles, "target": target,
                "level": level_str, "level_int": level_int},
        verify_checks=verify_checks,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  TIER 4 — due-date / notes / mixed-state Reminders tasks
#  Each generator emits a state-only task that exercises the EKReminder
#  surface added in prerequisite A (due_iso / notes / url). All ship the
#  same Tier-1 strict checks (count guards, subset of titles, no
#  irrelevant edits) plus per-task attribute checks on the changed field.
# ─────────────────────────────────────────────────────────────────────────────

# Short, distinctive note strings used by gen_add_notes_to_reminder.
# Kept short on purpose — long free text invites paraphrase failures
# under exact-match string comparison.
_NOTE_TEXTS = [
    "Bring the new laptop",
    "Confirm with Alex by Thursday",
    "Reserve a quiet conference room",
    "Check the inventory first",
    "Loop in finance before sending",
    "Ask about the budget revision",
    "Wait for the design review",
    "Save the PDF version too",
    "Ping Maria once it's posted",
    "Use the back door entrance",
    "Receipt is in the glove box",
    "Take the small carry-on bag",
    "Vegetarian options preferred",
    "Two hours before the flight",
    "Coupon code expires next week",
    "Pack a phone charger this time",
]


def gen_set_due_date_on_reminder():
    """Add a due date to a specific reminder. Random date strategy
    (today / tomorrow / next Monday / in N days / next month) and a
    coin flip on date-only vs date+time."""
    list_name = pick("reminder_lists")
    n         = random.choice([3, 4])
    titles    = pickn("work_items", n)
    target    = random.choice(titles)
    due_iso, due_phr = _resolve_due()

    spec = _reminders_spec(list_name, [
        {"title": t, "completed": False} for t in titles
    ])
    spec += _springboard_noise()

    instruction = random.choice([
        f"Open Reminders. Schedule '{target}' for {due_phr}.",
        f"Open Reminders. Set '{target}' to be due {due_phr}.",
        f"Open Reminders. Add a due date to '{target}' — make it "
        f"{due_phr}.",
    ])

    verify_checks = [
        {"kind": "attribute_eq",
         "resource": "reminders.items",
         "selector": {"list": list_name, "title": target},
         "attr": "due", "value": due_iso,
         "severity": "blocking",
         "label": f"'{target}' due == {due_iso}"},
        {"kind": "count",
         "resource": "reminders.items",
         "selector": {"list": list_name, "due": None},
         "op": "eq", "n": n - 1,
         "severity": "blocking",
         "label": f"only '{target}' has a due date "
                  f"({n - 1} items without)"},
        {"kind": "count",
         "resource": "reminders.items",
         "selector": {"list": list_name},
         "op": "eq", "n": n,
         "severity": "blocking",
         "label": f"'{list_name}' has exactly {n} items"},
        {"kind": "subset",
         "resource": "reminders.items",
         "selector": {"list": list_name},
         "key": "title", "expected": titles,
         "severity": "blocking",
         "label": f"all {n} original titles still in '{list_name}'"},
        # Standard "no irrelevant edits" — but note: we skip the due-null
        # count from the helper since this task DELIBERATELY sets one due.
        # The earlier check above already enforces "only target has due".
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "completed": True},
         "op": "eq", "n": 0,
         "severity": "blocking",
         "label": f"no items in '{list_name}' are completed"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "priority": 0},
         "op": "eq", "n": n,
         "severity": "blocking",
         "label": f"no priority changes in '{list_name}'"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "notes": None},
         "op": "eq", "n": n,
         "severity": "blocking",
         "label": f"no notes added in '{list_name}'"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "url": None},
         "op": "eq", "n": n,
         "severity": "blocking",
         "label": f"no URLs added in '{list_name}'"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "recurrence": None},
         "op": "eq", "n": n,
         "severity": "blocking",
         "label": f"no recurrence rules added in '{list_name}'"},
    ]

    return Task(
        task_id="", flow="single_reminders_set_due",
        apps=["Reminders"],
        instruction=instruction,
        verify=f"reminders.items[list={list_name}, title={target}]"
               f".due == {due_iso}",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"Reminders list '{list_name}' with {n} items, "
                     f"no due dates"],
            absent=[],
            noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=4,
        complexity=complexity_score(4),
        detail_level=0.0,
        params={"list": list_name, "items": titles, "target": target,
                "due_iso": due_iso, "due_phr": due_phr},
        verify_checks=verify_checks,
    )


def gen_change_due_date():
    """The target already has a due date; the agent moves it to a new
    one. Tests that the agent doesn't just *add* alongside the old."""
    list_name = pick("reminder_lists")
    n         = random.choice([3, 4])
    titles    = pickn("work_items", n)
    target    = random.choice(titles)
    # Old date: a past or far-future date that won't equal a typical
    # _resolve_due() result. Use the overdue helper for variety; if the
    # new date is overdue too (unlikely with allow_today=True), re-roll.
    old_iso = _past_iso(random.choice([3, 7, 14]))
    new_iso, new_phr = _resolve_due()
    while new_iso == old_iso:
        new_iso, new_phr = _resolve_due()

    spec = _reminders_spec(list_name, [
        {"title": t, "completed": False,
         **({"due_iso": old_iso} if t == target else {})}
        for t in titles
    ])
    spec += _springboard_noise()

    instruction = random.choice([
        f"Open Reminders. Change '{target}'s due date to {new_phr}.",
        f"Open Reminders. Reschedule '{target}' for {new_phr}.",
        f"Open Reminders. Move '{target}' to {new_phr}.",
    ])

    verify_checks = [
        {"kind": "attribute_eq",
         "resource": "reminders.items",
         "selector": {"list": list_name, "title": target},
         "attr": "due", "value": new_iso,
         "severity": "blocking",
         "label": f"'{target}' due == {new_iso} (new date)"},
        # The old date must be gone from EVERY item in the list — catches
        # "agent created a duplicate with the new date" as well as
        # "agent didn't change anything".
        {"kind": "count",
         "resource": "reminders.items",
         "selector": {"list": list_name, "due": old_iso},
         "op": "eq", "n": 0,
         "severity": "blocking",
         "label": f"old date {old_iso} no longer on any item"},
        {"kind": "count",
         "resource": "reminders.items",
         "selector": {"list": list_name, "due": None},
         "op": "eq", "n": n - 1,
         "severity": "blocking",
         "label": f"only '{target}' has a due date"},
        {"kind": "count",
         "resource": "reminders.items",
         "selector": {"list": list_name},
         "op": "eq", "n": n,
         "severity": "blocking",
         "label": f"'{list_name}' has exactly {n} items"},
        {"kind": "subset",
         "resource": "reminders.items",
         "selector": {"list": list_name},
         "key": "title", "expected": titles,
         "severity": "blocking",
         "label": f"all {n} original titles still in '{list_name}'"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "completed": True},
         "op": "eq", "n": 0,
         "severity": "blocking",
         "label": f"no completion changes"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "priority": 0},
         "op": "eq", "n": n,
         "severity": "blocking",
         "label": f"no priority changes"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "notes": None},
         "op": "eq", "n": n,
         "severity": "blocking",
         "label": f"no notes added"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "url": None},
         "op": "eq", "n": n,
         "severity": "blocking",
         "label": f"no URLs added"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "recurrence": None},
         "op": "eq", "n": n,
         "severity": "blocking",
         "label": f"no recurrence rules added"},
    ]

    return Task(
        task_id="", flow="single_reminders_change_due",
        apps=["Reminders"],
        instruction=instruction,
        verify=f"reminders.items[title={target}].due moves "
               f"{old_iso} → {new_iso}",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"'{target}' starts with due={old_iso}; "
                     f"{n - 1} siblings have no due"],
            absent=[],
            noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=4,
        complexity=complexity_score(4),
        detail_level=0.0,
        params={"list": list_name, "items": titles, "target": target,
                "old_iso": old_iso, "new_iso": new_iso,
                "new_phr": new_phr},
        verify_checks=verify_checks,
    )


def gen_complete_all_overdue():
    """The list contains a mix of overdue items (due in the past) and
    items with no due date. Agent must complete only the overdue ones.

    Tests reading the Today/Scheduled smart list or filtering by due
    date — the agent needs to identify which items are overdue without
    being told titles."""
    list_name   = pick("reminder_lists")
    all_titles  = pickn("work_items", 5)
    # Pick 2 of the 5 to be overdue; the other 3 have no due date.
    overdue     = random.sample(all_titles, 2)
    non_overdue = [t for t in all_titles if t not in overdue]
    past_isos   = [_past_iso(random.choice(_OVERDUE_DAYS_AGO))
                    for _ in overdue]

    items = []
    for t in all_titles:
        if t in overdue:
            iso = past_isos[overdue.index(t)]
            items.append({"title": t, "completed": False, "due_iso": iso})
        else:
            items.append({"title": t, "completed": False})

    spec = _reminders_spec(list_name, items)
    spec += _springboard_noise()

    instruction = random.choice([
        f"Open Reminders. Mark every overdue reminder in the "
        f"'{list_name}' list as completed.",
        f"Open Reminders. Complete all the overdue items in "
        f"'{list_name}'.",
        f"Open Reminders. Anything in '{list_name}' that's past its "
        f"due date should be checked off.",
    ])

    verify_checks: List[Dict[str, Any]] = []
    # The two overdue items must end up completed.
    for t in overdue:
        verify_checks.append({
            "kind": "attribute_eq",
            "resource": "reminders.items",
            "selector": {"list": list_name, "title": t},
            "attr": "completed", "value": True,
            "severity": "blocking",
            "label": f"overdue '{t}' is completed",
        })
    # The three non-overdue items must NOT be completed.
    for t in non_overdue:
        verify_checks.append({
            "kind": "attribute_eq",
            "resource": "reminders.items",
            "selector": {"list": list_name, "title": t},
            "attr": "completed", "value": False,
            "severity": "blocking",
            "label": f"non-overdue '{t}' is not completed",
        })
    # Aggregate guards.
    verify_checks += [
        {"kind": "count",
         "resource": "reminders.items",
         "selector": {"list": list_name, "completed": True},
         "op": "eq", "n": 2,
         "severity": "blocking",
         "label": f"exactly 2 items completed in '{list_name}'"},
        {"kind": "count",
         "resource": "reminders.items",
         "selector": {"list": list_name},
         "op": "eq", "n": 5,
         "severity": "blocking",
         "label": f"'{list_name}' still has 5 items"},
        {"kind": "subset",
         "resource": "reminders.items",
         "selector": {"list": list_name},
         "key": "title", "expected": all_titles,
         "severity": "blocking",
         "label": f"all 5 original titles still in '{list_name}'"},
        # The overdue items must KEEP their due date — the agent
        # shouldn't have cleared it as a side-effect of completing.
        # Encoded as count of items with each specific past_iso.
        # Same iso twice in past_isos is fine (count selector is just
        # equality), but we encode unique pairs.
        {"kind": "count",
         "resource": "reminders.items",
         "selector": {"list": list_name, "due": None},
         "op": "eq", "n": 3,
         "severity": "blocking",
         "label": f"the 3 non-overdue items still have no due date"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "priority": 0},
         "op": "eq", "n": 5,
         "severity": "blocking",
         "label": f"no priority changes"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "notes": None},
         "op": "eq", "n": 5,
         "severity": "blocking",
         "label": f"no notes added"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "url": None},
         "op": "eq", "n": 5,
         "severity": "blocking",
         "label": f"no URLs added"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "recurrence": None},
         "op": "eq", "n": 5,
         "severity": "blocking",
         "label": f"no recurrence rules added"},
    ]

    return Task(
        task_id="", flow="single_reminders_complete_overdue",
        apps=["Reminders"],
        instruction=instruction,
        verify=f"items with due<today in '{list_name}' marked completed",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"'{list_name}' has 5 items: 2 overdue, 3 with no due"],
            absent=[],
            noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=5,
        complexity=complexity_score(5),
        detail_level=0.0,
        params={"list": list_name, "items": all_titles,
                "overdue": overdue, "non_overdue": non_overdue,
                "past_isos": past_isos},
        verify_checks=verify_checks,
    )


def gen_add_notes_to_reminder():
    """Add a specific note text to one reminder. The note text is
    chosen from a small distinctive pool; comparison is case-sensitive
    so the agent must transcribe the prompt exactly."""
    list_name = pick("reminder_lists")
    n         = random.choice([3, 4])
    titles    = pickn("work_items", n)
    target    = random.choice(titles)
    note_text = random.choice(_NOTE_TEXTS)

    spec = _reminders_spec(list_name, [
        {"title": t, "completed": False} for t in titles
    ])
    spec += _springboard_noise()

    instruction = random.choice([
        f"Open Reminders. Add a note to '{target}' that says: "
        f"\"{note_text}\".",
        f"Open Reminders. Annotate '{target}' with this note: "
        f"\"{note_text}\".",
        f"Open Reminders. On '{target}', add the following note: "
        f"\"{note_text}\".",
    ])

    verify_checks = [
        {"kind": "attribute_eq",
         "resource": "reminders.items",
         "selector": {"list": list_name, "title": target},
         "attr": "notes", "value": note_text,
         "severity": "blocking",
         "label": f"'{target}' notes == \"{note_text}\""},
        {"kind": "count",
         "resource": "reminders.items",
         "selector": {"list": list_name, "notes": None},
         "op": "eq", "n": n - 1,
         "severity": "blocking",
         "label": f"only '{target}' has notes"},
        {"kind": "count",
         "resource": "reminders.items",
         "selector": {"list": list_name},
         "op": "eq", "n": n,
         "severity": "blocking",
         "label": f"'{list_name}' has exactly {n} items"},
        {"kind": "subset",
         "resource": "reminders.items",
         "selector": {"list": list_name},
         "key": "title", "expected": titles,
         "severity": "blocking",
         "label": f"all {n} original titles still in '{list_name}'"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "completed": True},
         "op": "eq", "n": 0,
         "severity": "blocking",
         "label": f"no completion changes"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "priority": 0},
         "op": "eq", "n": n,
         "severity": "blocking",
         "label": f"no priority changes"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "due": None},
         "op": "eq", "n": n,
         "severity": "blocking",
         "label": f"no due dates added"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "url": None},
         "op": "eq", "n": n,
         "severity": "blocking",
         "label": f"no URLs added"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "recurrence": None},
         "op": "eq", "n": n,
         "severity": "blocking",
         "label": f"no recurrence rules added"},
    ]

    return Task(
        task_id="", flow="single_reminders_add_notes",
        apps=["Reminders"],
        instruction=instruction,
        verify=f"reminders.items[title={target}].notes == \"{note_text}\"",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"'{list_name}' has {n} items, no notes anywhere"],
            absent=[],
            noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=4,
        complexity=complexity_score(4),
        detail_level=0.0,
        params={"list": list_name, "items": titles, "target": target,
                "note": note_text},
        verify_checks=verify_checks,
    )


def gen_clear_completed_only():
    """List has 5 items, 3 completed + 2 not. Agent must delete only
    the completed items. Catches over-deletion (touched the wrong
    items) and under-deletion (left a completed item behind)."""
    list_name   = pick("reminder_lists")
    all_titles  = pickn("work_items", 5)
    completed   = random.sample(all_titles, 3)
    remaining   = [t for t in all_titles if t not in completed]

    spec = _reminders_spec(list_name, [
        {"title": t, "completed": (t in completed)} for t in all_titles
    ])
    spec += _springboard_noise()

    instruction = random.choice([
        f"Open Reminders. Delete every completed reminder from the "
        f"'{list_name}' list.",
        f"Open Reminders. Clean up '{list_name}' by removing all the "
        f"completed items.",
        f"Open Reminders. Get rid of the completed tasks in "
        f"'{list_name}' — the unfinished ones should stay.",
    ])

    verify_checks: List[Dict[str, Any]] = []
    # Each previously-completed item must be absent.
    for t in completed:
        verify_checks.append({
            "kind": "absent",
            "resource": "reminders.items",
            "selector": {"list": list_name, "title": t},
            "severity": "blocking",
            "label": f"completed '{t}' was deleted",
        })
    # Each previously-incomplete item must still exist + still incomplete.
    for t in remaining:
        verify_checks.append({
            "kind": "exists",
            "resource": "reminders.items",
            "selector": {"list": list_name, "title": t},
            "severity": "blocking",
            "label": f"unfinished '{t}' still present",
        })
        verify_checks.append({
            "kind": "attribute_eq",
            "resource": "reminders.items",
            "selector": {"list": list_name, "title": t},
            "attr": "completed", "value": False,
            "severity": "blocking",
            "label": f"'{t}' is still not completed",
        })
    verify_checks += [
        {"kind": "count",
         "resource": "reminders.items",
         "selector": {"list": list_name},
         "op": "eq", "n": 2,
         "severity": "blocking",
         "label": f"'{list_name}' has exactly 2 items remaining"},
        {"kind": "count",
         "resource": "reminders.items",
         "selector": {"list": list_name, "completed": True},
         "op": "eq", "n": 0,
         "severity": "blocking",
         "label": f"no completed items remain in '{list_name}'"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "priority": 0},
         "op": "eq", "n": 2,
         "severity": "blocking",
         "label": f"no priority changes"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "notes": None},
         "op": "eq", "n": 2,
         "severity": "blocking",
         "label": f"no notes added"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "due": None},
         "op": "eq", "n": 2,
         "severity": "blocking",
         "label": f"no due dates added"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "url": None},
         "op": "eq", "n": 2,
         "severity": "blocking",
         "label": f"no URLs added"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "recurrence": None},
         "op": "eq", "n": 2,
         "severity": "blocking",
         "label": f"no recurrence rules added"},
    ]

    return Task(
        task_id="", flow="single_reminders_clear_completed",
        apps=["Reminders"],
        instruction=instruction,
        verify=f"'{list_name}' shrinks from 5 → 2 items "
               f"(only completed deleted)",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"'{list_name}' has 5 items: 3 completed, 2 not"],
            absent=[],
            noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=5,
        complexity=complexity_score(5),
        detail_level=0.0,
        params={"list": list_name, "completed": completed,
                "remaining": remaining},
        verify_checks=verify_checks,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  TIER 5 — reporting Reminders tasks (agent_answer)
#  The agent must READ Reminders and ANSWER with structured JSON. Each
#  generator pairs:
#    • `make_answer_check()` — produces a verifier check dict that
#      compares against the agent's terminal ANSWER payload.
#    • `describe_answer_format()` — emits a deterministic schema
#      sentence the generator splices into the instruction so the
#      agent sees the exact output format expected.
#    • `observation_required=["com.apple.reminders"]` — observation
#      gate: the agent must actually open Reminders, not just emit
#      a hardcoded answer.
#  Each also ships state-preservation guards — the reporting agent
#  must not modify any reminder while reporting.
# ─────────────────────────────────────────────────────────────────────────────


def _today_iso() -> str:
    """Date-only ISO for today, local."""
    return _dt.date.today().isoformat()


def _tomorrow_iso() -> str:
    return (_dt.date.today() + _dt.timedelta(days=1)).isoformat()


def _state_preservation_checks(
    list_name: str,
    items_with_due: List[Tuple[str, str]],
    items_without_due: List[str],
) -> List[Dict[str, Any]]:
    """Standard state-preservation guards for reporting tasks.
    `items_with_due` is [(title, expected_due_iso), …]; `items_without_due`
    is plain titles. Verifies no items added/deleted, all titles
    preserved, every item's due (or no-due) preserved, and no
    completion / priority / notes / url / recurrence side-effects."""
    n = len(items_with_due) + len(items_without_due)
    all_titles = [t for t, _ in items_with_due] + items_without_due
    checks: List[Dict[str, Any]] = [
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name},
         "op": "eq", "n": n, "severity": "blocking",
         "label": f"'{list_name}' has exactly {n} items"},
        {"kind": "subset", "resource": "reminders.items",
         "selector": {"list": list_name},
         "key": "title", "expected": all_titles,
         "severity": "blocking",
         "label": f"all original titles preserved"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "completed": True},
         "op": "eq", "n": 0, "severity": "blocking",
         "label": f"no items completed"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "priority": 0},
         "op": "eq", "n": n, "severity": "blocking",
         "label": f"no priority changes"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "url": None},
         "op": "eq", "n": n, "severity": "blocking",
         "label": f"no URLs added"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "recurrence": None},
         "op": "eq", "n": n, "severity": "blocking",
         "label": f"no recurrence rules added"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "due": None},
         "op": "eq", "n": len(items_without_due),
         "severity": "blocking",
         "label": f"exactly {len(items_without_due)} items have no due"},
    ]
    # Per-item due preservation.
    for title, due_iso in items_with_due:
        checks.append({
            "kind": "attribute_eq", "resource": "reminders.items",
            "selector": {"list": list_name, "title": title},
            "attr": "due", "value": due_iso,
            "severity": "blocking",
            "label": f"'{title}'.due preserved at {due_iso}",
        })
    return checks


# gen_count_overdue and gen_yesno_overdue were prototyped here (2026-05-19)
# and DROPPED 2026-05-20 after the adversarial-cheats critic flagged
# them as too easy to chance-guess: a constant-2 answer on the
# integer task hits ~33% pass rate, and a coin-flip on the boolean
# task hits 50%. Both pass the observation gate trivially with a
# drive-by Reminders open. Re-introducing them requires either
# widening the integer answer space dramatically (k ∈ {0..10}+) or
# pairing the yes/no answer with structured evidence (e.g.,
# supporting item titles) so guess-vs-skill is distinguishable in a
# single episode. Until then, the corpus stays at 3 Tier 5 tasks
# whose answer spaces are large enough (title sets, free-text notes)
# to keep guessing tractable only with real observations.


def gen_list_due_today():
    """Report the titles of all reminders due today in a list."""
    list_name      = pick("reminder_lists")
    n_today        = random.choice([1, 2, 3])
    n_other        = random.choice([2, 3])
    n              = n_today + n_other
    titles         = pickn("work_items", n)
    today_titles   = titles[:n_today]
    other_titles   = titles[n_today:]
    today_iso      = _today_iso()

    items = []
    for t in today_titles:
        items.append({"title": t, "completed": False,
                      "due_iso": today_iso})
    for t in other_titles:
        items.append({"title": t, "completed": False})

    spec = _reminders_spec(list_name, items)
    spec += _springboard_noise()

    answer_check, schema = make_answer_check(
        match="set_equals",
        expected=[{"title": t} for t in today_titles],
        path="$.items",
        item_keys=["title"],
        observation_required=["com.apple.reminders"],
    )

    intro = random.choice([
        f"Open Reminders. List every reminder in '{list_name}' that's "
        f"due today. ",
        f"Open Reminders. What's due today in '{list_name}'? Tell me "
        f"their titles. ",
        f"Open Reminders. Tell me which reminders in '{list_name}' "
        f"are due today. ",
    ])
    instruction = intro + schema

    verify_checks = [answer_check] + _state_preservation_checks(
        list_name,
        [(t, today_iso) for t in today_titles],
        other_titles,
    )

    return Task(
        task_id="", flow="single_reminders_list_due_today",
        apps=["Reminders"],
        instruction=instruction,
        verify=f"ANSWER items == {sorted(today_titles)} (due today)",
        verify_mode="agent_response",
        initial_state=InitialState(
            present=[f"'{list_name}' has {n_today} items due today "
                     f"({today_iso}) + {n_other} without due"],
            absent=[],
            noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=4,
        complexity=complexity_score(4),
        detail_level=0.0,
        params={"list": list_name, "today_iso": today_iso,
                "today_titles": today_titles,
                "other_titles": other_titles},
        verify_checks=verify_checks,
    )


def gen_list_due_tomorrow():
    """Report the titles of all reminders due tomorrow in a list."""
    list_name        = pick("reminder_lists")
    n_tomorrow       = random.choice([1, 2, 3])
    n_other          = random.choice([2, 3])
    n                = n_tomorrow + n_other
    titles           = pickn("work_items", n)
    tomorrow_titles  = titles[:n_tomorrow]
    other_titles     = titles[n_tomorrow:]
    tomorrow_iso     = _tomorrow_iso()

    items = []
    for t in tomorrow_titles:
        items.append({"title": t, "completed": False,
                      "due_iso": tomorrow_iso})
    for t in other_titles:
        items.append({"title": t, "completed": False})

    spec = _reminders_spec(list_name, items)
    spec += _springboard_noise()

    answer_check, schema = make_answer_check(
        match="set_equals",
        expected=[{"title": t} for t in tomorrow_titles],
        path="$.items",
        item_keys=["title"],
        observation_required=["com.apple.reminders"],
    )

    intro = random.choice([
        f"Open Reminders. List every reminder in '{list_name}' that's "
        f"due tomorrow. ",
        f"Open Reminders. What's due tomorrow in '{list_name}'? Tell "
        f"me their titles. ",
        f"Open Reminders. Tell me which reminders in '{list_name}' "
        f"are scheduled for tomorrow. ",
    ])
    instruction = intro + schema

    verify_checks = [answer_check] + _state_preservation_checks(
        list_name,
        [(t, tomorrow_iso) for t in tomorrow_titles],
        other_titles,
    )

    return Task(
        task_id="", flow="single_reminders_list_due_tomorrow",
        apps=["Reminders"],
        instruction=instruction,
        verify=f"ANSWER items == {sorted(tomorrow_titles)} (due tomorrow)",
        verify_mode="agent_response",
        initial_state=InitialState(
            present=[f"'{list_name}' has {n_tomorrow} items due "
                     f"tomorrow ({tomorrow_iso}) + {n_other} without"],
            absent=[],
            noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=4,
        complexity=complexity_score(4),
        detail_level=0.0,
        params={"list": list_name, "tomorrow_iso": tomorrow_iso,
                "tomorrow_titles": tomorrow_titles,
                "other_titles": other_titles},
        verify_checks=verify_checks,
    )


# gen_yesno_overdue — DROPPED 2026-05-20. See note above
# gen_count_overdue for rationale and re-introduction criteria.


def gen_lookup_reminder_notes():
    """Report the note text on a specific reminder. case-insensitive +
    whitespace-trimmed so the agent doesn't fail on incidental typing."""
    list_name = pick("reminder_lists")
    n         = random.choice([3, 4])
    titles    = pickn("work_items", n)
    target    = random.choice(titles)
    note_text = random.choice(_NOTE_TEXTS)

    items = []
    for t in titles:
        if t == target:
            items.append({"title": t, "completed": False,
                          "notes": note_text})
        else:
            items.append({"title": t, "completed": False})

    spec = _reminders_spec(list_name, items)
    spec += _springboard_noise()

    answer_check, schema = make_answer_check(
        match="string_eq",
        expected=note_text,
        path="$.value",
        case_sensitive=False,
        trim_strings=True,
        observation_required=["com.apple.reminders"],
    )

    intro = random.choice([
        f"Open Reminders. What does the note on '{target}' in "
        f"'{list_name}' say? ",
        f"Open Reminders. Tell me the note attached to '{target}' in "
        f"'{list_name}'. ",
        f"Open Reminders. Read me the note on '{target}'. ",
    ])
    instruction = intro + schema

    # State preservation — notes verifier is per-title (target keeps
    # its note; others remain note-free).
    verify_checks = [
        answer_check,
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name},
         "op": "eq", "n": n, "severity": "blocking",
         "label": f"'{list_name}' has exactly {n} items"},
        {"kind": "subset", "resource": "reminders.items",
         "selector": {"list": list_name},
         "key": "title", "expected": titles,
         "severity": "blocking",
         "label": f"all original titles preserved"},
        # Target's notes preserved verbatim.
        {"kind": "attribute_eq", "resource": "reminders.items",
         "selector": {"list": list_name, "title": target},
         "attr": "notes", "value": note_text,
         "severity": "blocking",
         "label": f"'{target}'.notes preserved"},
        # Exactly one item has notes (the target).
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "notes": None},
         "op": "eq", "n": n - 1, "severity": "blocking",
         "label": f"only '{target}' has notes"},
        # No completion / priority / due / url / recurrence side-effects.
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "completed": True},
         "op": "eq", "n": 0, "severity": "blocking",
         "label": f"no completion changes"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "priority": 0},
         "op": "eq", "n": n, "severity": "blocking",
         "label": f"no priority changes"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "due": None},
         "op": "eq", "n": n, "severity": "blocking",
         "label": f"no due dates added"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "url": None},
         "op": "eq", "n": n, "severity": "blocking",
         "label": f"no URLs added"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "recurrence": None},
         "op": "eq", "n": n, "severity": "blocking",
         "label": f"no recurrence rules added"},
    ]

    return Task(
        task_id="", flow="single_reminders_lookup_notes",
        apps=["Reminders"],
        instruction=instruction,
        verify=f"ANSWER value ~== {note_text!r} (case-insensitive)",
        verify_mode="agent_response",
        initial_state=InitialState(
            present=[f"'{target}' in '{list_name}' has note "
                     f"\"{note_text}\"; {n - 1} siblings note-free"],
            absent=[],
            noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=3,
        complexity=complexity_score(3),
        detail_level=0.0,
        params={"list": list_name, "target": target, "note": note_text},
        verify_checks=verify_checks,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  TIER 4b — recurrence-based Reminders tasks
#  Built on the recurrence prerequisite (Swift + spec + fetcher + verifier
#  dot-path / attribute_exists / attribute_absent) landed 2026-05-20.
#  Constraints baked into the generators:
#    • recurrence requires a due date (EKReminder silently drops the rule
#      otherwise — Swift and fake both reject loudly).
#    • frequency ∈ {daily, weekly, monthly, yearly}; interval=1 for v1.
#    • completion-of-recurring is NOT modeled — these generators avoid
#      asking the agent to complete a recurring reminder.
# ─────────────────────────────────────────────────────────────────────────────

_RECURRENCE_FREQUENCIES = ["daily", "weekly", "monthly", "yearly"]


def gen_make_reminder_recurring():
    """Add a recurrence rule to an existing reminder that already has a
    due date. Siblings have no due and no recurrence."""
    list_name = pick("reminder_lists")
    n         = random.choice([3, 4])
    titles    = pickn("work_items", n)
    target    = random.choice(titles)
    due_iso, due_phr = _resolve_due()
    frequency = random.choice(_RECURRENCE_FREQUENCIES)

    spec = _reminders_spec(list_name, [
        {"title": t, "completed": False,
         **({"due_iso": due_iso} if t == target else {})}
        for t in titles
    ])
    spec += _springboard_noise()

    instruction = random.choice([
        f"Open Reminders. Make '{target}' repeat {frequency}.",
        f"Open Reminders. Set '{target}' to repeat {frequency}.",
        f"Open Reminders. Add a {frequency} repeat to '{target}'.",
    ])

    verify_checks = [
        # The requested change — assert sub-fields via dot-path.
        {"kind": "attribute_eq", "resource": "reminders.items",
         "selector": {"list": list_name, "title": target},
         "attr": "recurrence.frequency", "value": frequency,
         "severity": "blocking",
         "label": f"'{target}'.recurrence.frequency == {frequency}"},
        {"kind": "attribute_eq", "resource": "reminders.items",
         "selector": {"list": list_name, "title": target},
         "attr": "recurrence.interval", "value": 1,
         "severity": "blocking",
         "label": f"'{target}'.recurrence.interval == 1"},
        # Target's due is preserved.
        {"kind": "attribute_eq", "resource": "reminders.items",
         "selector": {"list": list_name, "title": target},
         "attr": "due", "value": due_iso,
         "severity": "blocking",
         "label": f"'{target}'.due preserved at {due_iso}"},
        # Strict structural guards.
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name},
         "op": "eq", "n": n, "severity": "blocking",
         "label": f"'{list_name}' has exactly {n} items"},
        {"kind": "subset", "resource": "reminders.items",
         "selector": {"list": list_name},
         "key": "title", "expected": titles,
         "severity": "blocking",
         "label": f"all original titles preserved"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "due": None},
         "op": "eq", "n": n - 1, "severity": "blocking",
         "label": f"only '{target}' has a due date"},
        # No side effects.
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "completed": True},
         "op": "eq", "n": 0, "severity": "blocking",
         "label": f"no completion changes"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "priority": 0},
         "op": "eq", "n": n, "severity": "blocking",
         "label": f"no priority changes"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "notes": None},
         "op": "eq", "n": n, "severity": "blocking",
         "label": f"no notes added"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "url": None},
         "op": "eq", "n": n, "severity": "blocking",
         "label": f"no URLs added"},
    ]
    # Per-sibling: recurrence must be absent. Using attribute_absent
    # with a per-title selector keeps the error message specific if a
    # sibling accidentally gets a rule.
    for t in titles:
        if t == target:
            continue
        verify_checks.append({
            "kind": "attribute_absent",
            "resource": "reminders.items",
            "selector": {"list": list_name, "title": t},
            "attr": "recurrence",
            "severity": "blocking",
            "label": f"sibling '{t}' has no recurrence rule",
        })

    return Task(
        task_id="", flow="single_reminders_make_recurring",
        apps=["Reminders"],
        instruction=instruction,
        verify=f"reminders.items[title={target}].recurrence "
               f"== {frequency}/1",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"'{list_name}' has {n} items, '{target}' due "
                     f"{due_phr}, none recurring"],
            absent=[],
            noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=4,
        complexity=complexity_score(4),
        detail_level=0.0,
        params={"list": list_name, "items": titles, "target": target,
                "due_iso": due_iso, "due_phr": due_phr,
                "frequency": frequency},
        verify_checks=verify_checks,
    )


def gen_change_recurrence_frequency():
    """Change an existing recurrence rule's frequency. Target starts
    with one frequency (e.g., daily) and the agent moves it to a
    different one (e.g., weekly)."""
    list_name = pick("reminder_lists")
    n         = random.choice([3, 4])
    titles    = pickn("work_items", n)
    target    = random.choice(titles)
    due_iso, due_phr = _resolve_due()
    old_freq, new_freq = random.sample(_RECURRENCE_FREQUENCIES, 2)

    spec = _reminders_spec(list_name, [
        {"title": t, "completed": False,
         **({"due_iso": due_iso,
              "recurrence": {"frequency": old_freq, "interval": 1}}
            if t == target else {})}
        for t in titles
    ])
    spec += _springboard_noise()

    instruction = random.choice([
        f"Open Reminders. Make '{target}' repeat {new_freq} instead of "
        f"{old_freq}.",
        f"Open Reminders. Change the repeat on '{target}' from "
        f"{old_freq} to {new_freq}.",
        f"Open Reminders. Switch '{target}' to a {new_freq} repeat.",
    ])

    verify_checks = [
        {"kind": "attribute_eq", "resource": "reminders.items",
         "selector": {"list": list_name, "title": target},
         "attr": "recurrence.frequency", "value": new_freq,
         "severity": "blocking",
         "label": f"'{target}'.recurrence.frequency == {new_freq}"},
        {"kind": "attribute_eq", "resource": "reminders.items",
         "selector": {"list": list_name, "title": target},
         "attr": "recurrence.interval", "value": 1,
         "severity": "blocking",
         "label": f"'{target}'.recurrence.interval == 1"},
        {"kind": "attribute_eq", "resource": "reminders.items",
         "selector": {"list": list_name, "title": target},
         "attr": "due", "value": due_iso,
         "severity": "blocking",
         "label": f"'{target}'.due preserved at {due_iso}"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name},
         "op": "eq", "n": n, "severity": "blocking",
         "label": f"'{list_name}' has exactly {n} items"},
        {"kind": "subset", "resource": "reminders.items",
         "selector": {"list": list_name},
         "key": "title", "expected": titles,
         "severity": "blocking",
         "label": f"all original titles preserved"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "due": None},
         "op": "eq", "n": n - 1, "severity": "blocking",
         "label": f"only '{target}' has a due date"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "completed": True},
         "op": "eq", "n": 0, "severity": "blocking",
         "label": f"no completion changes"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "priority": 0},
         "op": "eq", "n": n, "severity": "blocking",
         "label": f"no priority changes"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "notes": None},
         "op": "eq", "n": n, "severity": "blocking",
         "label": f"no notes added"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "url": None},
         "op": "eq", "n": n, "severity": "blocking",
         "label": f"no URLs added"},
    ]
    for t in titles:
        if t == target:
            continue
        verify_checks.append({
            "kind": "attribute_absent",
            "resource": "reminders.items",
            "selector": {"list": list_name, "title": t},
            "attr": "recurrence",
            "severity": "blocking",
            "label": f"sibling '{t}' has no recurrence rule",
        })

    return Task(
        task_id="", flow="single_reminders_change_recurrence",
        apps=["Reminders"],
        instruction=instruction,
        verify=f"reminders.items[title={target}].recurrence.frequency "
               f"{old_freq} → {new_freq}",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"'{target}' due {due_phr}, repeating {old_freq}"],
            absent=[],
            noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=4,
        complexity=complexity_score(4),
        detail_level=0.0,
        params={"list": list_name, "items": titles, "target": target,
                "due_iso": due_iso, "due_phr": due_phr,
                "old_frequency": old_freq, "new_frequency": new_freq},
        verify_checks=verify_checks,
    )


def gen_stop_recurrence():
    """Remove the recurrence rule from a recurring reminder, leaving
    the reminder itself + its due date intact."""
    list_name = pick("reminder_lists")
    n         = random.choice([3, 4])
    titles    = pickn("work_items", n)
    target    = random.choice(titles)
    due_iso, due_phr = _resolve_due()
    frequency = random.choice(_RECURRENCE_FREQUENCIES)

    spec = _reminders_spec(list_name, [
        {"title": t, "completed": False,
         **({"due_iso": due_iso,
              "recurrence": {"frequency": frequency, "interval": 1}}
            if t == target else {})}
        for t in titles
    ])
    spec += _springboard_noise()

    instruction = random.choice([
        f"Open Reminders. Stop '{target}' from repeating.",
        f"Open Reminders. Make '{target}' a one-time reminder again — "
        f"remove the repeat.",
        f"Open Reminders. Get rid of the repeat on '{target}'.",
    ])

    verify_checks = [
        {"kind": "attribute_absent", "resource": "reminders.items",
         "selector": {"list": list_name, "title": target},
         "attr": "recurrence",
         "severity": "blocking",
         "label": f"'{target}' no longer has a recurrence rule"},
        # Target's due is preserved.
        {"kind": "attribute_eq", "resource": "reminders.items",
         "selector": {"list": list_name, "title": target},
         "attr": "due", "value": due_iso,
         "severity": "blocking",
         "label": f"'{target}'.due preserved at {due_iso}"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name},
         "op": "eq", "n": n, "severity": "blocking",
         "label": f"'{list_name}' has exactly {n} items"},
        {"kind": "subset", "resource": "reminders.items",
         "selector": {"list": list_name},
         "key": "title", "expected": titles,
         "severity": "blocking",
         "label": f"all original titles preserved"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "due": None},
         "op": "eq", "n": n - 1, "severity": "blocking",
         "label": f"only '{target}' has a due date"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "completed": True},
         "op": "eq", "n": 0, "severity": "blocking",
         "label": f"no completion changes"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "priority": 0},
         "op": "eq", "n": n, "severity": "blocking",
         "label": f"no priority changes"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "notes": None},
         "op": "eq", "n": n, "severity": "blocking",
         "label": f"no notes added"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "url": None},
         "op": "eq", "n": n, "severity": "blocking",
         "label": f"no URLs added"},
    ]
    # Siblings also have no recurrence (sanity, they never did).
    for t in titles:
        if t == target:
            continue
        verify_checks.append({
            "kind": "attribute_absent",
            "resource": "reminders.items",
            "selector": {"list": list_name, "title": t},
            "attr": "recurrence",
            "severity": "blocking",
            "label": f"sibling '{t}' has no recurrence rule",
        })

    return Task(
        task_id="", flow="single_reminders_stop_recurrence",
        apps=["Reminders"],
        instruction=instruction,
        verify=f"reminders.items[title={target}].recurrence is absent",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"'{target}' due {due_phr}, repeating {frequency}"],
            absent=[],
            noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=3,
        complexity=complexity_score(3),
        detail_level=0.0,
        params={"list": list_name, "items": titles, "target": target,
                "due_iso": due_iso, "due_phr": due_phr,
                "frequency": frequency},
        verify_checks=verify_checks,
    )


def gen_create_recurring_with_due():
    """Create a new reminder in an existing (empty-of-target) list with
    both a due date and a recurrence rule. Combined surface — agent
    has to set TWO non-default fields in a single creation flow."""
    list_name = pick("reminder_lists")
    new_title = pick("work_items")
    due_iso, due_phr = _resolve_due()
    frequency = random.choice(_RECURRENCE_FREQUENCIES)

    # The list pre-exists but starts empty. Keeps verification simple
    # (count==1 catches duplicates).
    spec: List[Dict[str, Any]] = [
        {"app": "Reminders", "type": "list", "name": list_name},
    ]
    spec += _springboard_noise()

    instruction = random.choice([
        f"Open Reminders. In '{list_name}', add '{new_title}' scheduled "
        f"for {due_phr} and have it repeat {frequency}.",
        f"Open Reminders. Create a reminder '{new_title}' in "
        f"'{list_name}', due {due_phr}, repeating {frequency}.",
        f"Open Reminders. Drop '{new_title}' into '{list_name}' with "
        f"a due date of {due_phr} and a {frequency} repeat.",
    ])

    verify_checks = [
        {"kind": "exists", "resource": "reminders.items",
         "selector": {"list": list_name, "title": new_title},
         "severity": "blocking",
         "label": f"'{new_title}' exists in '{list_name}'"},
        {"kind": "attribute_eq", "resource": "reminders.items",
         "selector": {"list": list_name, "title": new_title},
         "attr": "due", "value": due_iso,
         "severity": "blocking",
         "label": f"'{new_title}'.due == {due_iso}"},
        {"kind": "attribute_eq", "resource": "reminders.items",
         "selector": {"list": list_name, "title": new_title},
         "attr": "recurrence.frequency", "value": frequency,
         "severity": "blocking",
         "label": f"'{new_title}'.recurrence.frequency == {frequency}"},
        {"kind": "attribute_eq", "resource": "reminders.items",
         "selector": {"list": list_name, "title": new_title},
         "attr": "recurrence.interval", "value": 1,
         "severity": "blocking",
         "label": f"'{new_title}'.recurrence.interval == 1"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name},
         "op": "eq", "n": 1, "severity": "blocking",
         "label": f"'{list_name}' has exactly 1 item (no duplicates)"},
        # Side-effect guards. We can't use _no_irrelevant_edits_checks
        # here because that helper assumes no item has a due — but this
        # generator DELIBERATELY creates a due. Explicit guards instead:
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "completed": True},
         "op": "eq", "n": 0, "severity": "blocking",
         "label": f"no items completed"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "priority": 0},
         "op": "eq", "n": 1, "severity": "blocking",
         "label": f"no priority assigned"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "notes": None},
         "op": "eq", "n": 1, "severity": "blocking",
         "label": f"no notes attached"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name, "url": None},
         "op": "eq", "n": 1, "severity": "blocking",
         "label": f"no URL attached"},
    ]

    return Task(
        task_id="", flow="single_reminders_create_recurring",
        apps=["Reminders"],
        instruction=instruction,
        verify=f"reminders.items[title={new_title}].due == {due_iso}; "
               f".recurrence.frequency == {frequency}",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"'{list_name}' exists, empty"],
            absent=[f"any reminder titled '{new_title}'"],
            noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=5,
        complexity=complexity_score(5),
        detail_level=0.0,
        params={"list": list_name, "new_title": new_title,
                "due_iso": due_iso, "due_phr": due_phr,
                "frequency": frequency},
        verify_checks=verify_checks,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  TIER 2/3 — bulk + structural Reminders tasks
#  Easy bulk (Tier 2): complete_all_in_list, delete_specific_reminder
#  Medium structural (Tier 3): delete_entire_list,
#                              move_reminder_between_lists, rename_reminder
#  Multi-list tasks ship per-list count + subset guards so the distractor
#  list (or the source/dest list) is verified unchanged outside the
#  expected delta.
# ─────────────────────────────────────────────────────────────────────────────


def gen_complete_all_in_list():
    """Mark every item in a target list as completed. A distractor list
    must stay fully incomplete. Tests scoped-bulk action; the agent has
    to read the list boundary and not bleed into the other one."""
    target_list, distractor_list = random.sample(C["reminder_lists"], 2)
    n_target     = random.choice([3, 4, 5])
    n_distractor = random.choice([2, 3])
    pool         = pickn("work_items", n_target + n_distractor)
    target_titles     = pool[:n_target]
    distractor_titles = pool[n_target:]

    spec = _reminders_spec(target_list, [
        {"title": t, "completed": False} for t in target_titles
    ])
    # Append the distractor list — single spec list-entry + items.
    spec.append({"app": "Reminders", "type": "list", "name": distractor_list})
    for t in distractor_titles:
        spec.append({"app": "Reminders", "type": "item",
                     "list": distractor_list, "title": t,
                     "completed": False})
    spec += _springboard_noise()

    instruction = random.choice([
        f"Open Reminders. Mark every item in '{target_list}' as completed.",
        f"Open Reminders. Complete all the reminders in '{target_list}'.",
        f"Open Reminders. Finish off everything in '{target_list}'.",
    ])

    verify_checks = [
        # Target list: every item completed, size + titles preserved.
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": target_list},
         "op": "eq", "n": n_target,
         "severity": "blocking",
         "label": f"'{target_list}' still has {n_target} items"},
        {"kind": "subset", "resource": "reminders.items",
         "selector": {"list": target_list},
         "key": "title", "expected": target_titles,
         "severity": "blocking",
         "label": f"all original titles in '{target_list}'"},
        # Distractor list: size + titles preserved.
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": distractor_list},
         "op": "eq", "n": n_distractor,
         "severity": "blocking",
         "label": f"distractor '{distractor_list}' has exactly "
                  f"{n_distractor} items"},
        {"kind": "subset", "resource": "reminders.items",
         "selector": {"list": distractor_list},
         "key": "title", "expected": distractor_titles,
         "severity": "blocking",
         "label": f"all distractor titles preserved"},
        # Symmetric "no irrelevant edits" on both lists.
        *_no_irrelevant_edits_checks(target_list, n_target,
                                      completed_count=n_target),
        *_no_irrelevant_edits_checks(distractor_list, n_distractor,
                                      completed_count=0),
    ]

    return Task(
        task_id="", flow="single_reminders_complete_all_in_list",
        apps=["Reminders"],
        instruction=instruction,
        verify=f"all items in '{target_list}' completed; "
               f"'{distractor_list}' unchanged",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"'{target_list}' with {n_target} incomplete items, "
                     f"'{distractor_list}' with {n_distractor} incomplete items"],
            absent=[],
            noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=4,
        complexity=complexity_score(4),
        detail_level=0.0,
        params={"target_list": target_list, "target_titles": target_titles,
                "distractor_list": distractor_list,
                "distractor_titles": distractor_titles},
        verify_checks=verify_checks,
    )


def gen_delete_specific_reminder():
    """Delete exactly one named reminder. Siblings remain intact, no
    other modifications anywhere."""
    list_name = pick("reminder_lists")
    n         = random.choice([4, 5])
    titles    = pickn("work_items", n)
    target    = random.choice(titles)
    remaining = [t for t in titles if t != target]

    spec = _reminders_spec(list_name, [
        {"title": t, "completed": False} for t in titles
    ])
    spec += _springboard_noise()

    instruction = random.choice([
        f"Open Reminders. Delete '{target}' from '{list_name}'.",
        f"Open Reminders. Remove '{target}' from the '{list_name}' list.",
        f"Open Reminders. Get rid of '{target}' in '{list_name}'.",
    ])

    verify_checks = [
        {"kind": "absent", "resource": "reminders.items",
         "selector": {"list": list_name, "title": target},
         "severity": "blocking",
         "label": f"'{target}' is gone from '{list_name}'"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name},
         "op": "eq", "n": n - 1,
         "severity": "blocking",
         "label": f"'{list_name}' has exactly {n - 1} items"},
        {"kind": "subset", "resource": "reminders.items",
         "selector": {"list": list_name},
         "key": "title", "expected": remaining,
         "severity": "blocking",
         "label": f"all {n - 1} non-target titles preserved"},
        *_no_irrelevant_edits_checks(list_name, n - 1, completed_count=0),
    ]

    return Task(
        task_id="", flow="single_reminders_delete_one",
        apps=["Reminders"],
        instruction=instruction,
        verify=f"reminders.items[list={list_name}, title={target}] absent",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"'{list_name}' with {n} items (target='{target}')"],
            absent=[],
            noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=3,
        complexity=complexity_score(3),
        detail_level=0.0,
        params={"list": list_name, "items": titles, "target": target,
                "remaining": remaining},
        verify_checks=verify_checks,
    )


def gen_delete_entire_list():
    """Delete a whole user list — its items go with it (iOS cascades).
    A second user list must remain untouched. The system 'Reminders'
    list is `immutable=True` and undeletable; we always target a user
    list."""
    target_list, distractor_list = random.sample(C["reminder_lists"], 2)
    n_target     = random.choice([2, 3])
    n_distractor = random.choice([2, 3])
    pool         = pickn("work_items", n_target + n_distractor)
    target_titles     = pool[:n_target]
    distractor_titles = pool[n_target:]

    spec = _reminders_spec(target_list, [
        {"title": t, "completed": False} for t in target_titles
    ])
    spec.append({"app": "Reminders", "type": "list", "name": distractor_list})
    for t in distractor_titles:
        spec.append({"app": "Reminders", "type": "item",
                     "list": distractor_list, "title": t,
                     "completed": False})
    spec += _springboard_noise()

    instruction = random.choice([
        f"Open Reminders. Delete the '{target_list}' list entirely.",
        f"Open Reminders. Remove the '{target_list}' list and everything "
        f"in it.",
        f"Open Reminders. Get rid of the whole '{target_list}' list.",
    ])

    verify_checks = [
        # Target list is gone.
        {"kind": "absent", "resource": "reminders.lists",
         "selector": {"name": target_list},
         "severity": "blocking",
         "label": f"'{target_list}' list deleted"},
        # No items remain under the target list name (cascade verified).
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": target_list},
         "op": "eq", "n": 0,
         "severity": "blocking",
         "label": f"no items remain under '{target_list}'"},
        # Distractor list still present + intact.
        {"kind": "exists", "resource": "reminders.lists",
         "selector": {"name": distractor_list},
         "severity": "blocking",
         "label": f"distractor '{distractor_list}' still exists"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": distractor_list},
         "op": "eq", "n": n_distractor,
         "severity": "blocking",
         "label": f"distractor has {n_distractor} items unchanged"},
        {"kind": "subset", "resource": "reminders.items",
         "selector": {"list": distractor_list},
         "key": "title", "expected": distractor_titles,
         "severity": "blocking",
         "label": f"distractor titles preserved"},
        # Symmetric "no irrelevant edits" on the distractor list
        # (the target list is gone, so it's not checked here).
        *_no_irrelevant_edits_checks(distractor_list, n_distractor,
                                      completed_count=0),
    ]

    return Task(
        task_id="", flow="single_reminders_delete_list",
        apps=["Reminders"],
        instruction=instruction,
        verify=f"'{target_list}' list absent; '{distractor_list}' unchanged",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"'{target_list}' with {n_target} items, "
                     f"'{distractor_list}' with {n_distractor} items"],
            absent=[],
            noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=3,
        complexity=complexity_score(3),
        detail_level=0.0,
        params={"target_list": target_list, "target_titles": target_titles,
                "distractor_list": distractor_list,
                "distractor_titles": distractor_titles},
        verify_checks=verify_checks,
    )


def gen_move_reminder_between_lists():
    """Move one named reminder from a source list to a destination list.
    The target item must exist in dest, be absent from source, and both
    lists' other items must be unchanged."""
    source, dest = random.sample(C["reminder_lists"], 2)
    n_source = random.choice([3, 4])
    n_dest   = random.choice([1, 2])
    pool     = pickn("work_items", n_source + n_dest)
    source_titles = pool[:n_source]
    dest_titles   = pool[n_source:]
    target        = random.choice(source_titles)
    remaining_in_source = [t for t in source_titles if t != target]
    expected_in_dest    = list(dest_titles) + [target]

    spec = _reminders_spec(source, [
        {"title": t, "completed": False} for t in source_titles
    ])
    spec.append({"app": "Reminders", "type": "list", "name": dest})
    for t in dest_titles:
        spec.append({"app": "Reminders", "type": "item",
                     "list": dest, "title": t, "completed": False})
    spec += _springboard_noise()

    instruction = random.choice([
        f"Open Reminders. Move '{target}' from '{source}' to '{dest}'.",
        f"Open Reminders. '{target}' should be in '{dest}', not "
        f"'{source}'.",
        f"Open Reminders. Take '{target}' out of '{source}' and put it "
        f"in '{dest}'.",
    ])

    verify_checks = [
        {"kind": "exists", "resource": "reminders.items",
         "selector": {"list": dest, "title": target},
         "severity": "blocking",
         "label": f"'{target}' now in '{dest}'"},
        {"kind": "absent", "resource": "reminders.items",
         "selector": {"list": source, "title": target},
         "severity": "blocking",
         "label": f"'{target}' no longer in '{source}'"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": source},
         "op": "eq", "n": n_source - 1,
         "severity": "blocking",
         "label": f"'{source}' shrunk to {n_source - 1} items"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": dest},
         "op": "eq", "n": n_dest + 1,
         "severity": "blocking",
         "label": f"'{dest}' grew to {n_dest + 1} items"},
        {"kind": "subset", "resource": "reminders.items",
         "selector": {"list": source},
         "key": "title", "expected": remaining_in_source,
         "severity": "blocking",
         "label": f"source siblings preserved"},
        {"kind": "subset", "resource": "reminders.items",
         "selector": {"list": dest},
         "key": "title", "expected": expected_in_dest,
         "severity": "blocking",
         "label": f"dest now contains its originals + the moved item"},
        # Symmetric "no irrelevant edits" on both lists, using the
        # post-move sizes.
        *_no_irrelevant_edits_checks(source, n_source - 1,
                                      completed_count=0),
        *_no_irrelevant_edits_checks(dest, n_dest + 1,
                                      completed_count=0),
    ]

    return Task(
        task_id="", flow="single_reminders_move_between_lists",
        apps=["Reminders"],
        instruction=instruction,
        verify=f"'{target}' moves {source} → {dest}",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"'{source}' has {n_source} items (incl. target "
                     f"'{target}'); '{dest}' has {n_dest} unrelated items"],
            absent=[],
            noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=4,
        complexity=complexity_score(4),
        detail_level=0.0,
        params={"source": source, "dest": dest, "target": target,
                "source_titles": source_titles, "dest_titles": dest_titles,
                "remaining_in_source": remaining_in_source,
                "expected_in_dest": expected_in_dest},
        verify_checks=verify_checks,
    )


def gen_rename_reminder():
    """Change a specific reminder's title from OLD to NEW. List size
    must stay at n (catches the lazy 'create new + leave old' path)."""
    list_name = pick("reminder_lists")
    n         = random.choice([3, 4])
    titles    = pickn("work_items", n)
    old_title = random.choice(titles)
    # Pick a new title not already in the list.
    candidates = [w for w in C["work_items"] if w not in titles]
    new_title  = random.choice(candidates)
    expected_titles = [t if t != old_title else new_title for t in titles]

    spec = _reminders_spec(list_name, [
        {"title": t, "completed": False} for t in titles
    ])
    spec += _springboard_noise()

    instruction = random.choice([
        f"Open Reminders. Rename '{old_title}' to '{new_title}' in "
        f"'{list_name}'.",
        f"Open Reminders. Change the title of '{old_title}' to "
        f"'{new_title}'.",
        f"Open Reminders. Update '{old_title}' to be called "
        f"'{new_title}' instead.",
    ])

    verify_checks = [
        {"kind": "exists", "resource": "reminders.items",
         "selector": {"list": list_name, "title": new_title},
         "severity": "blocking",
         "label": f"'{new_title}' exists in '{list_name}'"},
        {"kind": "absent", "resource": "reminders.items",
         "selector": {"list": list_name, "title": old_title},
         "severity": "blocking",
         "label": f"'{old_title}' no longer in '{list_name}'"},
        # Strict: list size unchanged. Catches "create new, leave old"
        # (count would be n+1) and "delete, don't create" (count would
        # be n-1).
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": list_name},
         "op": "eq", "n": n,
         "severity": "blocking",
         "label": f"'{list_name}' still has exactly {n} items"},
        {"kind": "subset", "resource": "reminders.items",
         "selector": {"list": list_name},
         "key": "title", "expected": expected_titles,
         "severity": "blocking",
         "label": f"renamed-list contains all expected titles"},
        *_no_irrelevant_edits_checks(list_name, n, completed_count=0),
    ]

    return Task(
        task_id="", flow="single_reminders_rename",
        apps=["Reminders"],
        instruction=instruction,
        verify=f"reminders.items[title={old_title}] → title={new_title}",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"'{list_name}' with {n} items (incl. '{old_title}')"],
            absent=[f"any reminder titled '{new_title}'"],
            noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=3,
        complexity=complexity_score(3),
        detail_level=0.0,
        params={"list": list_name, "items": titles, "old_title": old_title,
                "new_title": new_title,
                "expected_titles": expected_titles},
        verify_checks=verify_checks,
    )


# ═════════════════════════════════════════════════════════════════════════════
#  CALENDAR — Tier 1 single-action generators (Phase 2c)
# ═════════════════════════════════════════════════════════════════════════════
#
# Single-event, single-field mutations on the iOS default `"Calendar"`
# (only writable calendar on a fresh iOS 26.3 sim — see IOS_SIM_QUIRKS §16).
# Multi-calendar tasks (move-between-calendars, delete-by-calendar) land
# in T2/3 once `create_calendar` Swift command lands as a prereq.
#
# Verification pattern (per 2026-05-20 T1 critic):
#   1. attribute_eq on the target (the requested field has the requested value)
#   2. count on the requested change (catches add/delete-then-recreate)
#   3. subset(key=title) on the corpus (catches accidental rename of survivors)
#   4. identity(compare_fields=…, exclude_match={identifier: target}) over the
#      distractors — catches edits to any other event's title / start / end /
#      all_day / location / notes. Replaces Reminders' `count(field=None)==n`
#      pattern because Calendar events have always-set start/end/calendar
#      fields and that pattern can't certify them.
#
# Identity checks require a BaselineSnapshot captured between
# apply_initial_state and the agent's first action. sibb_replay and
# sibb_episode.run_episode_async already wire this in via
# `_baseline_resources_for` + `BaselineSnapshot.capture`.


_CAL_DISTRACTOR_FIELDS = ["title", "start_iso", "end_iso",
                           "all_day", "location", "notes",
                           "calendar", "url"]


def _calendar_spec(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build a Calendar InitialState.spec list: one `event` entry per
    item. Each `events` dict is a kwargs bundle for `CalendarEvent`
    (title + start_iso + end_iso required; calendar/all_day/location/
    notes optional). No `list` setup needed — the default `"Calendar"`
    calendar always exists on a fresh sim."""
    spec: List[Dict[str, Any]] = []
    for ev in events:
        entry: Dict[str, Any] = {
            "app": "Calendar", "type": "event",
            "title":     ev["title"],
            "start_iso": ev["start_iso"],
            "end_iso":   ev["end_iso"],
        }
        for k in ("calendar", "all_day", "location", "notes",
                   "url", "recurrence"):
            if ev.get(k) is not None:
                entry[k] = ev[k]
        spec.append(entry)
    return spec


def _tomorrow_date():
    """Date object for tomorrow (local)."""
    return _dt.date.today() + _dt.timedelta(days=1)


# Federal + commonly-displayed US holidays iOS Calendar.app renders
# from the read-only "US Holidays" calendar (see IOS_SIM_QUIRKS §16).
# Seeding events on these days causes set_equals to fail in T5 tasks
# because the agent's UI view includes the holiday alongside our
# seeded events. Resample to skip these dates.
_US_HOLIDAYS = {
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-14", "2026-02-16",
    "2026-03-17", "2026-04-01", "2026-04-22", "2026-05-05",
    "2026-05-10", "2026-05-25", "2026-06-14", "2026-06-19",
    "2026-06-21", "2026-07-04", "2026-09-07", "2026-10-12",
    "2026-10-31", "2026-11-02", "2026-11-11", "2026-11-26",
    "2026-12-24", "2026-12-25", "2026-12-31",
    # 2027 (rough — refresh year-by-year)
    "2027-01-01", "2027-01-18", "2027-02-14", "2027-02-15",
    "2027-03-17", "2027-04-01", "2027-04-22", "2027-05-05",
    "2027-05-09", "2027-05-31", "2027-06-14", "2027-06-19",
    "2027-07-04", "2027-09-06", "2027-10-11", "2027-10-31",
    "2027-11-02", "2027-11-11", "2027-11-25", "2027-12-24",
    "2027-12-25", "2027-12-31",
}


def _calendar_anchor_date():
    """Random date in [today+1, today+30], avoiding US holidays that
    iOS Calendar.app would render alongside our seeded events.

    Per 2026-05-21 T2/3 critic 4 / S1 ("tomorrow-only bias"). Generators
    that previously seeded on tomorrow now spread across the next month,
    defeating the "Calendar = tomorrow" prior.

    Range starts at today+1 (not today=0) so events are never visible
    on the default Calendar.app landing view — the agent always has
    to navigate at least one day forward.

    US Holidays filter added post-T5 critic 2 — iOS renders ~30
    days/year as US Holidays from its read-only system calendar; agents
    see them alongside our seeded events, breaking strict-set verifiers
    (IOS_SIM_QUIRKS §16 + §18). Re-sample if a holiday lands; fall back
    to tomorrow if 30 tries fail (extremely unlikely in practice)."""
    for _ in range(30):
        d = _dt.date.today() + _dt.timedelta(days=random.randint(1, 30))
        if d.isoformat() not in _US_HOLIDAYS:
            return d
    return _dt.date.today() + _dt.timedelta(days=1)


def _iso_time(d, hour: int, minute: int = 0) -> str:
    """Local-form ISO ('YYYY-MM-DDTHH:MM:SS') matching the Swift
    list_events round-trip canonical form."""
    return f"{d.isoformat()}T{hour:02d}:{minute:02d}:00"


def _day_reference(d) -> str:
    """Natural-language reference to a date.

    Returns "tomorrow" when d is exactly tomorrow (the most natural
    phrasing), otherwise the explicit weekday + month + day (e.g.
    "on Thursday July 09"). The caller embeds this verbatim:

        f"Schedule '{title}' {_day_reference(d)} at {nice_time}."

    This keeps the spread defended by _calendar_anchor_date intact —
    the anchor still ranges over [today+1, today+30] — while letting
    the prose use "tomorrow" when that's actually accurate. Resolves
    the 2026-06-11 "prose-says-tomorrow-but-anchor-is-random" bug
    that v3b sim run surfaced once the agent had a working clock."""
    days = (d - _dt.date.today()).days
    if days == 1:
        return "tomorrow"
    return f"on {d.strftime('%A %B %d')}"


def _pick_meeting_slots(d, n: int) -> List[Tuple[int, int]]:
    """Pick `n` distinct (start_hour, duration_min) slots on date `d`
    that don't overlap. Used to seed corpora of timed events with
    non-conflicting times. Returns (hour, duration_minutes) tuples
    so the caller can derive start_iso/end_iso.

    Raises ValueError if n > len(slot_hours). Previously the helper
    used `min(n, len(slot_hours))` and silently capped the sample —
    callers that sliced the returned list (T2.4 with n_total up to 12)
    got zero-length tail slices and the spec/verifier diverged
    invisibly. T2/3 critic 3 flagged this 2026-05-21."""
    slot_hours = [7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
    if n > len(slot_hours):
        raise ValueError(
            f"_pick_meeting_slots: requested {n} non-overlapping slots, "
            f"but only {len(slot_hours)} hour positions available "
            f"({slot_hours[0]}..{slot_hours[-1]}). Either reduce n or "
            f"expand the slot pool.")
    chosen_hours = sorted(random.sample(slot_hours, n))
    durations = [15, 30, 30, 45, 60, 60, 60, 90]
    return [(h, random.choice(durations)) for h in chosen_hours]


def _target_unchanged_checks(title_selector: str,
                              baseline: Dict[str, Any]
                              ) -> List[Dict[str, Any]]:
    """Emit attribute_eq guards asserting each field in `baseline`
    on the target (selected by title) matches its baseline value.

    Closes a cheat-path that `_event_distractor_identity_check` alone
    leaves open: `exclude_match` drops the target from BOTH sides of
    the distractor-identity comparison, so any field on the target NOT
    referenced by an explicit attribute_eq check can be mutated freely.

    Concrete cheats this helper closes:
      • change_event_title — agent renames AND adds notes to target.
      • set_event_location — agent sets location AND moves target's time.
      • change_event_time — agent moves time AND adds notes to target.
      • toggle_event_all_day — agent toggles AND adds spurious notes.

    Generator passes the AFTER-state title (e.g. new_title for renames,
    target_title otherwise) plus a `{field: baseline_value}` dict for
    every non-mutated field. EventKit's `e.location ?? ""` and
    `e.notes ?? ""` mean empty string represents 'unset'."""
    return [
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"title": title_selector},
         "attr": field, "value": value,
         "severity": "blocking",
         "label": f"'{title_selector}' {field} unchanged from baseline"}
        for field, value in baseline.items()
    ]


def _event_distractor_recurrence_absent_checks(
        non_target_titles: List[str]) -> List[Dict[str, Any]]:
    """Per-distractor `attribute_absent(recurrence)` sentinels.

    `_CAL_DISTRACTOR_FIELDS` does NOT include `recurrence` because
    that field is a dict, and `_signature_set` would build a tuple
    containing a dict — unhashable → `set()` raises TypeError on the
    cheat path (latent crash, found by T4b critic 3 pre-impl review
    2026-05-21). Per-distractor `attribute_absent` sentinels are the
    same pattern Reminders T4b uses to certify "no other reminder
    gained a recurrence rule."

    Caller passes the list of non-target titles. Each title gets one
    blocking attribute_absent check."""
    return [
        {"kind": "attribute_absent",
         "resource": "calendar.events",
         "selector": {"title": t},
         "attr": "recurrence",
         "severity": "blocking",
         "label": f"'{t}' has no recurrence rule"}
        for t in non_target_titles
    ]


def _event_distractor_identity_check(
        target_value: Any,
        *, key: str = "title",
        label: str = "no irrelevant edits",
        ) -> Dict[str, Any]:
    """Build an `identity` check that asserts every event EXCEPT the
    target (filtered by `key=target_value`) has byte-equal field values
    to the pre-task baseline. Catches the agent renaming a sibling,
    moving its time, flipping all_day, setting location/notes, etc.

    `key` defaults to `"title"` because titles are unique within a
    corpus AND stable across the agent's edits in most T1 generators.
    For RENAME tasks (gen_change_event_title) the title itself changes
    mid-task — use `key="start_iso"` instead, which is invariant across
    rename and still unique because slots are non-overlapping.

    Why exclude by a stable scalar instead of identifier? At
    spec-apply time the generator doesn't know event identifiers
    (assigned by EventKit/fake on save), so we exclude by a value
    the generator emitted into the spec."""
    return {
        "kind": "identity",
        "resource": "calendar.events",
        "selector": {},
        "compare_fields": list(_CAL_DISTRACTOR_FIELDS),
        "exclude_match": {key: target_value},
        "severity": "blocking",
        "label": label,
    }


# ─── Contacts distractor / identity helpers (Phase 2 prereqs, 2026-05-24) ──
#
# Parallel to the Calendar `_CAL_DISTRACTOR_FIELDS` + `_target_unchanged_checks`
# + `_event_distractor_identity_check`. Same pattern, different field set.
#
# Identity check on Contacts depends on `_signature_set` JSON-serializing
# multi-value list fields (phones/emails/postal_addresses/urls/dates) so the
# tuple-of-values stays hashable. Verified by L1 tests + the Phase 1 smoke
# probe; CHECK_KINDS now also routes `attribute_set_equals` for per-record
# multi-value comparisons that need digits_only_keys / time_keys
# canonicalization.

_CONTACT_DISTRACTOR_FIELDS = [
    "given_name", "family_name", "middle_name", "nickname",
    "phonetic_given_name", "phonetic_family_name", "phonetic_middle_name",
    "organization", "job_title", "department", "birthday",
    "phones", "emails", "postal_addresses", "urls", "dates",
]


def _contact_spec(contacts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build a Contacts InitialState.spec list — one entry per contact.
    Each `contacts` dict is a kwargs bundle for the `Contact` spec class
    (given_name + family_name required; optional fields passed through
    to the Swift handler verbatim). Mirrors `_calendar_spec`."""
    spec: List[Dict[str, Any]] = []
    for c in contacts:
        entry: Dict[str, Any] = {
            "app": "Contacts", "type": "contact",
            "given_name":  c.get("given_name", ""),
            "family_name": c.get("family_name", ""),
        }
        for k in ("middle_name", "nickname",
                   "phonetic_given_name", "phonetic_family_name",
                   "phonetic_middle_name",
                   "phone", "email",  # legacy single-value
                   "organization", "job_title", "department",
                   "birthday",
                   "phones", "emails", "postal_addresses",
                   "urls", "dates"):
            if c.get(k) is not None:
                entry[k] = c[k]
        spec.append(entry)
    return spec


# Small inline data dictionaries (Contacts-Phase 2 only — moved into
# main C if these go beyond the first 5 generators).
_CONTACT_ORGS = [
    "Acme Corp", "Initech", "Stark Industries", "Wayne Enterprises",
    "Soylent", "Globex", "Hooli", "Pied Piper", "Wonka Industries",
    "Cyberdyne",
]
_CONTACT_JOB_TITLES = [
    "Engineer", "Manager", "Designer", "Analyst", "Director",
    "Specialist", "Consultant", "Coordinator", "Lead",
]
_CONTACT_DEPARTMENTS = [
    "Engineering", "Sales", "Marketing", "Operations", "Product",
    "Finance", "HR", "Research",
]
_CONTACT_STREETS = [
    "174 Arbor St", "1 Apple Park Way", "350 5th Ave", "1600 Amphitheatre Pkwy",
    "742 Evergreen Terrace", "221 Baker St", "12 Grimmauld Pl",
    "1955 Hill Valley Rd", "1247 N Sycamore Ave", "55 Music Row",
]
_CONTACT_CITIES = [
    ("Cupertino", "CA", "95014"),
    ("Mountain View", "CA", "94043"),
    ("New York", "NY", "10001"),
    ("Springfield", "OR", "97477"),
    ("Brooklyn", "NY", "11201"),
    ("Seattle", "WA", "98101"),
    ("Chicago", "IL", "60601"),
    ("Boston", "MA", "02108"),
]


def _random_contact_phone() -> str:
    """Random US 10-digit phone string, formatted hyphenated."""
    return f"{random.choice([415, 510, 650, 408, 212, 718])}-555-{random.randint(1000, 9999):04d}"


def _random_birthday_iso() -> str:
    """Random "YYYY-MM-DD" birthday between 1950 and 2005."""
    y = random.randint(1950, 2005)
    m = random.randint(1, 12)
    # Day-of-month safety: 28 covers all months.
    d = random.randint(1, 28)
    return f"{y:04d}-{m:02d}-{d:02d}"


def _pick_distinct_contact_names(n: int) -> List[Tuple[str, str]]:
    """Pick n distinct (given_name, family_name) pairs. Avoids "Mom"/"Dad"
    (single-name conventions don't fit family_name)."""
    firsts = [f for f in C["contact_first"] if f not in ("Mom", "Dad")]
    pairs: set = set()
    while len(pairs) < n:
        pairs.add((random.choice(firsts), random.choice(C["contact_last"])))
    return list(pairs)



# Scalar Contacts fields that the Swift `list_contacts` row OMITS when
# empty (sparse emission, mirrors list_reminders convention). `r.get(attr)`
# returns None for these when absent; an attribute_eq check with value=""
# would then false-fail (None != ""). _contact_unchanged_checks coerces
# baseline "" → None for these fields so callers can use either polarity.
# `given_name` / `family_name` / `identifier` are ALWAYS emitted, even
# when empty, so they don't appear here.
_CONTACT_SPARSE_SCALAR_FIELDS = frozenset({
    "middle_name", "nickname",
    "phonetic_given_name", "phonetic_family_name", "phonetic_middle_name",
    "organization", "job_title", "department",
    "birthday", "phone", "email",
})


def _contact_unchanged_checks(*, given_name: str, family_name: str,
                                baseline: Dict[str, Any]
                                ) -> List[Dict[str, Any]]:
    """Emit attribute_eq / attribute_set_equals guards asserting each
    field in `baseline` on the target (selected by given_name +
    family_name) matches its baseline value. Parallel to
    `_target_unchanged_checks` for Calendar — closes the cheat-path
    that `_contact_distractor_identity_check`'s exclude_match opens
    on the target itself.

    For list-valued fields (phones / emails / postal_addresses / urls /
    dates), emits `attribute_set_equals` with the relevant item_keys
    schema; scalar fields use `attribute_eq` exact match. Caller passes
    the post-mutation given_name/family_name (so it works for rename
    tasks where the name itself changes — the target lookup is by the
    AFTER-state name)."""
    selector = {"given_name": given_name, "family_name": family_name}
    checks: List[Dict[str, Any]] = []
    list_field_keys = {
        "phones":           ["label", "value"],
        "emails":           ["label", "value"],
        "urls":             ["label", "value"],
        "dates":            ["label", "iso"],
        "postal_addresses": ["label", "street", "city", "state",
                              "postal_code", "country"],
    }
    list_field_digit_keys = {"phones": ["value"]}
    for field, value in baseline.items():
        if field in list_field_keys:
            checks.append({
                "kind": "attribute_set_equals",
                "resource": "contacts.all",
                "selector": dict(selector),
                "attr": field,
                "expected": value,
                "item_keys": list_field_keys[field],
                "digits_only_keys": list_field_digit_keys.get(field),
                "severity": "blocking",
                "label": f"'{given_name} {family_name}' {field} "
                         f"unchanged from baseline",
            })
        else:
            # Sparse-row polarity: Swift omits empty scalar fields, so
            # `r.get(attr)` returns None for absent fields. Coerce
            # baseline "" → None for known-sparse scalars so callers
            # can use either form to mean "unset".
            expected = value
            if field in _CONTACT_SPARSE_SCALAR_FIELDS and value == "":
                expected = None
            checks.append({
                "kind": "attribute_eq",
                "resource": "contacts.all",
                "selector": dict(selector),
                "attr": field, "value": expected,
                "severity": "blocking",
                "label": f"'{given_name} {family_name}' {field} "
                         f"unchanged from baseline",
            })
    return checks


def _contact_distractor_identity_check(
        *, target_given_name: str, target_family_name: str,
        label: str = "no irrelevant edits on distractor contacts",
        ) -> Dict[str, Any]:
    """Build an `identity` check that asserts every contact EXCEPT the
    target (excluded by given_name + family_name) has byte-equal field
    values to the pre-task baseline. Catches cheats where the agent
    mutates a sibling contact.

    Exclusion is keyed on (given_name, family_name) — both must match
    to drop a row. For Contacts T1 generators this is unique enough;
    if a future generator seeds two contacts with the same name,
    switch to identifier-based exclusion (requires knowing the CN
    identifier at spec-apply time, which CN assigns on save)."""
    return {
        "kind": "identity",
        "resource": "contacts.all",
        "selector": {},
        "compare_fields": list(_CONTACT_DISTRACTOR_FIELDS),
        "exclude_match": {"given_name": target_given_name,
                           "family_name": target_family_name},
        "severity": "blocking",
        "label": label,
    }


# ── T1.1 — create_event_with_title_time ──────────────────────────────────────

def gen_create_event_with_title_time():
    """Create one event with a given title at a given time on tomorrow.
    Strict: no other events should appear, no irrelevant fields set."""
    title  = pick("event_titles")
    d      = _calendar_anchor_date()
    hour   = random.choice([9, 10, 11, 13, 14, 15, 16])
    dur    = random.choice([30, 45, 60])
    start  = _iso_time(d, hour)
    # End hour might roll over; for T1 keep within the hour by picking
    # durations that don't cross midnight (always true here).
    end_h, end_m = (hour + (dur // 60), dur % 60)
    end    = _iso_time(d, end_h, end_m)
    spec   = _calendar_spec([])
    spec  += _springboard_noise()

    nice_time = f"{hour % 12 or 12}{'pm' if hour >= 12 else 'am'}"
    end_nice  = f"{end_h % 12 or 12}:{end_m:02d}{'pm' if end_h >= 12 else 'am'}"
    # 2026-06-11: prose-vs-anchor mismatch fix (see _day_reference).
    when = _day_reference(d)  # "tomorrow" if d==today+1, else "on <date>"
    instruction = random.choice([
        f"Open Calendar. Create an event titled '{title}' {when} "
        f"from {nice_time} to {end_nice}.",
        f"Open Calendar. Add a {dur}-minute event {when} at "
        f"{nice_time} called '{title}'.",
        f"Open Calendar. Schedule '{title}' {when} at {nice_time}.",
    ])

    verify_checks = [
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {},
         "op": "eq", "n": 1,
         "severity": "blocking",
         "label": "exactly one event in the writable calendar"},
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"title": title},
         "attr": "start_iso", "value": start,
         "severity": "blocking",
         "label": f"'{title}' starts at {start}"},
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"title": title},
         "attr": "end_iso", "value": end,
         "severity": "blocking",
         "label": f"'{title}' ends at {end}"},
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"title": title},
         "attr": "all_day", "value": False,
         "severity": "blocking",
         "label": f"'{title}' is NOT an all-day event"},
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {"location": ""},
         "op": "eq", "n": 1,
         "severity": "blocking",
         "label": "no extraneous location set"},
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {"notes": ""},
         "op": "eq", "n": 1,
         "severity": "blocking",
         "label": "no extraneous notes set"},
    ]

    return Task(
        task_id="", flow="single_calendar_create",
        apps=["Calendar"],
        instruction=instruction,
        verify=f"calendar.events[title={title}] exists with start={start}, end={end}",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[],
            absent=[],
            noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=4,
        complexity=complexity_score(4),
        detail_level=0.0,
        params={"title": title, "start_iso": start, "end_iso": end,
                "duration_minutes": dur},
        verify_checks=verify_checks,
    )


# ── T1.2 — delete_specific_event ─────────────────────────────────────────────

def gen_delete_specific_event():
    """Delete one named event from a corpus of 7-10. Strict: every
    distractor event byte-equal to baseline."""
    d = _calendar_anchor_date()
    n = random.choice([7, 8, 9, 10])
    titles = pickn("event_titles", n)
    target = random.choice(titles)
    slots = _pick_meeting_slots(d, n)
    events = []
    for t, (hour, dur) in zip(titles, slots):
        end_h = hour + (dur // 60)
        end_m = dur % 60
        events.append({"title": t,
                        "start_iso": _iso_time(d, hour),
                        "end_iso":   _iso_time(d, end_h, end_m)})
    spec  = _calendar_spec(events)
    spec += _springboard_noise()

    instruction = random.choice([
        f"Open Calendar. Delete the event titled '{target}' from my calendar.",
        f"Open Calendar. Remove my '{target}' meeting.",
        f"Open Calendar. Cancel '{target}' tomorrow.",
    ])

    survivors = [t for t in titles if t != target]
    verify_checks = [
        # The deletion happened.
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {"title": target},
         "op": "eq", "n": 0,
         "severity": "blocking",
         "label": f"'{target}' removed"},
        # No collateral deletes / creates.
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {},
         "op": "eq", "n": n - 1,
         "severity": "blocking",
         "label": f"calendar has exactly {n - 1} events left"},
        # Every survivor's title still present.
        {"kind": "subset",
         "resource": "calendar.events",
         "selector": {},
         "key": "title", "expected": survivors,
         "severity": "blocking",
         "label": f"all {n - 1} non-target titles survive"},
        # Distractor-signature: byte-equal field tuples on every non-target.
        _event_distractor_identity_check(
            target,
            label="distractors byte-equal to baseline"),
    ]

    return Task(
        task_id="", flow="single_calendar_delete",
        apps=["Calendar"],
        instruction=instruction,
        verify=f"calendar.events[title={target}] absent",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"{n} events on {d.isoformat()}"],
            absent=[], noise_records=[],
            setup_commands=[], spec=spec,
            expected_behavior="complete",
        ),
        steps=3,
        complexity=complexity_score(3),
        detail_level=0.0,
        params={"titles": titles, "target": target,
                "survivors": survivors, "date": d.isoformat()},
        verify_checks=verify_checks,
    )


# ── T1.3 — change_event_title ────────────────────────────────────────────────

def gen_change_event_title():
    """Rename one specific event. Strict: every other event byte-equal
    to baseline, and target's start/end/all_day/location/notes
    unchanged."""
    d = _calendar_anchor_date()
    n = random.choice([7, 8, 9, 10])
    pool = pickn("event_titles", n + 1)
    titles, new_title = pool[:n], pool[n]
    old_title = random.choice(titles)
    slots = _pick_meeting_slots(d, n)
    events = []
    target_start = target_end = None
    for t, (hour, dur) in zip(titles, slots):
        end_h, end_m = hour + (dur // 60), dur % 60
        s = _iso_time(d, hour)
        e = _iso_time(d, end_h, end_m)
        events.append({"title": t, "start_iso": s, "end_iso": e})
        if t == old_title:
            target_start, target_end = s, e
    spec  = _calendar_spec(events)
    spec += _springboard_noise()

    instruction = random.choice([
        f"Open Calendar. Rename the '{old_title}' event to '{new_title}'.",
        f"Open Calendar. Change '{old_title}' to '{new_title}'.",
        f"Open Calendar. Update the title of '{old_title}' — it should "
        f"say '{new_title}' instead.",
    ])

    expected_titles = [t for t in titles if t != old_title] + [new_title]
    verify_checks = [
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {"title": old_title},
         "op": "eq", "n": 0,
         "severity": "blocking",
         "label": f"old title '{old_title}' gone"},
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {"title": new_title},
         "op": "eq", "n": 1,
         "severity": "blocking",
         "label": f"new title '{new_title}' appears once"},
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {},
         "op": "eq", "n": n,
         "severity": "blocking",
         "label": f"calendar still has exactly {n} events"},
        {"kind": "subset",
         "resource": "calendar.events",
         "selector": {},
         "key": "title", "expected": expected_titles,
         "severity": "blocking",
         "label": "every other event title preserved"},
        # Target's other fields unchanged. start_iso/end_iso are the
        # "documented" guards; all_day/location/notes close the cheat
        # path where exclude_match would otherwise drop the target.
        *_target_unchanged_checks(new_title, {
            "start_iso": target_start,
            "end_iso":   target_end,
            "all_day":   False,
            "location":  "",
            "notes":     "",
            "url":       "",
        }),
        # Distractor preservation. CAN'T exclude by title — old title
        # lives in baseline, new title lives in current. Use start_iso,
        # which is invariant across rename AND unique (slots are
        # non-overlapping).
        _event_distractor_identity_check(
            target_start, key="start_iso",
            label="distractors byte-equal to baseline "
                  "(rename target excluded by start_iso)"),
    ]

    return Task(
        task_id="", flow="single_calendar_rename",
        apps=["Calendar"],
        instruction=instruction,
        verify=f"calendar.events rename '{old_title}' → '{new_title}'",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"{n} events; one titled '{old_title}'"],
            absent=[], noise_records=[],
            setup_commands=[], spec=spec,
            expected_behavior="complete",
        ),
        steps=3,
        complexity=complexity_score(3),
        detail_level=0.0,
        params={"titles": titles, "old_title": old_title,
                "new_title": new_title, "date": d.isoformat()},
        verify_checks=verify_checks,
    )


# ── T1.4 — set_event_location ────────────────────────────────────────────────

def gen_set_event_location():
    """Set a location on one specific event (others have empty location).
    Strict: only the target gains a location, every other field on
    every event unchanged including target's start/end/all_day/notes."""
    d = _calendar_anchor_date()
    # Bumped 4-6 → 6-8 per 2026-05-21 critic — location confusability
    # benefits from a denser corpus.
    n = random.choice([6, 7, 8])
    titles = pickn("event_titles", n)
    target = random.choice(titles)
    location = pick("event_locations")
    slots = _pick_meeting_slots(d, n)
    events = []
    target_start = target_end = None
    for t, (hour, dur) in zip(titles, slots):
        end_h, end_m = hour + (dur // 60), dur % 60
        s = _iso_time(d, hour)
        e = _iso_time(d, end_h, end_m)
        events.append({"title": t, "start_iso": s, "end_iso": e})
        if t == target:
            target_start, target_end = s, e
    spec  = _calendar_spec(events)
    spec += _springboard_noise()

    instruction = random.choice([
        f"Open Calendar. Set the location of the '{target}' event to "
        f"'{location}'.",
        f"Open Calendar. '{target}' is happening at {location} — add that "
        f"to the event.",
        f"Open Calendar. Put '{location}' on the '{target}' event as the "
        f"location.",
    ])

    verify_checks = [
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"title": target},
         "attr": "location", "value": location,
         "severity": "blocking",
         "label": f"'{target}' location set"},
        # No add/delete.
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {},
         "op": "eq", "n": n,
         "severity": "blocking",
         "label": f"calendar has exactly {n} events"},
        # All titles preserved.
        {"kind": "subset",
         "resource": "calendar.events",
         "selector": {},
         "key": "title", "expected": titles,
         "severity": "blocking",
         "label": "every title preserved"},
        # Target's non-mutated fields unchanged from baseline. Closes
        # the cheat where exclude_match drops target from the distractor
        # identity check — without these, agent could set location AND
        # silently shift time / toggle all_day / add notes on target.
        *_target_unchanged_checks(target, {
            "start_iso": target_start,
            "end_iso":   target_end,
            "all_day":   False,
            "notes":     "",
            "url":       "",
        }),
        # Distractor byte-equality (excludes target). Includes location
        # field in the compare_fields, so distractors gaining a location
        # gets caught here — drops the redundant count(location="") guard.
        _event_distractor_identity_check(
            target,
            label="distractors byte-equal to baseline (target's location excluded)"),
    ]

    return Task(
        task_id="", flow="single_calendar_set_location",
        apps=["Calendar"],
        instruction=instruction,
        verify=f"calendar.events[title={target}].location == '{location}'",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"{n} events with no location"],
            absent=[], noise_records=[],
            setup_commands=[], spec=spec,
            expected_behavior="complete",
        ),
        steps=3,
        complexity=complexity_score(3),
        detail_level=0.0,
        params={"titles": titles, "target": target,
                "location": location, "date": d.isoformat()},
        verify_checks=verify_checks,
    )


# ── T1.5 — change_event_time ─────────────────────────────────────────────────
#
# Critic-added 2026-05-20. Highest-frequency real-world calendar edit,
# strictly easier than toggle_all_day (no midnight rewrite). Move start
# and end while preserving duration. The agent could also "delete + re-
# create at new time" — fine, verifier doesn't care about *how*.

def gen_change_event_time():
    """Move one event's start time on the same day. Duration preserved.
    Strict: target's other fields unchanged, distractor events
    byte-equal to baseline."""
    d = _calendar_anchor_date()
    n = random.choice([5, 6, 7])
    titles = pickn("event_titles", n)
    target = random.choice(titles)
    slots = _pick_meeting_slots(d, n)
    events = []
    target_dur = None
    for t, (hour, dur) in zip(titles, slots):
        end_h, end_m = hour + (dur // 60), dur % 60
        events.append({"title": t,
                        "start_iso": _iso_time(d, hour),
                        "end_iso":   _iso_time(d, end_h, end_m)})
        if t == target:
            target_dur = dur
            target_old_hour = hour
    spec  = _calendar_spec(events)
    spec += _springboard_noise()

    # Pick a new hour that doesn't overlap any other event on the day.
    used = {h for h, _ in slots if h != target_old_hour}
    candidates = [h for h in [8, 9, 10, 11, 13, 14, 15, 16, 17, 18]
                  if h not in used and h != target_old_hour]
    new_hour = random.choice(candidates)
    new_start = _iso_time(d, new_hour)
    new_end_h, new_end_m = new_hour + (target_dur // 60), target_dur % 60
    new_end = _iso_time(d, new_end_h, new_end_m)

    new_nice = f"{new_hour % 12 or 12}{'pm' if new_hour >= 12 else 'am'}"
    # 2026-06-11: prose-vs-anchor mismatch fix (see _day_reference).
    when = _day_reference(d)  # "tomorrow" if d==today+1, else "on <date>"
    instruction = random.choice([
        f"Open Calendar. Move '{target}' to start at {new_nice} {when}.",
        f"Open Calendar. Reschedule the '{target}' {when} to {new_nice}.",
        f"Open Calendar. '{target}' {when} needs to move — {new_nice} "
        f"works better.",
    ])

    verify_checks = [
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"title": target},
         "attr": "start_iso", "value": new_start,
         "severity": "blocking",
         "label": f"'{target}' starts at the new time"},
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"title": target},
         "attr": "end_iso", "value": new_end,
         "severity": "blocking",
         "label": f"'{target}' ends at the new time (duration preserved)"},
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {},
         "op": "eq", "n": n,
         "severity": "blocking",
         "label": f"calendar has exactly {n} events"},
        {"kind": "subset",
         "resource": "calendar.events",
         "selector": {},
         "key": "title", "expected": titles,
         "severity": "blocking",
         "label": "every title preserved"},
        # Target's non-mutated fields unchanged from baseline. Closes
        # the cheat where exclude_match drops target from distractor
        # identity — without these, agent could move time AND add notes
        # / set location / toggle all_day on target.
        *_target_unchanged_checks(target, {
            "all_day":   False,
            "location":  "",
            "notes":     "",
            "url":       "",
        }),
        _event_distractor_identity_check(
            target,
            label="distractors byte-equal to baseline (target's time excluded)"),
    ]

    return Task(
        task_id="", flow="single_calendar_change_time",
        apps=["Calendar"],
        instruction=instruction,
        verify=f"calendar.events[title={target}] moves to {new_start}",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"{n} events on {d.isoformat()}"],
            absent=[], noise_records=[],
            setup_commands=[], spec=spec,
            expected_behavior="complete",
        ),
        steps=3,
        complexity=complexity_score(3),
        detail_level=0.0,
        params={"titles": titles, "target": target,
                "new_start_iso": new_start, "new_end_iso": new_end,
                "duration_minutes": target_dur, "date": d.isoformat()},
        verify_checks=verify_checks,
    )


# ── T1.6 — toggle_event_all_day ──────────────────────────────────────────────
#
# iOS side-effect (documented in IOS_SIM_QUIRKS §16 implications): when
# all_day=true, iOS overwrites start to D 00:00 and end to D+1 00:00
# (date-only YYYY-MM-DD in the list_events round-trip). Generator
# pre-aligns the target event to date D so the rewrite is a no-op-shaped
# assertion — verifier checks all_day=True AND the exact start/end pair
# the rewrite produces. Both expected, both asserted.

def gen_toggle_event_all_day():
    """Convert one timed event to all-day. Distractors stay timed.
    Strict: every other event byte-equal to baseline."""
    d = _calendar_anchor_date()
    n = random.choice([4, 5, 6])
    titles = pickn("event_titles", n)
    target = random.choice(titles)
    slots = _pick_meeting_slots(d, n)
    events = []
    for t, (hour, dur) in zip(titles, slots):
        end_h, end_m = hour + (dur // 60), dur % 60
        events.append({"title": t,
                        "start_iso": _iso_time(d, hour),
                        "end_iso":   _iso_time(d, end_h, end_m)})
    spec  = _calendar_spec(events)
    spec += _springboard_noise()

    instruction = random.choice([
        f"Open Calendar. Make '{target}' an all-day event.",
        f"Open Calendar. Convert '{target}' to all-day.",
        f"Open Calendar. '{target}' should span the whole day, not just "
        f"a meeting slot.",
    ])

    # Post-rewrite values iOS produces for a single-day all-day event.
    # EMPIRICALLY VERIFIED 2026-05-21 via sibb_probe_calendar.py Q4:
    # iOS stores all-day `endDate` as "last second of the inclusive
    # last day" — when formatted date-only, end_iso EQUALS start_iso
    # for a one-day all-day event (NOT start+1 as one might assume from
    # the "exclusive end" input convention). See IOS_SIM_QUIRKS §16.
    expected_start = d.isoformat()
    expected_end   = d.isoformat()

    verify_checks = [
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"title": target},
         "attr": "all_day", "value": True,
         "severity": "blocking",
         "label": f"'{target}' is now all-day"},
        # iOS rewrites start/end on all_day flip — assert the expected
        # rewrite happened (date-only round-trip per Swift list_events
        # canonicalization).
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"title": target},
         "attr": "start_iso", "value": expected_start,
         "severity": "blocking",
         "label": f"'{target}' start rewritten to {expected_start}"},
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"title": target},
         "attr": "end_iso", "value": expected_end,
         "severity": "blocking",
         "label": f"'{target}' end rewritten to {expected_end}"},
        # Exactly 1 all-day event total.
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {"all_day": True},
         "op": "eq", "n": 1,
         "severity": "blocking",
         "label": "exactly one all-day event"},
        # No add/delete.
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {},
         "op": "eq", "n": n,
         "severity": "blocking",
         "label": f"calendar has exactly {n} events"},
        # All titles preserved.
        {"kind": "subset",
         "resource": "calendar.events",
         "selector": {},
         "key": "title", "expected": titles,
         "severity": "blocking",
         "label": "every title preserved"},
        # Target's non-mutated fields (location, notes) unchanged.
        # start/end ARE mutated by the all_day flip; assertions above
        # cover the rewrite. Closes the cheat where agent toggles AND
        # adds spurious notes.
        *_target_unchanged_checks(target, {
            "location": "",
            "notes":    "",
            "url":      "",
        }),
        # Distractor byte-equality (excludes target).
        _event_distractor_identity_check(
            target,
            label="distractors byte-equal to baseline (target's all_day flip excluded)"),
    ]

    return Task(
        task_id="", flow="single_calendar_toggle_all_day",
        apps=["Calendar"],
        instruction=instruction,
        verify=f"calendar.events[title={target}].all_day == True",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"{n} timed events; one being toggled all-day"],
            absent=[], noise_records=[],
            setup_commands=[], spec=spec,
            expected_behavior="complete",
        ),
        steps=3,
        complexity=complexity_score(3),
        detail_level=0.0,
        params={"titles": titles, "target": target,
                "expected_start_iso": expected_start,
                "expected_end_iso": expected_end,
                "date": d.isoformat()},
        verify_checks=verify_checks,
    )


# ═════════════════════════════════════════════════════════════════════════════
#  CALENDAR — Tier 2/3 bulk + structural generators (Phase 2c)
# ═════════════════════════════════════════════════════════════════════════════
#
# Bulk filter+delete and coupled multi-step mutations. Two of four
# generators land first (no extra prereq); `delete_events_in_calendar`
# and `move_event_between_calendars` ship after the `create_calendar`
# Swift command lands (multi-calendar tasks need ≥2 writable calendars).


def _day_window(d):
    """Return (window_start_iso, window_end_iso) covering the full day
    `d` in the canonical local-form ISO. Used for windowed selectors
    on calendar.events resources."""
    return (_iso_time(d, 0, 0), _iso_time(d, 23, 59))


# ── T2.1 — delete_all_events_on_date ─────────────────────────────────────────

def gen_delete_all_events_on_date():
    """Delete every event on a specific date. Strict: events on
    OTHER dates byte-equal to baseline, no add/delete elsewhere."""
    d_target = _calendar_anchor_date()
    d_other  = d_target + _dt.timedelta(days=1)
    n_target = random.choice([3, 4, 5])
    n_other  = random.choice([4, 5, 6])
    all_titles = pickn("event_titles", n_target + n_other)
    titles_target = all_titles[:n_target]
    titles_other  = all_titles[n_target:]

    slots_target = _pick_meeting_slots(d_target, n_target)
    slots_other  = _pick_meeting_slots(d_other,  n_other)

    events = []
    for t, (h, dur) in zip(titles_target, slots_target):
        end_h, end_m = h + (dur // 60), dur % 60
        events.append({"title": t,
                        "start_iso": _iso_time(d_target, h),
                        "end_iso":   _iso_time(d_target, end_h, end_m)})
    for t, (h, dur) in zip(titles_other, slots_other):
        end_h, end_m = h + (dur // 60), dur % 60
        events.append({"title": t,
                        "start_iso": _iso_time(d_other, h),
                        "end_iso":   _iso_time(d_other, end_h, end_m)})

    spec  = _calendar_spec(events)
    spec += _springboard_noise()

    # Phrase the date in natural language. strftime("%A") gives day-of-week.
    day_name = d_target.strftime("%A")
    nice_date = d_target.strftime(f"%A, %B {d_target.day}")
    instruction = random.choice([
        f"Open Calendar. Delete every event scheduled for {nice_date}.",
        f"Open Calendar. Clear my calendar for {day_name} — remove all "
        f"events that day.",
        f"Open Calendar. I'm taking {nice_date} off; cancel every event "
        f"on that day.",
    ])

    target_window_start, target_window_end = _day_window(d_target)
    other_window_start,  other_window_end  = _day_window(d_other)

    verify_checks = [
        # All target-date events gone.
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {"start_iso": target_window_start,
                       "end_iso":   target_window_end},
         "op": "eq", "n": 0,
         "severity": "blocking",
         "label": f"no events remain on {d_target.isoformat()}"},
        # All other-date events still present (count).
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {"start_iso": other_window_start,
                       "end_iso":   other_window_end},
         "op": "eq", "n": n_other,
         "severity": "blocking",
         "label": f"{n_other} events remain on {d_other.isoformat()}"},
        # Total writable events = n_other (catches collateral creates
        # on a third date).
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {},
         "op": "eq", "n": n_other,
         "severity": "blocking",
         "label": f"only {n_other} events total"},
        # Other-date events byte-equal to baseline (catches "agent
        # deleted target-date events but ALSO shifted an other-date
        # event's time / changed title / added notes").
        {"kind": "identity",
         "resource": "calendar.events",
         "selector": {"start_iso": other_window_start,
                       "end_iso":   other_window_end},
         "compare_fields": list(_CAL_DISTRACTOR_FIELDS),
         "severity": "blocking",
         "label": f"other-date events byte-equal to baseline"},
    ]

    return Task(
        task_id="", flow="calendar_delete_all_on_date",
        apps=["Calendar"],
        instruction=instruction,
        verify=f"calendar.events on {d_target.isoformat()} all absent",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"{n_target} events on {d_target.isoformat()}, "
                      f"{n_other} events on {d_other.isoformat()}"],
            absent=[], noise_records=[],
            setup_commands=[], spec=spec,
            expected_behavior="complete",
        ),
        steps=max(3, n_target),
        complexity=complexity_score(max(3, n_target)),
        detail_level=0.0,
        params={"titles_target": titles_target,
                "titles_other":  titles_other,
                "date_target": d_target.isoformat(),
                "date_other":  d_other.isoformat()},
        verify_checks=verify_checks,
    )


# ── T2.2 — duplicate_event_to_next_week ──────────────────────────────────────

def gen_duplicate_event_to_next_week():
    """Copy one event 7 days forward (same time, same duration).
    Tests coupled mutation: read source + create new with shifted
    date. Source event preserved unchanged."""
    d_source = _calendar_anchor_date()
    d_next_week = d_source + _dt.timedelta(days=7)
    d_other  = d_source + _dt.timedelta(days=2)  # neither source nor +7
    n_source = random.choice([4, 5, 6])
    n_other  = random.choice([3, 4, 5])
    all_titles = pickn("event_titles", n_source + n_other)
    titles_source = all_titles[:n_source]
    titles_other  = all_titles[n_source:]
    target = random.choice(titles_source)

    slots_source = _pick_meeting_slots(d_source, n_source)
    slots_other  = _pick_meeting_slots(d_other,  n_other)

    events = []
    target_hour = target_dur = None
    for t, (h, dur) in zip(titles_source, slots_source):
        end_h, end_m = h + (dur // 60), dur % 60
        events.append({"title": t,
                        "start_iso": _iso_time(d_source, h),
                        "end_iso":   _iso_time(d_source, end_h, end_m)})
        if t == target:
            target_hour, target_dur = h, dur
    for t, (h, dur) in zip(titles_other, slots_other):
        end_h, end_m = h + (dur // 60), dur % 60
        events.append({"title": t,
                        "start_iso": _iso_time(d_other, h),
                        "end_iso":   _iso_time(d_other, end_h, end_m)})

    spec  = _calendar_spec(events)
    spec += _springboard_noise()

    # Expected duplicate landing: same hour, same duration, +7 days.
    end_h, end_m = target_hour + (target_dur // 60), target_dur % 60
    expected_start = _iso_time(d_next_week, target_hour)
    expected_end   = _iso_time(d_next_week, end_h, end_m)

    # Phrasings ALL anchor explicitly to the source date or to a
    # concrete next-week date — earlier versions used "next week" /
    # "7 days later" without a reference point, which ambiguously
    # interpreted as +7 from today vs +7 from source. Now every
    # phrasing names the target date (or the source date) so the
    # +7-from-source ground truth is unambiguous. T2/3 critic 4
    # also caught phrasing #3 ("weekly thing") as a lie — replaced
    # with a non-recurrence-implying phrasing.
    nice_next = d_next_week.strftime(f"%A %B {d_next_week.day}")
    nice_source = d_source.strftime(f"%A %B {d_source.day}")
    instruction = random.choice([
        f"Open Calendar. Copy '{target}' so the same event also "
        f"appears on {nice_next}.",
        f"Open Calendar. Duplicate '{target}' from {nice_source} to "
        f"{nice_next} — same time, same duration.",
        f"Open Calendar. Add a copy of '{target}' on {nice_next} at "
        f"the same time.",
    ])

    source_win_start, source_win_end = _day_window(d_source)
    other_win_start,  other_win_end  = _day_window(d_other)
    nextwk_win_start, nextwk_win_end = _day_window(d_next_week)

    verify_checks = [
        # Source date still has n_source events (no add/delete).
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {"start_iso": source_win_start,
                       "end_iso":   source_win_end},
         "op": "eq", "n": n_source,
         "severity": "blocking",
         "label": f"source date has {n_source} events"},
        # Source-date events byte-equal to baseline.
        {"kind": "identity",
         "resource": "calendar.events",
         "selector": {"start_iso": source_win_start,
                       "end_iso":   source_win_end},
         "compare_fields": list(_CAL_DISTRACTOR_FIELDS),
         "severity": "blocking",
         "label": "source-date events unchanged"},
        # Other-date distractors unchanged.
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {"start_iso": other_win_start,
                       "end_iso":   other_win_end},
         "op": "eq", "n": n_other,
         "severity": "blocking",
         "label": f"other-date has {n_other} events"},
        {"kind": "identity",
         "resource": "calendar.events",
         "selector": {"start_iso": other_win_start,
                       "end_iso":   other_win_end},
         "compare_fields": list(_CAL_DISTRACTOR_FIELDS),
         "severity": "blocking",
         "label": "other-date events unchanged"},
        # Exactly one event on +7 days (the duplicate).
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {"start_iso": nextwk_win_start,
                       "end_iso":   nextwk_win_end},
         "op": "eq", "n": 1,
         "severity": "blocking",
         "label": f"one event on {d_next_week.isoformat()}"},
        # The +7 event has the right title (windowed lookup).
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"start_iso": nextwk_win_start,
                       "end_iso":   nextwk_win_end},
         "attr": "title", "value": target,
         "severity": "blocking",
         "label": f"+7 event titled '{target}'"},
        # The +7 event has the right start_iso (same hour, +7 days).
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"start_iso": nextwk_win_start,
                       "end_iso":   nextwk_win_end},
         "attr": "start_iso", "value": expected_start,
         "severity": "blocking",
         "label": f"+7 event starts at {expected_start}"},
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"start_iso": nextwk_win_start,
                       "end_iso":   nextwk_win_end},
         "attr": "end_iso", "value": expected_end,
         "severity": "blocking",
         "label": f"+7 event ends at {expected_end} "
                  f"(duration preserved)"},
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"start_iso": nextwk_win_start,
                       "end_iso":   nextwk_win_end},
         "attr": "all_day", "value": False,
         "severity": "blocking",
         "label": "+7 event is timed, not all-day"},
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"start_iso": nextwk_win_start,
                       "end_iso":   nextwk_win_end},
         "attr": "location", "value": "",
         "severity": "blocking",
         "label": "+7 event has no spurious location"},
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"start_iso": nextwk_win_start,
                       "end_iso":   nextwk_win_end},
         "attr": "notes", "value": "",
         "severity": "blocking",
         "label": "+7 event has no spurious notes"},
        # Total writable events: source + other + 1 duplicate.
        # Catches a spurious copy to yet another day.
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {},
         "op": "eq", "n": n_source + n_other + 1,
         "severity": "blocking",
         "label": f"total events = {n_source + n_other + 1}"},
    ]

    return Task(
        task_id="", flow="calendar_duplicate_next_week",
        apps=["Calendar"],
        instruction=instruction,
        verify=f"calendar.events title={target} appears on "
                f"{d_source.isoformat()} AND {d_next_week.isoformat()}",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"{n_source} events on {d_source.isoformat()}, "
                      f"{n_other} on {d_other.isoformat()}"],
            absent=[], noise_records=[],
            setup_commands=[], spec=spec,
            expected_behavior="complete",
        ),
        steps=4,
        complexity=complexity_score(4),
        detail_level=0.0,
        params={"titles_source": titles_source,
                "titles_other":  titles_other,
                "target": target,
                "date_source": d_source.isoformat(),
                "date_other":  d_other.isoformat(),
                "date_next_week": d_next_week.isoformat(),
                "expected_start_iso": expected_start,
                "expected_end_iso":   expected_end,
                "duration_minutes": target_dur},
        verify_checks=verify_checks,
    )


# ── T2.4 — delete_events_in_calendar (multi-calendar) ───────────────────────

def gen_delete_events_in_calendar():
    """Delete every event in a specific user-created calendar while
    preserving events in other calendars. Requires create_calendar
    prereq (T2.3). Strict: target calendar's events all gone; other
    calendars byte-equal to baseline; the target calendar itself
    survives (deleting events ≠ deleting the calendar)."""
    d = _calendar_anchor_date()
    # Pick two distinct user-created calendar names; the third bucket
    # is the iOS default "Calendar".
    cal_pool = pickn("calendar_names", 2)
    target_cal = cal_pool[0]
    other_cal  = cal_pool[1]
    default_cal = "Calendar"

    n_target = random.choice([3, 4, 5])
    n_other  = random.choice([2, 3, 4])
    n_default = random.choice([2, 3])
    n_total = n_target + n_other + n_default

    all_titles = pickn("event_titles", n_total)
    titles_target = all_titles[:n_target]
    titles_other  = all_titles[n_target:n_target + n_other]
    titles_default = all_titles[n_target + n_other:]

    slots = _pick_meeting_slots(d, n_total)
    slots_target = slots[:n_target]
    slots_other  = slots[n_target:n_target + n_other]
    slots_default = slots[n_target + n_other:]

    # Calendar entries can appear anywhere in the spec — the dispatcher
    # honors CalendarHandler.apply_order_by_type to ensure calendars
    # are applied before events.
    spec: List[Dict[str, Any]] = [
        {"app": "Calendar", "type": "calendar", "name": target_cal},
        {"app": "Calendar", "type": "calendar", "name": other_cal},
    ]
    events = []
    for t, (h, dur) in zip(titles_target, slots_target):
        end_h, end_m = h + (dur // 60), dur % 60
        events.append({"title": t,
                        "calendar": target_cal,
                        "start_iso": _iso_time(d, h),
                        "end_iso":   _iso_time(d, end_h, end_m)})
    for t, (h, dur) in zip(titles_other, slots_other):
        end_h, end_m = h + (dur // 60), dur % 60
        events.append({"title": t,
                        "calendar": other_cal,
                        "start_iso": _iso_time(d, h),
                        "end_iso":   _iso_time(d, end_h, end_m)})
    for t, (h, dur) in zip(titles_default, slots_default):
        end_h, end_m = h + (dur // 60), dur % 60
        events.append({"title": t,
                        # No calendar field — default to "Calendar"
                        "start_iso": _iso_time(d, h),
                        "end_iso":   _iso_time(d, end_h, end_m)})
    spec += _calendar_spec(events)
    spec += _springboard_noise()

    # One phrasing omits the word "calendar" so the agent must infer
    # the scope from context — mirrors T1 directive that prompts
    # don't have to mention the list explicitly.
    instruction = random.choice([
        f"Open Calendar. Delete every event in the '{target_cal}' calendar.",
        f"Open Calendar. Clear out my '{target_cal}' calendar — remove "
        f"all events from it.",
        f"Open Calendar. Wipe my '{target_cal}' schedule.",
    ])

    verify_checks = [
        # No events left in target calendar.
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {"calendar": target_cal},
         "op": "eq", "n": 0,
         "severity": "blocking",
         "label": f"no events remain in '{target_cal}'"},
        # Other calendar still has its events.
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {"calendar": other_cal},
         "op": "eq", "n": n_other,
         "severity": "blocking",
         "label": f"'{other_cal}' still has {n_other} events"},
        # Default calendar still has its events.
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {"calendar": default_cal},
         "op": "eq", "n": n_default,
         "severity": "blocking",
         "label": f"'{default_cal}' still has {n_default} events"},
        # Total = non-target events only.
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {},
         "op": "eq", "n": n_other + n_default,
         "severity": "blocking",
         "label": f"total events = {n_other + n_default}"},
        # Target calendar itself still exists (deleting events ≠
        # deleting the calendar).
        {"kind": "count",
         "resource": "calendar.calendars",
         "selector": {"name": target_cal},
         "op": "eq", "n": 1,
         "severity": "blocking",
         "label": f"calendar '{target_cal}' still exists"},
        # Events in non-target calendars byte-equal to baseline.
        {"kind": "identity",
         "resource": "calendar.events",
         "selector": {},
         "compare_fields": list(_CAL_DISTRACTOR_FIELDS),
         "exclude_match": {"calendar": target_cal},
         "severity": "blocking",
         "label": f"non-'{target_cal}' events byte-equal to baseline"},
    ]

    return Task(
        task_id="", flow="calendar_delete_in_calendar",
        apps=["Calendar"],
        instruction=instruction,
        verify=f"all events in '{target_cal}' deleted, others preserved",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"{n_target} events in '{target_cal}', "
                      f"{n_other} in '{other_cal}', "
                      f"{n_default} in '{default_cal}'"],
            absent=[], noise_records=[],
            setup_commands=[], spec=spec,
            expected_behavior="complete",
        ),
        steps=max(3, n_target),
        complexity=complexity_score(max(3, n_target)),
        detail_level=0.0,
        params={"target_calendar": target_cal,
                "other_calendar":  other_cal,
                "titles_target":   titles_target,
                "titles_other":    titles_other,
                "titles_default":  titles_default},
        verify_checks=verify_checks,
    )


# ── T2.5 — move_event_between_calendars ─────────────────────────────────────

def gen_move_event_between_calendars():
    """Move one event from its source calendar to a destination
    calendar. Source and dest are user-created. Strict: every other
    event byte-equal to baseline (including their calendar field);
    target's title / start / end / all_day / location / notes
    unchanged — only its calendar changes."""
    d = _calendar_anchor_date()
    cal_pool = pickn("calendar_names", 2)
    source_cal, dest_cal = cal_pool[0], cal_pool[1]

    n_source = random.choice([3, 4, 5])
    n_dest   = random.choice([2, 3, 4])
    n_total  = n_source + n_dest
    all_titles = pickn("event_titles", n_total)
    titles_source = all_titles[:n_source]
    titles_dest   = all_titles[n_source:]
    target = random.choice(titles_source)

    slots = _pick_meeting_slots(d, n_total)
    slots_source = slots[:n_source]
    slots_dest   = slots[n_source:]

    spec: List[Dict[str, Any]] = [
        {"app": "Calendar", "type": "calendar", "name": source_cal},
        {"app": "Calendar", "type": "calendar", "name": dest_cal},
    ]
    events = []
    target_start = target_end = None
    for t, (h, dur) in zip(titles_source, slots_source):
        end_h, end_m = h + (dur // 60), dur % 60
        s = _iso_time(d, h)
        e = _iso_time(d, end_h, end_m)
        events.append({"title": t,
                        "calendar": source_cal,
                        "start_iso": s, "end_iso": e})
        if t == target:
            target_start, target_end = s, e
    for t, (h, dur) in zip(titles_dest, slots_dest):
        end_h, end_m = h + (dur // 60), dur % 60
        events.append({"title": t,
                        "calendar": dest_cal,
                        "start_iso": _iso_time(d, h),
                        "end_iso":   _iso_time(d, end_h, end_m)})
    spec += _calendar_spec(events)
    spec += _springboard_noise()

    # Last phrasing omits the source calendar — agent must look up
    # where '{target}' lives and reassign it. Mirrors T1 directive
    # that prompts shouldn't always state the obvious.
    instruction = random.choice([
        f"Open Calendar. Move the '{target}' event from '{source_cal}' "
        f"to '{dest_cal}'.",
        f"Open Calendar. '{target}' shouldn't be in '{source_cal}' — "
        f"move it to '{dest_cal}'.",
        f"Open Calendar. Move '{target}' to '{dest_cal}'.",
    ])

    verify_checks = [
        # Defensive: target title must remain unique post-move. Closes
        # the 3-cycle rotation cheat where agent renames a distractor
        # to the original target name (subset+identity catch the
        # rotation today, but this explicit guard prevents future
        # corpora drift). Critic 1 flag, T2/T3 review 2026-05-21.
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {"title": target},
         "op": "eq", "n": 1,
         "severity": "blocking",
         "label": f"exactly one event titled '{target}'"},
        # Target now belongs to dest calendar.
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"title": target},
         "attr": "calendar", "value": dest_cal,
         "severity": "blocking",
         "label": f"'{target}' is in '{dest_cal}'"},
        # Source calendar lost exactly one event.
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {"calendar": source_cal},
         "op": "eq", "n": n_source - 1,
         "severity": "blocking",
         "label": f"'{source_cal}' has {n_source - 1} events"},
        # Dest calendar gained exactly one event.
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {"calendar": dest_cal},
         "op": "eq", "n": n_dest + 1,
         "severity": "blocking",
         "label": f"'{dest_cal}' has {n_dest + 1} events"},
        # Total events unchanged (no spurious creates/deletes).
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {},
         "op": "eq", "n": n_total,
         "severity": "blocking",
         "label": f"total events = {n_total}"},
        # All original titles still present.
        {"kind": "subset",
         "resource": "calendar.events",
         "selector": {},
         "key": "title", "expected": titles_source + titles_dest,
         "severity": "blocking",
         "label": "every original title preserved"},
        # Target's non-mutated fields unchanged. Title is locked via
        # selector; calendar is the mutation; assert everything else
        # equals baseline.
        *_target_unchanged_checks(target, {
            "start_iso": target_start,
            "end_iso":   target_end,
            "all_day":   False,
            "location":  "",
            "notes":     "",
            "url":       "",
        }),
        # Distractor preservation: every event except target is
        # byte-equal to baseline. compare_fields includes "calendar"
        # so a "moved the WRONG event" cheat surfaces here.
        _event_distractor_identity_check(
            target,
            label=f"non-target events byte-equal to baseline"),
    ]

    return Task(
        task_id="", flow="calendar_move_between_calendars",
        apps=["Calendar"],
        instruction=instruction,
        verify=f"calendar.events[title={target}].calendar == '{dest_cal}'",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"{n_source} events in '{source_cal}' "
                      f"(including target '{target}'), "
                      f"{n_dest} in '{dest_cal}'"],
            absent=[], noise_records=[],
            setup_commands=[], spec=spec,
            expected_behavior="complete",
        ),
        steps=3,
        complexity=complexity_score(3),
        detail_level=0.0,
        params={"source_calendar": source_cal,
                "dest_calendar":   dest_cal,
                "target": target,
                "titles_source": titles_source,
                "titles_dest":   titles_dest},
        verify_checks=verify_checks,
    )


# ═════════════════════════════════════════════════════════════════════════════
#  CALENDAR — Tier 4 time edits / notes / url (Phase 2c)
# ═════════════════════════════════════════════════════════════════════════════
#
# Tier 4 layers on Tier 1's single-field mutations with:
#   • cross-day reschedule (T4.1 — distinct from T1.5 which is same-day)
#   • single-endpoint boundary edits (T4.2 — merged extend/shorten)
#   • multi-line notes attach (T4.3 — analogous to Reminders)
#   • url attach (T4.4 — uses the T4.0 url field)


# ── T4.1 — reschedule_event_same_duration (cross-day move) ──────────────────

def gen_reschedule_event_same_duration():
    """Move one event to a DIFFERENT DAY at a chosen hour, preserving
    duration. Distinct from T1's `change_event_time` (same-day shift)
    in that the date changes. Strict: target's other fields unchanged;
    distractors byte-equal to baseline."""
    d_source = _calendar_anchor_date()
    # Pick a destination 2-5 days later. The agent must read both the
    # date AND the time from the instruction; the verifier asserts
    # exact ISO equality.
    d_new = d_source + _dt.timedelta(days=random.choice([2, 3, 4, 5]))

    n = random.choice([5, 6, 7])
    titles = pickn("event_titles", n)
    target = random.choice(titles)
    slots = _pick_meeting_slots(d_source, n)

    events = []
    target_dur = None
    target_old_hour = None
    for t, (h, dur) in zip(titles, slots):
        end_h, end_m = h + (dur // 60), dur % 60
        events.append({"title": t,
                        "start_iso": _iso_time(d_source, h),
                        "end_iso":   _iso_time(d_source, end_h, end_m)})
        if t == target:
            target_dur = dur
            target_old_hour = h
    spec  = _calendar_spec(events)
    spec += _springboard_noise()

    # New hour on the destination day. Free to overlap source-day events
    # because they live on different dates.
    new_hour = random.choice([h for h in range(8, 20)
                              if h != target_old_hour])
    new_end_h, new_end_m = new_hour + (target_dur // 60), target_dur % 60
    new_start = _iso_time(d_new, new_hour)
    new_end   = _iso_time(d_new, new_end_h, new_end_m)

    new_nice = f"{new_hour % 12 or 12}{'pm' if new_hour >= 12 else 'am'}"
    new_date_nice = d_new.strftime(f"%A %B {d_new.day}")
    instruction = random.choice([
        f"Open Calendar. Move '{target}' to {new_date_nice} at {new_nice}.",
        f"Open Calendar. Reschedule '{target}' to {new_nice} on "
        f"{new_date_nice}.",
        f"Open Calendar. '{target}' needs to move to {new_date_nice} "
        f"({new_nice}) — same duration.",
    ])

    src_win_start, src_win_end = _day_window(d_source)
    new_win_start, new_win_end = _day_window(d_new)

    verify_checks = [
        # Target's new start + end.
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"title": target},
         "attr": "start_iso", "value": new_start,
         "severity": "blocking",
         "label": f"'{target}' starts at {new_start}"},
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"title": target},
         "attr": "end_iso", "value": new_end,
         "severity": "blocking",
         "label": f"'{target}' ends at {new_end} "
                  f"(duration preserved)"},
        # Source day has n-1 events.
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {"start_iso": src_win_start,
                       "end_iso":   src_win_end},
         "op": "eq", "n": n - 1,
         "severity": "blocking",
         "label": f"source day has {n - 1} events left"},
        # Destination day has exactly the target.
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {"start_iso": new_win_start,
                       "end_iso":   new_win_end},
         "op": "eq", "n": 1,
         "severity": "blocking",
         "label": f"destination day has 1 event"},
        # Total events unchanged.
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {},
         "op": "eq", "n": n,
         "severity": "blocking",
         "label": f"total events = {n}"},
        # All titles preserved.
        {"kind": "subset",
         "resource": "calendar.events",
         "selector": {},
         "key": "title", "expected": titles,
         "severity": "blocking",
         "label": "every original title preserved"},
        # Target's non-time fields unchanged.
        *_target_unchanged_checks(target, {
            "all_day":  False,
            "location": "",
            "notes":    "",
            "url":      "",
        }),
        # Source-day distractors byte-equal to baseline. Exclude target
        # by title since the target has LEFT the source window in the
        # current state (moved to the destination day) but is still
        # present in baseline's source window.
        {"kind": "identity",
         "resource": "calendar.events",
         "selector": {"start_iso": src_win_start,
                       "end_iso":   src_win_end},
         "compare_fields": list(_CAL_DISTRACTOR_FIELDS),
         "exclude_match": {"title": target},
         "severity": "blocking",
         "label": "source-day distractors byte-equal to baseline"},
    ]

    return Task(
        task_id="", flow="calendar_reschedule_cross_day",
        apps=["Calendar"],
        instruction=instruction,
        verify=f"calendar.events[title={target}] moves to {new_start}",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"{n} events on {d_source.isoformat()}"],
            absent=[], noise_records=[],
            setup_commands=[], spec=spec,
            expected_behavior="complete",
        ),
        steps=3,
        complexity=complexity_score(3),
        detail_level=0.0,
        params={"titles": titles, "target": target,
                "date_source": d_source.isoformat(),
                "date_new":    d_new.isoformat(),
                "new_start_iso": new_start,
                "new_end_iso":   new_end,
                "duration_minutes": target_dur},
        verify_checks=verify_checks,
    )


# ── T4.2 — adjust_event_boundary (extend / shorten one endpoint) ────────────

def gen_adjust_event_boundary():
    """Adjust ONE endpoint (start OR end) of a target event while
    preserving the OTHER endpoint. Tests end-only or start-only
    edits — merged from the originally-proposed extend/shorten into
    a single parameterized generator per T1 critic 1 (2026-05-20)."""
    d = _calendar_anchor_date()
    n = random.choice([5, 6, 7])
    titles = pickn("event_titles", n)
    target = random.choice(titles)
    slots = _pick_meeting_slots(d, n)

    events = []
    target_start = target_end = None
    target_hour = target_dur = None
    for t, (h, dur) in zip(titles, slots):
        end_h, end_m = h + (dur // 60), dur % 60
        s = _iso_time(d, h)
        e = _iso_time(d, end_h, end_m)
        events.append({"title": t, "start_iso": s, "end_iso": e})
        if t == target:
            target_start, target_end = s, e
            target_hour, target_dur = h, dur

    # Compute the set of VALID (side, direction, delta_min) options
    # given the target's baseline (target_hour, target_dur), then pick
    # uniformly. Avoids the cascade-fallback bug at h=7/dur=15 where
    # no start-side option is valid (can't extend below hour 7, can't
    # shorten below 15 min duration) and the resolver oscillates.
    # Constraints:
    #   • start-extend: target_hour*60 - delta >= 7*60
    #   • start-shorten: target_dur - delta >= 15
    #   • end-extend: target_hour*60 + target_dur + delta <= 23*60
    #   • end-shorten: target_dur - delta >= 15
    valid_options: List[Tuple[str, str, int]] = []
    for delta in (15, 30, 45):
        if target_hour * 60 - delta >= 7 * 60:
            valid_options.append(("start", "extend", delta))
        if target_dur - delta >= 15:
            valid_options.append(("start", "shorten", delta))
        if target_hour * 60 + target_dur + delta <= 23 * 60:
            valid_options.append(("end", "extend", delta))
        if target_dur - delta >= 15:
            valid_options.append(("end", "shorten", delta))
    # _pick_meeting_slots guarantees hour in [7, 20] and dur >= 15;
    # end-extend at hour=20+dur=90+delta=15 lands at 22:15 ≤ 23:00, so
    # at least one option is always available. Defense-in-depth assert.
    if not valid_options:
        raise AssertionError(
            f"gen_adjust_event_boundary: no valid options for "
            f"target_hour={target_hour} target_dur={target_dur}")
    side, direction, delta_min = random.choice(valid_options)

    # Compute new boundary.
    if side == "start":
        # Only start moves; end stays the same.
        sign = -1 if direction == "extend" else +1
        new_start_minutes = target_hour * 60 + sign * delta_min
        new_h = new_start_minutes // 60
        new_m = new_start_minutes % 60
        new_start = _iso_time(d, new_h, new_m)
        new_end = target_end
        # For instruction phrasing.
        nice_old = f"{target_hour % 12 or 12}{'pm' if target_hour >= 12 else 'am'}"
        nice_new = f"{new_h % 12 or 12}:{new_m:02d}{'pm' if new_h >= 12 else 'am'}"
    else:  # side == "end"
        sign = +1 if direction == "extend" else -1
        end_h_old, end_m_old = target_hour + (target_dur // 60), target_dur % 60
        new_end_minutes = end_h_old * 60 + end_m_old + sign * delta_min
        new_h = new_end_minutes // 60
        new_m = new_end_minutes % 60
        new_start = target_start
        new_end = _iso_time(d, new_h, new_m)
        nice_old = f"{end_h_old % 12 or 12}:{end_m_old:02d}{'pm' if end_h_old >= 12 else 'am'}"
        nice_new = f"{new_h % 12 or 12}:{new_m:02d}{'pm' if new_h >= 12 else 'am'}"

    # Sanity: the cascade must produce a positive duration AND keep
    # boundaries within [07:00, 23:59]. Catches future cascade
    # regressions and any (target_hour, dur, delta) combo we didn't
    # anticipate. ISO form is "YYYY-MM-DDTHH:MM:SS"; slice the time
    # portion for the range check.
    if not (new_start < new_end):
        raise AssertionError(
            f"gen_adjust_event_boundary cascade produced invalid window: "
            f"start={new_start} end={new_end} "
            f"(target_hour={target_hour} dur={target_dur} "
            f"side={side} direction={direction} delta_min={delta_min})")
    new_start_time = new_start[11:19]
    new_end_time = new_end[11:19]
    if not ("07:00:00" <= new_start_time and new_end_time <= "23:59:59"):
        raise AssertionError(
            f"gen_adjust_event_boundary cascade produced out-of-range "
            f"boundary: start={new_start_time!r} end={new_end_time!r}")

    spec  = _calendar_spec(events)
    spec += _springboard_noise()

    # Phrasings vary per (side, direction). Never leak `nice_old`
    # (the baseline value) into the instruction — that would give
    # the agent the original time "for free" and asymmetrically
    # benefits 2 of 6 phrasings (critic 2 caught the leak 2026-05-21).
    if side == "start":
        if direction == "extend":
            phrasings = [
                f"Open Calendar. '{target}' actually starts earlier — "
                f"move the start to {nice_new}.",
                f"Open Calendar. Shift '{target}' to start at {nice_new} "
                f"(keep the end time).",
                f"Open Calendar. '{target}' needs to begin at {nice_new}.",
            ]
        else:  # shorten
            phrasings = [
                f"Open Calendar. '{target}' actually starts later — "
                f"move the start to {nice_new}, keep the end.",
                f"Open Calendar. Trim '{target}' at the start: it begins "
                f"at {nice_new} now.",
                f"Open Calendar. Shift the start of '{target}' to "
                f"{nice_new}.",
            ]
    else:  # side == "end"
        if direction == "extend":
            phrasings = [
                f"Open Calendar. '{target}' runs longer — extend it to "
                f"end at {nice_new}.",
                f"Open Calendar. Push the end of '{target}' to {nice_new} "
                f"(keep the start).",
                f"Open Calendar. '{target}' should end at {nice_new}.",
            ]
        else:
            phrasings = [
                f"Open Calendar. '{target}' is shorter — end it at "
                f"{nice_new} instead.",
                f"Open Calendar. Trim the end of '{target}' back to "
                f"{nice_new}.",
                f"Open Calendar. '{target}' should end at {nice_new} "
                f"(don't change the start).",
            ]
    instruction = random.choice(phrasings)
    # `nice_old` was used only for the now-removed comparative phrasings;
    # silence the unused-variable lint without changing computation.
    _ = nice_old

    verify_checks = [
        # Target's new boundaries.
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"title": target},
         "attr": "start_iso", "value": new_start,
         "severity": "blocking",
         "label": f"'{target}' start_iso = {new_start}"},
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"title": target},
         "attr": "end_iso", "value": new_end,
         "severity": "blocking",
         "label": f"'{target}' end_iso = {new_end}"},
        # No add/delete.
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {},
         "op": "eq", "n": n,
         "severity": "blocking",
         "label": f"calendar has exactly {n} events"},
        {"kind": "subset",
         "resource": "calendar.events",
         "selector": {},
         "key": "title", "expected": titles,
         "severity": "blocking",
         "label": "every title preserved"},
        # Target's non-boundary fields unchanged.
        *_target_unchanged_checks(target, {
            "all_day":  False,
            "location": "",
            "notes":    "",
            "url":      "",
        }),
        _event_distractor_identity_check(
            target,
            label=f"distractors byte-equal to baseline "
                  f"(target's {side} excluded)"),
    ]

    return Task(
        task_id="", flow="calendar_adjust_boundary",
        apps=["Calendar"],
        instruction=instruction,
        verify=f"calendar.events[title={target}] {side} adjusted to "
                f"({new_start},{new_end})",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"{n} events; '{target}' will have its {side} adjusted"],
            absent=[], noise_records=[],
            setup_commands=[], spec=spec,
            expected_behavior="complete",
        ),
        steps=3,
        complexity=complexity_score(3),
        detail_level=0.0,
        params={"titles": titles, "target": target,
                "side": side, "direction": direction,
                "delta_minutes": delta_min,
                "new_start_iso": new_start,
                "new_end_iso":   new_end,
                "original_start_iso": target_start,
                "original_end_iso":   target_end},
        verify_checks=verify_checks,
    )


# ── T4.3 — add_notes_to_event ────────────────────────────────────────────────

def gen_add_notes_to_event():
    """Add a multi-line note to a target event. Strict: only the
    target gains notes, every other field on every event unchanged.
    Mirrors gen_add_notes_to_reminder."""
    d = _calendar_anchor_date()
    n = random.choice([5, 6, 7])
    titles = pickn("event_titles", n)
    target = random.choice(titles)
    note_body = pick("event_notes")
    slots = _pick_meeting_slots(d, n)
    events = []
    target_start = target_end = None
    for t, (h, dur) in zip(titles, slots):
        end_h, end_m = h + (dur // 60), dur % 60
        s = _iso_time(d, h)
        e = _iso_time(d, end_h, end_m)
        events.append({"title": t, "start_iso": s, "end_iso": e})
        if t == target:
            target_start, target_end = s, e
    spec  = _calendar_spec(events)
    spec += _springboard_noise()

    instruction = random.choice([
        f"Open Calendar. Add a note to '{target}': {note_body}",
        f"Open Calendar. On the '{target}' event, add this note — "
        f"{note_body}",
        f"Open Calendar. Note for '{target}': {note_body}",
    ])

    verify_checks = [
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"title": target},
         "attr": "notes", "value": note_body,
         "severity": "blocking",
         "label": f"'{target}' has the expected note"},
        # No add/delete.
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {},
         "op": "eq", "n": n,
         "severity": "blocking",
         "label": f"calendar has exactly {n} events"},
        # Titles preserved.
        {"kind": "subset",
         "resource": "calendar.events",
         "selector": {},
         "key": "title", "expected": titles,
         "severity": "blocking",
         "label": "every title preserved"},
        # Target's non-notes fields unchanged.
        *_target_unchanged_checks(target, {
            "start_iso": target_start,
            "end_iso":   target_end,
            "all_day":   False,
            "location":  "",
            "url":       "",
        }),
        # Distractor preservation.
        _event_distractor_identity_check(
            target,
            label="distractors byte-equal to baseline "
                  "(target's notes excluded)"),
    ]

    return Task(
        task_id="", flow="calendar_add_notes",
        apps=["Calendar"],
        instruction=instruction,
        verify=f"calendar.events[title={target}].notes == note",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"{n} events; '{target}' will gain a note"],
            absent=[], noise_records=[],
            setup_commands=[], spec=spec,
            expected_behavior="complete",
        ),
        steps=3,
        complexity=complexity_score(3),
        detail_level=0.0,
        params={"titles": titles, "target": target,
                "note": note_body,
                "target_start_iso": target_start,
                "target_end_iso":   target_end},
        verify_checks=verify_checks,
    )


# ── T4.4 — add_event_url ─────────────────────────────────────────────────────
#
# T4.0 prereq makes the url field round-trip; this generator exercises it.

_EVENT_URL_CORPUS = [
    "https://zoom.us/j/1234567890",
    "https://meet.google.com/abc-defg-hij",
    "https://teams.microsoft.com/l/meetup-join/123",
    "https://my.company.com/event/q3-planning",
    "https://calendly.com/sarah/30min",
    "https://docs.google.com/document/d/abc",
    "https://notion.so/Q1-Review-abcdef",
    "https://github.com/team/project/issues/42",
]


def gen_add_event_url():
    """Add a URL to a target event. Same shape as add_notes_to_event;
    different field. Requires T4.0 url prereq."""
    d = _calendar_anchor_date()
    n = random.choice([5, 6, 7])
    titles = pickn("event_titles", n)
    target = random.choice(titles)
    url = random.choice(_EVENT_URL_CORPUS)
    slots = _pick_meeting_slots(d, n)
    events = []
    target_start = target_end = None
    for t, (h, dur) in zip(titles, slots):
        end_h, end_m = h + (dur // 60), dur % 60
        s = _iso_time(d, h)
        e = _iso_time(d, end_h, end_m)
        events.append({"title": t, "start_iso": s, "end_iso": e})
        if t == target:
            target_start, target_end = s, e
    spec  = _calendar_spec(events)
    spec += _springboard_noise()

    instruction = random.choice([
        f"Open Calendar. Attach this URL to '{target}': {url}",
        f"Open Calendar. The '{target}' event has a link — set "
        f"its URL to {url}",
        f"Open Calendar. Add {url} as the URL on '{target}'.",
    ])

    verify_checks = [
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"title": target},
         "attr": "url", "value": url,
         "severity": "blocking",
         "label": f"'{target}' has the expected URL"},
        # No add/delete.
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {},
         "op": "eq", "n": n,
         "severity": "blocking",
         "label": f"calendar has exactly {n} events"},
        # Titles preserved.
        {"kind": "subset",
         "resource": "calendar.events",
         "selector": {},
         "key": "title", "expected": titles,
         "severity": "blocking",
         "label": "every title preserved"},
        # Target's non-url fields unchanged.
        *_target_unchanged_checks(target, {
            "start_iso": target_start,
            "end_iso":   target_end,
            "all_day":   False,
            "location":  "",
            "notes":     "",
        }),
        _event_distractor_identity_check(
            target,
            label="distractors byte-equal to baseline "
                  "(target's url excluded)"),
    ]

    return Task(
        task_id="", flow="calendar_add_url",
        apps=["Calendar"],
        instruction=instruction,
        verify=f"calendar.events[title={target}].url == url",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"{n} events; '{target}' will gain a URL"],
            absent=[], noise_records=[],
            setup_commands=[], spec=spec,
            expected_behavior="complete",
        ),
        steps=3,
        complexity=complexity_score(3),
        detail_level=0.0,
        params={"titles": titles, "target": target, "url": url,
                "target_start_iso": target_start,
                "target_end_iso":   target_end},
        verify_checks=verify_checks,
    )


# ═════════════════════════════════════════════════════════════════════════════
#  CALENDAR — Tier 4b recurrence generators (Phase 2c)
# ═════════════════════════════════════════════════════════════════════════════
#
# Architecture decisions per 2026-05-21 pre-impl critic round:
#   • iOS EXPANDS recurring events under predicateForEvents (Q6.2).
#     Swift list_events deduplicates to one row per series (master's
#     start_iso) when `master_only=True` (default ON).
#   • `recurrence` field on each row is a dict {frequency, interval,
#     end_iso XOR end_count} or absent. Mirrors RemindersItem.
#   • DO NOT add `recurrence` to `_CAL_DISTRACTOR_FIELDS` — dict is
#     unhashable in set comparison; would crash on cheat paths.
#     Use `_event_distractor_recurrence_absent_checks(non_target_titles)`
#     for per-distractor sentinels (Reminders T4b pattern).
#   • Generator-seeded recurring events use start dates ≥ tomorrow so
#     past-occurrence detachment branches never execute on stop.


# ── T4b.1 — make_event_recurring (MVP) ───────────────────────────────────────

def gen_make_event_recurring():
    """Attach a weekly recurrence rule to a target event. Strict:
    every distractor stays non-recurring; target's other fields
    unchanged. Critic-flagged target-side cheat closed via
    _target_unchanged_checks including url=''. Per-distractor
    attribute_absent(recurrence) sentinels close the
    'rule on the wrong event' cheat."""
    d = _calendar_anchor_date()
    n = random.choice([5, 6, 7])
    titles = pickn("event_titles", n)
    target = random.choice(titles)
    slots = _pick_meeting_slots(d, n)
    events = []
    target_start = target_end = None
    for t, (h, dur) in zip(titles, slots):
        end_h, end_m = h + (dur // 60), dur % 60
        s = _iso_time(d, h)
        e = _iso_time(d, end_h, end_m)
        events.append({"title": t, "start_iso": s, "end_iso": e})
        if t == target:
            target_start, target_end = s, e
    spec  = _calendar_spec(events)
    spec += _springboard_noise()

    # Pin end_count so the verifier knows the rule terminates rather
    # than running forever. iOS Calendar UI exposes "Repeat → Weekly"
    # with separate "End Repeat" picker; the agent must set both.
    end_count = random.choice([4, 5, 6, 8])
    # All three phrasings unambiguously instruct the agent to ADD a
    # recurrence rule (vs the baseline non-recurring event). Earlier
    # phrasing "'X' repeats weekly — set up the recurrence" was
    # ambiguous (could be read as "X already repeats", which would
    # imply the baseline event was already recurring — confusing per
    # 2026-05-21 demo feedback).
    instruction = random.choice([
        f"Open Calendar. Make '{target}' a weekly recurring event "
        f"for {end_count} occurrences.",
        f"Open Calendar. Turn '{target}' into a weekly series — "
        f"{end_count} occurrences total.",
        f"Open Calendar. Convert '{target}' to a weekly series of "
        f"{end_count} meetings.",
    ])

    # End-specification verification: iOS Calendar UI's default
    # Repeat picker exposes ONLY date-based "End Repeat"; "After N
    # times" is buried under Custom. A capable agent might reason
    # from N to the end date arithmetically — legitimate solution.
    # Verify the SEMANTIC outcome (N expanded occurrences) rather
    # than the rule's internal encoding by counting actual occurrences
    # in a wide window with master_only=False.
    #
    # Window: from event's day to N+4 weeks past — generous so
    # over-shoot surfaces as count > N, and edge timing doesn't
    # cut the last expected occurrence.
    _occ_window_end_iso = (
        _dt.date.fromisoformat(target_start[:10])
        + _dt.timedelta(weeks=end_count + 4)
    ).isoformat() + "T23:59:59"

    non_target_titles = [t for t in titles if t != target]
    verify_checks = [
        # Target now has a weekly recurrence.
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"title": target},
         "attr": "recurrence.frequency", "value": "weekly",
         "severity": "blocking",
         "label": f"'{target}' has weekly frequency"},
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"title": target},
         "attr": "recurrence.interval", "value": 1,
         "severity": "blocking",
         "label": f"'{target}' recurrence interval is 1"},
        # Occurrence-count check (semantic, encoding-agnostic).
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {
             "title": target,
             "master_only": False,
             "start_iso": target_start[:10] + "T00:00:00",
             "end_iso":   _occ_window_end_iso,
         },
         "op": "eq", "n": end_count,
         "severity": "blocking",
         "label": f"'{target}' produces {end_count} occurrences"},
        # Total event count unchanged (master_only dedup keeps the
        # recurring series as one row).
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {},
         "op": "eq", "n": n,
         "severity": "blocking",
         "label": f"calendar has exactly {n} events"},
        {"kind": "subset",
         "resource": "calendar.events",
         "selector": {},
         "key": "title", "expected": titles,
         "severity": "blocking",
         "label": "every title preserved"},
        # Target's non-recurrence fields unchanged.
        *_target_unchanged_checks(target, {
            "start_iso": target_start,
            "end_iso":   target_end,
            "all_day":   False,
            "location":  "",
            "notes":     "",
            "url":       "",
        }),
        # Every distractor stays non-recurring.
        *_event_distractor_recurrence_absent_checks(non_target_titles),
        # Distractor byte-equality on the non-recurrence fields.
        _event_distractor_identity_check(
            target,
            label="distractors byte-equal to baseline (target's "
                  "recurrence excluded)"),
    ]

    return Task(
        task_id="", flow="calendar_make_recurring",
        apps=["Calendar"],
        instruction=instruction,
        verify=f"calendar.events[title={target}] becomes weekly × {end_count}",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"{n} non-recurring events; '{target}' will gain "
                      f"a weekly recurrence × {end_count}"],
            absent=[], noise_records=[],
            setup_commands=[], spec=spec,
            expected_behavior="complete",
        ),
        steps=4,
        complexity=complexity_score(4),
        detail_level=0.0,
        params={"titles": titles, "target": target,
                "end_count": end_count,
                "target_start_iso": target_start,
                "target_end_iso":   target_end},
        verify_checks=verify_checks,
    )


# ── T4b.2 — stop_event_recurrence (MVP) ──────────────────────────────────────
#
# Named `gen_stop_event_recurrence` (not `gen_stop_recurrence`) to avoid
# collision with Reminders' generator of the same short name.

def gen_stop_event_recurrence():
    """Remove the recurrence rule from a recurring target event.
    Pre: target IS recurring (weekly); other events are not.
    Action: agent removes the rule via 'Repeat → Never' in iOS UI.
    Verify: target's recurrence is absent (None); other fields
    unchanged; distractors unchanged."""
    d = _calendar_anchor_date()
    n = random.choice([5, 6, 7])
    titles = pickn("event_titles", n)
    target = random.choice(titles)
    slots = _pick_meeting_slots(d, n)
    initial_end_count = random.choice([4, 5, 6])
    events = []
    target_start = target_end = None
    for t, (h, dur) in zip(titles, slots):
        end_h, end_m = h + (dur // 60), dur % 60
        s = _iso_time(d, h)
        e = _iso_time(d, end_h, end_m)
        ev: Dict[str, Any] = {"title": t, "start_iso": s, "end_iso": e}
        if t == target:
            ev["recurrence"] = {
                "frequency": "weekly", "interval": 1,
                "end_count": initial_end_count,
            }
            target_start, target_end = s, e
        events.append(ev)
    spec  = _calendar_spec(events)
    spec += _springboard_noise()

    instruction = random.choice([
        f"Open Calendar. '{target}' is no longer a recurring meeting "
        f"— remove the recurrence rule.",
        f"Open Calendar. Stop '{target}' from repeating.",
        f"Open Calendar. Set '{target}' back to a one-off event "
        f"(not recurring).",
    ])

    non_target_titles = [t for t in titles if t != target]
    verify_checks = [
        # Target's recurrence is gone.
        {"kind": "attribute_absent",
         "resource": "calendar.events",
         "selector": {"title": target},
         "attr": "recurrence",
         "severity": "blocking",
         "label": f"'{target}' has no recurrence rule"},
        # Target still exists exactly once (would be 0 if agent
        # deleted the event entirely instead of just the rule).
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {"title": target},
         "op": "eq", "n": 1,
         "severity": "blocking",
         "label": f"'{target}' still exists as a single event"},
        # Total count unchanged.
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {},
         "op": "eq", "n": n,
         "severity": "blocking",
         "label": f"calendar has exactly {n} events"},
        {"kind": "subset",
         "resource": "calendar.events",
         "selector": {},
         "key": "title", "expected": titles,
         "severity": "blocking",
         "label": "every title preserved"},
        # Target's non-recurrence fields unchanged.
        *_target_unchanged_checks(target, {
            "start_iso": target_start,
            "end_iso":   target_end,
            "all_day":   False,
            "location":  "",
            "notes":     "",
            "url":       "",
        }),
        # Distractors stay non-recurring.
        *_event_distractor_recurrence_absent_checks(non_target_titles),
        _event_distractor_identity_check(
            target,
            label="distractors byte-equal to baseline (target's "
                  "recurrence-removal excluded)"),
    ]

    return Task(
        task_id="", flow="calendar_stop_recurrence",
        apps=["Calendar"],
        instruction=instruction,
        verify=f"calendar.events[title={target}].recurrence is None",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"{n} events; '{target}' is weekly recurring × "
                      f"{initial_end_count}; agent will stop recurrence"],
            absent=[], noise_records=[],
            setup_commands=[], spec=spec,
            expected_behavior="complete",
        ),
        steps=3,
        complexity=complexity_score(3),
        detail_level=0.0,
        params={"titles": titles, "target": target,
                "initial_end_count": initial_end_count,
                "target_start_iso": target_start,
                "target_end_iso":   target_end},
        verify_checks=verify_checks,
    )


# ── T4b.3 — change_event_recurrence_frequency ────────────────────────────────

def gen_change_event_recurrence_frequency():
    """Change the recurrence frequency of a target event (e.g., daily
    → weekly). Per T4b critic 3: instruction must NOT leak the old
    frequency — the agent should infer it from observation, mirroring
    the T1 critic directive against comparative phrasings."""
    d = _calendar_anchor_date()
    n = random.choice([5, 6, 7])
    titles = pickn("event_titles", n)
    target = random.choice(titles)
    slots = _pick_meeting_slots(d, n)

    # Pick two distinct frequencies — original and new.
    freqs = ["daily", "weekly", "monthly"]
    random.shuffle(freqs)
    old_freq, new_freq = freqs[0], freqs[1]
    end_count = random.choice([4, 5, 6])

    events = []
    target_start = target_end = None
    for t, (h, dur) in zip(titles, slots):
        end_h, end_m = h + (dur // 60), dur % 60
        s = _iso_time(d, h)
        e = _iso_time(d, end_h, end_m)
        ev: Dict[str, Any] = {"title": t, "start_iso": s, "end_iso": e}
        if t == target:
            ev["recurrence"] = {
                "frequency": old_freq, "interval": 1,
                "end_count": end_count,
            }
            target_start, target_end = s, e
        events.append(ev)
    spec  = _calendar_spec(events)
    spec += _springboard_noise()

    # Phrasings DON'T leak old_freq — agent observes via Calendar UI.
    instruction = random.choice([
        f"Open Calendar. Change '{target}' to repeat {new_freq} "
        f"instead.",
        f"Open Calendar. '{target}' should be a {new_freq} event now.",
        f"Open Calendar. Update '{target}' so it recurs {new_freq}.",
    ])

    non_target_titles = [t for t in titles if t != target]
    verify_checks = [
        # Target's new frequency.
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"title": target},
         "attr": "recurrence.frequency", "value": new_freq,
         "severity": "blocking",
         "label": f"'{target}' now has {new_freq} frequency"},
        # Interval unchanged (critic 1's "agent changes interval
        # instead of frequency" cheat).
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"title": target},
         "attr": "recurrence.interval", "value": 1,
         "severity": "blocking",
         "label": f"'{target}' recurrence interval still 1"},
        # end_count preserved.
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"title": target},
         "attr": "recurrence.end_count", "value": end_count,
         "severity": "blocking",
         "label": f"'{target}' end_count preserved at {end_count}"},
        # Target still exists once.
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {"title": target},
         "op": "eq", "n": 1,
         "severity": "blocking",
         "label": f"exactly one '{target}' event"},
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {},
         "op": "eq", "n": n,
         "severity": "blocking",
         "label": f"calendar has exactly {n} events"},
        {"kind": "subset",
         "resource": "calendar.events",
         "selector": {},
         "key": "title", "expected": titles,
         "severity": "blocking",
         "label": "every title preserved"},
        # Target's non-recurrence fields unchanged.
        *_target_unchanged_checks(target, {
            "start_iso": target_start,
            "end_iso":   target_end,
            "all_day":   False,
            "location":  "",
            "notes":     "",
            "url":       "",
        }),
        # Distractors stay non-recurring.
        *_event_distractor_recurrence_absent_checks(non_target_titles),
        _event_distractor_identity_check(
            target,
            label="distractors byte-equal to baseline (target's "
                  "recurrence change excluded)"),
    ]

    return Task(
        task_id="", flow="calendar_change_recurrence_frequency",
        apps=["Calendar"],
        instruction=instruction,
        verify=f"calendar.events[title={target}].recurrence.frequency = {new_freq}",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[f"{n} events; '{target}' is {old_freq}-recurring × "
                      f"{end_count}; agent changes to {new_freq}"],
            absent=[], noise_records=[],
            setup_commands=[], spec=spec,
            expected_behavior="complete",
        ),
        steps=3,
        complexity=complexity_score(3),
        detail_level=0.0,
        params={"titles": titles, "target": target,
                "old_frequency": old_freq,
                "new_frequency": new_freq,
                "end_count": end_count,
                "target_start_iso": target_start,
                "target_end_iso":   target_end},
        verify_checks=verify_checks,
    )


# ── T4b.4 — create_recurring_event ───────────────────────────────────────────

def gen_create_recurring_event():
    """Create a recurring event from an empty calendar. Pin end_count
    explicitly so the verifier knows the rule terminates."""
    d = _calendar_anchor_date()
    title = pick("event_titles")
    hour  = random.choice([9, 10, 11, 13, 14, 15, 16])
    dur   = random.choice([30, 45, 60])
    end_h, end_m = hour + (dur // 60), dur % 60
    start = _iso_time(d, hour)
    end   = _iso_time(d, end_h, end_m)
    end_count = random.choice([4, 5, 6, 8])

    spec   = _calendar_spec([])
    spec  += _springboard_noise()

    nice_time = f"{hour % 12 or 12}{'pm' if hour >= 12 else 'am'}"
    end_nice  = f"{end_h % 12 or 12}:{end_m:02d}{'pm' if end_h >= 12 else 'am'}"
    # 2026-06-11: prose-vs-anchor mismatch fix (see _day_reference).
    when = _day_reference(d)  # "tomorrow" if d==today+1, else "on <date>"
    instruction = random.choice([
        f"Open Calendar. Create a weekly event titled '{title}' "
        f"starting {when} at {nice_time} (ending at {end_nice}), "
        f"repeating {end_count} times.",
        f"Open Calendar. Add '{title}' as a weekly series of "
        f"{end_count} starting {when} at {nice_time} ({dur}-minute).",
        f"Open Calendar. Schedule '{title}' weekly for {end_count} "
        f"occurrences, starting {when} {nice_time} to {end_nice}.",
    ])

    verify_checks = [
        # The event exists with the right title.
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {"title": title},
         "op": "eq", "n": 1,
         "severity": "blocking",
         "label": f"exactly one '{title}' event"},
        # Total events = 1 (no spurious creates).
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {},
         "op": "eq", "n": 1,
         "severity": "blocking",
         "label": "calendar has exactly 1 event"},
        # Master start/end + all_day + location/notes/url defaults.
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"title": title},
         "attr": "start_iso", "value": start,
         "severity": "blocking",
         "label": f"'{title}' starts at {start}"},
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"title": title},
         "attr": "end_iso", "value": end,
         "severity": "blocking",
         "label": f"'{title}' ends at {end}"},
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"title": title},
         "attr": "all_day", "value": False,
         "severity": "blocking",
         "label": f"'{title}' is timed (not all-day)"},
        # The recurrence rule on the target.
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"title": title},
         "attr": "recurrence.frequency", "value": "weekly",
         "severity": "blocking",
         "label": f"'{title}' weekly recurrence"},
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"title": title},
         "attr": "recurrence.interval", "value": 1,
         "severity": "blocking",
         "label": f"'{title}' recurrence interval 1"},
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"title": title},
         "attr": "recurrence.end_count", "value": end_count,
         "severity": "blocking",
         "label": f"'{title}' recurrence end_count={end_count}"},
        # No spurious location/notes/url on the created event.
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {"location": ""},
         "op": "eq", "n": 1,
         "severity": "blocking",
         "label": "no extraneous location set"},
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {"notes": ""},
         "op": "eq", "n": 1,
         "severity": "blocking",
         "label": "no extraneous notes set"},
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {"url": ""},
         "op": "eq", "n": 1,
         "severity": "blocking",
         "label": "no extraneous url set"},
    ]

    return Task(
        task_id="", flow="calendar_create_recurring",
        apps=["Calendar"],
        instruction=instruction,
        verify=f"calendar.events[title={title}] weekly × {end_count}",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[],
            absent=[], noise_records=[],
            setup_commands=[], spec=spec,
            expected_behavior="complete",
        ),
        steps=5,
        complexity=complexity_score(5),
        detail_level=0.0,
        params={"title": title, "start_iso": start, "end_iso": end,
                "duration_minutes": dur,
                "end_count": end_count},
        verify_checks=verify_checks,
    )


# ═════════════════════════════════════════════════════════════════════════════
#  CALENDAR — Tier 5 reporting via agent_answer (Phase 2c)
# ═════════════════════════════════════════════════════════════════════════════
#
# Per pre-impl critic round (2026-05-21):
#   • 4 generators (dropped `list_events_this_week` as redundant
#     with `list_events_today`; dropped `event_duration_lookup` for
#     chance-guess hazard).
#   • Avoid recurring events in T5 corpus for v1 — master_only=True
#     dedup vs UI's expanded-occurrence view would mismatch.
#   • Human-demo deferred to LLM trial per Reminders T5 precedent.
#   • Episode-time pinning ships with same-day fragility (same as
#     Reminders T5); documented in TODO_DEFERRED §H.


# ── T5.1 — lookup_event_location ─────────────────────────────────────────────

def gen_lookup_event_location():
    """Report the location of a named event. case-insensitive +
    whitespace-trimmed answer. Direct port of
    `gen_lookup_reminder_notes`."""
    d = _calendar_anchor_date()
    n = random.choice([3, 4, 5])
    titles = pickn("event_titles", n)
    target = random.choice(titles)
    location = pick("event_locations")
    slots = _pick_meeting_slots(d, n)

    events = []
    for t, (h, dur) in zip(titles, slots):
        end_h, end_m = h + (dur // 60), dur % 60
        ev: Dict[str, Any] = {"title": t,
                                "start_iso": _iso_time(d, h),
                                "end_iso":   _iso_time(d, end_h, end_m)}
        if t == target:
            ev["location"] = location
        events.append(ev)
    spec  = _calendar_spec(events)
    spec += _springboard_noise()

    answer_check, schema = make_answer_check(
        match="string_eq",
        expected=location,
        path="$.value",
        case_sensitive=False,
        trim_strings=True,
        observation_required=["com.apple.mobilecal"],
    )

    intro = random.choice([
        f"Open Calendar. Where is '{target}' happening? ",
        f"Open Calendar. Tell me the location of the '{target}' event. ",
        f"Open Calendar. What's the location set on '{target}'? ",
    ])
    instruction = intro + schema

    non_target_titles = [t for t in titles if t != target]
    verify_checks = [
        answer_check,
        # No add/delete.
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {},
         "op": "eq", "n": n,
         "severity": "blocking",
         "label": f"calendar has exactly {n} events"},
        {"kind": "subset",
         "resource": "calendar.events",
         "selector": {},
         "key": "title", "expected": titles,
         "severity": "blocking",
         "label": "every original title preserved"},
        # Target's location preserved (the agent is REPORTING, not
        # editing — must not mutate state).
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"title": target},
         "attr": "location", "value": location,
         "severity": "blocking",
         "label": f"'{target}'.location preserved verbatim"},
        # Only the target has a location; distractors stay empty.
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {"location": ""},
         "op": "eq", "n": n - 1,
         "severity": "blocking",
         "label": f"only '{target}' has a location set"},
        # No spurious recurrence rules.
        *_event_distractor_recurrence_absent_checks(non_target_titles),
        {"kind": "attribute_absent",
         "resource": "calendar.events",
         "selector": {"title": target},
         "attr": "recurrence",
         "severity": "blocking",
         "label": f"'{target}' has no recurrence rule"},
    ]

    return Task(
        task_id="", flow="single_calendar_lookup_location",
        apps=["Calendar"],
        instruction=instruction,
        verify=f"ANSWER value ~== {location!r} (case-insensitive)",
        verify_mode="agent_response",
        initial_state=InitialState(
            present=[f"'{target}' has location '{location}'; "
                      f"{n - 1} other events have no location"],
            absent=[], noise_records=[],
            setup_commands=[], spec=spec,
            expected_behavior="complete",
        ),
        steps=2,
        complexity=complexity_score(2),
        detail_level=0.0,
        params={"titles": titles, "target": target,
                "location": location, "date": d.isoformat()},
        verify_checks=verify_checks,
    )


# ── T5.2 — list_events_today ─────────────────────────────────────────────────
#
# Episode-time fragility: "today" is computed at task-generation time,
# events seeded relative to that. If the episode straddles midnight,
# the agent's "today" may differ. Documented in TODO_DEFERRED §H per
# Reminders T5 precedent.

def gen_list_events_today():
    """Report the titles of all events on today's date. Excludes
    recurring events from the corpus (master_only dedup mismatches
    with UI's expanded-occurrence view; see T5 pre-impl critic A4).

    Skips generation if today is a US holiday — Calendar.app's UI
    would include the holiday in the agent's view, breaking
    set_equals (IOS_SIM_QUIRKS §16 + §18). Callers should retry
    next day or pick a different generator on those ~30 days/year."""
    d_today = _dt.date.today()
    if d_today.isoformat() in _US_HOLIDAYS:
        raise RuntimeError(
            f"gen_list_events_today: today ({d_today.isoformat()}) "
            f"is a US holiday — iOS Calendar.app would render the "
            f"holiday alongside our seeded events and break "
            f"set_equals. Skip this generator today; retry tomorrow.")
    # Pick a separate "other day" to seed distractors. Avoid the
    # adjacent days to reduce off-by-one-with-tz risk on edge runs.
    d_other = d_today + _dt.timedelta(days=random.choice([3, 5, 7, 10]))
    # Gate ≥2 today events so "dump all visible titles" doesn't trivially
    # win (T5 critic 1's chance-guess minimum).
    n_today = random.choice([2, 3])
    n_other = random.choice([2, 3, 4])
    n_total = n_today + n_other
    all_titles = pickn("event_titles", n_total)
    today_titles = all_titles[:n_today]
    other_titles = all_titles[n_today:]

    slots_today = _pick_meeting_slots(d_today, n_today)
    slots_other = _pick_meeting_slots(d_other, n_other)

    events = []
    for t, (h, dur) in zip(today_titles, slots_today):
        end_h, end_m = h + (dur // 60), dur % 60
        events.append({"title": t,
                        "start_iso": _iso_time(d_today, h),
                        "end_iso":   _iso_time(d_today, end_h, end_m)})
    for t, (h, dur) in zip(other_titles, slots_other):
        end_h, end_m = h + (dur // 60), dur % 60
        events.append({"title": t,
                        "start_iso": _iso_time(d_other, h),
                        "end_iso":   _iso_time(d_other, end_h, end_m)})

    spec  = _calendar_spec(events)
    spec += _springboard_noise()

    answer_check, schema = make_answer_check(
        match="set_equals",
        expected=[{"title": t} for t in today_titles],
        path="$.items",
        item_keys=["title"],
        observation_required=["com.apple.mobilecal"],
    )

    intro = random.choice([
        f"Open Calendar. List every event scheduled for today. ",
        f"Open Calendar. What events do I have today? Tell me their "
        f"titles. ",
        f"Open Calendar. Today's events — what are they? ",
    ])
    instruction = intro + schema

    today_window_start, today_window_end = _day_window(d_today)
    other_window_start, other_window_end = _day_window(d_other)
    verify_checks = [
        answer_check,
        # State-preservation: agent is reporting, must not mutate.
        # Today's events still in place.
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {"start_iso": today_window_start,
                       "end_iso":   today_window_end},
         "op": "eq", "n": n_today,
         "severity": "blocking",
         "label": f"today still has {n_today} events"},
        {"kind": "subset",
         "resource": "calendar.events",
         "selector": {"start_iso": today_window_start,
                       "end_iso":   today_window_end},
         "key": "title", "expected": today_titles,
         "severity": "blocking",
         "label": "every today-title preserved"},
        # Other-day events preserved.
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {"start_iso": other_window_start,
                       "end_iso":   other_window_end},
         "op": "eq", "n": n_other,
         "severity": "blocking",
         "label": f"other-date events preserved ({n_other})"},
        # Total events.
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {},
         "op": "eq", "n": n_total,
         "severity": "blocking",
         "label": f"total events = {n_total}"},
        # No spurious recurrence anywhere.
        *_event_distractor_recurrence_absent_checks(list(all_titles)),
    ]

    return Task(
        task_id="", flow="single_calendar_list_today",
        apps=["Calendar"],
        instruction=instruction,
        verify=f"ANSWER items == {sorted(today_titles)} (today)",
        verify_mode="agent_response",
        initial_state=InitialState(
            present=[f"{n_today} events on today ({d_today.isoformat()})"
                      f", {n_other} on another date"],
            absent=[], noise_records=[],
            setup_commands=[], spec=spec,
            expected_behavior="complete",
        ),
        steps=3,
        complexity=complexity_score(3),
        detail_level=0.0,
        params={"today_titles": today_titles,
                "other_titles": other_titles,
                "date_today": d_today.isoformat(),
                "date_other": d_other.isoformat()},
        verify_checks=verify_checks,
    )


# ── T5.3 — list_conflicting_events (calendar-native) ────────────────────────
#
# Invariants per T5 critic 1 (hard-pinned to avoid trivial cheats):
#   • EXACTLY ONE overlapping pair (2 events that overlap each other)
#   • ≥3 non-overlapping events on the same day (else "report all"
#     cheats by enumerating titles)
#   • All-day events not in v1 corpus (defer the all-day-vs-timed
#     overlap semantic to a future round)
#   • Answer set: BOTH titles in the overlap pair (transitive closure)

def gen_list_conflicting_events():
    """Identify events that participate in a time overlap. Strict
    invariants prevent enumeration-style cheats. Pure reporting —
    no state mutation expected."""
    d = _calendar_anchor_date()
    # 2 conflicting + 3-4 non-conflicting distractors.
    n_distractors = random.choice([3, 4])
    titles = pickn("event_titles", 2 + n_distractors)
    conflict_a, conflict_b = titles[0], titles[1]
    distractor_titles = titles[2:]

    # Conflict pair: A at H:00-H+1:00, B at H:30-H+1:30.
    # Pick H from a range that leaves room for distractors on either side.
    conflict_hour = random.choice([10, 11, 13, 14])
    events = [
        {"title": conflict_a,
         "start_iso": _iso_time(d, conflict_hour),
         "end_iso":   _iso_time(d, conflict_hour + 1)},
        {"title": conflict_b,
         "start_iso": _iso_time(d, conflict_hour, 30),
         "end_iso":   _iso_time(d, conflict_hour + 1, 30)},
    ]
    # Non-conflicting distractors at hours NOT adjacent to the conflict
    # pair (avoid creating a 3-way chain).
    free_hours = [h for h in [7, 8, 9, 16, 17, 18, 19, 20]
                   if abs(h - conflict_hour) >= 2
                      and abs(h - (conflict_hour + 1)) >= 2]
    chosen = sorted(random.sample(free_hours, n_distractors))
    for t, h in zip(distractor_titles, chosen):
        events.append({"title": t,
                        "start_iso": _iso_time(d, h),
                        "end_iso":   _iso_time(d, h, 30)})

    spec  = _calendar_spec(events)
    spec += _springboard_noise()

    answer_check, schema = make_answer_check(
        match="set_equals",
        expected=[{"title": conflict_a}, {"title": conflict_b}],
        path="$.items",
        item_keys=["title"],
        observation_required=["com.apple.mobilecal"],
    )

    nice_date = d.strftime(f"%A %B {d.day}")
    intro = random.choice([
        f"Open Calendar. Which events on {nice_date} overlap with "
        f"each other? ",
        f"Open Calendar. I think I have a scheduling conflict on "
        f"{nice_date} — which events are double-booked? ",
        f"Open Calendar. Find every event on {nice_date} that "
        f"participates in a time overlap. ",
    ])
    instruction = intro + schema

    window_start, window_end = _day_window(d)
    all_titles = [conflict_a, conflict_b] + distractor_titles
    n_total = len(all_titles)
    verify_checks = [
        answer_check,
        # State preservation — agent must not mutate.
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {"start_iso": window_start,
                       "end_iso":   window_end},
         "op": "eq", "n": n_total,
         "severity": "blocking",
         "label": f"day still has {n_total} events"},
        {"kind": "subset",
         "resource": "calendar.events",
         "selector": {"start_iso": window_start,
                       "end_iso":   window_end},
         "key": "title", "expected": all_titles,
         "severity": "blocking",
         "label": "every title preserved"},
        # Conflict pair's start_iso AND end_iso preserved (catches
        # "agent moved one of the conflicting events to resolve the
        # overlap" — both start-shift and end-shrink variants).
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"title": conflict_a},
         "attr": "start_iso", "value": _iso_time(d, conflict_hour),
         "severity": "blocking",
         "label": f"'{conflict_a}' start unchanged"},
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"title": conflict_a},
         "attr": "end_iso", "value": _iso_time(d, conflict_hour + 1),
         "severity": "blocking",
         "label": f"'{conflict_a}' end unchanged"},
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"title": conflict_b},
         "attr": "start_iso",
         "value": _iso_time(d, conflict_hour, 30),
         "severity": "blocking",
         "label": f"'{conflict_b}' start unchanged"},
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"title": conflict_b},
         "attr": "end_iso",
         "value": _iso_time(d, conflict_hour + 1, 30),
         "severity": "blocking",
         "label": f"'{conflict_b}' end unchanged"},
        # Total events on calendar.
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {},
         "op": "eq", "n": n_total,
         "severity": "blocking",
         "label": f"total events = {n_total}"},
        # No spurious recurrence.
        *_event_distractor_recurrence_absent_checks(all_titles),
    ]

    return Task(
        task_id="", flow="single_calendar_list_conflicts",
        apps=["Calendar"],
        instruction=instruction,
        verify=f"ANSWER items == {{ {conflict_a!r}, {conflict_b!r} }}",
        verify_mode="agent_response",
        initial_state=InitialState(
            present=[f"'{conflict_a}' and '{conflict_b}' overlap on "
                      f"{d.isoformat()}; {n_distractors} non-conflicting "
                      f"events on the same day"],
            absent=[], noise_records=[],
            setup_commands=[], spec=spec,
            expected_behavior="complete",
        ),
        steps=4,
        complexity=complexity_score(4),
        detail_level=0.0,
        params={"conflict_a": conflict_a,
                "conflict_b": conflict_b,
                "distractor_titles": distractor_titles,
                "all_titles": all_titles,
                "date": d.isoformat(),
                "conflict_hour": conflict_hour},
        verify_checks=verify_checks,
    )


# ── T5.4 — next_event_lookup (composite answer) ──────────────────────────────
#
# Per T5 pre-impl critic round:
#   • Anchor "next" to a FIXED time T in the instruction (not "now")
#     — agent's perception of "now" drifts during the episode.
#   • Composite answer: {title, start_local} where start_local is
#     HH:MM 24-hour — avoids ISO timezone ambiguity.
#   • Encoded as set_equals with a 1-element list at $.items, using
#     item_keys=["title", "start_local"] to verify both fields.

def gen_next_event_lookup():
    """Report the next event after a fixed wall-clock time T on a
    specific date. Composite answer (title + start_local HH:MM)
    raises chance-guess entropy substantially vs bare title."""
    d = _calendar_anchor_date()
    # T = the anchor hour in the instruction. Pick noon-ish so we
    # have room for events before AND after.
    anchor_hour = random.choice([11, 12, 13])
    n_before = random.choice([1, 2])
    n_after  = random.choice([2, 3, 4])
    n_total  = n_before + n_after

    # Pre-anchor hours: distinct & all < anchor_hour.
    before_pool = [h for h in [7, 8, 9, 10] if h < anchor_hour]
    before_hours = sorted(random.sample(before_pool,
                                          min(n_before, len(before_pool))))
    # Post-anchor hours: distinct & all > anchor_hour.
    after_pool = [h for h in [14, 15, 16, 17, 18, 19]
                  if h > anchor_hour]
    after_hours = sorted(random.sample(after_pool, n_after))
    all_hours = before_hours + after_hours
    titles = pickn("event_titles", len(all_hours))

    events = []
    for t, h in zip(titles, all_hours):
        events.append({"title": t,
                        "start_iso": _iso_time(d, h),
                        "end_iso":   _iso_time(d, h, 30)})

    spec  = _calendar_spec(events)
    spec += _springboard_noise()

    # Target = first event whose hour > anchor_hour (smallest in after_hours).
    target_idx = len(before_hours)
    target_title = titles[target_idx]
    target_hour = after_hours[0]
    target_start_local = f"{target_hour:02d}:00"

    # Bind date into the answer so an agent can't "find a coincidentally-
    # matching event on a different day" (T5 critic 1 C2 cheat path).
    target_date_iso = d.isoformat()
    answer_check, schema = make_answer_check(
        match="set_equals",
        expected=[{"title": target_title,
                    "start_local": target_start_local,
                    "date_iso":    target_date_iso}],
        path="$.items",
        item_keys=["title", "start_local", "date_iso"],
        # time_keys canonicalizes start_local 12h↔24h before compare,
        # so agent emitting "2:00 PM" or "14:00" both pass (iOS UI
        # exposes the 12-hour form per AX probe 2026-05-21).
        time_keys=["start_local"],
        case_sensitive=False,
        trim_strings=True,
        observation_required=["com.apple.mobilecal"],
    )

    nice_anchor = (f"{anchor_hour % 12 or 12}"
                    f"{'pm' if anchor_hour >= 12 else 'am'}")
    nice_date = d.strftime(f"%A %B {d.day}")
    intro = random.choice([
        f"Open Calendar. What's the next event after {nice_anchor} on "
        f"{nice_date}? ",
        f"Open Calendar. On {nice_date}, what's the first event after "
        f"{nice_anchor}? ",
        f"Open Calendar. Find the next event on my calendar after "
        f"{nice_anchor} on {nice_date}. ",
    ])
    instruction = intro + schema

    window_start, window_end = _day_window(d)
    verify_checks = [
        answer_check,
        # State preservation.
        {"kind": "count",
         "resource": "calendar.events",
         "selector": {"start_iso": window_start,
                       "end_iso":   window_end},
         "op": "eq", "n": n_total,
         "severity": "blocking",
         "label": f"day still has {n_total} events"},
        {"kind": "subset",
         "resource": "calendar.events",
         "selector": {"start_iso": window_start,
                       "end_iso":   window_end},
         "key": "title", "expected": titles,
         "severity": "blocking",
         "label": "every original title preserved"},
        # Target's start_iso preserved (catches "agent moved an event
        # to make it the answer").
        {"kind": "attribute_eq",
         "resource": "calendar.events",
         "selector": {"title": target_title},
         "attr": "start_iso", "value": _iso_time(d, target_hour),
         "severity": "blocking",
         "label": f"'{target_title}' start unchanged"},
        # No spurious recurrence rules.
        *_event_distractor_recurrence_absent_checks(list(titles)),
    ]

    return Task(
        task_id="", flow="single_calendar_next_event_lookup",
        apps=["Calendar"],
        instruction=instruction,
        verify=(f"ANSWER items == [{{'title': {target_title!r}, "
                 f"'start_local': {target_start_local!r}, "
                 f"'date_iso': {target_date_iso!r}}}]"),
        verify_mode="agent_response",
        initial_state=InitialState(
            present=[f"{n_total} events on {d.isoformat()}: "
                      f"{n_before} before {nice_anchor}, "
                      f"{n_after} after; agent reports the next event "
                      f"after {nice_anchor}"],
            absent=[], noise_records=[],
            setup_commands=[], spec=spec,
            expected_behavior="complete",
        ),
        steps=3,
        complexity=complexity_score(3),
        detail_level=0.0,
        params={"titles": titles,
                "target_title": target_title,
                "target_start_local": target_start_local,
                "target_date_iso": target_date_iso,
                "target_hour": target_hour,
                "anchor_hour": anchor_hour,
                "date": d.isoformat()},
        verify_checks=verify_checks,
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  Contacts Phase 2 generators (v1, locked 2026-05-24)
# ═══════════════════════════════════════════════════════════════════════════════
#
# Five generators across T1/T2/T4/T5. The sixth planned generator —
# gen_birthday_with_calendar_check (T4b cross-app) — is deferred to
# Phase 3 alongside (a) url_contains/url_endswith selector on
# calendar.events, (b) SymbolicRef plumbing for post-apply CN-UUIDs.
#
# Reviewers' guidance applied:
#   - V-F1: `_signature_set` JSON-sorts list items (multi-value Contacts
#     identity tolerant to CN's unstable post-write order).
#   - V-S1/S2: `_canonicalize_phone_digits` strips extensions + folds
#     Unicode digits — phones round-trip across UI normalization.
#   - H-F1: `_contact_unchanged_checks` coerces baseline "" → None on
#     sparse scalar fields (matches `r.get(attr)` = None when omitted).
#
# Helper conventions:
#   - `_contact_spec(contacts)` builds the InitialState.spec entries.
#   - `_pick_distinct_contact_names(n)` returns n unique pairs.
#   - For cross-contact tasks needing the agent to identify a specific
#     target, the generator includes the FULL name in the instruction.
#     Distractor contacts share neither first nor last name with target.


# ── T1.1 — set_contact_birthday ──────────────────────────────────────────────

def gen_set_contact_birthday():
    """Agent edits an existing contact and adds their birthday.

    Phase B refactor: 6-8 diverse contacts; full target baseline
    preserved; agent must locate target in a longer list."""
    n_contacts = random.choice([6, 7, 8])
    contacts = _build_diverse_contact_seed(n_contacts)
    target = contacts[0]
    target_given = target["given_name"]
    target_family = target["family_name"]
    target_baseline = _baseline_from_contact_seed(target)

    birthday = _random_birthday_iso()
    bday_y, bday_m, bday_d = birthday.split("-")
    bday_obj = _dt.date(int(bday_y), int(bday_m), int(bday_d))
    bday_human = bday_obj.strftime("%B %-d, %Y")

    spec = _contact_spec(contacts) + _springboard_noise()

    instruction = random.choice([
        f"Open Contacts. Find {target_given} {target_family}. "
        f"Set their birthday to {bday_human}.",
        f"In Contacts, edit {target_given} {target_family} and add "
        f"their birthday ({bday_human}).",
    ])

    verify_checks: List[Dict[str, Any]] = [
        {"kind": "attribute_eq",
         "resource": "contacts.all",
         "selector": {"given_name": target_given,
                       "family_name": target_family},
         "attr": "birthday", "value": birthday,
         "severity": "blocking",
         "label": f"'{target_given} {target_family}' birthday set to {birthday}"},
        # Comprehensive preservation: every pre-existing field on the
        # target must survive the mutation (no nickname being clobbered,
        # no organization being unset, etc.).
        *_contact_unchanged_checks(
            given_name=target_given, family_name=target_family,
            baseline=target_baseline),
        _contact_distractor_identity_check(
            target_given_name=target_given,
            target_family_name=target_family),
        {"kind": "count",
         "resource": "contacts.all",
         "selector": {},
         "op": "eq", "n": len(contacts),
         "severity": "blocking",
         "label": f"exactly {len(contacts)} contacts"},
    ]

    return Task(
        task_id="", flow="single_contacts_update",
        apps=["Contacts"],
        instruction=instruction,
        verify=f"contacts.all[name={target_given} {target_family}] "
               f"birthday={birthday}",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[], absent=[], noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=6,
        complexity=complexity_score(6, noise_count=n_contacts - 1),
        detail_level=0.0,
        params={"target_given_name": target_given,
                "target_family_name": target_family,
                "birthday": birthday,
                "n_contacts": n_contacts},
        verify_checks=verify_checks,
    )


# ── T1.1b — set_contact_birthday_no_year ─────────────────────────────────────

def gen_set_contact_birthday_no_year():
    """Agent edits an existing contact and adds their birthday WITHOUT
    a year (month + day only — the year-omitted case, iOS Contacts'
    default). Tests:
      - `--MM-DD` round-trip through parseContactDateComponents
      - The agent's ability to deliberately leave the year wheel at
        the "----" sentinel default
      - That the agent can ignore an irrelevant wheel column
    Easier than gen_set_contact_birthday — no 60-tick year traversal.

    Phase B refactor: 6-8 diverse contacts; full target baseline
    preserved."""
    n_contacts = random.choice([6, 7, 8])
    contacts = _build_diverse_contact_seed(n_contacts)
    target = contacts[0]
    target_given = target["given_name"]
    target_family = target["family_name"]
    target_baseline = _baseline_from_contact_seed(target)

    bday_obj = _dt.date(2000, random.randint(1, 12), random.randint(1, 28))
    birthday = bday_obj.strftime("--%m-%d")
    bday_human = bday_obj.strftime("%B %-d")

    spec = _contact_spec(contacts) + _springboard_noise()

    instruction = random.choice([
        f"Open Contacts. Find {target_given} {target_family}. "
        f"Set their birthday to {bday_human} (no year — leave the "
        f"year as 'no year set').",
        f"In Contacts, edit {target_given} {target_family} and add "
        f"their birthday ({bday_human}). Leave the year unset.",
    ])

    verify_checks: List[Dict[str, Any]] = [
        {"kind": "attribute_eq",
         "resource": "contacts.all",
         "selector": {"given_name": target_given,
                       "family_name": target_family},
         "attr": "birthday", "value": birthday,
         "severity": "blocking",
         "label": f"'{target_given} {target_family}' birthday set to "
                  f"{birthday} (year-omitted)"},
        *_contact_unchanged_checks(
            given_name=target_given, family_name=target_family,
            baseline=target_baseline),
        _contact_distractor_identity_check(
            target_given_name=target_given,
            target_family_name=target_family),
        {"kind": "count",
         "resource": "contacts.all",
         "selector": {},
         "op": "eq", "n": len(contacts),
         "severity": "blocking",
         "label": f"exactly {len(contacts)} contacts"},
    ]

    return Task(
        task_id="", flow="single_contacts_update",
        apps=["Contacts"],
        instruction=instruction,
        verify=f"contacts.all[name={target_given} {target_family}] "
               f"birthday={birthday} (year-omitted)",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[], absent=[], noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=5,
        complexity=complexity_score(5, noise_count=n_contacts - 1),
        detail_level=0.0,
        params={"target_given_name": target_given,
                "target_family_name": target_family,
                "birthday": birthday,
                "n_contacts": n_contacts},
        verify_checks=verify_checks,
    )


# ── T2.1 — add_second_phone_label ─────────────────────────────────────────────

def gen_add_second_phone_label():
    """Agent adds a labeled second phone to a contact who already has a
    `mobile` phone. Tests `attribute_set_equals` over CNLabeledValue
    arrays with `digits_only_keys` normalization.

    Phase B refactor: 6-8 diverse contacts; the SECOND phone label is
    randomized across home/work/other so the agent must use the
    SPECIFIC label requested. Full target baseline preserved (org,
    nickname, etc. on pre-existing target survive)."""
    n_contacts = random.choice([6, 7, 8])
    contacts = _build_diverse_contact_seed(n_contacts)
    target = contacts[0]
    target_given = target["given_name"]
    target_family = target["family_name"]

    mobile_phone = _random_contact_phone()
    new_phone = _random_contact_phone()
    while _canonicalize_work_phone_distinct(new_phone, mobile_phone):
        new_phone = _random_contact_phone()
    new_label = random.choice(["work", "home", "other"])

    # The diverse-seed factory put a legacy single `phone` on the
    # target. Replace it with a multi-value `phones` field so we have
    # ONE known starting phone (mobile) — this is what the task is
    # built around. Drop the legacy `phone` from baseline since it's
    # superseded.
    target.pop("phone", None)
    target["phones"] = [{"label": "mobile", "value": mobile_phone}]
    target_baseline = _baseline_from_contact_seed(target)

    spec = _contact_spec(contacts) + _springboard_noise()

    instruction = random.choice([
        f"Open Contacts. Find {target_given} {target_family}. "
        f"Add a '{new_label}' phone number {new_phone} (keep their "
        f"existing mobile number).",
        f"In Contacts, edit {target_given} {target_family} and add "
        f"{new_label} phone {new_phone} alongside their mobile.",
    ])

    expected_phones = [
        {"label": "mobile", "value": mobile_phone},
        {"label": new_label, "value": new_phone},
    ]

    verify_checks: List[Dict[str, Any]] = [
        {"kind": "attribute_set_equals",
         "resource": "contacts.all",
         "selector": {"given_name": target_given,
                       "family_name": target_family},
         "attr": "phones",
         "expected": expected_phones,
         "item_keys": ["label", "value"],
         "digits_only_keys": ["value"],
         "case_sensitive": False,
         "severity": "blocking",
         "label": f"'{target_given} {target_family}' has both phones "
                  f"with labels mobile + {new_label}"},
        # Comprehensive preservation: every pre-existing field except
        # phones (which we mutate) must survive.
        *_contact_unchanged_checks(
            given_name=target_given, family_name=target_family,
            baseline={k: v for k, v in target_baseline.items()
                       if k != "phones"}),
        _contact_distractor_identity_check(
            target_given_name=target_given,
            target_family_name=target_family),
        {"kind": "count",
         "resource": "contacts.all",
         "selector": {},
         "op": "eq", "n": len(contacts),
         "severity": "blocking",
         "label": f"exactly {len(contacts)} contacts"},
    ]

    return Task(
        task_id="", flow="single_contacts_update",
        apps=["Contacts"],
        instruction=instruction,
        verify=f"contacts.all[name={target_given} {target_family}] "
               f"phones set_equals expected (mobile + {new_label})",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[], absent=[], noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=7,
        complexity=complexity_score(7, noise_count=n_contacts - 1),
        detail_level=0.0,
        params={"target_given_name": target_given,
                "target_family_name": target_family,
                "mobile_phone": mobile_phone,
                "new_phone": new_phone,
                "new_label": new_label,
                "n_contacts": n_contacts},
        verify_checks=verify_checks,
    )


def _canonicalize_work_phone_distinct(a: str, b: str) -> bool:
    """Returns True if `a` and `b` canonicalize to the SAME phone
    (caller should resample). Defined inline so it stays close to the
    `gen_add_second_phone_label` call site that uses it."""
    norm = lambda s: "".join(c for c in s if c.isdigit())
    return norm(a) == norm(b)


# ── T2.2 — create_contact_with_address ────────────────────────────────────────

def gen_create_contact_with_address():
    """Agent creates a new contact with name + phone + postal address.
    Tests `CNPostalAddress` round-trip + identity preservation on
    pre-existing distractor contacts.

    Note: agent must reproduce empty strings for unspecified address
    components (state="" if seed has empty state). Generator seeds ALL
    6 components explicitly to avoid this brittleness.

    Phase B refactor: 5-7 diverse distractors with varied state (some
    have org+title, some emails, some pre-existing addresses with
    label='school'). Target is NOT in the seed — agent creates it."""
    n_distractors = random.choice([5, 6, 7])
    # Seed (n_distractors + 1) diverse contacts; take the first one's
    # NAME as the target (the agent will create them), and use the
    # other dicts as distractors.
    seeded = _build_diverse_contact_seed(n_distractors + 1)
    target_given = seeded[0]["given_name"]
    target_family = seeded[0]["family_name"]
    distractors = seeded[1:]

    phone = _random_contact_phone()
    street = random.choice(_CONTACT_STREETS)
    city, state, postal = random.choice(_CONTACT_CITIES)
    country = "USA"

    spec = _contact_spec(distractors) + _springboard_noise()

    instruction = (
        f"Open Contacts. Create a new contact: {target_given} {target_family}, "
        f"phone {phone}, home address {street}, {city}, {state} {postal}.")

    expected_addresses = [{
        "label": "home",
        "street": street, "city": city, "state": state,
        "postal_code": postal, "country": country,
    }]

    verify_checks: List[Dict[str, Any]] = [
        {"kind": "count",
         "resource": "contacts.all",
         "selector": {},
         "op": "eq", "n": len(distractors) + 1,
         "severity": "blocking",
         "label": f"exactly {len(distractors) + 1} contacts after create"},
        {"kind": "exists",
         "resource": "contacts.all",
         "selector": {"given_name": target_given,
                       "family_name": target_family},
         "severity": "blocking",
         "label": f"'{target_given} {target_family}' exists"},
        {"kind": "attribute_set_equals",
         "resource": "contacts.all",
         "selector": {"given_name": target_given,
                       "family_name": target_family},
         "attr": "phones",
         "expected": [{"label": "mobile", "value": phone}],
         "item_keys": ["label", "value"],
         "digits_only_keys": ["value"],
         "case_sensitive": False,
         "severity": "blocking",
         "label": f"'{target_given} {target_family}' phone is {phone}"},
        {"kind": "attribute_set_equals",
         "resource": "contacts.all",
         "selector": {"given_name": target_given,
                       "family_name": target_family},
         "attr": "postal_addresses",
         "expected": expected_addresses,
         "item_keys": ["label", "street", "city", "state",
                        "postal_code", "country"],
         "case_sensitive": False,
         "trim_strings": True,
         "severity": "blocking",
         "label": f"'{target_given} {target_family}' has the seeded address"},
        # Distractors unchanged.
        _contact_distractor_identity_check(
            target_given_name=target_given,
            target_family_name=target_family),
    ]

    return Task(
        task_id="", flow="single_contacts_create",
        apps=["Contacts"],
        instruction=instruction,
        verify=f"contacts.all[name={target_given} {target_family}] "
               f"created with phone={phone} + structured address",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[], absent=[], noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=8,
        complexity=complexity_score(8, noise_count=len(distractors)),
        detail_level=0.0,
        params={"target_given_name": target_given,
                "target_family_name": target_family,
                "phone": phone,
                "street": street, "city": city, "state": state,
                "postal_code": postal, "country": country,
                "n_distractors": n_distractors},
        verify_checks=verify_checks,
    )


# ── T4.1 — full_business_card ─────────────────────────────────────────────────

def gen_full_business_card():
    """Integration test: agent creates a contact with the full v1 field
    surface — name + multi-value phone (2 entries) + multi-value email
    (2 entries) + postal address + nickname + organization + job title.

    If this passes, simpler T1/T2 variants for any single field are
    trivially derivable. Distractor preservation is the cheat block.

    Phase B refactor: 5-7 diverse distractors with rich pre-existing
    state. Target is NOT in the seed — agent creates it."""
    n_distractors = random.choice([5, 6, 7])
    seeded = _build_diverse_contact_seed(n_distractors + 1)
    target_given = seeded[0]["given_name"]
    target_family = seeded[0]["family_name"]
    distractors = seeded[1:]

    nickname = random.choice(["Al", "Sam", "Jay", "Kay", "Mo"])
    org = random.choice(_CONTACT_ORGS)
    job_title = random.choice(_CONTACT_JOB_TITLES)
    mobile_phone = _random_contact_phone()
    work_phone = _random_contact_phone()
    while _canonicalize_work_phone_distinct(mobile_phone, work_phone):
        work_phone = _random_contact_phone()
    home_email = f"{target_given.lower()}@home.example"
    work_email = f"{target_given.lower()}@{org.split()[0].lower()}.example"
    street = random.choice(_CONTACT_STREETS)
    city, state, postal = random.choice(_CONTACT_CITIES)
    country = "USA"

    spec = _contact_spec(distractors) + _springboard_noise()

    instruction = (
        f"Open Contacts. Create a new contact with the following "
        f"information:\n"
        f"  Name: {target_given} {target_family}\n"
        f"  Nickname: {nickname}\n"
        f"  Organization: {org}\n"
        f"  Job title: {job_title}\n"
        f"  Mobile phone: {mobile_phone}\n"
        f"  Work phone: {work_phone}\n"
        f"  Home email: {home_email}\n"
        f"  Work email: {work_email}\n"
        f"  Home address: {street}, {city}, {state} {postal}")

    expected_phones = [
        {"label": "mobile", "value": mobile_phone},
        {"label": "work",   "value": work_phone},
    ]
    expected_emails = [
        {"label": "home", "value": home_email},
        {"label": "work", "value": work_email},
    ]
    expected_addresses = [{
        "label": "home",
        "street": street, "city": city, "state": state,
        "postal_code": postal, "country": country,
    }]

    sel = {"given_name": target_given, "family_name": target_family}

    verify_checks: List[Dict[str, Any]] = [
        {"kind": "count",
         "resource": "contacts.all",
         "selector": {},
         "op": "eq", "n": len(distractors) + 1,
         "severity": "blocking",
         "label": f"exactly {len(distractors) + 1} contacts after create"},
        {"kind": "exists",
         "resource": "contacts.all",
         "selector": sel,
         "severity": "blocking",
         "label": f"'{target_given} {target_family}' exists"},
        {"kind": "attribute_eq",
         "resource": "contacts.all",
         "selector": sel,
         "attr": "nickname", "value": nickname,
         "severity": "blocking",
         "label": f"nickname = {nickname}"},
        {"kind": "attribute_eq",
         "resource": "contacts.all",
         "selector": sel,
         "attr": "organization", "value": org,
         "severity": "blocking",
         "label": f"organization = {org}"},
        {"kind": "attribute_eq",
         "resource": "contacts.all",
         "selector": sel,
         "attr": "job_title", "value": job_title,
         "severity": "blocking",
         "label": f"job_title = {job_title}"},
        {"kind": "attribute_set_equals",
         "resource": "contacts.all",
         "selector": sel,
         "attr": "phones",
         "expected": expected_phones,
         "item_keys": ["label", "value"],
         "digits_only_keys": ["value"],
         "case_sensitive": False,
         "severity": "blocking",
         "label": "phones (mobile + work) both present"},
        {"kind": "attribute_set_equals",
         "resource": "contacts.all",
         "selector": sel,
         "attr": "emails",
         "expected": expected_emails,
         "item_keys": ["label", "value"],
         "case_sensitive": False,  # iOS may lowercase email
         "trim_strings": True,
         "severity": "blocking",
         "label": "emails (home + work) both present"},
        {"kind": "attribute_set_equals",
         "resource": "contacts.all",
         "selector": sel,
         "attr": "postal_addresses",
         "expected": expected_addresses,
         "item_keys": ["label", "street", "city", "state",
                        "postal_code", "country"],
         "case_sensitive": False,
         "trim_strings": True,
         "severity": "blocking",
         "label": "home address present"},
        # Distractors unchanged.
        _contact_distractor_identity_check(
            target_given_name=target_given,
            target_family_name=target_family),
    ]

    return Task(
        task_id="", flow="single_contacts_create",
        apps=["Contacts"],
        instruction=instruction,
        verify=f"contacts.all[name={target_given} {target_family}] "
               f"has full business card",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[], absent=[], noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=14,
        complexity=complexity_score(14, noise_count=len(distractors)),
        detail_level=0.0,
        params={"target_given_name": target_given,
                "target_family_name": target_family,
                "nickname": nickname, "organization": org,
                "job_title": job_title,
                "mobile_phone": mobile_phone, "work_phone": work_phone,
                "home_email": home_email, "work_email": work_email,
                "street": street, "city": city, "state": state,
                "postal_code": postal, "country": country,
                "n_distractors": n_distractors},
        verify_checks=verify_checks,
    )


# ── T5.1 — lookup_phone_by_name ───────────────────────────────────────────────

def gen_lookup_phone_by_name():
    """Read-only T5: agent looks up a specific contact's mobile phone
    and emits a JSON ANSWER. Tests `agent_answer` + `digits_only_keys`
    over scalar string compare (the canonicalizer normalizes both seed
    and agent output).

    `observation_required=["com.apple.MobileAddressBook"]` enforces the
    agent navigate to Contacts before answering — prevents hardcoded
    answers / pre-training memorization.

    Phase B refactor: 6-8 diverse contacts; target has a known mobile
    phone in `phones` (multi-value), other pre-existing fields are
    randomized via the diverse seed."""
    n_contacts = random.choice([6, 7, 8])
    contacts = _build_diverse_contact_seed(n_contacts)
    target = contacts[0]
    target_given = target["given_name"]
    target_family = target["family_name"]
    target_phone = _random_contact_phone()

    # Replace the seed's legacy single `phone` with a multi-value
    # `phones` field carrying the known lookup value.
    target.pop("phone", None)
    target["phones"] = [{"label": "mobile", "value": target_phone}]
    target_baseline = _baseline_from_contact_seed(target)
    # Distractors keep their legacy `phone` field (set by the seed).

    spec = _contact_spec(contacts) + _springboard_noise()

    answer_check, schema_sentence = make_answer_check(
        match="string_eq",
        expected=target_phone,
        path="$.phone",
        digits_only=True,
        case_sensitive=False,
        trim_strings=True,
        observation_required=["com.apple.MobileAddressBook"],
        label=f"agent reports {target_given} {target_family}'s mobile "
              f"phone matches {target_phone} (digit-normalized)",
    )

    instruction = (
        f"Open Contacts. What is {target_given} {target_family}'s mobile "
        f"phone number? {schema_sentence}")

    verify_checks: List[Dict[str, Any]] = [
        answer_check,
        # Cheat-block: the agent must not have mutated state while
        # looking up. Distractor identity catches sibling edits;
        # target_unchanged catches stealth edits to the target itself
        # (e.g. adding a nickname, changing org) that the identity
        # check's name-keyed exclude would silently skip.
        *_contact_unchanged_checks(
            given_name=target_given, family_name=target_family,
            baseline=target_baseline),
        _contact_distractor_identity_check(
            target_given_name=target_given,
            target_family_name=target_family,
            label="no contacts edited during read-only lookup"),
        {"kind": "count",
         "resource": "contacts.all",
         "selector": {},
         "op": "eq", "n": len(contacts),
         "severity": "blocking",
         "label": f"contact count unchanged ({len(contacts)})"},
    ]

    return Task(
        task_id="", flow="single_contacts_lookup",
        apps=["Contacts"],
        instruction=instruction,
        verify=f"agent ANSWER $.phone matches {target_phone} "
               f"(digit-normalized)",
        verify_mode="agent_answer",
        initial_state=InitialState(
            present=[], absent=[], noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=5,
        complexity=complexity_score(5, noise_count=n_contacts - 1),
        detail_level=0.0,
        params={"target_given_name": target_given,
                "target_family_name": target_family,
                "target_phone": target_phone,
                "n_contacts": n_contacts},
        verify_checks=verify_checks,
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  Phase 3 cross-app generators (locked 2026-05-26)
# ═══════════════════════════════════════════════════════════════════════════════
#
# Cross-app generators exercise multiple handlers + verifier surfaces
# in one episode. The agent must context-switch between apps and
# preserve state across the switch.


# ── X1 — maps_search_to_contact (Maps → Contacts) ────────────────────────────

# Lesser-known places — real, in Apple Maps with stable addresses, but
# NOT famous enough that an LLM can hallucinate the address from training
# memory. Mix of museums, parks, small businesses, historical buildings.
# The verifier only checks city (stable) + the address LABEL the agent
# chose — not the specific street, because Maps may format streets
# differently across iOS versions.
#
# Each tuple: (search_query, expected_city).
# Keep this list curated and conservative — if a place's address
# becomes ambiguous in a later iOS version, remove it.
_LESSER_KNOWN_PLACES = [
    ("Salk Institute",                "La Jolla"),
    ("Marin Headlands Visitor Center", "Sausalito"),
    ("The Last Bookstore",            "Los Angeles"),
    ("Tartine Manufactory",           "San Francisco"),
    ("City Lights Bookstore",         "San Francisco"),
    ("Stahl House",                   "Los Angeles"),
    ("Powell's City of Books",        "Portland"),
    ("Hollyhock House",               "Los Angeles"),
    ("Bouchon Bakery",                "Yountville"),
    ("Heinz History Center",          "Pittsburgh"),
]

_ADDRESS_LABELS = ["home", "work", "other"]

_ORG_FOR_SEED = [
    "Acme Corp", "Initech", "Globex", "Stark Industries",
    "Wayne Enterprises", "Hooli", "Pied Piper", "Soylent",
]
_JOB_TITLES_FOR_SEED = [
    "Engineer", "Designer", "Manager", "Analyst", "Consultant",
    "Director", "Lead", "Coordinator",
]


def _build_diverse_contact_seed(n: int) -> List[Dict[str, Any]]:
    """Build N contacts with varied pre-existing fields.

    Each contact has at minimum given_name + family_name + phone.
    Randomized variations:
      ~50% have organization + job_title
      ~33% have an email (home label)
      ~25% have a pre-existing postal_address with a label DIFFERENT
            from what the agent will use (to test that the agent
            ADDS without REPLACING)
      ~25% have a nickname
    """
    names = _pick_distinct_contact_names(n)
    contacts: List[Dict[str, Any]] = []
    for i, (gn, fn) in enumerate(names):
        c: Dict[str, Any] = {
            "given_name": gn,
            "family_name": fn,
            "phone": _random_contact_phone(),
        }
        if random.random() < 0.5:
            c["organization"] = random.choice(_ORG_FOR_SEED)
            c["job_title"] = random.choice(_JOB_TITLES_FOR_SEED)
        if random.random() < 0.33:
            c["email"] = f"{gn.split()[0].lower()}{i}@example.com"
        if random.random() < 0.25:
            c["nickname"] = gn.split()[0][:3]
        if random.random() < 0.25:
            # Pre-existing address with a fixed label that the target
            # won't collide with (we use "school" since the agent will
            # pick from home/work/other). Two purposes: tests that the
            # agent ADDS rather than REPLACES, and that distractors
            # with rich state are byte-preserved.
            city_data = random.choice(_CONTACT_CITIES)
            c["postal_addresses"] = [{
                "label": "school",
                "street": random.choice(_CONTACT_STREETS),
                "city": city_data[0],
                "state": city_data[1],
                "postal_code": city_data[2],
                "country": "USA",
            }]
        contacts.append(c)
    return contacts


def _baseline_from_contact_seed(c: Dict[str, Any]) -> Dict[str, Any]:
    """Extract every field from a seeded contact that should be
    preserved verbatim post-mutation. Used to populate
    `_contact_unchanged_checks(baseline=...)`."""
    # Strip the spec-shape keys ("app", "type", "identifier") that
    # aren't actual data fields.
    return {k: v for k, v in c.items()
              if k not in ("app", "type", "identifier")}


def gen_maps_search_to_contact():
    """Cross-app: agent looks up a place's address (any source — Maps,
    Spotlight, Safari) and adds it to a specific contact under a
    randomized label (home / work / other).

    The task instruction does NOT prescribe Maps usage — the agent
    picks the path. Verifier only checks the resulting contact state:
    the target has at least one postal_address with the expected label
    and city. Streets aren't checked (Maps may format them as "1 X St"
    vs "One X Street" vs "X St" — too brittle).

    Diversity / cheat-resistance:
      - Lesser-known places (Salk Institute, City Lights Bookstore,
        Hollyhock House etc.) — LLMs can't reliably hallucinate these
        addresses from training memory
      - Address label randomized (home/work/other) — agent must use the
        SPECIFIC label requested
      - 6-8 contacts seeded with varied pre-existing state (some w/ org,
        emails, nicknames, even pre-existing "school" addresses on
        ~25% of contacts to test ADD-not-REPLACE)
      - Per-contact baseline tracked — verifier asserts EVERY pre-
        existing field on the target survives, plus distractors are
        byte-equal to baseline via the identity check
    """
    place_query, expected_city = random.choice(_LESSER_KNOWN_PLACES)
    address_label = random.choice(_ADDRESS_LABELS)

    # 6-8 contacts with varied state. Target is index 0 for determinism;
    # the agent finds them by name (random first/last from the data
    # dictionary, so the target name varies per seed).
    n_contacts = random.choice([6, 7, 8])
    contacts = _build_diverse_contact_seed(n_contacts)
    target = contacts[0]
    target_given = target["given_name"]
    target_family = target["family_name"]
    target_baseline = _baseline_from_contact_seed(target)

    spec = _contact_spec(contacts) + _springboard_noise()

    instruction = (
        f"Find the address of {place_query} and add it as the "
        f"{address_label} address for {target_given} {target_family} "
        f"in Contacts. Save and finish.")

    verify_checks: List[Dict[str, Any]] = [
        # Target still exists by name.
        {"kind": "exists",
         "resource": "contacts.all",
         "selector": {"given_name": target_given,
                       "family_name": target_family},
         "severity": "blocking",
         "label": f"'{target_given} {target_family}' still exists"},

        # Load-bearing check: target's postal_addresses contains an
        # entry with the expected LABEL and the expected CITY. Street
        # is intentionally NOT constrained (Maps formats vary). If
        # target already had a postal_address (the ~25% case with
        # label="school"), that one is preserved by the identity-style
        # baseline checks below, and the new entry is the additional
        # match here.
        {"kind": "attribute_set_contains",
         "resource": "contacts.all",
         "selector": {"given_name": target_given,
                       "family_name": target_family},
         "attr": "postal_addresses",
         "expected": [{"label": address_label,
                        "city": expected_city}],
         "case_sensitive": False,
         "trim_strings": True,
         "severity": "blocking",
         "label": f"'{target_given} {target_family}' postal_addresses "
                  f"contains entry with label='{address_label}' and "
                  f"city='{expected_city}'"},

        # Comprehensive target-preservation: every pre-existing field
        # on the target must survive the mutation. If target had a
        # pre-existing postal_address (school label), it's verified
        # via the helper's attribute_set_equals for the postal_addresses
        # baseline — BUT the agent will have added a new entry, making
        # the set larger. So we EXCLUDE postal_addresses from this
        # comprehensive check; the new-entry verification above is
        # the postal-side guard.
        *_contact_unchanged_checks(
            given_name=target_given, family_name=target_family,
            baseline={k: v for k, v in target_baseline.items()
                       if k != "postal_addresses"}),

        # Distractors byte-equal to baseline (identity check excludes
        # target by name, asserts every other contact unchanged).
        _contact_distractor_identity_check(
            target_given_name=target_given,
            target_family_name=target_family),

        # Count: no spurious contact creations.
        {"kind": "count",
         "resource": "contacts.all",
         "selector": {},
         "op": "eq", "n": len(contacts),
         "severity": "blocking",
         "label": f"exactly {len(contacts)} contacts"},
    ]

    return Task(
        task_id="", flow="cross_maps_contacts",
        apps=["Contacts"],   # Maps usage no longer prescribed
        instruction=instruction,
        verify=(f"contacts.all[name={target_given} {target_family}] "
                 f"has postal_address with label='{address_label}', "
                 f"city='{expected_city}'"),
        verify_mode="db_query",
        initial_state=InitialState(
            present=[], absent=[], noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=14,  # address lookup + cross-app switch + Contacts edit
        complexity=complexity_score(14, noise_count=len(contacts) - 1),
        detail_level=0.0,
        params={"place_query": place_query,
                "expected_city": expected_city,
                "address_label": address_label,
                "target_given_name": target_given,
                "target_family_name": target_family,
                "n_contacts": n_contacts},
        verify_checks=verify_checks,
    )


# ── X2 / X3 — message → contact (→ maps) ─────────────────────────────────────

# Curated address corpus for Messages tasks. Each entry is a 6-tuple
# (street, city, state, postal, lat, lon). Picked to resolve robustly
# in Apple Maps so the agent can commit a Directions route. All on
# well-known thoroughfares.
#
# The lat/lon were captured via the in-sim `geocode_query` command
# (MKLocalSearch, same backend Maps.app uses). They're the canonical
# expected destinations for variant D / E's geo_within_m verifier
# check (50 m tolerance). See sibb/benchmark/sibb_geocode_addresses.py
# for the geocoding tool; re-run if the corpus is edited.
#
# Notes from the geocode run:
#   - Apple's ZIP corrections (94111 vs 94105 for 100 Market; 90012
#     vs 90026 for 1100 Sunset; 20212 vs 20004 for 1500 Pennsylvania)
#     don't matter — lat/lon is the source of truth.
#   - "400 Lake Shore Dr Chicago" is silently normalized to the
#     renamed "N DuSable Lake Shore Dr"; same coord.
#   - "1 Yawkey Way Boston" → "1 Jersey St" (renamed 2018); Fenway Park.
#   - "Lakeshore Drive Oakland" is ambiguous to MKLocalSearch (snaps to
#     SF Lake Merced); the corpus uses "3300 Lakeshore Avenue" — the
#     correct Lake Merritt thoroughfare.
_MESSAGE_ADDRESSES = [
    ("100 Market Street",          "San Francisco", "CA", "94105", 37.794199, -122.394911),
    ("350 5th Avenue",             "New York",      "NY", "10118", 40.748238,  -73.985058),
    ("1100 W Sunset Blvd",         "Los Angeles",   "CA", "90026", 34.065438, -118.249573),
    ("400 Lake Shore Drive",       "Chicago",       "IL", "60611", 41.890156,  -87.614743),
    ("700 Massachusetts Avenue",   "Boston",        "MA", "02118", 42.335170,  -71.075000),
    ("2200 Pike Place",            "Seattle",       "WA", "98101", 47.610438, -122.343100),
    ("1500 Pennsylvania Avenue",   "Washington",    "DC", "20004", 38.898188,  -77.034412),
    ("250 Howard Street",          "San Francisco", "CA", "94105", 37.790217, -122.394261),
    ("3300 Lakeshore Avenue",      "Oakland",       "CA", "94610", 37.810448, -122.243964),
    ("1 Yawkey Way",               "Boston",        "MA", "02215", 42.346604,  -71.099075),
]


# Diverse message-template pools by variant. Each template is a
# callable that takes the variant-specific fields and returns the
# message text. Style mix (casual / formal / fragmented / terse /
# interrogative) keeps the LLM's parse task non-trivial across seeds.

# Variant A — sender introduces themselves; agent uses the sender's
# phone number (from the thread header) + the name from message body.
# Each template returns either a single string (one bubble) OR a
# list of strings (multi-bubble — JA sends N short messages, all
# appear as inbound bubbles in KB). The multi-bubble form mirrors
# real-world chat where someone types thoughts in fragments rather
# than one paragraph. Generators pick single vs multi at random
# (see `_pick_message_payload`).
#
# Single-bubble templates are imperative-LESS than the old set —
# the OUTER PROMPT now names the action ("save the address" /
# "create a contact + directions"); the message body is just data
# in casual phrasing, like a real text.

_MESSAGE_TEMPLATES_SELF_INTRO_SINGLE = [
    lambda g, f: f"hey it's {g} {f}",
    lambda g, f: f"hi this is {g} {f}",
    lambda g, f: f"{g} {f} here, new phone",
    lambda g, f: f"new number — {g} {f}",
    lambda g, f: f"it's {g} {f} btw",
    lambda g, f: f"hi! {g} {f}",
]
_MESSAGE_TEMPLATES_SELF_INTRO_MULTI = [
    lambda g, f: ["hey", f"this is {g} {f}"],
    lambda g, f: ["hi", f"{g} {f} here"],
    lambda g, f: [f"{g} {f}", "new phone, new number"],
    lambda g, f: ["new number", f"it's {g} {f}"],
]

# Variant B — third-party forward. Sender shares someone ELSE's name
# + phone. The named person is NOT the sender. Phone format varies
# (formatted / hyphenated / parens) so the verifier's digits-only
# match is what enforces correctness.
# Variant B — name + phone for a THIRD PARTY (not the sender).
# Templates use "their/them/they're" pronouns and explicit framings
# ("if you need…", "forwarding…", "passing along…") so the agent
# can't confuse {g} {f} with the sender. Rewritten 2026-05-31 after
# trial showed the agent assuming {g} {f} = sender when the framing
# was bare ("Riley Jones's number: …").
_MESSAGE_TEMPLATES_BODY_PHONE_SINGLE = [
    # "their/them" pronouns make it explicit
    lambda g, f, ph: f"got {g} {f}'s info — they're at {ph}",
    lambda g, f, ph: f"{g} {f}'s number — text them at {ph}",
    # "if you need" framing
    lambda g, f, ph: f"if you need to reach {g} {f}, their number is {ph}",
    lambda g, f, ph: f"in case you need {g} {f} — {ph}",
    # "forwarding" / "passing along" framing
    lambda g, f, ph: f"forwarding {g} {f}'s contact: {ph}",
    lambda g, f, ph: f"passing along — {g} {f} can be reached at {ph}",
]
_MESSAGE_TEMPLATES_BODY_PHONE_MULTI = [
    # Last bubble carries the data so the inbox preview shows it
    # (iOS Messages inbox preview = last bubble only). If the agent
    # only glances at the inbox, it still sees the phone number.
    lambda g, f, ph: ["the contact you asked about —",
                       f"{g} {f}: {ph}"],
    lambda g, f, ph: [f"btw if you need {g} {f}",
                       f"their number is {ph}"],
    lambda g, f, ph: [f"forwarding {g} {f}'s info",
                       f"text them at {ph}"],
    lambda g, f, ph: ["passing along —", f"{g} {f}",
                       f"reach them at {ph}"],
]

# Variant C — name + address + label for a THIRD PARTY, no
# directions ask. Same third-party framing as variant B.
_MESSAGE_TEMPLATES_ADDRESS_SINGLE = [
    lambda g, f, s, c, lbl: f"got {g} {f}'s {lbl} — they're at {s}, {c}",
    lambda g, f, s, c, lbl: f"{g} {f}'s {lbl}: {s}, {c} — that's where they are",
    lambda g, f, s, c, lbl: f"if you need to mail {g} {f}, their {lbl} is {s}, {c}",
    lambda g, f, s, c, lbl: f"in case you need {g} {f}, their {lbl} is {s}, {c}",
    lambda g, f, s, c, lbl: f"forwarding {g} {f}'s {lbl} — {s}, {c}",
    lambda g, f, s, c, lbl: f"passing along — {g} {f}'s {lbl}: {s}, {c}",
]
_MESSAGE_TEMPLATES_ADDRESS_MULTI = [
    lambda g, f, s, c, lbl: [f"forwarding {g} {f}'s {lbl}", f"{s}, {c}"],
    lambda g, f, s, c, lbl: [f"if you need {g} {f}",
                              f"their {lbl} is {s}, {c}"],
    lambda g, f, s, c, lbl: ["passing along —",
                              f"{g} {f}'s {lbl}", f"{s}, {c}"],
    lambda g, f, s, c, lbl: [f"in case you need to mail {g} {f}",
                              f"their {lbl}: {s}, {c}"],
]

# Variants D / E — name + address + label for a THIRD PARTY, with
# a casual directions ask. Same framing pattern. Multi-bubble
# variants put the address in the LAST bubble so iOS Messages'
# inbox preview shows it (avoids the failure mode where the agent
# sees only "see you there" and concludes no address).
_MESSAGE_TEMPLATES_ADDRESS_PLUS_DIRECTIONS_SINGLE = [
    lambda g, f, s, c, lbl: f"hey it's KB — meeting {g} {f} at their {lbl}, {s}, {c}. can you head over?",
    lambda g, f, s, c, lbl: f"hey, KB here. if you're picking up {g} {f}, their {lbl} is {s}, {c}",
    lambda g, f, s, c, lbl: f"hey it's KB — forwarding {g} {f}'s {lbl}: {s}, {c}. drop by",
    lambda g, f, s, c, lbl: f"KB here — in case you need to head to {g} {f}'s, their {lbl} is {s}, {c}",
    lambda g, f, s, c, lbl: f"hey, KB here — passing along {g} {f}'s {lbl}: {s}, {c}. come over",
]
_MESSAGE_TEMPLATES_ADDRESS_PLUS_DIRECTIONS_MULTI = [
    lambda g, f, s, c, lbl: ["hey it's KB", f"forwarding {g} {f}'s {lbl}",
                              f"{s}, {c}"],
    lambda g, f, s, c, lbl: ["hey, KB here",
                              f"if you need {g} {f}, their {lbl} is {s}, {c}"],
    lambda g, f, s, c, lbl: ["KB here —",
                              f"passing along {g} {f}'s {lbl}: {s}, {c}"],
    lambda g, f, s, c, lbl: ["hey it's KB", f"meeting {g} {f} —",
                              f"their {lbl}", f"{s}, {c}"],
]


# Variant F — sender introduces themselves AND shares their own
# address. Merge of A's "name in body / phone from sender header"
# pattern with C's "label + address in body" pattern. Agent must
# create a contact carrying the SENDER's phone (visible in the KB
# thread header) AND the address from the body, with the label woven
# in.
_MESSAGE_TEMPLATES_SELF_INTRO_WITH_ADDRESS_SINGLE = [
    lambda g, f, s, c, lbl: f"hey it's {g} {f}, my {lbl} is {s}, {c}",
    lambda g, f, s, c, lbl: f"hi this is {g} {f} — {lbl} address {s} {c}",
    lambda g, f, s, c, lbl: f"{g} {f} here. my {lbl}: {s}, {c}",
    lambda g, f, s, c, lbl: f"it's {g} {f}, swing by my {lbl} — {s}, {c}",
    lambda g, f, s, c, lbl: f"new number — {g} {f}. my {lbl} is {s}, {c}",
]
_MESSAGE_TEMPLATES_SELF_INTRO_WITH_ADDRESS_MULTI = [
    lambda g, f, s, c, lbl: ["hey", f"this is {g} {f}",
                              f"my {lbl} is {s}, {c}"],
    lambda g, f, s, c, lbl: [f"{g} {f}", f"{lbl} address",
                              f"{s}, {c}"],
    lambda g, f, s, c, lbl: ["hi", f"it's {g} {f}",
                              f"swing by my {lbl}", f"{s}, {c}"],
    lambda g, f, s, c, lbl: [f"new number — {g} {f}",
                              f"my {lbl}", f"{s}, {c}"],
]


def _pick_message_payload(single_templates, multi_templates,
                            multi_prob: float = 0.5, *template_args):
    """Pick a single-bubble or multi-bubble template at random and
    render it. Returns:
      - str  (single-bubble — backwards compatible)
      - list[str] (multi-bubble — MessagesHandler sends each as a
        separate iMessage so they appear as N inbound bubbles).
    """
    if random.random() < multi_prob and multi_templates:
        tmpl = random.choice(multi_templates)
        return tmpl(*template_args)
    tmpl = random.choice(single_templates)
    return tmpl(*template_args)


# Per-variant outer instructions. Each opens with "Find the latest
# received message" to anchor the agent, then names the apps + action
# explicitly. The label (home/work/other) is sourced from the message
# body (not the instruction) — the agent must parse it.

_INSTRUCTION_A = (
    "Find the latest received message thread and read the "
    "message(s) carefully. The sender introduces themselves. "
    "Create a new contact for them in Contacts using their name "
    "from the message and the phone number the message came from."
)

_INSTRUCTION_B = (
    "Find the latest received message thread and read the "
    "message(s) carefully. It contains another person's name and "
    "phone number. Create a new contact in Contacts using only "
    "the name and phone in the message — not the number it came "
    "from."
)

_INSTRUCTION_C = (
    "Find the latest received message thread and read the "
    "message(s) carefully. There is an address and a name. Save "
    "the address to their contact in Contacts under the name. If "
    "they're not already in your contacts, create a new contact "
    "for them. Make sure the address is saved under the correct "
    "label (home/work/other) — the message specifies which."
)

_INSTRUCTION_D = (
    "Find the latest received message thread and read the "
    "message(s) carefully. It has an address for someone already "
    "in your Contacts. Open the Contacts app and find their "
    "entry, then save the address under the correct label "
    "(home/work/other) specified in the message, then open Maps "
    "and start navigating to that address."
)

_INSTRUCTION_E = (
    "Find the latest received message thread and read the "
    "message(s) carefully. It has an address for someone who "
    "isn't in your contacts. Open the Contacts app and create a "
    "new contact for them with this address under the correct "
    "label (home/work/other) specified in the message, then open "
    "Maps and start navigating there."
)

_INSTRUCTION_F = (
    "Find the latest received message thread and read the "
    "message(s) carefully. The sender introduces themselves and "
    "shares their own address. Create a new contact for them in "
    "Contacts using their name from the message, the phone number "
    "the message came from, AND the address from the message body "
    "under the correct label (home/work/other) specified in the "
    "message."
)

# Back-compat alias — old code paths importing _INSTRUCTION_GENERIC
# get a sensible (if non-specific) fallback. New code should use the
# per-variant constants above.
_INSTRUCTION_GENERIC = _INSTRUCTION_C


def _message_episode_setup(rng_target_in_seed: bool):
    """Shared setup for all message → contact (→ maps) generators.

    Returns: (contacts, distractors, target_dict_or_none, target_given,
             target_family, street, city, state, postal, lat, lon,
             address_label).

    `rng_target_in_seed=True` (pre-existing target): target is at
    contacts[0]; spec serializes ALL contacts. Target's
    postal_addresses is wiped so the task's address add doesn't
    collide with seed state.
    `rng_target_in_seed=False` (agent-creates): target name comes
    from seeded[0] but its dict is DISCARDED; spec serializes
    distractors only.

    The (lat, lon) is the canonical MKLocalSearch-resolved coord for
    `street, city, state, postal` (captured at design time via
    `sibb_geocode_addresses.py`). Variants D/E pass these into the
    `geo_within_m` verifier check.

    Caller picks the template + builds the message_spec to allow
    different variants (self-intro / body-phone / address / address+
    directions) to share this setup but produce different message
    bodies.
    """
    n_contacts = random.choice([6, 7, 8])
    seeded = _build_diverse_contact_seed(n_contacts)
    if rng_target_in_seed:
        contacts = seeded
        target = contacts[0]
        distractors = contacts[1:]
        target.pop("postal_addresses", None)
    else:
        contacts = None
        target = None
        distractors = seeded[1:]
    target_given = seeded[0]["given_name"]
    target_family = seeded[0]["family_name"]

    street, city, state, postal, lat, lon = random.choice(_MESSAGE_ADDRESSES)
    address_label = random.choice(["home", "work", "other"])

    return (contacts, distractors, target, target_given, target_family,
            street, city, state, postal, lat, lon, address_label)


def _build_message_spec(payload, thread: str = "JA") -> Dict[str, Any]:
    """Wrap message payload into a Messages handler spec entry.

    `payload` is either:
      - str          → single-bubble (legacy form, stored as `text`)
      - list[str]    → multi-bubble (stored as `texts`; handler sends
                       each as a separate iMessage so they appear as
                       N inbound bubbles in the OPPOSITE thread via
                       IDS loopback)

    `thread` selects which phantom thread to send TO; the IDS loopback
    echoes the message(s) back as inbound on the OPPOSITE thread
    (send to JA → inbound bubbles appear in KB).
    """
    base = {"app": "Messages", "type": "send_in_thread", "thread": thread}
    if isinstance(payload, list):
        return {**base, "texts": [str(t) for t in payload if t]}
    return {**base, "text": str(payload)}


# The sender's phone number from the iOS-sim phantom inbox cell when we
# send TO JA. The loopback inbound bubble appears in KB, whose thread
# header displays this number. The agent reads this number from the AX
# tree (cell label is `+1 (555) 564-8583, <body>, <time>`) and types
# it into a Contacts phone field. `_digits_only_norm` (verifier) strips
# the leading 1, so any of these typed forms match: "+1 (555) 564-8583",
# "(555) 564-8583", "555-564-8583", "5555648583".
_SENDER_PHONE_DIGITS = "5555648583"
_SENDER_PHONE_DISPLAY = "+1 (555) 564-8583"


def _maps_directions_committed_check(
        label_suffix: str,
        *,
        expected_lat: Optional[float] = None,
        expected_lon: Optional[float] = None,
        radius_m: float = 50.0) -> List[Dict[str, Any]]:
    """Verifier checks: agent actually tapped Go to start navigation
    AND (optionally) navigated to the expected destination.

    Returns a LIST so callers can splat with `*_maps_..._check(...)`
    into the verify_checks list — supports the optional geo check
    cleanly without complicating the calling code.

    Check 1 — activation gate (`exists` on `maps.active_route`).
    Uses the unified rstorage-backed Phase A++ reader; requires the
    plist active-nav blob is present AND a `GraphDirections/<UUID>`
    marker matching the active-nav response UUID exists (the marker
    is written only when the user taps Go, not when previewing).
    `$baseline_epoch` scopes the file scan to this episode.

    Check 2 — destination match (`geo_within_m`, only if
    expected_lat/expected_lon are passed). Reads the active route's
    `destination.lat`/`destination.lon` and asserts haversine
    distance from the expected coord is ≤ radius_m (default 50 m).
    Expected coords come from `_MESSAGE_ADDRESSES` and were
    pre-resolved via in-sim MKLocalSearch (same backend Maps.app
    uses), so SDK-build drift is zero and the only residual
    variance is the agent's query-string fuzz — typically < 30 m
    for fully-qualified addresses.

    Stronger than the legacy `maps.history z_ent=16 row count`:
    history rows are written even on preview-only directions taps,
    so the old check could pass without activation. This one can't.
    """
    checks: List[Dict[str, Any]] = [
        {"kind": "exists",
         "resource": "maps.active_route",
         "selector": {"require_activated": True,
                       "min_mtime_epoch": "$baseline_epoch"},
         "severity": "blocking",
         "label": f"agent started Maps navigation ({label_suffix})"},
    ]
    if expected_lat is not None and expected_lon is not None:
        checks.append({
            "kind": "geo_within_m",
            "resource": "maps.active_route",
            "selector": {"require_activated": True,
                          "min_mtime_epoch": "$baseline_epoch"},
            "lat": float(expected_lat),
            "lon": float(expected_lon),
            "radius_m": float(radius_m),
            "severity": "blocking",
            "label": (f"Maps destination within {int(radius_m)} m "
                       f"of expected ({label_suffix})"),
        })
    return checks


def _message_address_post_state_checks(
        *, target_given: str, target_family: str,
        target_baseline: Optional[Dict[str, Any]],
        address_label: str, city: str, street: str,
        ) -> List[Dict[str, Any]]:
    """Strict per-field verifier for the message-address variants
    (gen_message_save_address, gen_message_to_contact_to_maps,
    gen_message_to_new_contact_to_maps).

    Closes the cheat where the agent uses `tap sender # → Create New
    Contact` in Messages — iOS auto-prefills the new contact with the
    SENDER's phone, which isn't supposed to belong to the third-party
    named in the message body. The address-only message implies an
    address-only contact mutation.

    Two paths:
      - target_baseline=None (CREATE): new contact has exactly one
        postal_address (the message's); zero phones/emails/urls/dates;
        all sparse scalars unset.
      - target_baseline=dict (UPDATE): all baseline fields byte-equal
        post-task except postal_addresses, which is
        baseline + [new entry] (labels never collide — baseline uses
        "school"; new uses home/work/other).

    Caller still emits `exists` and `count` + the distractor identity
    check separately so they remain visible at the call site."""
    selector = {"given_name": target_given, "family_name": target_family}
    checks: List[Dict[str, Any]] = []

    if target_baseline is None:
        # CREATE path: lock the post-state to exactly what the message
        # specified. Anything else (e.g. an iOS-prefilled phone from
        # the Messages contact shortcut) is a cheat.
        checks.append({
            "kind": "attribute_list_length",
            "resource": "contacts.all",
            "selector": dict(selector),
            "attr": "postal_addresses",
            "op": "eq", "n": 1,
            "severity": "blocking",
            "label": (f"'{target_given} {target_family}' has exactly "
                      f"one postal_address"),
        })
        checks.append({
            "kind": "attribute_set_contains",
            "resource": "contacts.all",
            "selector": dict(selector),
            "attr": "postal_addresses",
            "expected": [{"label": address_label, "street": street,
                           "city": city}],
            "case_sensitive": False,
            "trim_strings": True,
            "street_norm_keys": ["street"],
            "severity": "blocking",
            "label": (f"'{target_given} {target_family}' postal_addresses "
                      f"contains entry with label='{address_label}', "
                      f"street='{street}' (normalized), city='{city}'"),
        })
        # No iOS-auto-prefill leak — no phones/emails/urls/dates etc.
        for list_field in ("phones", "emails", "urls", "dates"):
            checks.append({
                "kind": "attribute_list_length",
                "resource": "contacts.all",
                "selector": dict(selector),
                "attr": list_field,
                "op": "eq", "n": 0,
                "severity": "blocking",
                "label": (f"'{target_given} {target_family}' has no "
                          f"{list_field} (message only provided "
                          f"address)"),
            })
        # Sparse scalars + legacy convenience fields must be absent.
        for scalar in ("phone", "email", "middle_name", "nickname",
                       "phonetic_given_name", "phonetic_family_name",
                       "phonetic_middle_name", "organization",
                       "job_title", "department", "birthday"):
            checks.append({
                "kind": "attribute_eq",
                "resource": "contacts.all",
                "selector": dict(selector),
                "attr": scalar, "value": None,
                "severity": "blocking",
                "label": (f"'{target_given} {target_family}' {scalar} "
                          f"unset (not in message)"),
            })
        return checks

    # UPDATE path. Target baseline has all pre-existing fields. The
    # task adds one postal_address; everything else must be unchanged.
    baseline_pa = list(target_baseline.get("postal_addresses", []) or [])
    expected_len = len(baseline_pa) + 1
    checks.append({
        "kind": "attribute_list_length",
        "resource": "contacts.all",
        "selector": dict(selector),
        "attr": "postal_addresses",
        "op": "eq", "n": expected_len,
        "severity": "blocking",
        "label": (f"'{target_given} {target_family}' postal_addresses "
                  f"has exactly {expected_len} entries "
                  f"({len(baseline_pa)} baseline + 1 new)"),
    })
    checks.append({
        "kind": "attribute_set_contains",
        "resource": "contacts.all",
        "selector": dict(selector),
        "attr": "postal_addresses",
        "expected": [{"label": address_label, "street": street,
                       "city": city}],
        "case_sensitive": False,
        "trim_strings": True,
        "street_norm_keys": ["street"],
        "severity": "blocking",
        "label": (f"'{target_given} {target_family}' postal_addresses "
                  f"contains entry with label='{address_label}', "
                  f"street='{street}' (normalized), city='{city}'"),
    })
    # Pre-existing addresses preserved (set_contains catches deletion
    # or replacement of e.g. the baseline "school" address).
    for entry in baseline_pa:
        checks.append({
            "kind": "attribute_set_contains",
            "resource": "contacts.all",
            "selector": dict(selector),
            "attr": "postal_addresses",
            "expected": [{"label": entry.get("label", ""),
                          "city": entry.get("city", "")}],
            "case_sensitive": False,
            "trim_strings": True,
            "severity": "blocking",
            "label": (f"'{target_given} {target_family}' pre-existing "
                      f"postal_address (label='{entry.get('label')}', "
                      f"city='{entry.get('city')}') preserved"),
        })
    # All non-postal_addresses baseline fields byte-equal.
    baseline_minus_pa = {k: v for k, v in target_baseline.items()
                          if k != "postal_addresses"}
    checks.extend(_contact_unchanged_checks(
        given_name=target_given, family_name=target_family,
        baseline=baseline_minus_pa))
    # The seed uses legacy `phone` / `email` singulars; ensure the
    # actual `phones` / `emails` LIST counts match (catches the case
    # where agent ADDED a second phone via Messages auto-prefill on
    # top of the existing one — legacy `phone` (first) still equals
    # baseline, but `phones` list has 2 entries).
    expected_n_phones = 1 if target_baseline.get("phone") else 0
    checks.append({
        "kind": "attribute_list_length",
        "resource": "contacts.all",
        "selector": dict(selector),
        "attr": "phones",
        "op": "eq", "n": expected_n_phones,
        "severity": "blocking",
        "label": (f"'{target_given} {target_family}' phones list "
                  f"length = {expected_n_phones} (matches baseline)"),
    })
    expected_n_emails = 1 if target_baseline.get("email") else 0
    checks.append({
        "kind": "attribute_list_length",
        "resource": "contacts.all",
        "selector": dict(selector),
        "attr": "emails",
        "op": "eq", "n": expected_n_emails,
        "severity": "blocking",
        "label": (f"'{target_given} {target_family}' emails list "
                  f"length = {expected_n_emails} (matches baseline)"),
    })
    return checks


# X2 — gen_message_to_contact_to_maps (Messages → Contacts → Maps,
# pre-existing target)
def gen_message_to_contact_to_maps():
    """3-app cross-app task. Pre-runner sends an iMessage in the JA
    phantom thread; the simulator's no-account IDS loopback echoes it
    as an inbound gray bubble in KB. The agent:

      1. Opens Messages, finds the inbound bubble in KB
      2. Parses the named person + address from the message text
      3. Opens Contacts, locates that contact (already in the seed),
         adds the address under a randomized label (home/work/other)
      4. Opens Maps, navigates to the address, taps Directions, taps GO

    Verifier:
      - Target's postal_addresses contains an entry with the requested
        label and the expected city (street is fuzzy — Maps may
        format/correct it)
      - Target's other pre-existing fields are byte-equal to baseline
      - At least one z_ent=16 (HistoryDirectionsItem) row was created
        after baseline capture (proves directions were taken, not just
        searched)
      - Distractor contacts byte-equal to baseline; count unchanged

    See IOS_SIM_QUIRKS.md §11 for the Messages loopback contract;
    `sibb_state.py:MessagesHandler` for the seed-send implementation.
    """
    (contacts, _distractors, _target,
     target_given, target_family,
     street, city, state, postal, lat, lon,
     address_label) = _message_episode_setup(rng_target_in_seed=True)
    target = contacts[0]
    target_baseline = _baseline_from_contact_seed(target)

    payload = _pick_message_payload(
        _MESSAGE_TEMPLATES_ADDRESS_PLUS_DIRECTIONS_SINGLE,
        _MESSAGE_TEMPLATES_ADDRESS_PLUS_DIRECTIONS_MULTI,
        0.5,
        target_given, target_family, street, city, address_label)
    message_spec = _build_message_spec(payload)

    spec = _contact_spec(contacts) + _springboard_noise() + [message_spec]

    instruction = _INSTRUCTION_D

    verify_checks: List[Dict[str, Any]] = [
        # Target still exists
        {"kind": "exists",
         "resource": "contacts.all",
         "selector": {"given_name": target_given,
                       "family_name": target_family},
         "severity": "blocking",
         "label": f"'{target_given} {target_family}' still exists"},

        # Distractor identity
        _contact_distractor_identity_check(
            target_given_name=target_given,
            target_family_name=target_family),

        # Count unchanged (no spurious creations)
        {"kind": "count",
         "resource": "contacts.all",
         "selector": {},
         "op": "eq", "n": len(contacts),
         "severity": "blocking",
         "label": f"exactly {len(contacts)} contacts"},

        # Strict per-field post-state checks for the target: address
        # added under requested label/city; all baseline fields
        # byte-preserved (catches phone/email mutations + count drift).
        *_message_address_post_state_checks(
            target_given=target_given, target_family=target_family,
            target_baseline=target_baseline,
            address_label=address_label, street=street, city=city),

        # Maps: directions route was committed AND went to the
        # expected destination (50 m). The committed check fires
        # only if the agent tapped Go; the geo check then asserts
        # the active route's destination is within 50 m of the
        # MKLocalSearch-resolved expected coord (Phase B).
        *_maps_directions_committed_check(
            f"destination near {city}",
            expected_lat=lat, expected_lon=lon),
    ]

    return Task(
        task_id="", flow="cross_message_contacts_maps",
        apps=["Messages", "Contacts", "Maps"],
        instruction=instruction,
        verify=(f"contacts.all[name={target_given} {target_family}] "
                 f"address {address_label}/{city} + maps.active_route "
                 f"@ ({lat:.4f},{lon:.4f})"),
        verify_mode="db_query",
        initial_state=InitialState(
            present=[], absent=[], noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=18,
        complexity=complexity_score(18, noise_count=len(contacts) - 1),
        detail_level=0.0,
        params={"target_given_name": target_given,
                "target_family_name": target_family,
                "street": street, "city": city,
                "state": state, "postal_code": postal,
                "expected_lat": lat, "expected_lon": lon,
                "address_label": address_label,
                "n_contacts": len(contacts)},
        verify_checks=verify_checks,
    )


# X3 — gen_message_to_new_contact_to_maps (Messages → Contacts → Maps,
# agent CREATES the contact)
def gen_message_to_new_contact_to_maps():
    """Sibling of X2 with target NOT pre-seeded. The agent must:

      1. Read the inbound iMessage in KB
      2. Parse the named person + address from the text
      3. CREATE a new contact in Contacts with that name + the address
         under a randomized label (home/work/other)
      4. Open the address in Maps and start directions to it

    Verifier mirrors X2 but with `count == len(distractors) + 1` (one
    new contact) and an `exists` check on the new name.
    """
    (_contacts, distractors, _target,
     target_given, target_family,
     street, city, state, postal, lat, lon,
     address_label) = _message_episode_setup(rng_target_in_seed=False)

    payload = _pick_message_payload(
        _MESSAGE_TEMPLATES_ADDRESS_PLUS_DIRECTIONS_SINGLE,
        _MESSAGE_TEMPLATES_ADDRESS_PLUS_DIRECTIONS_MULTI,
        0.5,
        target_given, target_family, street, city, address_label)
    message_spec = _build_message_spec(payload)

    spec = _contact_spec(distractors) + _springboard_noise() + [message_spec]

    instruction = _INSTRUCTION_E

    verify_checks: List[Dict[str, Any]] = [
        # New target exists
        {"kind": "exists",
         "resource": "contacts.all",
         "selector": {"given_name": target_given,
                       "family_name": target_family},
         "severity": "blocking",
         "label": f"new contact '{target_given} {target_family}' "
                  f"exists"},

        # Exactly one new contact (no spurious creations)
        {"kind": "count",
         "resource": "contacts.all",
         "selector": {},
         "op": "eq", "n": len(distractors) + 1,
         "severity": "blocking",
         "label": f"exactly {len(distractors) + 1} contacts after "
                  f"create"},

        # Distractors byte-equal to baseline
        _contact_distractor_identity_check(
            target_given_name=target_given,
            target_family_name=target_family),

        # Strict per-field post-state checks: new contact has ONLY
        # the address from the message — no iOS sender-phone leak,
        # no spurious emails/orgs/etc.
        *_message_address_post_state_checks(
            target_given=target_given, target_family=target_family,
            target_baseline=None,
            address_label=address_label, street=street, city=city),

        # Maps: directions route was committed AND went to the
        # expected destination (50 m).
        *_maps_directions_committed_check(
            f"destination near {city}",
            expected_lat=lat, expected_lon=lon),
    ]

    return Task(
        task_id="", flow="cross_message_new_contact_maps",
        apps=["Messages", "Contacts", "Maps"],
        instruction=instruction,
        verify=(f"contacts.all created [name={target_given} "
                 f"{target_family}] with {address_label}/{city} + "
                 f"maps.active_route @ ({lat:.4f},{lon:.4f})"),
        verify_mode="db_query",
        initial_state=InitialState(
            present=[], absent=[], noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=20,
        complexity=complexity_score(20, noise_count=len(distractors)),
        detail_level=0.0,
        params={"target_given_name": target_given,
                "target_family_name": target_family,
                "street": street, "city": city,
                "state": state, "postal_code": postal,
                "expected_lat": lat, "expected_lon": lon,
                "address_label": address_label,
                "n_distractors": len(distractors)},
        verify_checks=verify_checks,
    )


# A — gen_message_save_sender: agent reads received message, saves the
# SENDER's phone (visible in thread/inbox header) + the NAME parsed
# from the body. Target NEVER pre-seeded (the "sender" is a stranger
# the user doesn't have saved yet).
def gen_message_save_sender():
    """Sender self-introduces with just a name; agent creates a contact
    using that name + the sender's phone number (the +1 (555) 564-8583
    visible in the AX as the thread header and inbox-cell prefix).

    Realistic vignette: someone you don't have saved texts you their
    name and asks to be added. Agent has to:
      1. Open Messages, find the latest message they received
      2. Parse the name from the message body
      3. Read the sender's phone number from the thread header
         (or the inbox cell's leading-number prefix)
      4. Create a new contact in Contacts with that name + phone

    Verifier (strict per user spec):
      - A new contact exists with the target name
      - That contact's phones contains a 'mobile' entry whose value
        digit-normalizes to the sender's number (5555648583 — leading
        1 normalized away by `_digits_only_norm`)
      - Distractor identity byte-equal to baseline
      - Count = len(distractors) + 1
    """
    (_contacts, distractors, _target,
     target_given, target_family,
     _street, _city, _state, _postal, _lat, _lon,
     _label) = _message_episode_setup(rng_target_in_seed=False)

    payload = _pick_message_payload(
        _MESSAGE_TEMPLATES_SELF_INTRO_SINGLE,
        _MESSAGE_TEMPLATES_SELF_INTRO_MULTI,
        0.5,
        target_given, target_family)
    message_spec = _build_message_spec(payload)

    spec = _contact_spec(distractors) + _springboard_noise() + [message_spec]

    verify_checks: List[Dict[str, Any]] = [
        {"kind": "exists",
         "resource": "contacts.all",
         "selector": {"given_name": target_given,
                       "family_name": target_family},
         "severity": "blocking",
         "label": f"new contact '{target_given} {target_family}' "
                  f"exists"},
        # Phone field matches the SENDER's number after US-digit
        # normalization. Label is intentionally NOT constrained — iOS's
        # "Create New Contact" flow from a Messages thread stores the
        # auto-prefilled number with an empty-string label (`""`), not
        # `mobile`. Verifier checks only the phone-number digits.
        {"kind": "attribute_set_contains",
         "resource": "contacts.all",
         "selector": {"given_name": target_given,
                       "family_name": target_family},
         "attr": "phones",
         "expected": [{"value": _SENDER_PHONE_DISPLAY}],
         "digits_only_keys": ["value"],
         "case_sensitive": False,
         "severity": "blocking",
         "label": f"'{target_given} {target_family}' has phone "
                  f"matching sender ({_SENDER_PHONE_DISPLAY})"},
        # Strict: target has EXACTLY one phone — the sender's. Catches
        # the cheat where the agent adds extra phones beyond what the
        # task asked for.
        {"kind": "attribute_list_length",
         "resource": "contacts.all",
         "selector": {"given_name": target_given,
                       "family_name": target_family},
         "attr": "phones",
         "op": "eq", "n": 1,
         "severity": "blocking",
         "label": f"'{target_given} {target_family}' has exactly one "
                  f"phone entry"},
        {"kind": "count",
         "resource": "contacts.all",
         "selector": {},
         "op": "eq", "n": len(distractors) + 1,
         "severity": "blocking",
         "label": f"exactly {len(distractors) + 1} contacts after "
                  f"create"},
        _contact_distractor_identity_check(
            target_given_name=target_given,
            target_family_name=target_family),
    ]

    return Task(
        task_id="", flow="cross_message_save_sender",
        apps=["Messages", "Contacts"],
        instruction=_INSTRUCTION_A,
        verify=(f"contacts.all created [name={target_given} "
                 f"{target_family}, phone=sender]"),
        verify_mode="db_query",
        initial_state=InitialState(
            present=[], absent=[], noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=12,
        complexity=complexity_score(12, noise_count=len(distractors)),
        detail_level=0.0,
        params={"target_given_name": target_given,
                "target_family_name": target_family,
                "sender_phone_display": _SENDER_PHONE_DISPLAY,
                "n_distractors": len(distractors)},
        verify_checks=verify_checks,
    )


# F — gen_message_save_sender_with_address: sender introduces themselves
# AND shares their OWN address. Agent creates a new contact carrying:
#   - name (from body)
#   - phone (from thread header — sender's KB number, like variant A)
#   - postal address (from body, label woven into wording)
# Merge of A's phone-from-header pattern + C's label/street/city pattern.
def gen_message_save_sender_with_address():
    """Body example (single-bubble):
        "hey it's Sarah Lin, my home is 22 Beach Rd Santa Monica"
    Body example (multi-bubble):
        ["hi", "this is Sarah Lin",
         "my home is 22 Beach Rd, Santa Monica"]

    Verifier:
      - New contact exists for target_given/target_family
      - phones has exactly one entry, value digit-normalizes to the
        sender's phone (5555648583)
      - postal_addresses has exactly one entry with the requested
        label, street (normalized), and city — caught by the strict
        address-post-state helper (with target_baseline=None so all
        other fields are required absent)
      - distractor identity preserved; count = distractors + 1
    """
    (_contacts, distractors, _target,
     target_given, target_family,
     street, city, _state, _postal, _lat, _lon,
     address_label) = _message_episode_setup(rng_target_in_seed=False)

    payload = _pick_message_payload(
        _MESSAGE_TEMPLATES_SELF_INTRO_WITH_ADDRESS_SINGLE,
        _MESSAGE_TEMPLATES_SELF_INTRO_WITH_ADDRESS_MULTI,
        0.5,
        target_given, target_family, street, city, address_label)
    message_spec = _build_message_spec(payload)

    spec = _contact_spec(distractors) + _springboard_noise() + [message_spec]

    selector = {"given_name": target_given, "family_name": target_family}
    verify_checks: List[Dict[str, Any]] = [
        {"kind": "exists",
         "resource": "contacts.all",
         "selector": dict(selector),
         "severity": "blocking",
         "label": f"new contact '{target_given} {target_family}' "
                  f"exists"},
        # Phone matches sender (like variant A).
        {"kind": "attribute_set_contains",
         "resource": "contacts.all",
         "selector": dict(selector),
         "attr": "phones",
         "expected": [{"value": _SENDER_PHONE_DISPLAY}],
         "digits_only_keys": ["value"],
         "case_sensitive": False,
         "severity": "blocking",
         "label": f"'{target_given} {target_family}' has phone "
                  f"matching sender ({_SENDER_PHONE_DISPLAY})"},
        {"kind": "attribute_list_length",
         "resource": "contacts.all",
         "selector": dict(selector),
         "attr": "phones",
         "op": "eq", "n": 1,
         "severity": "blocking",
         "label": f"'{target_given} {target_family}' has exactly one "
                  f"phone entry"},
        # Address must have label/street/city from the body — strict
        # like C/D/E's CREATE path. Exactly one postal_address; no
        # email/url/date leaks; sparse scalars absent.
        {"kind": "attribute_list_length",
         "resource": "contacts.all",
         "selector": dict(selector),
         "attr": "postal_addresses",
         "op": "eq", "n": 1,
         "severity": "blocking",
         "label": f"'{target_given} {target_family}' has exactly "
                  f"one postal_address"},
        {"kind": "attribute_set_contains",
         "resource": "contacts.all",
         "selector": dict(selector),
         "attr": "postal_addresses",
         "expected": [{"label": address_label, "street": street,
                        "city": city}],
         "case_sensitive": False,
         "trim_strings": True,
         "street_norm_keys": ["street"],
         "severity": "blocking",
         "label": f"'{target_given} {target_family}' postal_addresses "
                  f"contains entry with label='{address_label}', "
                  f"street='{street}' (normalized), city='{city}'"},
        # No leakage into other list fields (emails / urls / dates).
        *[{"kind": "attribute_list_length",
            "resource": "contacts.all",
            "selector": dict(selector),
            "attr": list_field,
            "op": "eq", "n": 0,
            "severity": "blocking",
            "label": f"'{target_given} {target_family}' has no "
                     f"{list_field}"}
          for list_field in ("emails", "urls", "dates")],
        # Sparse scalars absent (message has no email / org / nickname etc.).
        *[{"kind": "attribute_eq",
            "resource": "contacts.all",
            "selector": dict(selector),
            "attr": scalar, "value": None,
            "severity": "blocking",
            "label": f"'{target_given} {target_family}' {scalar} unset"}
          for scalar in ("email", "middle_name", "nickname",
                          "phonetic_given_name", "phonetic_family_name",
                          "phonetic_middle_name", "organization",
                          "job_title", "department", "birthday")],
        {"kind": "count",
         "resource": "contacts.all",
         "selector": {},
         "op": "eq", "n": len(distractors) + 1,
         "severity": "blocking",
         "label": f"exactly {len(distractors) + 1} contacts after "
                  f"create"},
        _contact_distractor_identity_check(
            target_given_name=target_given,
            target_family_name=target_family),
    ]

    return Task(
        task_id="", flow="cross_message_save_sender_with_address",
        apps=["Messages", "Contacts"],
        instruction=_INSTRUCTION_F,
        verify=(f"contacts.all created [name={target_given} "
                 f"{target_family}, phone=sender, "
                 f"{address_label}/{city}]"),
        verify_mode="db_query",
        initial_state=InitialState(
            present=[], absent=[], noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=15,
        complexity=complexity_score(15, noise_count=len(distractors)),
        detail_level=0.0,
        params={"target_given_name": target_given,
                "target_family_name": target_family,
                "sender_phone_display": _SENDER_PHONE_DISPLAY,
                "street": street, "city": city,
                "address_label": address_label,
                "n_distractors": len(distractors)},
        verify_checks=verify_checks,
    )


# B — gen_message_save_body: message body contains a name AND a phone
# number; agent extracts both from body and creates a contact. The
# sender's header phone is irrelevant.
def gen_message_save_body():
    """Body has explicit name + phone number; agent uses BOTH from body
    to create a new contact. Tests message-body parsing (not header
    extraction)."""
    (_contacts, distractors, _target,
     target_given, target_family,
     _street, _city, _state, _postal, _lat, _lon,
     _label) = _message_episode_setup(rng_target_in_seed=False)

    body_phone = _random_contact_phone()
    payload = _pick_message_payload(
        _MESSAGE_TEMPLATES_BODY_PHONE_SINGLE,
        _MESSAGE_TEMPLATES_BODY_PHONE_MULTI,
        0.5,
        target_given, target_family, body_phone)
    message_spec = _build_message_spec(payload)

    spec = _contact_spec(distractors) + _springboard_noise() + [message_spec]

    verify_checks: List[Dict[str, Any]] = [
        {"kind": "exists",
         "resource": "contacts.all",
         "selector": {"given_name": target_given,
                       "family_name": target_family},
         "severity": "blocking",
         "label": f"new contact '{target_given} {target_family}' "
                  f"exists"},
        # Label intentionally NOT constrained — iOS' new-contact flow
        # stores phones with whatever default label is active in the
        # sheet (often empty-string or `home`). Check only the number.
        {"kind": "attribute_set_contains",
         "resource": "contacts.all",
         "selector": {"given_name": target_given,
                       "family_name": target_family},
         "attr": "phones",
         "expected": [{"value": body_phone}],
         "digits_only_keys": ["value"],
         "case_sensitive": False,
         "severity": "blocking",
         "label": f"'{target_given} {target_family}' has phone "
                  f"matching body ({body_phone})"},
        # Strict: target has EXACTLY ONE phone — the body's. Catches
        # the cheat where the agent uses the Messages shortcut (which
        # auto-prefills the sender's phone) AND adds the body phone
        # without removing the sender phone first, leaving the contact
        # with two phones (one wrong, one right).
        {"kind": "attribute_list_length",
         "resource": "contacts.all",
         "selector": {"given_name": target_given,
                       "family_name": target_family},
         "attr": "phones",
         "op": "eq", "n": 1,
         "severity": "blocking",
         "label": f"'{target_given} {target_family}' has exactly one "
                  f"phone entry (no leftover sender phone)"},
        {"kind": "count",
         "resource": "contacts.all",
         "selector": {},
         "op": "eq", "n": len(distractors) + 1,
         "severity": "blocking",
         "label": f"exactly {len(distractors) + 1} contacts after "
                  f"create"},
        _contact_distractor_identity_check(
            target_given_name=target_given,
            target_family_name=target_family),
    ]

    return Task(
        task_id="", flow="cross_message_save_body",
        apps=["Messages", "Contacts"],
        instruction=_INSTRUCTION_B,
        verify=(f"contacts.all created [name={target_given} "
                 f"{target_family}, phone={body_phone}]"),
        verify_mode="db_query",
        initial_state=InitialState(
            present=[], absent=[], noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=12,
        complexity=complexity_score(12, noise_count=len(distractors)),
        detail_level=0.0,
        params={"target_given_name": target_given,
                "target_family_name": target_family,
                "body_phone": body_phone,
                "n_distractors": len(distractors)},
        verify_checks=verify_checks,
    )


# C — gen_message_save_address: message has name + address (no
# directions, no phone in body). Generator picks at random whether to
# pre-seed the target name in contacts; agent has to figure out from
# Contacts state whether to CREATE or UPDATE.
def gen_message_save_address():
    """Message body: name + address + label. Target may or may not be
    pre-seeded — the agent decides create-vs-update by looking at the
    contact list. Verifier checks the END STATE, which is the same
    either way: a contact with that name + the address under the
    requested label.

    No Maps directions — that's the D/E generators.
    """
    target_in_seed = random.random() < 0.5
    (contacts, distractors, target,
     target_given, target_family,
     street, city, _state, _postal, _lat, _lon,
     address_label) = _message_episode_setup(
        rng_target_in_seed=target_in_seed)

    payload = _pick_message_payload(
        _MESSAGE_TEMPLATES_ADDRESS_SINGLE,
        _MESSAGE_TEMPLATES_ADDRESS_MULTI,
        0.5,
        target_given, target_family, street, city, address_label)
    message_spec = _build_message_spec(payload)

    if target_in_seed:
        spec_contacts = contacts
        expected_count = len(contacts)
        target_baseline = _baseline_from_contact_seed(target)
    else:
        spec_contacts = distractors
        expected_count = len(distractors) + 1
        target_baseline = None

    spec = _contact_spec(spec_contacts) + _springboard_noise() + [message_spec]

    verify_checks: List[Dict[str, Any]] = [
        {"kind": "exists",
         "resource": "contacts.all",
         "selector": {"given_name": target_given,
                       "family_name": target_family},
         "severity": "blocking",
         "label": f"'{target_given} {target_family}' exists "
                  f"after task"},
        {"kind": "count",
         "resource": "contacts.all",
         "selector": {},
         "op": "eq", "n": expected_count,
         "severity": "blocking",
         "label": f"exactly {expected_count} contacts "
                  f"({'updated existing' if target_in_seed else 'created new'})"},
        _contact_distractor_identity_check(
            target_given_name=target_given,
            target_family_name=target_family),
    ]
    # Strict per-field post-state checks (covers both paths). Locks
    # the create path to "name + address only" — no iOS prefill of
    # sender phone — and the update path to "baseline + new address".
    verify_checks.extend(_message_address_post_state_checks(
        target_given=target_given, target_family=target_family,
        target_baseline=target_baseline,
        address_label=address_label, street=street, city=city))

    return Task(
        task_id="", flow="cross_message_save_address",
        apps=["Messages", "Contacts"],
        instruction=_INSTRUCTION_C,
        verify=(f"contacts.all[name={target_given} {target_family}] "
                 f"has postal_address with label='{address_label}', "
                 f"city='{city}' "
                 f"({'update' if target_in_seed else 'create'})"),
        verify_mode="db_query",
        initial_state=InitialState(
            present=[], absent=[], noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=14,
        complexity=complexity_score(14, noise_count=len(spec_contacts) - (0 if target_in_seed else 0)),
        detail_level=0.0,
        params={"target_given_name": target_given,
                "target_family_name": target_family,
                "street": street, "city": city,
                "address_label": address_label,
                "target_pre_existing": target_in_seed,
                "n_contacts_pre_task": len(spec_contacts)},
        verify_checks=verify_checks,
    )


# ─────────────────────────────────────────────────────────────────────
# Safari single-app generators (Phase 4 / Tier 1, 2026-06-05)
# ─────────────────────────────────────────────────────────────────────
#
# Tier-0 infra all in place (commits 793eaa9..fad4d82):
#   * `safari.bookmarks` resource fetcher walks the BookmarksBar
#     tree (folder-aware) and returns rows with id/title/url/
#     parent_id/parent_title/folder_path/kind. Supports
#     `folder=`, `include_subfolders`, `include_reading_list`,
#     `url_canonicalize=True` selectors.
#   * `Bookmark` spec dataclass has an optional `folder` field.
#   * `SafariHandler.apply("bookmark", folder=…)` creates the
#     folder on demand and inserts the leaf.
#   * `_canonicalize_url` (sibb_verify.py) normalizes scheme/host/
#     port/trailing-slash for verifier compare — used when the
#     selector passes `url_canonicalize=True`.
#
# First generator: `gen_safari_bookmark_specific_url` — agent
# navigates to a target URL in Safari and bookmarks it. Pre-seeded
# distractor bookmarks live under BookmarksBar with a mix of root +
# subfolder placement; verifier asserts the target URL appears
# anywhere in the BookmarksBar tree (so the generator is robust to
# whichever folder iOS' Add-Bookmark UI defaults to).


# Pool of URLs whose pages are stable enough across iOS bumps to use
# as bookmark targets. Each row is (url, page_title_substring). The
# title substring is only used for the optional title-contains check
# — the load-bearing verifier predicate is the URL match.
_SAFARI_BOOKMARK_TARGET_URLS: List[Tuple[str, str]] = [
    ("https://example.com",                      "Example"),
    ("https://en.wikipedia.org/wiki/Pi",         "Pi"),
    ("https://en.wikipedia.org/wiki/Speed_of_light", "Speed of light"),
    ("https://en.wikipedia.org/wiki/Marie_Curie", "Marie Curie"),
    ("https://en.wikipedia.org/wiki/IOS",        "iOS"),
    ("https://en.wikipedia.org/wiki/Pluto",      "Pluto"),
    ("https://news.ycombinator.com/item?id=1",   "Hacker News"),
    ("https://www.rfc-editor.org/rfc/rfc2606",   "RFC 2606"),
    ("https://www.iana.org/help/example-domains", "IANA"),
    ("https://developer.mozilla.org/en-US/docs/Web/HTML/Element/form",
     "form"),
]


# Distractor URLs — distinct hosts from the target pool so a
# substring match on the host can't collide. Titles here are
# illustrative; the runtime title is whatever Safari fetched at
# bookmark-save time, but for SEED bookmarks we control both.
_SAFARI_BOOKMARK_DISTRACTOR_POOL: List[Tuple[str, str]] = [
    ("https://www.apple.com",              "Apple"),
    ("https://www.bing.com",               "Bing"),
    ("https://duckduckgo.com",             "DuckDuckGo"),
    ("https://www.yahoo.com",              "Yahoo"),
    ("https://www.google.com",             "Google"),
    ("https://www.github.com",             "GitHub"),
    ("https://www.stackoverflow.com",      "Stack Overflow"),
    ("https://www.reddit.com",             "Reddit"),
    ("https://www.amazon.com",             "Amazon"),
    ("https://www.cnn.com",                "CNN"),
    ("https://www.bbc.com",                "BBC"),
    ("https://www.nytimes.com",            "NY Times"),
]


# Subfolders the distractor pattern may sprinkle bookmarks into so
# the agent's saved bookmark is NOT trivially the only bookmark under
# the BookmarksBar root. Mirrors how a real user's Favorites looks
# (mix of root-level and a couple of organizational subfolders).
_SAFARI_BOOKMARK_FOLDERS: List[Optional[str]] = [
    None, None, None,           # ~50% at root (most-likely user pattern)
    "News", "Tech", "Reference",
]


def _build_diverse_bookmark_seed(n: int) -> List[Dict[str, Any]]:
    """N distractor bookmarks with diverse titles, URLs, and folder
    placement. Returns dicts shaped for `Bookmark.from_dict`.

    Mirrors `_build_diverse_contact_seed` in spirit: ~50% at root,
    rest sprinkled across 1-3 named subfolders so the bookmark tree
    isn't a flat list. This is what makes the folder-aware fetcher
    matter — a verifier that only walked BookmarksBar root would miss
    bookmarks the agent put into "News" or "Tech".
    """
    picks = random.sample(_SAFARI_BOOKMARK_DISTRACTOR_POOL,
                           min(n, len(_SAFARI_BOOKMARK_DISTRACTOR_POOL)))
    seed: List[Dict[str, Any]] = []
    for url, title in picks:
        entry: Dict[str, Any] = {
            "app": "Safari", "type": "bookmark",
            "title": title, "url": url,
        }
        folder = random.choice(_SAFARI_BOOKMARK_FOLDERS)
        if folder:
            entry["folder"] = folder
        seed.append(entry)
    return seed


def _safari_bookmark_spec(
        bookmarks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build a Safari InitialState.spec list — one entry per
    bookmark. Mirrors `_contact_spec`."""
    spec: List[Dict[str, Any]] = []
    for b in bookmarks:
        entry: Dict[str, Any] = {
            "app": "Safari", "type": "bookmark",
            "title": b.get("title", ""),
            "url":   b.get("url", ""),
        }
        if b.get("folder"):
            entry["folder"] = b["folder"]
        spec.append(entry)
    return spec


def _baseline_from_bookmark_seed(b: Dict[str, Any]) -> Dict[str, Any]:
    """Pre-task fields that should be byte-preserved on a distractor
    bookmark. Same shape as `_baseline_from_contact_seed`."""
    return {k: v for k, v in b.items()
              if k not in ("app", "type", "identifier")}


def _safari_bookmark_distractor_identity_check(
        *, target_url: str,
        label: str = ("no irrelevant edits on distractor bookmarks")
        ) -> Dict[str, Any]:
    """Identity check: every bookmark EXCEPT the agent's target
    (excluded by canonical URL match) preserves its pre-task
    title / url / folder_path. Catches cheats where the agent
    relabels or moves a distractor instead of creating the target.

    Uses `url_canonicalize=True` so the exclusion compares canonical
    forms — Safari may have rewritten the agent's target URL on
    save, and we can't false-fail when the agent did the right
    thing in a slightly different form.
    """
    return {
        "kind": "identity",
        "resource": "safari.bookmarks",
        "selector": {"url_canonicalize": True},
        "compare_fields": ["title", "url", "folder_path"],
        "exclude_match": {"url": target_url},
        "severity": "blocking",
        "label": label,
    }


# ── S1 — bookmark_specific_url ───────────────────────────────────────


def gen_safari_bookmark_specific_url():
    """T1 / Safari: agent navigates to a target URL in Safari and
    bookmarks the page.

    Distractor pattern (mirrors Contacts T1):
      * 5-7 pre-existing bookmarks with diverse host names
      * ~50% live directly under BookmarksBar (Favorites in UI),
        the rest sprinkled across "News", "Tech", "Reference"
        subfolders
      * Verifier walks the WHOLE BookmarksBar tree (folder-aware
        fetcher) so the agent's bookmark surfaces no matter which
        folder iOS' Add-Bookmark UI defaults to.

    Verifier:
      * exists  : target URL is bookmarked somewhere under
                  BookmarksBar (canonicalized URL compare)
      * count   : n+1 total bookmarks (no duplicate creates)
      * identity: every distractor still has its baseline
                  title / url / folder placement
    """
    n_distractors = random.choice([5, 6, 7])
    distractors = _build_diverse_bookmark_seed(n_distractors)

    # Pick a target URL whose host is NOT in the distractor pool.
    distractor_hosts = {
        b["url"].split("/")[2].lower() for b in distractors
        if b.get("url", "").startswith("http")
    }
    candidates = [
        (u, t) for (u, t) in _SAFARI_BOOKMARK_TARGET_URLS
        if u.split("/")[2].lower() not in distractor_hosts
    ]
    target_url, target_title_hint = random.choice(candidates)

    spec = _safari_bookmark_spec(distractors) + _springboard_noise()

    instruction = random.choice([
        f"Open Safari, navigate to {target_url}, and bookmark the "
        f"page.",
        f"In Safari, go to {target_url} and add it to your "
        f"bookmarks.",
        f"Bookmark the page at {target_url} in Safari.",
    ])

    verify_checks: List[Dict[str, Any]] = [
        # The bookmark surfaces somewhere in the BookmarksBar tree
        # (Favorites root OR any subfolder). url_canonicalize=True
        # absorbs scheme / trailing-slash drift Safari may apply.
        {"kind": "exists",
         "resource": "safari.bookmarks",
         "selector": {"url_canonicalize": True,
                       "url": target_url},
         "severity": "blocking",
         "label": f"bookmark for {target_url!r} exists"},
        # Total bookmark count = distractors + 1. Guards against the
        # agent re-bookmarking the same page (or accidentally adding
        # the distractor URLs).
        {"kind": "count",
         "resource": "safari.bookmarks",
         "selector": {},
         "op": "eq", "n": n_distractors + 1,
         "severity": "blocking",
         "label": f"exactly {n_distractors + 1} bookmarks "
                  f"(distractors + 1 new)"},
        # Distractors preserved byte-for-byte (title + url +
        # folder_path).
        _safari_bookmark_distractor_identity_check(
            target_url=target_url),
    ]

    return Task(
        task_id="", flow="single_safari_create",
        apps=["Safari"],
        instruction=instruction,
        verify=f"safari.bookmarks[url={target_url}] exists",
        verify_mode="db_query",
        initial_state=InitialState(
            present=[], absent=[], noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=8,
        complexity=complexity_score(8, noise_count=n_distractors),
        detail_level=0.0,
        params={"target_url": target_url,
                 "target_title_hint": target_title_hint,
                 "n_distractors": n_distractors,
                 "distractor_folders": sorted(
                     {b.get("folder") for b in distractors
                       if b.get("folder")}),
                 },
        verify_checks=verify_checks,
    )


# ─────────────────────────────────────────────────────────────────────
# Safari Phase 4 / Tier 1 — first harness-based generator (2026-06-05)
# ─────────────────────────────────────────────────────────────────────
#
# `gen_safari_rsvp_form` is the smallest task that exercises the full
# Phase 4 harness stack end-to-end:
#
#   * MockSite with `static_pages={"/event": "rsvp_event"}` + `page_seed`
#     + `start_path="/event"` so Safari opens directly on the form.
#   * `harness_pages.rsvp_event` template (registered via
#     `@register_page`) composes filler paragraphs, a collapsed venue-
#     notes section, the RSVP form with `shuffled_fields(name, email,
#     attending)`, 2-3 distractor buttons, more filler. Layout is
#     deterministic per (page_seed, path) for replayability.
#   * Verifier checks: (1) count of non-decoy submissions == 1 (catches
#     decoy-only clicks), (2) exists path=/rsvp, (3) attribute_eq on
#     each of fields.{name,email,attending}.
#
# The page CONTENT (event name, date, venue) is decoration; the
# verifier doesn't read it. What's checked is that the agent reads
# the *instruction* values, fills the right fields, and taps the
# right submit.

_RSVP_PERSONAS = [
    # (first, last, email, phone) — sample personas. The generator
    # picks ONE of email/phone per episode based on contact_type
    # (which is derived from the SAME RNG state the page template
    # consumes, so they always agree).
    ("Alice",   "Chen",     "alice.chen@example.com",   "415-555-0142"),
    ("Riley",   "Brooks",   "riley.b@example.org",       "510-555-0277"),
    ("Jordan",  "Park",     "jpark@example.net",         "650-555-0319"),
    ("Sam",     "Rivera",   "sam.rivera@example.com",    "408-555-0488"),
    ("Morgan",  "Kim",      "morgan.kim@example.org",    "212-555-0531"),
    ("Casey",   "Nguyen",   "casey.n@example.com",       "718-555-0664"),
    ("Avery",   "Patel",    "avery.patel@example.net",   "323-555-0717"),
    ("Quinn",   "Singh",    "quinn.s@example.com",       "503-555-0825"),
]

# Prompt-side label pools. These are DIFFERENT from the form-side
# pools in `harness_pages.rsvp_event`. The agent gets one label in
# the instruction ("Full Name: Alice Chen") and must map it to the
# semantically-equivalent but textually-different field label on
# the page ("Attendee name") to fill the form correctly.
_PROMPT_NAME_LABELS = ["Name", "Full Name", "Your name"]
_PROMPT_EMAIL_LABELS = ["Email", "Email address", "Contact email"]
_PROMPT_PHONE_LABELS = ["Phone", "Phone number", "Contact number"]
_PROMPT_ATTENDING_LABELS = ["Attending", "RSVP", "Going"]

# Friendly hostnames for the harness URL the agent sees. All sit
# under the reserved `.test` TLD (RFC 6761) so:
#   (a) they can't collide with real DNS, and
#   (b) `/etc/resolver/test` routes ALL of them to our DNS server
#       (a single per-suffix resolver entry covers every `*.test`
#       name).
# `.example` was tempting but is NOT routed by the resolver, which
# would send Safari to a Google search instead of the MockSite.
_RSVP_HOSTNAMES = [
    "aurora-conference.test",
    "helix-symposium.test",
    "lumen-festival.test",
    "tessera-summit.test",
    "events.test",
    "rsvp.test",
]


def gen_safari_rsvp_form():
    """T1 / Safari (harness-based): agent fills + submits an RSVP form
    on a harness-served event page.

    iOS Safari opens directly on `/event` (the harness page). The page
    has filler paragraphs above/below the form, a collapsed venue-
    notes section, a 3-field form (`name`, `email`, `attending`) in
    randomized order, and 2-3 distractor buttons ("Save Draft" /
    "Cancel" / etc.) the agent must NOT tap.

    Verifier:
      * `count(mock_site.submissions) == 1` — exactly one non-decoy
        submission. Decoys are filtered out by default, so a clicked
        "Save Draft" doesn't satisfy this on its own.
      * `exists path=/rsvp` — the submission hit the correct endpoint.
      * `attribute_eq fields.name/email/attending` — the values
        match what the instruction asked for.

    Cheat-resistance:
      * Form field order randomized per seed (agent matches by label).
      * 2-3 distractor buttons with realistic labels (agent picks by
        text, not position).
      * `random_pad` + filler paragraphs push the submit below the
        fold so the agent must SCROLL to find it.
      * `attending` value randomized so the agent can't always type
        "yes".
    """
    # Pick the persona first; contact_type comes from the page-side
    # RNG to keep generator and template aligned.
    first, last, email, phone = random.choice(_RSVP_PERSONAS)
    name = f"{first} {last}"
    attending = random.choice(["yes", "no"])

    site_id = f"rsvp-{uuid.uuid4().hex[:8]}"
    hostname = random.choice(_RSVP_HOSTNAMES)
    page_seed = random.randint(1, 0xFFFFFFFE)

    # Re-derive the SAME RNG sequence the `rsvp_event` template will
    # consume. The first choices in `rsvp_event_choices` consume the
    # page-side randomization; we read just the values we need
    # (`contact_type`) here. As long as we use the same `Random`
    # state, the template's downstream choices stay aligned.
    from harness_layout import compute_path_seed
    from harness_pages import rsvp_event_choices
    page_rng = random.Random(compute_path_seed(page_seed, "/event"))
    page_cfg = rsvp_event_choices(page_rng)
    contact_type = page_cfg["contact_type"]  # "email" or "phone"
    contact_value = email if contact_type == "email" else phone

    # Randomize the prompt-side labels (independent of page labels).
    prompt_name_label = random.choice(_PROMPT_NAME_LABELS)
    prompt_attending_label = random.choice(_PROMPT_ATTENDING_LABELS)
    if contact_type == "email":
        prompt_contact_label = random.choice(_PROMPT_EMAIL_LABELS)
    else:
        prompt_contact_label = random.choice(_PROMPT_PHONE_LABELS)

    spec = [
        {
            "app": "Safari",
            "type": "mock_site",
            "site_id": site_id,
            "static_pages": {"/event": "rsvp_event"},
            "page_seed": page_seed,
            "start_path": "/event",
            "open_at_start": False,
            "hostname": hostname,
            "credentials": {},
        },
    ] + _springboard_noise()

    # Step 5i (2026-06-07) — `{port:<site_id>}` placeholder gets
    # resolved to the live MockSite port by `apply_initial_state` after
    # the handler spawns the HTTP server (port is OS-assigned, so the
    # generator cannot know it upfront). With `open_at_start=False`
    # the agent has to TYPE the URL, so the port must be present.
    # See sibb_state.py `_resolve_port_placeholders` for the resolver.
    #
    # Step 5j (2026-06-07) — agent-shown URL uses 127.0.0.1 (numeric),
    # NOT the friendly `.test` hostname. The iOS sim's Safari URL-bar
    # typing does NOT honor /etc/resolver/test (only `simctl openurl`
    # does — empirically verified). Typed `.test` URLs fall through to
    # Google search. Numeric IPs avoid the DNS lookup entirely. The
    # `hostname` field stays in spec for logging/diagnostic continuity
    # but is decorative from the agent's perspective. See
    # IOS_SIM_QUIRKS §22.
    event_url = f"http://127.0.0.1:{{port:{site_id}}}/event"
    instruction = (
        f"Go to Safari and open the event page: {event_url}. "
        f"RSVP with the following info:\n"
        f"  - {prompt_name_label}: {name}\n"
        f"  - {prompt_contact_label}: {contact_value}\n"
        f"  - {prompt_attending_label}: {attending}\n"
        f"Make sure to submit. The page may have multiple "
        f"buttons — find the one that actually submits the RSVP."
    )

    verify_checks: List[Dict[str, Any]] = [
        # Exactly one non-decoy submission. Distractor clicks land in
        # `_submissions` as `is_decoy=True` and the default fetcher
        # filters them, so this catches "agent clicked Cancel" or
        # "agent never submitted at all".
        {"kind": "count",
         "resource": "mock_site.submissions",
         "selector": {"site_id": site_id},
         "op": "eq", "n": 1,
         "severity": "blocking",
         "label": "exactly one non-decoy form submission"},
        # The submission was to /rsvp (not a typo or wrong-form).
        {"kind": "exists",
         "resource": "mock_site.submissions",
         "selector": {"site_id": site_id, "path": "/rsvp"},
         "severity": "blocking",
         "label": "submission landed at /rsvp"},
        # Each field carries the exact value the instruction asked
        # for. `_walk_attr` handles the `fields.X` dot notation.
        # NOTE: the form's contact field is named `contact` regardless
        # of whether the type is email or phone, so the verifier
        # selector is stable.
        {"kind": "attribute_eq",
         "resource": "mock_site.submissions",
         "selector": {"site_id": site_id, "path": "/rsvp"},
         "attr": "fields.name", "value": name,
         "severity": "blocking",
         "label": f"name field == {name!r}"},
        {"kind": "attribute_eq",
         "resource": "mock_site.submissions",
         "selector": {"site_id": site_id, "path": "/rsvp"},
         "attr": "fields.contact", "value": contact_value,
         "severity": "blocking",
         "label": f"contact field == {contact_value!r}"},
        {"kind": "attribute_eq",
         "resource": "mock_site.submissions",
         "selector": {"site_id": site_id, "path": "/rsvp"},
         "attr": "fields.attending", "value": attending,
         "severity": "blocking",
         "label": f"attending field == {attending!r}"},
    ]

    return Task(
        task_id="", flow="single_safari_harness",
        apps=["Safari"],
        instruction=instruction,
        verify=(
            f"mock_site.submissions[site_id={site_id},path=/rsvp] "
            f"name={name!r}, contact={contact_value!r}, "
            f"attending={attending!r}"),
        verify_mode="db_query",
        initial_state=InitialState(
            present=[], absent=[], noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=10,
        complexity=complexity_score(10, noise_count=0),
        detail_level=0.0,
        params={"site_id": site_id,
                 "hostname": hostname,
                 "event_url": event_url,
                 "target_name": name,
                 "target_contact": contact_value,
                 "target_contact_type": contact_type,
                 "target_attending": attending,
                 "page_seed": page_seed,
                 "prompt_name_label": prompt_name_label,
                 "prompt_contact_label": prompt_contact_label,
                 "prompt_attending_label": prompt_attending_label,
                 # Mirror the page-side labels too so episode logs
                 # capture the exact label the agent saw.
                 "form_name_label": page_cfg["name_label"],
                 "form_contact_label": page_cfg["contact_label"],
                 "form_attending_label": page_cfg["attending_label"],
                 "form_submit_label": page_cfg["submit_label"],
                 # Step 5b (2026-06-07) — input font-size randomized
                 # per seed across iOS Safari's auto-zoom threshold
                 # (16 px). Logged so result aggregation can split
                 # pass-rate by zoom vs no-zoom condition.
                 "form_font_size_px": page_cfg["font_size_px"],
                 "form_triggers_auto_zoom":
                     page_cfg["font_size_px"] < 16},
        verify_checks=verify_checks,
    )


def gen_safari_rsvp_form_clipped():
    """Adversarial variant of `gen_safari_rsvp_form` — uses the
    `rsvp_event_clipped` template that positions the submit button
    past the bottom-right of the viewport. The agent must SCROLL
    down AND right (or use SWIPE) to bring the button into view
    before tapping it. Tests the agent's recovery when an interactive
    element isn't initially visible.

    Verifier and instruction format mirror `gen_safari_rsvp_form`
    so the same task spec works — only the page rendering differs.
    """
    first, last, email, phone = random.choice(_RSVP_PERSONAS)
    name = f"{first} {last}"
    attending = random.choice(["yes", "no"])

    site_id = f"rsvp-clipped-{uuid.uuid4().hex[:8]}"
    hostname = random.choice(_RSVP_HOSTNAMES)
    page_seed = random.randint(1, 0xFFFFFFFE)
    from harness_layout import compute_path_seed
    from harness_pages import rsvp_event_choices
    page_rng = random.Random(compute_path_seed(page_seed, "/event"))
    page_cfg = rsvp_event_choices(page_rng)
    contact_type = page_cfg["contact_type"]
    contact_value = email if contact_type == "email" else phone

    prompt_name_label = random.choice(_PROMPT_NAME_LABELS)
    prompt_attending_label = random.choice(_PROMPT_ATTENDING_LABELS)
    if contact_type == "email":
        prompt_contact_label = random.choice(_PROMPT_EMAIL_LABELS)
    else:
        prompt_contact_label = random.choice(_PROMPT_PHONE_LABELS)

    spec = [
        {
            "app": "Safari",
            "type": "mock_site",
            "site_id": site_id,
            "static_pages": {"/event": "rsvp_event_clipped"},
            "page_seed": page_seed,
            "start_path": "/event",
            "open_at_start": False,
            "hostname": hostname,
            "credentials": {},
        },
    ] + _springboard_noise()

    # Step 5i (2026-06-07) — see gen_safari_rsvp_form for the
    # `{port:<site_id>}` placeholder contract.
    # Step 5j — see gen_safari_rsvp_form: sim Safari URL-bar typing
    # cannot resolve .test hostnames, so use numeric IP.
    event_url = f"http://127.0.0.1:{{port:{site_id}}}/event"
    instruction = (
        f"Go to Safari and open the event page: {event_url}. "
        f"RSVP with the following info:\n"
        f"  - {prompt_name_label}: {name}\n"
        f"  - {prompt_contact_label}: {contact_value}\n"
        f"  - {prompt_attending_label}: {attending}\n"
        f"Make sure to submit. The submit button may not be "
        f"initially visible — scroll/swipe within the page to "
        f"bring it into view before tapping it."
    )

    verify_checks: List[Dict[str, Any]] = [
        {"kind": "count", "resource": "mock_site.submissions",
         "selector": {"site_id": site_id},
         "op": "eq", "n": 1, "severity": "blocking",
         "label": "exactly one non-decoy form submission"},
        {"kind": "exists", "resource": "mock_site.submissions",
         "selector": {"site_id": site_id, "path": "/rsvp"},
         "severity": "blocking",
         "label": "submission landed at /rsvp"},
        {"kind": "attribute_eq",
         "resource": "mock_site.submissions",
         "selector": {"site_id": site_id, "path": "/rsvp"},
         "attr": "fields.name", "value": name, "severity": "blocking",
         "label": f"name field == {name!r}"},
        {"kind": "attribute_eq",
         "resource": "mock_site.submissions",
         "selector": {"site_id": site_id, "path": "/rsvp"},
         "attr": "fields.contact", "value": contact_value,
         "severity": "blocking",
         "label": f"contact field == {contact_value!r}"},
        {"kind": "attribute_eq",
         "resource": "mock_site.submissions",
         "selector": {"site_id": site_id, "path": "/rsvp"},
         "attr": "fields.attending", "value": attending,
         "severity": "blocking",
         "label": f"attending field == {attending!r}"},
    ]

    return Task(
        task_id="", flow="single_safari_harness_clipped",
        apps=["Safari"],
        instruction=instruction,
        verify=(
            f"mock_site.submissions[site_id={site_id},path=/rsvp] "
            f"name={name!r}, contact={contact_value!r}, "
            f"attending={attending!r}"),
        verify_mode="db_query",
        initial_state=InitialState(
            present=[], absent=[], noise_records=[],
            setup_commands=[], spec=spec,
            expected_behavior="complete"),
        steps=15,
        complexity=complexity_score(15, noise_count=0),
        detail_level=0.0,
        params={"site_id": site_id, "hostname": hostname,
                 "event_url": event_url, "target_name": name,
                 "target_contact": contact_value,
                 "target_contact_type": contact_type,
                 "target_attending": attending,
                 "page_seed": page_seed,
                 "variant": "clipped",
                 "prompt_name_label": prompt_name_label,
                 "prompt_contact_label": prompt_contact_label,
                 "prompt_attending_label": prompt_attending_label,
                 "form_submit_label": page_cfg["submit_label"],
                 # Step 5b (2026-06-07) — see gen_safari_rsvp_form.
                 "form_font_size_px": page_cfg["font_size_px"],
                 "form_triggers_auto_zoom":
                     page_cfg["font_size_px"] < 16},
        verify_checks=verify_checks,
    )


# ── Safari shop V0 (Step 5M, 2026-06-08) ──────────────────────────────────
#
# `gen_safari_shop_pick_by_attrs` — agent searches for products in a
# mock shop, picks the one matching the instruction's attributes
# (category × brand × max-price), buys it via the checkout form.
#
# Catalog: WebMall (research-permissive; see sibb/benchmark/data/NOTICE).
# Page templates: shop_search_results / shop_pdp / shop_checkout in
# `harness_pages.py`. Generator + template agree on the same winner +
# distractors via the shared `shop_pick_by_attrs_choices(rng)` helper
# called over identical page-seed-derived rngs.
#
# V0 scope (Y/N):
#   * Search → pick → checkout flow      Y
#   * Persona shipping fields            Y (name + street + city + state + zip)
#   * Variant pickers (size/color)       N — WebMall has none; V0+ synthesizes
#   * Cart page                          N — PDP "Buy Now" goes direct to checkout
#   * Address from Contact (V1)          N — flat persona inline in instruction
#   * Card from Contact notes / Files (V2/V3) N — payment is inline form fields
#   * In-site `/account/cards` (V4)      N
#   * Dark patterns                      N — covered by gen_safari_shop_avoid_dark_pattern

def gen_safari_shop_pick_by_attrs():
    """Safari shop task — agent lands on a minimal storefront, types a
    search query into the search bar, picks the unique SKU matching
    the instruction's constraints from the BM25 search results, and
    completes checkout.

    Step 5N (2026-06-08) — V0.5 rewrite:
      * Drops the sponsored-decoy badge (was unprecedented in the
        literature per `sibb_runs/shopping_landing_and_search.md`).
      * Uses a minimal WebShop-style `/` landing page with a real
        search bar (not direct URL navigation to `/search`).
      * Search is BM25-backed against the catalog corpus
        (`title + brand + description`); top 8 results render as
        product cards. The agent's query DECIDES which 8.
      * Picks a winner that's UNIQUELY identified by (brand,
        category, max_price) in the catalog — closes the
        multi-match cheat earlier reviewers flagged.
      * Randomizes instruction archetype 50/50:
          Q1 (attribute-prose): agent must invent the query from
              brand + category + price cap.
          Q2 (named-product): instruction names a short search hint
              the agent types verbatim.
      * Q3 (URL-anchored) and Q4 (filter+sort cascade) deferred —
        see `TODO_DEFERRED.md` §H7-shop and the dedicated
        `gen_safari_shop_filter_and_sort` generator.
    """
    site_id = f"shop-{uuid.uuid4().hex[:8]}"
    hostname = random.choice(_RSVP_HOSTNAMES)
    page_seed = random.randint(1, 0xFFFFFFFE)

    # Re-derive the SAME rng the templates will use so generator +
    # templates agree on the same winner + archetype + persona.
    # `/` (landing) is the entry point so we seed on that path.
    from harness_layout import compute_path_seed
    from harness_pages import shop_pick_by_attrs_choices
    landing_rng = random.Random(compute_path_seed(page_seed, "/"))
    cfg = shop_pick_by_attrs_choices(landing_rng, page_seed=page_seed)
    winner = cfg["winner"]
    persona = cfg["persona"]  # (first, last, street, city, state, zip)
    persona_name = f"{persona[0]} {persona[1]}"

    spec = [
        {
            "app": "Safari",
            "type": "mock_site",
            "site_id": site_id,
            "static_pages": {
                "/":              "shop_landing",
                "/search":        "shop_search_results",
                "/product/":      "shop_pdp",
                "/checkout":      "shop_checkout",
                "/account/cards": "shop_account_cards",
            },
            "page_seed": page_seed,
            "start_path": "/",
            "open_at_start": False,
            "hostname": hostname,
            "credentials": {},
        },
    ] + _springboard_noise()

    # Numeric IP per 5j — sim Safari URL-bar can't resolve `.test`.
    landing_url = f"http://127.0.0.1:{{port:{site_id}}}/"

    # Build the instruction. Two archetypes — see helper docstring.
    if cfg["archetype"] == "Q1":
        # Attribute-prose: agent infers query from brand + category.
        task_lines = (
            f"You're shopping for a {winner.brand} product in the "
            f"{cfg['category']} section. Buy the CHEAPEST one priced "
            f"under ${cfg['max_price_cents'] / 100:.2f}.\n"
            f"\n"
            f"Use the search bar on the landing page to find "
            f"candidates — pick a search term that will surface "
            f"the right category of product. Compare the prices on "
            f"the results page before picking.\n"
        )
    else:  # Q2
        task_lines = (
            f"Search the shop for products matching this query: "
            f'"{cfg["search_hint"]}". Buy the CHEAPEST one priced '
            f"under ${cfg['max_price_cents'] / 100:.2f}.\n"
            f"\n"
            f"Use the search bar on the landing page. Type the "
            f"query above into it, then compare the prices on the "
            f"results before picking.\n"
        )
    # V4 (Step 5O, 2026-06-09): when use_saved_cards, instruct the
    # agent to fetch payment details from /account/cards first and
    # use the PERSONAL card on checkout. Renders 3 extra payment
    # fields under "Shipping & payment" on the checkout page.
    if cfg["use_saved_cards"]:
        personal = cfg["personal_card"]
        payment_block = (
            f"\n"
            f"Your saved payment methods live at /account/cards on "
            f"this shop. Before checking out, visit that page, find "
            f"your PERSONAL card, and read its last 4 digits and "
            f"expiration date. On checkout, fill the payment fields "
            f"with the PERSONAL card's details — NOT the Work card.\n"
        )
    else:
        payment_block = ""
    instruction = (
        f"Go to Safari and open the shop: {landing_url}.\n"
        f"\n"
        f"{task_lines}"
        f"\n"
        f"After picking a product, use its Buy Now link to proceed "
        f"to checkout. On the checkout page, fill in the shipping "
        f"address and place the order. Use this persona:\n"
        f"  - Full name: {persona_name}\n"
        f"  - Street address: {persona[2]}\n"
        f"  - City: {persona[3]}\n"
        f"  - State: {persona[4]}\n"
        f"  - ZIP code: {persona[5]}\n"
        f"{payment_block}"
    )

    verify_checks: List[Dict[str, Any]] = [
        # Exactly one non-decoy checkout submission.
        {"kind": "count",
         "resource": "mock_site.submissions",
         "selector": {"site_id": site_id, "path": "/checkout"},
         "op": "eq", "n": 1, "severity": "blocking",
         "label": "exactly one /checkout submission"},
        # The submitted SKU is the winner. The shop_checkout template
        # ships a hidden `sku` field bound to the path's `?sku=` query
        # so picking the wrong PDP carries through to a wrong sku.
        {"kind": "attribute_eq",
         "resource": "mock_site.submissions",
         "selector": {"site_id": site_id, "path": "/checkout"},
         "attr": "fields.sku", "value": winner.sku_id,
         "severity": "blocking",
         "label": f"sku == {winner.sku_id}"},
        # Shipping fields match the persona.
        {"kind": "attribute_eq",
         "resource": "mock_site.submissions",
         "selector": {"site_id": site_id, "path": "/checkout"},
         "attr": "fields.ship_name", "value": persona_name,
         "severity": "blocking",
         "label": f"ship_name == {persona_name!r}"},
        {"kind": "attribute_eq",
         "resource": "mock_site.submissions",
         "selector": {"site_id": site_id, "path": "/checkout"},
         "attr": "fields.ship_street", "value": persona[2],
         "severity": "blocking",
         "label": f"ship_street == {persona[2]!r}"},
        {"kind": "attribute_eq",
         "resource": "mock_site.submissions",
         "selector": {"site_id": site_id, "path": "/checkout"},
         "attr": "fields.ship_city", "value": persona[3],
         "severity": "blocking",
         "label": f"ship_city == {persona[3]!r}"},
        {"kind": "attribute_eq",
         "resource": "mock_site.submissions",
         "selector": {"site_id": site_id, "path": "/checkout"},
         "attr": "fields.ship_state", "value": persona[4],
         "severity": "blocking",
         "label": f"ship_state == {persona[4]!r}"},
        {"kind": "attribute_eq",
         "resource": "mock_site.submissions",
         "selector": {"site_id": site_id, "path": "/checkout"},
         "attr": "fields.ship_zip", "value": persona[5],
         "severity": "blocking",
         "label": f"ship_zip == {persona[5]!r}"},
    ]

    # V4 (Step 5O, 2026-06-09): when use_saved_cards, require the
    # 3 PERSONAL-card payment fields. Picking the Work card or
    # fabricating numbers both fail the verifier.
    if cfg["use_saved_cards"]:
        personal = cfg["personal_card"]
        verify_checks += [
            {"kind": "attribute_eq",
             "resource": "mock_site.submissions",
             "selector": {"site_id": site_id, "path": "/checkout"},
             "attr": "fields.pay_card_last4",
             "value": personal["last4"],
             "severity": "blocking",
             "label":
                 f"pay_card_last4 == {personal['last4']!r}"},
            {"kind": "attribute_eq",
             "resource": "mock_site.submissions",
             "selector": {"site_id": site_id, "path": "/checkout"},
             "attr": "fields.pay_exp_mm",
             "value": personal["exp_mm"],
             "severity": "blocking",
             "label":
                 f"pay_exp_mm == {personal['exp_mm']!r}"},
            {"kind": "attribute_eq",
             "resource": "mock_site.submissions",
             "selector": {"site_id": site_id, "path": "/checkout"},
             "attr": "fields.pay_exp_yy",
             "value": personal["exp_yy"],
             "severity": "blocking",
             "label":
                 f"pay_exp_yy == {personal['exp_yy']!r}"},
        ]

    return Task(
        task_id="", flow="single_safari_shop",
        apps=["Safari"],
        instruction=instruction,
        verify=(
            f"mock_site.submissions[site_id={site_id},path=/checkout] "
            f"sku={winner.sku_id!r}, ship_name={persona_name!r}, "
            f"ship_zip={persona[5]!r}"),
        verify_mode="db_query",
        initial_state=InitialState(
            present=[], absent=[], noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=20,  # search-pick-buy is ~12-18 turns for a competent LLM
        complexity=complexity_score(20, noise_count=0),
        detail_level=0.0,
        params={
            "site_id": site_id,
            "hostname": hostname,
            "landing_url": landing_url,
            "page_seed": page_seed,
            "target_sku":     winner.sku_id,
            "target_name":    winner.name,
            "target_brand":   winner.brand,
            "target_price_cents": winner.price_cents,
            "target_category":     cfg["category"],
            "target_max_price_cents": cfg["max_price_cents"],
            "persona_name":   persona_name,
            "persona_street": persona[2],
            "persona_city":   persona[3],
            "persona_state":  persona[4],
            "persona_zip":    persona[5],
            "archetype":      cfg["archetype"],
            "search_hint":    cfg["search_hint"],
            "use_saved_cards": cfg["use_saved_cards"],
            "personal_card":   cfg["personal_card"],
            "work_card":       cfg["work_card"],
        },
        verify_checks=verify_checks,
    )


def gen_safari_shop_filter_and_sort():
    """Safari shop Q4 — filter + sort cascade. Distinct from
    `gen_safari_shop_pick_by_attrs` (Q1/Q2): NO typed search, NO
    BM25. The agent reaches the winner by applying category +
    brand filters AND selecting a sort order on a faceted /browse
    page.

    Same catalog, persona, V4 saved-cards axis, and verifier shape
    as the V0.5 generator. Reuses the V0.5 cheapest-winner
    eligibility list (`Catalog.eligible_cheapest_winners`) so the
    winner is the UNIQUE cheapest in its (brand, category) family
    under the chosen cap — applying (cat=X, brand=Y,
    sort=price_asc) ALWAYS makes the winner the first result.

    V4 (`use_saved_cards`) axis applies orthogonally — same
    probability, same page_seed-only derivation as Q1/Q2 — and
    reuses shop_pdp + shop_checkout + shop_account_cards templates
    without modification.
    """
    site_id = f"shop-{uuid.uuid4().hex[:8]}"
    hostname = random.choice(_RSVP_HOSTNAMES)
    page_seed = random.randint(1, 0xFFFFFFFE)

    from harness_layout import compute_path_seed
    from harness_pages import shop_filter_sort_choices
    landing_rng = random.Random(compute_path_seed(page_seed, "/"))
    cfg = shop_filter_sort_choices(landing_rng, page_seed=page_seed)
    winner = cfg["winner"]
    persona = cfg["persona"]
    persona_name = f"{persona[0]} {persona[1]}"

    spec = [
        {
            "app": "Safari",
            "type": "mock_site",
            "site_id": site_id,
            # Q4 reuses shop_pdp + shop_checkout + shop_account_cards
            # because the V4 axis is page_seed-only — the templates
            # only need page_seed to derive cards, never archetype.
            "static_pages": {
                "/":              "shop_q4_landing",
                "/browse":        "shop_q4_browse",
                "/product/":      "shop_pdp",
                "/checkout":      "shop_checkout",
                "/account/cards": "shop_account_cards",
            },
            "page_seed": page_seed,
            "start_path": "/",
            "open_at_start": False,
            "hostname": hostname,
            "credentials": {},
        },
    ] + _springboard_noise()

    landing_url = f"http://127.0.0.1:{{port:{site_id}}}/"

    task_lines = (
        f"You're shopping for a product from this catalog. Browse "
        f"the store at /browse and apply these filters and sort:\n"
        f"  - Category: {cfg['category']}\n"
        f"  - Brand: {winner.brand}\n"
        f"  - Sort by: Price (low to high)\n"
        f"\n"
        f"After applying all three, the FIRST product in the "
        f"results is the cheapest match — pick that one. "
        f"Maximum price: ${cfg['max_price_cents'] / 100:.2f}.\n"
    )

    if cfg["use_saved_cards"]:
        payment_block = (
            f"\n"
            f"Your saved payment methods live at /account/cards on "
            f"this shop. Before checking out, visit that page, find "
            f"your PERSONAL card, and read its last 4 digits and "
            f"expiration date. On checkout, fill the payment fields "
            f"with the PERSONAL card's details — NOT the Work card.\n"
        )
    else:
        payment_block = ""

    instruction = (
        f"Go to Safari and open the shop: {landing_url}.\n"
        f"\n"
        f"{task_lines}"
        f"\n"
        f"After picking a product, use its Buy Now link to proceed "
        f"to checkout. On the checkout page, fill in the shipping "
        f"address and place the order. Use this persona:\n"
        f"  - Full name: {persona_name}\n"
        f"  - Street address: {persona[2]}\n"
        f"  - City: {persona[3]}\n"
        f"  - State: {persona[4]}\n"
        f"  - ZIP code: {persona[5]}\n"
        f"{payment_block}"
    )

    verify_checks: List[Dict[str, Any]] = [
        {"kind": "count",
         "resource": "mock_site.submissions",
         "selector": {"site_id": site_id, "path": "/checkout"},
         "op": "eq", "n": 1, "severity": "blocking",
         "label": "exactly one /checkout submission"},
        {"kind": "attribute_eq",
         "resource": "mock_site.submissions",
         "selector": {"site_id": site_id, "path": "/checkout"},
         "attr": "fields.sku", "value": winner.sku_id,
         "severity": "blocking",
         "label": f"sku == {winner.sku_id}"},
        {"kind": "attribute_eq",
         "resource": "mock_site.submissions",
         "selector": {"site_id": site_id, "path": "/checkout"},
         "attr": "fields.ship_name", "value": persona_name,
         "severity": "blocking",
         "label": f"ship_name == {persona_name!r}"},
        {"kind": "attribute_eq",
         "resource": "mock_site.submissions",
         "selector": {"site_id": site_id, "path": "/checkout"},
         "attr": "fields.ship_street", "value": persona[2],
         "severity": "blocking",
         "label": f"ship_street == {persona[2]!r}"},
        {"kind": "attribute_eq",
         "resource": "mock_site.submissions",
         "selector": {"site_id": site_id, "path": "/checkout"},
         "attr": "fields.ship_city", "value": persona[3],
         "severity": "blocking",
         "label": f"ship_city == {persona[3]!r}"},
        {"kind": "attribute_eq",
         "resource": "mock_site.submissions",
         "selector": {"site_id": site_id, "path": "/checkout"},
         "attr": "fields.ship_state", "value": persona[4],
         "severity": "blocking",
         "label": f"ship_state == {persona[4]!r}"},
        {"kind": "attribute_eq",
         "resource": "mock_site.submissions",
         "selector": {"site_id": site_id, "path": "/checkout"},
         "attr": "fields.ship_zip", "value": persona[5],
         "severity": "blocking",
         "label": f"ship_zip == {persona[5]!r}"},
    ]

    if cfg["use_saved_cards"]:
        personal = cfg["personal_card"]
        verify_checks += [
            {"kind": "attribute_eq",
             "resource": "mock_site.submissions",
             "selector": {"site_id": site_id, "path": "/checkout"},
             "attr": "fields.pay_card_last4",
             "value": personal["last4"],
             "severity": "blocking",
             "label":
                 f"pay_card_last4 == {personal['last4']!r}"},
            {"kind": "attribute_eq",
             "resource": "mock_site.submissions",
             "selector": {"site_id": site_id, "path": "/checkout"},
             "attr": "fields.pay_exp_mm",
             "value": personal["exp_mm"],
             "severity": "blocking",
             "label":
                 f"pay_exp_mm == {personal['exp_mm']!r}"},
            {"kind": "attribute_eq",
             "resource": "mock_site.submissions",
             "selector": {"site_id": site_id, "path": "/checkout"},
             "attr": "fields.pay_exp_yy",
             "value": personal["exp_yy"],
             "severity": "blocking",
             "label":
                 f"pay_exp_yy == {personal['exp_yy']!r}"},
        ]

    return Task(
        task_id="", flow="single_safari_shop_filter",
        apps=["Safari"],
        instruction=instruction,
        verify=(
            f"mock_site.submissions[site_id={site_id},path=/checkout] "
            f"sku={winner.sku_id!r}, ship_name={persona_name!r}, "
            f"ship_zip={persona[5]!r}"),
        verify_mode="db_query",
        initial_state=InitialState(
            present=[], absent=[], noise_records=[],
            setup_commands=[],
            spec=spec,
            expected_behavior="complete",
        ),
        steps=22,
        complexity=complexity_score(22, noise_count=0),
        detail_level=0.0,
        params={
            "site_id": site_id,
            "hostname": hostname,
            "landing_url": landing_url,
            "page_seed": page_seed,
            "target_sku":     winner.sku_id,
            "target_name":    winner.name,
            "target_brand":   winner.brand,
            "target_price_cents": winner.price_cents,
            "target_category":     cfg["category"],
            "target_max_price_cents": cfg["max_price_cents"],
            "persona_name":   persona_name,
            "persona_street": persona[2],
            "persona_city":   persona[3],
            "persona_state":  persona[4],
            "persona_zip":    persona[5],
            "canonical_sort":  cfg["canonical_sort"],
            "use_saved_cards": cfg["use_saved_cards"],
            "personal_card":   cfg["personal_card"],
            "work_card":       cfg["work_card"],
        },
        verify_checks=verify_checks,
    )


# ── Generator registry ────────────────────────────────────────────────────────
# Generators are gated on APP_REGISTRY["available"].
# When an app becomes available, move its generators from
# GENERATORS_PENDING to ALL_GENERATORS.

ALL_GENERATORS = {
    # Single-app tasks using confirmed available apps (SIBB-11)
    "single_standard": [
        gen_reminders_list,         # Reminders ✓
        gen_update_calendar_time,   # Calendar  ✓ (update flow doubles as single-app)
        gen_notes_in_folder,        # Notes     ✗ — kept for when Notes is fixed
    ],
    # Tier 1 single-action Reminders tasks (Phase 2 systematic design).
    # Run each via sibb_replay with the matching generator key.
    "tier1_reminders": [
        gen_complete_specific_reminder,
        gen_uncomplete_reminder,
        gen_add_reminder_to_existing_list,
        gen_set_priority,
    ],
    # Tier 4 due-date / notes / mixed-state Reminders tasks.
    "tier4_reminders": [
        gen_set_due_date_on_reminder,
        gen_change_due_date,
        gen_complete_all_overdue,
        gen_add_notes_to_reminder,
        gen_clear_completed_only,
    ],
    # Tier 2/3 bulk + structural Reminders tasks.
    "tier23_reminders": [
        gen_complete_all_in_list,
        gen_delete_specific_reminder,
        gen_delete_entire_list,
        gen_move_reminder_between_lists,
        gen_rename_reminder,
    ],
    # Tier 4b recurrence-based Reminders tasks.
    "tier4b_reminders": [
        gen_make_reminder_recurring,
        gen_change_recurrence_frequency,
        gen_stop_recurrence,
        gen_create_recurring_with_due,
    ],
    # Tier 5 reporting Reminders tasks (agent_answer).
    # gen_count_overdue + gen_yesno_overdue dropped 2026-05-20 —
    # too small an answer space (chance-guess ~33% / 50%).
    "tier5_reminders": [
        gen_list_due_today,
        gen_list_due_tomorrow,
        gen_lookup_reminder_notes,
    ],
    # Calendar Tier 1 single-action tasks (Phase 2c).
    "tier1_calendar": [
        gen_create_event_with_title_time,
        gen_delete_specific_event,
        gen_change_event_title,
        gen_set_event_location,
        gen_change_event_time,
        gen_toggle_event_all_day,
    ],
    # Calendar Tier 2/3 bulk + structural (Phase 2c).
    "tier23_calendar": [
        gen_delete_all_events_on_date,
        gen_duplicate_event_to_next_week,
        gen_delete_events_in_calendar,
        gen_move_event_between_calendars,
    ],
    # Calendar Tier 4 — time edits / notes / url (Phase 2c).
    "tier4_calendar": [
        gen_reschedule_event_same_duration,
        gen_adjust_event_boundary,
        gen_add_notes_to_event,
        gen_add_event_url,
    ],
    # Calendar Tier 4b — recurrence (Phase 2c).
    "tier4b_calendar": [
        gen_make_event_recurring,
        gen_stop_event_recurrence,
        gen_change_event_recurrence_frequency,
        gen_create_recurring_event,
    ],
    # Calendar Tier 5 — reporting via agent_answer (Phase 2c).
    "tier5_calendar": [
        gen_lookup_event_location,
        gen_list_events_today,
        gen_list_conflicting_events,
        gen_next_event_lookup,
    ],
    # Contacts Phase 2 v1 (2026-05-24). 5 generators across T1/T2/T4/T5.
    # gen_birthday_with_calendar_check (T4b cross-app) deferred to
    # Phase 3 pending url_contains selector + SymbolicRef plumbing.
    "tier1_contacts": [
        gen_set_contact_birthday,
        gen_set_contact_birthday_no_year,
    ],
    "tier2_contacts": [
        gen_add_second_phone_label,
        gen_create_contact_with_address,
    ],
    "tier4_contacts": [
        gen_full_business_card,
    ],
    "tier5_contacts": [
        gen_lookup_phone_by_name,
    ],
    # Safari Phase 4 / Tier 1 (2026-06-05). Single-app Safari
    # generators — bookmark mutation via the folder-aware fetcher,
    # plus harness-served form fill (Phase 4 first harness gen).
    "tier1_safari": [
        gen_safari_bookmark_specific_url,
        gen_safari_rsvp_form,
        gen_safari_rsvp_form_clipped,
        gen_safari_shop_pick_by_attrs,
        gen_safari_shop_filter_and_sort,
    ],
    # Phase 3 cross-app showcase (2026-05-26+).
    "cross_app": [
        gen_reminder_with_calendar_event,   # already registered above
        gen_maps_search_to_contact,         # Maps × Contacts
        gen_message_save_sender,            # Messages × Contacts (A)
        gen_message_save_body,               # Messages × Contacts (B)
        gen_message_save_address,           # Messages × Contacts (C)
        gen_message_to_contact_to_maps,     # Messages × Contacts × Maps (D)
        gen_message_to_new_contact_to_maps, # Messages × Contacts × Maps (E)
        gen_message_save_sender_with_address,  # Messages × Contacts (F)
    ],
    # Multi-app tasks — Phase 2b B4 onward
    "multi_app_standard": [
        gen_reminder_with_calendar_event,   # Reminders × Calendar ✓
    ],
    "impossibility": [
        gen_contact_not_found,              # Contacts/Messages ✓
        gen_partial_feasibility_blocking,   # Maps+Calendar+Messages ✓
        gen_fetch_not_found,                # Calendar ✓
    ],
    "ambiguous": [
        gen_ambiguous_contact_missing_phone,  # Contacts ✓
        gen_ambiguous_which_note,             # Notes ✗ — kept for when Notes is fixed
        gen_ambiguous_vague_instruction,      # Files ✓
    ],
    "constraint": [
        gen_files_size_range,          # Files ✓
        gen_notes_word_range,          # Notes ✗ — kept for when Notes is fixed
        gen_calendar_duration_range,   # Calendar ✓
    ],
    "fetch": [
        gen_fetch_calendar_events,     # Calendar ✓
    ],
    "update": [
        gen_update_contact_phone,      # Contacts ✓
        gen_update_calendar_time,      # Calendar ✓
    ],
}

# Generators that will be activated when their apps become available.
# To activate: move from GENERATORS_PENDING into ALL_GENERATORS above.
GENERATORS_PENDING = {
    "clock_available": [
        # gen_clock_alarm,           # Clock — when available
    ],
    "notes_available": [
        # gen_notes_in_folder already in ALL_GENERATORS (runs but warns)
        # Additional Notes generators go here when Notes is confirmed
    ],
    "music_available": [
        # Future: gen_music_playlist, gen_music_constraint_duration
    ],
    "mail_available": [
        # Future: gen_mail_compose, gen_contacts_calendar_mail
    ],
    "voice_memos_available": [
        # Future: gen_voice_notes (capture → organize flow)
    ],
}

def warn_unavailable(gen_fn, app_name: str):
    """Decorator that warns when a generator runs for an unavailable app."""
    import functools
    @functools.wraps(gen_fn)
    def wrapper(*args, **kwargs):
        if not check_app_available(app_name):
            reason = APP_REGISTRY.get(app_name, {}).get("unavailable_reason", "unknown")
            print(f"  ⚠  WARNING: {app_name} unavailable ({reason})")
            print(f"     Task generated but cannot be run on current simulator.")
        return gen_fn(*args, **kwargs)
    return wrapper


def fmt(t: Task, idx: int, section: str):
    eb_icon = {"complete": "✓", "create_then_complete": "✚",
               "fail_gracefully": "✗"}[t.initial_state.expected_behavior]
    print(f"\n{'═'*70}")
    print(f"  {section}.{idx}  │  {'+'.join(t.apps)}  │  "
          f"~{t.steps} steps  │  complexity={t.complexity}  │  {eb_icon} {t.initial_state.expected_behavior}")
    if t.initial_state.blocking:
        print(f"  BLOCKING:  " + "; ".join(t.initial_state.blocking))
    if t.initial_state.present:
        print(f"  PRESENT:   " + "; ".join(t.initial_state.present[:2]))
    if t.initial_state.absent:
        print(f"  ABSENT:    " + "; ".join(t.initial_state.absent[:2]))
    if t.initial_state.noise_records:
        layout_noise = [r for r in t.initial_state.noise_records if r.record_type == "layout"]
        db_noise     = [r for r in t.initial_state.noise_records if r.record_type != "layout"]
        noise_str = "; ".join(r.similarity for r in db_noise[:3])
        if layout_noise:
            lp = layout_noise[0]
            noise_str += f" | layout(seed={lp.params['seed']},cross={lp.params['cross_page']})"
        print(f"  NOISE ({len(t.initial_state.noise_records)}): {noise_str}")
    print(f"\n  INSTRUCTION\n  {t.instruction[:300]}")
    print(f"\n  VERIFY\n  {t.verify[:300]}")
    if t.clarify_slots:
        print(f"\n  CLARIFY SLOTS ({len(t.clarify_slots)}):")
        for s in t.clarify_slots:
            req = "required" if s.required else "optional"
            eg  = s.example_questions[0] if s.example_questions else "?"
            print(f"    [{req}] CLARIFY {s.question_type}:")
            print(f"           e.g. \"{eg[:65]}\"")
            print(f"           internal={s.slot_name!r}  answer={s.answer!r}  "
                  f"({len(s.example_questions)} example phrasings)")
    if t.positive_checks:
        print(f"\n  POSITIVE CHECKS: " + "; ".join(t.positive_checks))
    if t.negative_checks:
        print(f"  NEGATIVE CHECKS: " + "; ".join(t.negative_checks))
    if t.agent_check:
        print(f"  AGENT CHECK: terminal action must be '{t.agent_check}'")


if __name__ == "__main__":
    random.seed(42)
    print("█"*70)
    print("  SIBB TASK GENERATOR v3")
    print("  Binary scoring · Noise injection · FAIL tool · CLARIFY tool")
    print("  Constraint satisfaction · Impossibility · Ambiguous · Update · Fetch")
    print("█"*70)

    for section, gens in ALL_GENERATORS.items():
        print(f"\n\n{'▓'*70}")
        print(f"  {section.upper()}")
        print(f"{'▓'*70}")
        tasks = [g() for g in random.sample(gens, min(2, len(gens)))]
        for i, t in enumerate(tasks, 1):
            t.task_id = f"{section}_{i:02d}"
            fmt(t, i, section)
