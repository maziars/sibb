"""L1 fairness pin — the UI scaffold and the API scaffold see the
SAME instruction string for the same (generator, seed) pair.

Both runners go through the same `GENERATORS[gen_key][0]` callable
from `sibb_replay.py`, and both seed `random` to `args.seed` before
the call:

  - UI:  `sibb/benchmark/sibb_assistant.py:656` (random.seed) +
         line 657 (gen_fn())
  - API: `sibb/api_baseline/sibb_api_assistant.py:525-527`

So byte-equal instructions are structural. This file pins the
guarantee with a 26-element coverage sweep over the headline slate —
a future refactor that decouples seed-handling from generator-call,
or that fork the GENERATORS dict between baselines, trips the pin.

Without this pin, "API agent failed but UI agent passed (or vice
versa)" comparisons would be meaningless — the two scaffolds might
silently be running different problems on paper.
"""

from __future__ import annotations

import pathlib
import random
import sys


REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "sibb" / "benchmark"))
sys.path.insert(0, str(REPO_ROOT / "sibb" / "simulator"))

import sibb_replay as R  # noqa: E402
from sibb.api_baseline.sibb_api_runner import parse_classification_slate  # noqa: E402


def _instantiate(gen_key: str, seed: int):
    """Mirror what both runners do: seed random, call the generator."""
    gen_fn, _ = R.GENERATORS[gen_key]
    random.seed(seed)
    return gen_fn()


def test_instruction_is_deterministic_for_same_generator_same_seed():
    """Single-generator: two calls with the same seed produce the
    same instruction. If this fails, NO downstream fairness claim
    holds — the generator itself is reading entropy outside random."""
    for gen_key in ("create_event_with_title_time", "lookup_phone_by_name",
                      "maps_search_to_contact",
                      "reminder_with_calendar_event"):
        t1 = _instantiate(gen_key, seed=0)
        t2 = _instantiate(gen_key, seed=0)
        assert t1.instruction == t2.instruction, (
            f"{gen_key}: instruction not deterministic for seed=0")


def test_all_26_slate_instructions_deterministic_for_seed_0():
    """Full coverage sweep over the headline slate at seed=0. Pinned
    so a regression to non-deterministic behavior in any one generator
    surfaces as a unit failure, not a confusing mid-run divergence."""
    slate = parse_classification_slate()
    assert len(slate) == 26, "Headline slate size — bump pin if intended"
    for entry in slate:
        t1 = _instantiate(entry.runner_key, seed=0)
        t2 = _instantiate(entry.runner_key, seed=0)
        assert t1.instruction == t2.instruction, (
            f"{entry.runner_key}: instruction differs across calls "
            f"at seed=0 — UI/API baseline fairness broken")


def test_every_generator_in_replay_is_deterministic_for_seed_0():
    """Universal coverage: any new generator added to
    `sibb_replay.GENERATORS` (whether or not it's in the headline
    slate yet) MUST be deterministic for a given seed. This is the
    structural fairness guarantee — without it, UI vs API comparisons
    on that generator would silently run different problems.

    A new generator that reads time/random.SystemRandom/os.urandom
    without going through the seeded `random` module trips this.

    If a generator legitimately needs non-determinism (e.g. it pulls
    from an external API), it should NOT be in GENERATORS — it
    belongs in a separate dict that's excluded from comparison
    runs. Update the exclusion list below in that case.
    """
    # Generators excluded from the fairness-determinism contract.
    # Add here ONLY with a documented structural reason — and only
    # after verifying the non-determinism is in an instruction
    # SEGMENT that doesn't affect the task semantics.
    #
    # Safari mock-site generators below use `uuid.uuid4().hex[:8]` as
    # a per-run `site_id` to allocate a unique mock-site URL so two
    # parallel runs don't collide on the mock-site server port (see
    # sibb_mock_site.py). The site_id is a sandbox identifier; both
    # baselines see the same shape of task ("fill out the form at
    # http://127.0.0.1:<port>/...") with the same fields and
    # verifier — only the port differs. Fairness is preserved at
    # task-semantics level, not at instruction-byte level.
    EXCLUDED = {
        "safari_rsvp_form",
        "safari_rsvp_form_clipped",
        "safari_shop_pick_by_attrs",
        "safari_shop_filter_and_sort",
    }

    failures = []
    for gen_key, (gen_fn, _) in R.GENERATORS.items():
        if gen_key in EXCLUDED:
            continue
        try:
            t1 = _instantiate(gen_key, seed=0)
            t2 = _instantiate(gen_key, seed=0)
        except Exception as e:
            failures.append(f"{gen_key}: raised on instantiation "
                            f"({type(e).__name__}: {e})")
            continue
        if t1.instruction != t2.instruction:
            failures.append(
                f"{gen_key}: instruction differs across calls at "
                f"seed=0\n  call#1: {t1.instruction[:200]!r}\n"
                f"  call#2: {t2.instruction[:200]!r}")

    assert not failures, (
        f"{len(failures)} generator(s) non-deterministic at seed=0 — "
        f"comparing UI and API baselines on these would silently run "
        f"different problems:\n\n" + "\n\n".join(failures))


def test_every_generator_is_deterministic_for_multiple_seeds():
    """Belt-and-suspenders: the same determinism property must hold
    across a small set of distinct seeds. A generator that ONLY breaks
    at non-zero seeds (e.g. one that special-cases seed=0) would slip
    through `test_every_generator_in_replay_is_deterministic_for_seed_0`.
    """
    # Same exclusion list as the seed=0 test (kept in sync manually —
    # if these diverge, the structural reason for excluding has changed).
    EXCLUDED = {
        "safari_rsvp_form",
        "safari_rsvp_form_clipped",
        "safari_shop_pick_by_attrs",
        "safari_shop_filter_and_sort",
    }
    SEEDS = (0, 1, 7, 42, 100)
    failures = []
    for gen_key, (gen_fn, _) in R.GENERATORS.items():
        if gen_key in EXCLUDED:
            continue
        for seed in SEEDS:
            try:
                t1 = _instantiate(gen_key, seed=seed)
                t2 = _instantiate(gen_key, seed=seed)
            except Exception as e:
                failures.append(f"{gen_key}@seed={seed}: raised "
                                f"({type(e).__name__}: {e})")
                continue
            if t1.instruction != t2.instruction:
                failures.append(f"{gen_key}@seed={seed}: differs")
    assert not failures, (
        f"{len(failures)} (generator, seed) pair(s) non-deterministic:"
        f"\n  " + "\n  ".join(failures[:20]) + (
            "\n  ..." if len(failures) > 20 else ""))


def test_same_generators_dict_drives_both_runners():
    """Source-text pin: both baselines look up generators in the same
    sibb_replay.GENERATORS dict. If a future commit forks one side
    to a local dict, this trips.

    Equivalence is structural only when both runners (a) import the
    same dict and (b) seed `random` before calling. (b) is pinned by
    the other tests in this file; (a) is pinned here.
    """
    ui_src = (REPO_ROOT / "sibb" / "benchmark"
              / "sibb_assistant.py").read_text()
    api_src = (REPO_ROOT / "sibb" / "api_baseline"
                / "sibb_api_assistant.py").read_text()
    # Both reference GENERATORS as the dispatch table.
    assert "GENERATORS[args.generator]" in ui_src or \
            "GENERATORS[gen_key]" in ui_src
    assert "GENERATORS[args.generator]" in api_src or \
            "GENERATORS[gen_key]" in api_src
    # Both seed `random` to args.seed before calling the generator.
    assert "random.seed(args.seed)" in ui_src
    assert "random.seed(args.seed)" in api_src


def test_excluded_generators_are_only_non_deterministic_in_site_id():
    """The exclusion in `test_every_generator_in_replay_is_deterministic_*`
    only covers the `port:rsvp-<token>` / `port:shop-<token>` /
    `port:rsvp-clipped-<token>` site_id segment. If a future commit
    adds *other* non-determinism to these generators (a random field,
    a UUID elsewhere in the instruction, a clock-based timestamp), the
    exclusion would silently mask it.

    This test strips the documented site_id pattern and verifies the
    remainder is byte-stable.
    """
    import re
    SITE_ID_PATTERN = re.compile(
        r"\{port:(rsvp|shop|rsvp-clipped)-[0-9a-f]+\}")
    EXCLUDED = {
        "safari_rsvp_form",
        "safari_rsvp_form_clipped",
        "safari_shop_pick_by_attrs",
        "safari_shop_filter_and_sort",
    }
    leaks = []
    for gen_key in EXCLUDED:
        if gen_key not in R.GENERATORS:
            continue  # exclusion may outlive the generator; fine.
        t1 = _instantiate(gen_key, seed=0)
        t2 = _instantiate(gen_key, seed=0)
        scrubbed_1 = SITE_ID_PATTERN.sub("{port:<SITE_ID>}", t1.instruction)
        scrubbed_2 = SITE_ID_PATTERN.sub("{port:<SITE_ID>}", t2.instruction)
        if scrubbed_1 != scrubbed_2:
            # Find first divergence for the error message.
            for i, (a, b) in enumerate(zip(scrubbed_1, scrubbed_2)):
                if a != b:
                    ctx = scrubbed_1[max(0, i-40):i+40]
                    leaks.append(
                        f"{gen_key}: non-determinism beyond site_id at "
                        f"char {i}: ...{ctx}...")
                    break
            else:
                leaks.append(f"{gen_key}: lengths differ post-scrub")
    assert not leaks, (
        f"{len(leaks)} excluded generator(s) have non-determinism "
        f"OUTSIDE the documented site_id pattern — the exclusion is "
        f"masking new non-determinism:\n\n" + "\n\n".join(leaks))


def test_changing_seed_changes_instruction_for_randomized_generators():
    """Sanity check the seed parameter is actually plumbed: at least
    one generator must produce different instructions for seed=0 vs
    seed=1. Otherwise a buggy generator that ignores random.seed
    would silently pass the determinism tests above."""
    # gen_full_business_card picks names from a list; seed flips.
    a = _instantiate("full_business_card", seed=0)
    b = _instantiate("full_business_card", seed=1)
    assert a.instruction != b.instruction, (
        "full_business_card produces the same instruction for "
        "seeds 0 and 1 — random.seed is not actually affecting it")
