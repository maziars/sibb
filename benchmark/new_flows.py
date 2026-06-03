"""
Three new flow types for the iOS task generator.

Adds to the existing framework:
  - VerifyTarget  : what the verifier inspects (db | agent_response | screen)
  - PrePopulatedData : structured key/value data that MUST exist before the task
  - SearchParam   : a param whose value comes from a real-time search
  - UpdateDelta   : old_value / new_value pair for update tasks
"""

import random
from dataclasses import dataclass, field
from typing import Any, Optional, List, Dict

# ── Re-use canonical values from v2 ──────────────────────────────────────────
C = {
    "social_contacts": ["Alex", "Sam", "Jordan", "Taylor", "Chris", "Morgan"],
    "family_contacts": ["Mom", "Dad", "Sister", "Brother", "Grandma"],
    "work_contacts":   ["My Manager", "John Smith", "Sarah Chen", "Marcus Webb"],
    "maps_places":     ["Starbucks", "Whole Foods", "CVS Pharmacy",
                        "Apple Store", "Gold's Gym", "Tartine Bakery",
                        "Ferry Building", "Union Square", "Chinatown"],
    "landmark_places": ["Union Square", "Ferry Building", "Golden Gate Park",
                        "Fisherman's Wharf", "Coit Tower"],
    "work_times":      ["9:00 AM","10:00 AM","11:00 AM","1:00 PM",
                        "2:00 PM","3:00 PM","4:00 PM","5:00 PM"],
    "day_offsets":     ["today","tomorrow","this Friday","next Monday",
                        "next Wednesday","next Saturday"],
    "event_titles":    ["Dentist","Haircut","Car Service","Doctor Appointment",
                        "Gym Session","Coffee with {C}","Lunch with {C}",
                        "Team Standup","Budget Review","Project Kickoff"],
    "calendar_names":  ["Personal","Work","Family","Health & Fitness"],
    "reminder_lists":  ["Personal","Work","Health","Family","Shopping"],
    "note_folders":    ["Work","Personal","Health","Projects"],
    "address_types":   ["home","work","school","other"],
    "phone_area":      ["415","650","510","408","628"],
    "months":          ["January","February","March","April","May","June",
                        "July","August","September","October","November","December"],
    "safari_urls":     ["apple.com","en.wikipedia.org/wiki/San_Francisco",
                        "yelp.com","maps.apple.com"],
    "message_bodies":  ["Here's the info you asked for.",
                        "Just confirmed this for you.",
                        "See you then!",
                        "Let me know if you need anything else."],
    "note_titles":     ["Meeting Notes","Research","Ideas","Draft",
                        "Budget","Project Plan"],
}

def pick(key): return random.choice(C[key])
def pickn(key, n): return random.sample(C[key], min(n, len(C[key])))

def rand_phone():
    area = pick("phone_area")
    return f"{area}-555-{random.randint(1000,9999)}"

def rand_date_time():
    return f"{pick('day_offsets')} at {pick('work_times')}"

# ─────────────────────────────────────────────────────────────────────────────
#  NEW DATACLASSES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PrePopulatedData:
    """
    Structured data that MUST exist in the simulator before the task runs.
    Each entry is (app, entity_type, field, value).

    Used for Fetch and Update tasks.
    setup_commands: how to inject this data into the simulator.
    """
    entries:        List[Dict[str, Any]] = field(default_factory=list)
    setup_commands: List[str]            = field(default_factory=list)

    def add(self, app: str, entity_type: str, field_name: str,
            value: Any, identifier: str = ""):
        self.entries.append({
            "app": app,
            "entity_type": entity_type,
            "field": field_name,
            "value": value,
            "identifier": identifier,
        })
        return self

    def describe(self) -> List[str]:
        return [f"{e['app']}: {e['entity_type']} '{e['identifier']}' "
                f"has {e['field']}={e['value']}"
                for e in self.entries]


@dataclass
class UpdateDelta:
    """
    For Update tasks: what changes and what the old value was.
    Both old and new values must appear in the verifier.
    """
    field_name: str
    old_value:  Any
    new_value:  Any
    entity:     str   # e.g. "contact Greg" or "event Haircut"


@dataclass
class SearchParam:
    """
    A param whose value must be discovered at runtime via search.
    The generator cannot hardcode the value — only the query and
    the expected result type.

    The agent must:
      1. Issue the search query
      2. Extract the result_type from the results
      3. Use that value in the subsequent UI action

    The verifier checks that the downstream field was populated
    with a plausible value (non-empty, correct type, matching
    a known result where possible).
    """
    key:           str
    query:         str            # the search string the agent should use
    search_app:    str            # "Maps", "Safari", "Contacts", "Phone"
    result_type:   str            # "address", "phone", "hours", "url", "name"
    known_result:  Optional[str]  # if we know the ground truth (for Maps)
    required:      bool = True    # always mandatory for Search-then-act flows


@dataclass
class FetchTask:
    """
    Read-only task. Agent reads and reports data.

    pre_populated: what the simulator contains before the task
    question:      what the agent is asked to find/report
    expected_answer: the correct answer (used by verifier)
    verify_target: "agent_response" — we check the agent's text output
    """
    apps:            List[str]
    question:        str
    expected_answer: Dict[str, Any]    # field → expected value
    pre_populated:   PrePopulatedData
    steps:           int = 3
    verify_mode:     str = "agent_response"  # not db_query


@dataclass
class UpdateTask:
    """
    Modify an existing record.

    pre_populated:  what exists before (with old_value)
    delta:          what changes (old → new)
    extra_actions:  optional follow-up actions (e.g. also message the contact)
    """
    apps:          List[str]
    instruction:   str
    verify:        str
    pre_populated: PrePopulatedData
    delta:         UpdateDelta
    steps:         int = 6


@dataclass
class SearchThenActTask:
    """
    Agent searches for info, then uses it in a UI action.

    search_param:   what to search for and how
    downstream_app: where the search result gets used
    downstream_action: what to do with the result
    """
    apps:              List[str]
    instruction:       str
    verify:            str
    search_param:      SearchParam
    downstream_app:    str
    downstream_action: str
    steps:             int = 8


# ─────────────────────────────────────────────────────────────────────────────
#  FLOW S — SEARCH THEN ACT
# ─────────────────────────────────────────────────────────────────────────────

def gen_maps_search_then_calendar():
    """
    Find a place in Maps, then create a Calendar event at that location.
    The event's location field must be populated from the search result.
    """
    place     = pick("maps_places")
    landmark  = pick("landmark_places")
    day       = pick("day_offsets")
    time      = pick("work_times")
    contact   = random.choice(C["social_contacts"] + C["family_contacts"])
    cal       = pick("calendar_names")
    event     = f"Meet at {place.split(',')[0]}"

    search = SearchParam(
        key         = "place_address",
        query       = f"{place} near {landmark}",
        search_app  = "Maps",
        result_type = "address",
        known_result= None,   # real Maps result; verifier checks non-empty
        required    = True,
    )

    instruction = (
        f"1. MAPS: Search for '{place}' near {landmark}. "
        f"Find the top result and note its full address.\n\n"
        f"2. CALENDAR: Create a new event called '{event}' on {day} at {time} "
        f"in the '{cal}' calendar. "
        f"Paste the address from Maps into the Location field. "
        f"Add '{contact}' as an attendee. "
        f"Set a 30-minute-before alert."
    )

    verify = (
        f"Calendar '{cal}' has event='{event}' on {day} at {time}, "
        f"location field is non-empty (populated from Maps search for '{place}'), "
        f"attendee='{contact}', alert=30 minutes before. "
        f"The location must match a real address near {landmark}, not be blank."
    )

    return SearchThenActTask(
        apps=["Maps", "Calendar"],
        instruction=instruction,
        verify=verify,
        search_param=search,
        downstream_app="Calendar",
        downstream_action="create_event_with_location",
        steps=9,
    )


def gen_safari_search_then_notes():
    """
    Search for information in Safari, then save key facts to Notes.
    """
    topic   = random.choice(["best coffee shops in San Francisco",
                              "Golden Gate Park opening hours",
                              "BART schedule from Embarcadero",
                              "Tartine Bakery menu"])
    folder  = pick("note_folders")
    title   = f"Research: {topic[:30]}"

    search = SearchParam(
        key        = "search_result",
        query      = topic,
        search_app = "Safari",
        result_type= "text_facts",
        known_result= None,
        required   = True,
    )

    instruction = (
        f"1. SAFARI: Search for '{topic}'. "
        f"Read the top result and identify 3 key facts.\n\n"
        f"2. NOTES: Create a note titled '{title}'. "
        f"Write the 3 facts as a bullet list. "
        f"Add a 'Source' line with the URL. "
        f"Move to the '{folder}' folder."
    )

    verify = (
        f"Notes has '{title}' in folder='{folder}' "
        f"with at least 3 bullet points (non-empty, relevant to '{topic}') "
        f"and a Source URL. "
        f"The note content must reflect real information found during the search, "
        f"not hallucinated facts."
    )

    return SearchThenActTask(
        apps=["Safari", "Notes"],
        instruction=instruction,
        verify=verify,
        search_param=search,
        downstream_app="Notes",
        downstream_action="create_note_from_search",
        steps=10,
    )


def gen_maps_search_then_contacts():
    """
    Find a business in Maps and save its address to a contact.
    Contact may or may not already exist.
    """
    place   = pick("maps_places")
    contact_name = pick("social_contacts") + " " + random.choice(
        ["Smith","Johnson","Chen","Patel","Garcia"])
    addr_t  = "work"

    search = SearchParam(
        key        = "business_address",
        query      = place,
        search_app = "Maps",
        result_type= "address",
        known_result= None,
        required   = True,
    )

    instruction = (
        f"1. MAPS: Search for '{place}'. "
        f"Open the place card and note the full address.\n\n"
        f"2. CONTACTS: Find '{contact_name}' in your contacts. "
        f"Add the Maps address as their '{addr_t}' address. "
        f"Save the contact."
    )

    verify = (
        f"Contacts has '{contact_name}' with {addr_t}_address "
        f"populated from the Maps search for '{place}' (non-empty, valid address)."
    )

    return SearchThenActTask(
        apps=["Maps", "Contacts"],
        instruction=instruction,
        verify=verify,
        search_param=search,
        downstream_app="Contacts",
        downstream_action="update_contact_address",
        steps=7,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  FLOW R — FETCH / READ
# ─────────────────────────────────────────────────────────────────────────────

def gen_fetch_calendar_events():
    """
    Read all calendar events for a specific date. Agent must report them.
    Environment is pre-populated with N events on that date.
    """
    day         = pick("day_offsets")
    n_events    = random.randint(1, 4)
    events      = []
    for _ in range(n_events):
        title = pick("event_titles").replace("{C}", pick("social_contacts"))
        time  = pick("work_times")
        events.append({"title": title, "time": time})

    pre = PrePopulatedData()
    for ev in events:
        pre.add("Calendar", "event", "title", ev["title"], ev["title"])
        pre.add("Calendar", "event", "time",  ev["time"],  ev["title"])
        pre.setup_commands.append(
            f"xcrun simctl spawn booted calendarctl create-event "
            f"--title '{ev['title']}' --date '{day}' --time '{ev['time']}'"
        )

    titles_str = ", ".join(f"'{e['title']} at {e['time']}'" for e in events)

    task = FetchTask(
        apps=["Calendar"],
        question=f"What are my calendar events for {day}?",
        expected_answer={
            "event_count": n_events,
            "events": events,
            "date": day,
        },
        pre_populated=pre,
        steps=3,
    )

    # Attach human-readable strings for the generator output
    task.instruction = (
        f"Open the Calendar app. Find and list all events scheduled for {day}. "
        f"Report each event's title and time."
    )
    task.verify = (
        f"Agent's response lists exactly {n_events} event(s) for {day}: {titles_str}. "
        f"Each title and time must match the pre-populated events exactly. "
        f"verify_mode=agent_response — check the agent's text output, not a DB write."
    )
    task.setup_description = pre.describe()
    return task


def gen_fetch_specific_appointment():
    """
    Agent is asked about a named event. Answers: does it exist? When?
    Two variants: event exists (agent reports time) or doesn't exist (agent reports not found).
    """
    event_name = pick("event_titles").replace("{C}", pick("social_contacts"))
    variant    = random.choice(["exists", "not_found"])
    day        = pick("day_offsets")
    time       = pick("work_times")

    pre = PrePopulatedData()
    if variant == "exists":
        pre.add("Calendar", "event", "title", event_name, event_name)
        pre.add("Calendar", "event", "time",  time,       event_name)
        pre.add("Calendar", "event", "date",  day,        event_name)
        pre.setup_commands.append(
            f"xcrun simctl spawn booted calendarctl create-event "
            f"--title '{event_name}' --date '{day}' --time '{time}'"
        )
        expected = {"exists": True, "title": event_name, "date": day, "time": time}
        verify = (
            f"Agent reports: '{event_name}' is scheduled for {day} at {time}. "
            f"verify_mode=agent_response."
        )
    else:
        expected = {"exists": False, "title": event_name}
        verify = (
            f"Agent reports: no event named '{event_name}' found in calendar. "
            f"Agent does NOT fabricate a time or date. "
            f"verify_mode=agent_response."
        )

    task = FetchTask(
        apps=["Calendar"],
        question=f"When is my '{event_name}' appointment?",
        expected_answer=expected,
        pre_populated=pre,
        steps=3,
    )
    task.instruction = (
        f"Open the Calendar app and search for an event called '{event_name}'. "
        f"Report when it is scheduled, or say it was not found if it doesn't exist."
    )
    task.verify = verify
    task.variant = variant
    task.setup_description = pre.describe()
    return task


def gen_fetch_contact_info():
    """
    Fetch specific fields from a contact. Agent reads and reports them.
    """
    first  = random.choice(["Alex","Sam","Dr. Chen","Marcus","Jordan","Taylor"])
    last   = random.choice(["Smith","Johnson","Chen","Webb","Garcia"])
    name   = f"{first} {last}"
    phone  = rand_phone()
    email  = f"{first.lower()}.{last.lower()}@example.com"
    field_to_fetch = random.choice(["phone", "email", "address", "birthday"])
    field_values   = {
        "phone": phone, "email": email,
        "address": f"123 Main St, San Francisco",
        "birthday": f"{pick('months')} {random.randint(1,28)}"
    }
    val = field_values[field_to_fetch]

    pre = PrePopulatedData()
    pre.add("Contacts", "contact", "name",         name,  name)
    pre.add("Contacts", "contact", field_to_fetch, val,   name)
    pre.setup_commands.append(
        f"xcrun simctl spawn booted contactsctl create-contact "
        f"--first '{first}' --last '{last}' "
        f"--{field_to_fetch} '{val}'"
    )

    task = FetchTask(
        apps=["Contacts"],
        question=f"What is {name}'s {field_to_fetch}?",
        expected_answer={"name": name, "field": field_to_fetch, "value": val},
        pre_populated=pre,
        steps=3,
    )
    task.instruction = (
        f"Open Contacts. Find '{name}'. Report their {field_to_fetch}."
    )
    task.verify = (
        f"Agent reports {name}'s {field_to_fetch} = '{val}' exactly. "
        f"verify_mode=agent_response."
    )
    task.setup_description = pre.describe()
    return task


# ─────────────────────────────────────────────────────────────────────────────
#  FLOW U — UPDATE
# ─────────────────────────────────────────────────────────────────────────────

def gen_update_contact_phone():
    """
    Update a contact's phone number. Contact must pre-exist with old number.
    """
    first  = random.choice(["Greg","Alex","Sam","Jordan","Marcus","Taylor"])
    last   = random.choice(["Smith","Johnson","Chen","Webb","Garcia"])
    name   = f"{first} {last}"
    old_ph = rand_phone()
    new_ph = rand_phone()
    while new_ph == old_ph:
        new_ph = rand_phone()

    pre = PrePopulatedData()
    pre.add("Contacts", "contact", "name",  name,   name)
    pre.add("Contacts", "contact", "phone", old_ph, name)
    pre.setup_commands.append(
        f"xcrun simctl spawn booted contactsctl create-contact "
        f"--first '{first}' --last '{last}' --phone '{old_ph}'"
    )

    delta = UpdateDelta(
        field_name="phone",
        old_value=old_ph,
        new_value=new_ph,
        entity=f"contact '{name}'"
    )

    return UpdateTask(
        apps=["Contacts"],
        instruction=(
            f"Open Contacts. Find '{name}'. "
            f"Edit their phone number: change it from the current number "
            f"to {new_ph}. Save."
        ),
        verify=(
            f"Contacts has '{name}' with phone={new_ph} "
            f"(old value {old_ph} is no longer present). "
            f"verify_mode=db_query."
        ),
        pre_populated=pre,
        delta=delta,
        steps=6,
    )


def gen_update_calendar_event_time():
    """
    Move an existing calendar event to a new time.
    """
    event_name = pick("event_titles").replace("{C}", pick("social_contacts"))
    old_day    = "tomorrow"
    old_time   = pick("work_times")
    new_time   = pick("work_times")
    while new_time == old_time:
        new_time = pick("work_times")

    pre = PrePopulatedData()
    pre.add("Calendar", "event", "title", event_name, event_name)
    pre.add("Calendar", "event", "time",  old_time,   event_name)
    pre.add("Calendar", "event", "date",  old_day,    event_name)
    pre.setup_commands.append(
        f"xcrun simctl spawn booted calendarctl create-event "
        f"--title '{event_name}' --date '{old_day}' --time '{old_time}'"
    )

    delta = UpdateDelta(
        field_name="time",
        old_value=old_time,
        new_value=new_time,
        entity=f"event '{event_name}'"
    )

    return UpdateTask(
        apps=["Calendar"],
        instruction=(
            f"Open Calendar. Find the '{event_name}' event on {old_day}. "
            f"Edit its time: move it from {old_time} to {new_time}. Save."
        ),
        verify=(
            f"Calendar has '{event_name}' on {old_day} with time={new_time}. "
            f"Old time {old_time} is no longer set. "
            f"verify_mode=db_query."
        ),
        pre_populated=pre,
        delta=delta,
        steps=7,
    )


def gen_update_reminder_due_date():
    """
    Change the due date on an existing reminder.
    """
    list_name = pick("reminder_lists")
    title     = random.choice(["Pick up prescription","Call the bank",
                                "Submit report","Book hotel","Renew insurance"])
    old_day   = "tomorrow"
    new_day   = random.choice(["this Friday","next Monday","next Wednesday"])

    pre = PrePopulatedData()
    pre.add("Reminders", "reminder", "title",    title,   title)
    pre.add("Reminders", "reminder", "due_date", old_day, title)
    pre.add("Reminders", "reminder", "list",     list_name, title)
    pre.setup_commands.extend([
        f"xcrun simctl spawn booted remindersctl create-list '{list_name}'",
        f"xcrun simctl spawn booted remindersctl create-reminder "
        f"--title '{title}' --list '{list_name}' --due '{old_day}'"
    ])

    delta = UpdateDelta(
        field_name="due_date",
        old_value=old_day,
        new_value=new_day,
        entity=f"reminder '{title}'"
    )

    return UpdateTask(
        apps=["Reminders"],
        instruction=(
            f"Open Reminders. In the '{list_name}' list, find '{title}'. "
            f"Change its due date from {old_day} to {new_day}. Save."
        ),
        verify=(
            f"Reminders '{list_name}' has '{title}' with due_date={new_day}. "
            f"Old due_date {old_day} is no longer set. "
            f"verify_mode=db_query."
        ),
        pre_populated=pre,
        delta=delta,
        steps=6,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  MULTI-APP COMBINATIONS
# ─────────────────────────────────────────────────────────────────────────────

def gen_fetch_then_communicate():
    """
    R + B: Read calendar events → Message them to a contact.
    2 apps: Calendar (read) + Messages (create)
    """
    contact   = random.choice(C["social_contacts"] + C["family_contacts"])
    day       = pick("day_offsets")
    n_events  = random.randint(1, 3)
    events    = [{"title": pick("event_titles").replace("{C}", pick("social_contacts")),
                  "time":  pick("work_times")} for _ in range(n_events)]

    pre = PrePopulatedData()
    for ev in events:
        pre.add("Calendar", "event", "title", ev["title"], ev["title"])
        pre.add("Calendar", "event", "time",  ev["time"],  ev["title"])
        pre.setup_commands.append(
            f"xcrun simctl spawn booted calendarctl create-event "
            f"--title '{ev['title']}' --date '{day}' --time '{ev['time']}'"
        )

    titles_str = "; ".join(f"{e['title']} at {e['time']}" for e in events)

    # compose instruction
    instruction = (
        f"1. CALENDAR: Look at your schedule for {day}. "
        f"Note all events (title and time).\n\n"
        f"2. MESSAGES: Open a conversation with '{contact}'. "
        f"Send them your {day} schedule as a message."
    )

    verify = (
        f"Calendar pre-populated with {n_events} events on {day}: {titles_str}. "
        f"Messages thread with '{contact}' contains all event titles and times. "
        f"verify_mode=hybrid: check DB for message thread AND agent response lists events correctly."
    )

    # Embed pre-pop in a pseudo-UpdateTask for consistent output
    return {
        "flow": "R+B_fetch_communicate",
        "apps": ["Calendar", "Messages"],
        "instruction": instruction,
        "verify": verify,
        "pre_populated": pre.describe(),
        "setup_commands": pre.setup_commands,
        "steps": 7,
        "expected_behavior": "complete",
        "verify_mode": "hybrid",
    }


def gen_update_then_communicate():
    """
    U + B: Update a contact → Message them the change.
    2 apps: Contacts (update) + Messages (create)
    """
    first  = random.choice(["Greg","Alex","Sam","Jordan","Marcus"])
    last   = random.choice(["Smith","Johnson","Chen","Webb"])
    name   = f"{first} {last}"
    old_ph = rand_phone()
    new_ph = rand_phone()
    while new_ph == old_ph:
        new_ph = rand_phone()
    caption = pick("message_bodies")

    pre = PrePopulatedData()
    pre.add("Contacts","contact","name",  name,   name)
    pre.add("Contacts","contact","phone", old_ph, name)
    pre.setup_commands.append(
        f"xcrun simctl spawn booted contactsctl create-contact "
        f"--first '{first}' --last '{last}' --phone '{old_ph}'"
    )

    instruction = (
        f"1. CONTACTS: Find '{name}'. Update their phone number to {new_ph}. Save.\n\n"
        f"2. MESSAGES: Open a conversation with '{name}'. "
        f"Send the message: 'Hi {first}, I've updated your number to {new_ph} in my contacts. {caption}'"
    )

    verify = (
        f"Contacts '{name}' has phone={new_ph}, old value {old_ph} is gone. "
        f"Messages thread with '{name}' contains a message referencing {new_ph}. "
        f"verify_mode=db_query for contact field + thread existence for message."
    )

    return {
        "flow": "U+B_update_communicate",
        "apps": ["Contacts", "Messages"],
        "instruction": instruction,
        "verify": verify,
        "pre_populated": pre.describe(),
        "setup_commands": pre.setup_commands,
        "delta": {"field": "phone", "old": old_ph, "new": new_ph, "entity": name},
        "steps": 8,
        "expected_behavior": "complete",
        "verify_mode": "db_query+thread",
    }


def gen_search_create_communicate():
    """
    S + A + B: Search Maps → Create Calendar event → Message invite.
    3 apps: Maps + Calendar + Messages
    """
    place   = pick("maps_places")
    landmark = pick("landmark_places")
    day     = pick("day_offsets")
    time    = pick("work_times")
    contact = random.choice(C["social_contacts"])
    event   = f"Meet at {place}"
    cal     = pick("calendar_names")

    search = SearchParam(
        key        = "place_address",
        query      = f"{place} near {landmark}",
        search_app = "Maps",
        result_type= "address",
        known_result= None,
        required   = True,
    )

    instruction = (
        f"1. MAPS: Search for '{place}' near {landmark}. Note the address.\n\n"
        f"2. CALENDAR: Create '{event}' on {day} at {time} in '{cal}'. "
        f"Set location to the Maps address. Add '{contact}' as attendee.\n\n"
        f"3. MESSAGES: Share the Calendar event to '{contact}' via Messages. "
        f"Add: 'Looking forward to it!'"
    )

    verify = (
        f"Maps: search for '{place}' near {landmark} was performed. "
        f"Calendar '{cal}': event='{event}' on {day} at {time}, "
        f"location=non-empty address from Maps, attendee='{contact}'. "
        f"Messages: thread with '{contact}' has event card + caption. "
        f"verify_mode=db_query (calendar location non-empty) + thread_exists (messages)."
    )

    return {
        "flow": "S+A+B_search_create_communicate",
        "apps": ["Maps", "Calendar", "Messages"],
        "instruction": instruction,
        "verify": verify,
        "search_param": {
            "query": search.query,
            "search_app": search.search_app,
            "result_type": search.result_type,
        },
        "pre_populated": [],
        "steps": 11,
        "expected_behavior": "complete",
        "verify_mode": "db_query+thread",
    }


def gen_fetch_update_communicate():
    """
    R + U + B: Fetch appointment time → Update reminder → Message contact.
    3 apps: Calendar (read) + Reminders (update) + Messages (create)
    """
    event_name  = random.choice(["Dentist","Haircut","Car Service","Doctor"])
    event_day   = pick("day_offsets")
    event_time  = pick("work_times")
    reminder_title = f"{event_name} prep"
    old_rem_time = random.choice(["8:00 AM","9:00 AM","10:00 AM"])
    new_rem_time = f"1 hour before {event_name} ({event_time})"
    contact      = random.choice(C["family_contacts"] + C["social_contacts"])
    list_name    = "Personal"

    pre = PrePopulatedData()
    # Calendar event
    pre.add("Calendar","event","title", event_name,     event_name)
    pre.add("Calendar","event","date",  event_day,      event_name)
    pre.add("Calendar","event","time",  event_time,     event_name)
    pre.setup_commands.append(
        f"xcrun simctl spawn booted calendarctl create-event "
        f"--title '{event_name}' --date '{event_day}' --time '{event_time}'"
    )
    # Reminder
    pre.add("Reminders","reminder","title",    reminder_title, reminder_title)
    pre.add("Reminders","reminder","due_time", old_rem_time,   reminder_title)
    pre.setup_commands.extend([
        f"xcrun simctl spawn booted remindersctl create-list '{list_name}'",
        f"xcrun simctl spawn booted remindersctl create-reminder "
        f"--title '{reminder_title}' --list '{list_name}' --due '{old_rem_time}'"
    ])

    instruction = (
        f"1. CALENDAR: Find the '{event_name}' appointment. "
        f"Note its date and time.\n\n"
        f"2. REMINDERS: In the '{list_name}' list, find '{reminder_title}'. "
        f"Update its due time to 1 hour before the {event_name} appointment. Save.\n\n"
        f"3. MESSAGES: Message '{contact}': "
        f"'My {event_name} is {event_day} at {event_time}. Reminder updated.'"
    )

    verify = (
        f"Calendar: '{event_name}' exists on {event_day} at {event_time} (pre-populated). "
        f"Reminders: '{reminder_title}' due_time updated (old={old_rem_time}, "
        f"new=1hr before {event_time}). "
        f"Messages: thread with '{contact}' references {event_name} and {event_time}. "
        f"verify_mode=db_query (reminders) + agent_response (calendar read) + thread_exists."
    )

    return {
        "flow": "R+U+B_fetch_update_communicate",
        "apps": ["Calendar", "Reminders", "Messages"],
        "instruction": instruction,
        "verify": verify,
        "pre_populated": pre.describe(),
        "setup_commands": pre.setup_commands,
        "delta": {
            "field": "due_time",
            "old": old_rem_time,
            "new": f"1 hour before {event_time}",
            "entity": f"reminder '{reminder_title}'"
        },
        "steps": 10,
        "expected_behavior": "complete",
        "verify_mode": "db_query+agent_response+thread",
    }


# ─────────────────────────────────────────────────────────────────────────────
#  DEMO OUTPUT
# ─────────────────────────────────────────────────────────────────────────────

def show(t, label=""):
    if hasattr(t, '__dict__'):
        d = t.__dict__
    else:
        d = t
    apps = " + ".join(d.get("apps", []))
    flow = d.get("flow","")
    steps = d.get("steps", "?")
    print(f"\n{'═'*66}")
    print(f"  {label}  [{flow}]  {apps}  ~{steps} steps")
    print(f"{'─'*66}")
    if d.get("pre_populated"):
        print("PRE-POPULATED:")
        items = d["pre_populated"]
        if isinstance(items, list):
            for p in items:
                print(f"  ✦ {p}")
    if d.get('search_param'):
        sp = d['search_param']
        if hasattr(sp, 'query'):
            print(f"SEARCH: query='{sp.query}' via {sp.search_app} → {sp.result_type}")
        else:
            print(f"SEARCH: {sp}")
    if d.get("delta"):
        dt = d["delta"]
        print(f"DELTA: {dt['entity']} {dt['field']}: '{dt['old']}' → '{dt['new']}'")
    print(f"\nINSTRUCTION\n{d.get('instruction','')}")
    print(f"\nVERIFY\n{d.get('verify','')}")
    vm = d.get("verify_mode","")
    if vm: print(f"\nVERIFY MODE: {vm}")


if __name__ == "__main__":
    random.seed(7)
    print("\n" + "█"*66)
    print("  NEW FLOW TYPES: Search, Fetch, Update")
    print("█"*66)

    print("\n\n── FLOW S: SEARCH THEN ACT ──────────────────────────────────")
    show(gen_maps_search_then_calendar(),   "S1")
    show(gen_safari_search_then_notes(),    "S2")
    show(gen_maps_search_then_contacts(),   "S3")

    print("\n\n── FLOW R: FETCH / READ ─────────────────────────────────────")
    for task in [gen_fetch_calendar_events(),
                 gen_fetch_specific_appointment(),
                 gen_fetch_contact_info()]:
        d = task.__dict__
        show({
            "flow": "R_fetch",
            "apps": task.apps,
            "steps": task.steps,
            "pre_populated": task.setup_description,
            "instruction": task.instruction,
            "verify": task.verify,
            "verify_mode": task.verify_mode,
        }, "R")

    print("\n\n── FLOW U: UPDATE ───────────────────────────────────────────")
    for task in [gen_update_contact_phone(),
                 gen_update_calendar_event_time(),
                 gen_update_reminder_due_date()]:
        show({
            "flow": "U_update",
            "apps": task.apps,
            "steps": task.steps,
            "pre_populated": [e for e in task.pre_populated.describe()],
            "delta": {"entity": task.delta.entity,
                      "field":  task.delta.field_name,
                      "old":    task.delta.old_value,
                      "new":    task.delta.new_value},
            "instruction": task.instruction,
            "verify": task.verify,
            "verify_mode": "db_query",
        }, "U")

    print("\n\n── MULTI-APP COMBINATIONS ───────────────────────────────────")
    show(gen_fetch_then_communicate(),      "R+B")
    show(gen_update_then_communicate(),     "U+B")
    show(gen_search_create_communicate(),   "S+A+B")
    show(gen_fetch_update_communicate(),    "R+U+B")

