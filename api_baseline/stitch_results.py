"""Stitch per-task results.json files from multiple runner runs into a
single headline artifact.

Use when a 26-task headline is run as multiple partial slates (e.g. a
mid-run crash recovery, or a single-task re-run after a code fix) and
the published numbers must reflect the final state.

Resolution policy: later runs OVERRIDE earlier ones on the same
runner_key. Pass runs in chronological order; the rightmost wins.

Example:
    python -m sibb.api_baseline.stitch_results \\
        --runs sibb/api_baseline/results/run_20260610_214315 \\
               sibb/api_baseline/results/run_20260610_215205 \\
               sibb/api_baseline/results/run_20260610_220649 \\
        --out  sibb/api_baseline/results/headline_stitched_26tasks.json \\
        --description "Final stitched 26-task headline"
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any, Dict, List


def stitch(run_dirs: List[pathlib.Path], description: str) -> Dict[str, Any]:
    """Merge results.json files in `run_dirs` (chronological order).

    Returns the merged headline dict. Identical runner_key entries are
    resolved by keeping the rightmost (latest) occurrence.
    """
    merged_by_key: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    provider = model = None
    for d in run_dirs:
        rj = d / "results.json"
        if not rj.exists():
            print(f"  warn: {rj} missing — skipping", file=sys.stderr)
            continue
        doc = json.load(open(rj))
        provider = provider or doc.get("provider")
        model = model or doc.get("model")
        for r in doc.get("results", []):
            key = r["runner_key"]
            if key not in merged_by_key:
                order.append(key)
            merged_by_key[key] = r
    results = [merged_by_key[k] for k in order]

    by_cls: Dict[str, List[Dict[str, Any]]] = {}
    for r in results:
        by_cls.setdefault(r["cls"], []).append(r)
    summary = {
        cls: {
            "n": len(rs),
            "pass": sum(1 for x in rs if x.get("passed")),
        }
        for cls, rs in sorted(by_cls.items())
    }

    return {
        "description": description,
        "provider": provider,
        "model": model,
        "contributing_runs": [d.name for d in run_dirs],
        "n_total": len(results),
        "n_pass": sum(1 for x in results if x.get("passed")),
        "summary_by_class": summary,
        "results": results,
    }


def main(argv: List[str] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--runs", nargs="+", required=True,
                    help="run_* directories in chronological order")
    p.add_argument("--out", required=True,
                    help="output JSON path for the stitched headline")
    p.add_argument("--description", default="Stitched headline",
                    help="free-text description recorded in the output")
    args = p.parse_args(argv)

    run_dirs = [pathlib.Path(r) for r in args.runs]
    for d in run_dirs:
        if not d.is_dir():
            print(f"error: {d} is not a directory", file=sys.stderr)
            return 2

    merged = stitch(run_dirs, args.description)
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    json.dump(merged, open(out, "w"), indent=2)

    print(f"Stitched: {merged['n_total']} tasks  "
            f"{merged['n_pass']} pass  "
            f"({100 * merged['n_pass'] / max(1, merged['n_total']):.0f}%)")
    for cls, s in merged["summary_by_class"].items():
        rate = 100 * s["pass"] / max(1, s["n"])
        print(f"  {cls:>10}: {s['pass']:>2}/{s['n']:<2}  ({rate:>3.0f}%)")
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
