"""Per-scaffold difficulty estimate vs. success — analysis of the
26-task headline comparison.

For each task in the headline slate, compute TWO difficulty estimates
(one for the UI scaffold, one for the API scaffold) and one
"steps" estimate per scaffold. Then cross-reference with the
empirical PASS/FAIL from the API and UI runs.

The UI estimates are already on `Task.steps` and `Task.complexity`
(set by each generator; computed via `complexity_score(...)` in
sibb_task_generator_v3.py). They reflect the count and difficulty of
UI gestures.

The API estimates are computed analytically here. For `ui_only` tasks
the API estimate is `+inf` — no public Apple SDK call sequence
mutates the verifier-checked state, so an API agent cannot reach the
target regardless of how many calls it makes. For `api_only` /
`hybrid` tasks, the heuristic counts the minimum number of Apple-SDK
calls an oracle agent would need:

  - 1 for the principal action (create/update/delete/list)
  - +1 if the task references an existing entity by name (the agent
    must list_* to resolve the entity's identifier)
  - +(n_apps - 1) cross-app coordination overhead (each extra app =
    one extra SDK surface)
  - +1 if the task expects an `agent.answer` payload (read task)

Output: a CSV at sibb/api_baseline/results/difficulty_vs_success.csv
plus an ASCII summary printed to stdout (per-scaffold pass rate by
difficulty bin + point-biserial correlation).
"""

from __future__ import annotations

import csv
import json
import math
import pathlib
import random
import sys
from typing import Any, Dict, List, Optional, Tuple

_HERE = pathlib.Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "sibb" / "benchmark"))
sys.path.insert(0, str(_REPO_ROOT / "sibb" / "simulator"))

from sibb_replay import GENERATORS  # noqa: E402
from sibb.api_baseline.sibb_api_runner import parse_classification_slate  # noqa: E402


# ---------------------------------------------------------------------------
# API estimate heuristic
# ---------------------------------------------------------------------------


def estimate_api_steps(task: Any, cls: str) -> float:
    """Analytical estimate of the minimum number of Apple-SDK calls
    an oracle agent would need. `+inf` for `ui_only`.

    The heuristic is intentionally simple so the analysis is
    reproducible from the task object alone — no run data, no model
    behavior. Calibration ranges checked against the empirical
    `tool_calls_made` of the 15 passing API episodes in the headline.
    """
    if cls == "ui_only":
        return math.inf

    instr = task.instruction.lower()
    n_apps = max(1, len(task.apps))

    # Base: the principal action (1 SDK call).
    n = 1

    # Find-target overhead — tasks that reference an EXISTING entity by
    # name require an extra list_* call to resolve the identifier.
    # We trigger on the phrases the generators use to indicate this.
    references_existing = any(p in instr for p in (
        "find ",                  # "Find Chris Webb"
        "in the '", "in '",       # "in 'Side Projects'"
        "your contacts",           # "if they're already in your contacts"
        "the latest message",     # cross-app read tasks
        "what's the location",
        "what is",
        "tell me ",
    ))
    if references_existing:
        n += 1

    # Cross-app coordination — each app beyond the first is roughly one
    # additional SDK surface to call.
    n += max(0, n_apps - 1)

    # Read tasks emit agent.answer — counted as 1 call.
    is_read = any(p in instr for p in (
        "tell me", "output your final answer", "what's the",
        "what is the", "find the next", "what events",
    ))
    if is_read:
        n += 1

    return float(n)


def estimate_api_complexity(api_steps: float, n_apps: int) -> float:
    """v1 — call-count only. Same shape as `complexity_score` in
    sibb_task_generator_v3.py but applied on the API-side step
    count. Kept for backward comparison; `estimate_api_complexity_v2`
    is the active heuristic."""
    if math.isinf(api_steps):
        return math.inf
    score = api_steps / 4.0
    score += 0.8 * max(0, n_apps - 1)
    return round(score, 2)


def estimate_api_complexity_v2(api_steps: float, n_apps: int,
                                  task: Any) -> float:
    """v2 — call count + parameter-specification difficulty.

    The v1 estimate treated all API calls as equally hard, which
    misrepresented CREATE tasks (1 call but many fields to specify)
    relative to READ tasks (more calls but trivial parameters).
    Empirically the v1 estimate was anti-correlated with success on
    the API scaffold — a paper-grade signal that workflow depth is
    NOT the bottleneck for API-driven agents, but parameter-
    specification accuracy might be.

    v2 adds a per-field weight using the verifier's `attribute_eq`
    checks as the proxy for "fields the agent must produce
    correctly." A 12-field contact create scores higher than a
    1-field read.

    Weights tuned to keep v1 and v2 on comparable scales:
      base    = api_steps / 4.0           (call-count term, as v1)
      params  = 0.25 * n_attribute_eq     (per-field specification)
      multi   = 0.8  * (n_apps - 1)       (cross-app coordination)
    """
    if math.isinf(api_steps):
        return math.inf
    n_attr = sum(1 for c in (task.verify_checks or [])
                 if c.get("kind") == "attribute_eq")
    score = api_steps / 4.0
    score += 0.25 * n_attr
    score += 0.8 * max(0, n_apps - 1)
    return round(score, 2)


# ---------------------------------------------------------------------------
# Stats helpers (no scipy — point-biserial via Pearson formula)
# ---------------------------------------------------------------------------


def point_biserial(values: List[float], outcomes: List[bool]
                     ) -> Tuple[float, int]:
    """Pearson correlation of a numeric series with a 0/1 outcome
    series. Drops `inf` / `nan`. Returns (r, n_effective)."""
    pairs = [(v, 1.0 if o else 0.0)
             for v, o in zip(values, outcomes)
             if math.isfinite(v)]
    n = len(pairs)
    if n < 3:
        return float("nan"), n
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    dx = math.sqrt(sum((xs[i] - mx) ** 2 for i in range(n)))
    dy = math.sqrt(sum((ys[i] - my) ** 2 for i in range(n)))
    if dx == 0 or dy == 0:
        return float("nan"), n
    return num / (dx * dy), n


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    api_doc = json.load(open(
        _REPO_ROOT / "sibb" / "api_baseline" / "results"
        / "headline_v5_26tasks.json"))
    ui_doc = json.load(open(
        _REPO_ROOT / "sibb" / "api_baseline" / "results"
        / "headline_ui_v5_26tasks.json"))
    api_by = {r["runner_key"]: r for r in api_doc["results"]}
    ui_by = {r["runner_key"]: r for r in ui_doc["results"]}

    slate = parse_classification_slate()

    rows: List[Dict[str, Any]] = []
    for entry in slate:
        gen_fn, _ = GENERATORS[entry.runner_key]
        random.seed(0)
        task = gen_fn()
        ui_steps = task.steps
        ui_complexity = task.complexity
        api_steps = estimate_api_steps(task, entry.cls)
        api_complexity_v1 = estimate_api_complexity(
            api_steps, len(task.apps))
        api_complexity_v2 = estimate_api_complexity_v2(
            api_steps, len(task.apps), task)
        n_attribute_eq = sum(1 for c in (task.verify_checks or [])
                              if c.get("kind") == "attribute_eq")
        api_pass = api_by[entry.runner_key]["passed"]
        ui_pass = ui_by[entry.runner_key]["passed"]
        rows.append({
            "runner_key": entry.runner_key,
            "cls": entry.cls,
            "subset": entry.subset,
            "n_apps": len(task.apps),
            "ui_steps": ui_steps,
            "ui_complexity": ui_complexity,
            "api_steps": "inf" if math.isinf(api_steps) else int(api_steps),
            "n_attribute_eq": n_attribute_eq,
            "api_complexity_v1": "inf" if math.isinf(api_complexity_v1)
                                  else api_complexity_v1,
            "api_complexity_v2": "inf" if math.isinf(api_complexity_v2)
                                  else api_complexity_v2,
            "api_pass": api_pass,
            "ui_pass": ui_pass,
        })

    csv_path = (_REPO_ROOT / "sibb" / "api_baseline" / "results"
                / "difficulty_vs_success.csv")
    with open(csv_path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    print(f"Wrote {csv_path}\n")

    # --- ASCII summary ----------------------------------------------
    print("Per-task table (API columns: steps, n_attr_eq, cplx_v1, "
          "cplx_v2):")
    print(f"  {'task':<35} {'cls':<10} "
          f"{'UI[steps,cplx]':<18} "
          f"{'API[steps,attr,v1,v2]':<28} "
          f"{'API':>4} {'UI':>4}")
    for r in rows:
        ui_est = f"{r['ui_steps']:>2},{r['ui_complexity']:>5.1f}"
        if r['api_steps'] == "inf":
            api_est = f"{'inf':>3},{'-':>3},{'inf':>5},{'inf':>5}"
        else:
            api_est = (f"{r['api_steps']:>3},"
                        f"{r['n_attribute_eq']:>3},"
                        f"{r['api_complexity_v1']:>5},"
                        f"{r['api_complexity_v2']:>5}")
        print(f"  {r['runner_key']:<35} {r['cls']:<10} "
              f"{ui_est:<18} {api_est:<28} "
              f"{('PASS' if r['api_pass'] else 'FAIL'):>4} "
              f"{('PASS' if r['ui_pass'] else 'FAIL'):>4}")

    # --- Pass-rate by difficulty bin --------------------------------
    print("\nPass rate by UI complexity bin (UI scaffold):")
    bins = [(0, 2.5), (2.5, 3.5), (3.5, 4.5), (4.5, 99)]
    for lo, hi in bins:
        rs = [r for r in rows if lo <= r["ui_complexity"] < hi]
        if not rs:
            continue
        p = sum(1 for r in rs if r["ui_pass"])
        print(f"  cplx [{lo:>4.1f}, {hi:>4.1f}):  "
              f"n={len(rs):>2}  pass={p:>2}  "
              f"rate={100*p/len(rs):>5.1f}%")

    print("\nPass rate by API estimated steps (API scaffold,"
          " api_only only):")
    api_only_rows = [r for r in rows if r["cls"] == "api_only"]
    by_steps: Dict[int, List[Dict[str, Any]]] = {}
    for r in api_only_rows:
        s = r["api_steps"] if isinstance(r["api_steps"], int) else None
        if s is not None:
            by_steps.setdefault(s, []).append(r)
    for s in sorted(by_steps):
        rs = by_steps[s]
        p = sum(1 for r in rs if r["api_pass"])
        print(f"  api_steps={s}:  n={len(rs):>2}  pass={p:>2}  "
              f"rate={100*p/len(rs):>5.1f}%")

    # --- Point-biserial correlations -------------------------------
    print("\nPoint-biserial correlations (success vs. that "
          "scaffold's difficulty):")

    # UI scaffold vs UI complexity, all 26
    r_ui, n_ui = point_biserial(
        [r["ui_complexity"] for r in rows],
        [r["ui_pass"] for r in rows])
    print(f"  UI scaffold success vs UI  complexity   "
            f"(all 26)  r = {r_ui:>+.3f}  n={n_ui}")
    r_ui_s, _ = point_biserial(
        [float(r["ui_steps"]) for r in rows],
        [r["ui_pass"] for r in rows])
    print(f"  UI scaffold success vs UI  steps        "
            f"(all 26)  r = {r_ui_s:>+.3f}  n={n_ui}")

    # API scaffold vs API estimates, on the FINITE subset
    # (api_only + hybrid)
    finite_rows = [
        r for r in rows
        if isinstance(r["api_complexity_v1"], float)
        and math.isfinite(r["api_complexity_v1"])
    ]
    r_api_v1, n_api = point_biserial(
        [float(r["api_complexity_v1"]) for r in finite_rows],
        [r["api_pass"] for r in finite_rows])
    print(f"  API scaffold success vs API complexity v1   "
            f"(api-doable only)  r = {r_api_v1:>+.3f}  n={n_api}  "
            f"[call-count only]")
    r_api_v2, _ = point_biserial(
        [float(r["api_complexity_v2"]) for r in finite_rows],
        [r["api_pass"] for r in finite_rows])
    print(f"  API scaffold success vs API complexity v2   "
            f"(api-doable only)  r = {r_api_v2:>+.3f}  n={n_api}  "
            f"[+ n_attribute_eq weight]")
    r_api_s, _ = point_biserial(
        [float(r["api_steps"]) for r in finite_rows
         if isinstance(r["api_steps"], int)],
        [r["api_pass"] for r in finite_rows
         if isinstance(r["api_steps"], int)])
    print(f"  API scaffold success vs API steps           "
            f"(api-doable only)  r = {r_api_s:>+.3f}  n={n_api}")
    r_api_attr, _ = point_biserial(
        [float(r["n_attribute_eq"]) for r in finite_rows],
        [r["api_pass"] for r in finite_rows])
    print(f"  API scaffold success vs n_attribute_eq      "
            f"(api-doable only)  r = {r_api_attr:>+.3f}  n={n_api}  "
            f"[isolated param-count signal]")

    # Sanity: api_only restricted to UI scaffold vs UI complexity
    api_only_rows = [r for r in rows if r["cls"] == "api_only"]
    r_ui_apionly, n_apionly = point_biserial(
        [r["ui_complexity"] for r in api_only_rows],
        [r["ui_pass"] for r in api_only_rows])
    print(f"  UI scaffold success vs UI  complexity   "
            f"(api-doable only)  r = {r_ui_apionly:>+.3f}  n={n_apionly}")

    print()
    print("Interpretation guide:")
    print("  r < 0  → higher difficulty → lower success "
            "(the expected sign)")
    print("  r ≈ 0  → difficulty estimate uninformative for this "
            "scaffold")
    print("  r > 0  → INVERSE: scaffold succeeds more on harder tasks "
            "(model artifact)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
