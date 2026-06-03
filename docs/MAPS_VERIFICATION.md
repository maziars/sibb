# Maps verification — what's queryable, what isn't

Status: research log from 2026-05-28. Captures what we learned
empirically about iOS 26.3 Maps' DB state for verifier design.
Update when new findings land.

Probe used: [`sibb/simulator/sibb_probe_maps_directions.py`](../simulator/sibb_probe_maps_directions.py)

## What works today (shipped)

### "Did the agent commit a directions route?" → ✅ verifiable

The check is:
```python
{"kind": "count",
 "resource": "maps.history",
 "selector": {"z_ent": 16, "min_create_iso": "$baseline_iso"},
 "op": ">=", "n": 1}
```

`z_ent=16` is `HistoryDirectionsItem` in the `ZHISTORYITEM` table.
Created when the agent taps the **"X min, driving"** button on the
place card (not on the route options screen — that screen's "Steps"
buttons don't commit).

**Async-write race**: iOS Maps writes the row ~2-3s after the UI
action. The verifier runs immediately on episode end, so the row
may not be flushed when we read it. Fixed in `_fetch_maps_history`
with a bounded retry-when-empty loop (5s cap, 500ms cadence, only
when `min_create_iso` is in the selector). See
`test_maps_history_async_retry.py` for edge-case coverage.

### "Did the agent navigate to a place / search for it?" → ✅ verifiable
`z_ent=20` (HistoryPlaceItem) and `z_ent=22` (HistorySearchItem)
work the same way; ZQUERY column populated for `z_ent=22`,
lat/lon/muid for `z_ent=20`.

## Deferred — needs protobuf parsing

### "Did the agent pick driving / walking / transit / cycling?" → ⚠️ doable, not shipped

Empirical byte-diff between scenarios (probe 2026-05-28):

| Scenario A vs B | Byte-diff count | Concentrated where |
|---|---|---|
| WALK vs CYCLE | 100 bytes | offsets 3628-3645 (route geometry) |
| WALK vs TRANSIT | 0 bytes (Transit didn't commit cleanly in probe) | — |
| DRIVING vs DRIVING+AVOID_TOLLS | 6766 bytes | offsets 1, 4, 130-148 then full route geometry |

The mode enum is in `ZROUTEREQUESTSTORAGE` (a protobuf blob). The
first byte after the outer length prefix is consistently `08 04`
which suggests `field 1, varint 4`. Mode bytes are likely at small
offsets (≤ 10 bytes into the payload).

To verify mode, we'd need to either:
- Write a small protobuf reader (~50 lines — parse varint tags +
  pick field 1 from the inner message)
- Or hardcode "byte at offset N must equal {drive:0, walk:1,
  transit:2, cycle:3, ride:4}" after one more careful diff run

**Status**: not implemented. No current task requires mode-specific
verification.

### "Did the agent pick the fastest route?" → ❌ blocked by AX gap

The "Fastest" label is rendered on the route option card by iOS
Maps but does NOT appear in the AX tree. Agent can't see it,
verifier can't see which route was picked.

Route SELECTION (which of N route options the user picked) doesn't
appear to be encoded in `z_ent=16` row's queryable columns either —
it's likely in `ZROUTEREQUESTSTORAGE` but route geometry differs so
much per-route that diffs aren't useful for identifying "selection".

**Status**: blocked. Even if we wrote the protobuf parser, the
agent has no AX signal for which route to pick. Would need VLM
enrichment of the route cards.

### "Did the agent avoid tolls / highways?" → ⚠️ doable, not shipped

DRIVING+AVOID_TOLLS vs DRIVING bytes differ at offsets 1, 4,
130-148 (small payload changes) plus 6700+ bytes of recomputed
route geometry. The `avoid_tolls: bool` flag in the route request
likely lives at one of those small-offset diff points.

The toggle is in iOS Maps' UI (the "Avoid" button on the route
options screen → sheet with "Avoid Tolls" / "Avoid Highways"
switches), so the agent CAN access it.

**Status**: implementable with the same protobuf parser as mode.
No current task requires it.

## Doesn't help us

- `ZUSERROUTE` table — separate table for "saved" routes (named/favorited).
  Never populates from the route-commit flow. Won't help.
- `ZNAVIGATIONINTERRUPTED` column — flag for "user backed out of
  navigation mid-trip". Not relevant to verifier of route-commit.
- `Z_6PLACES`, `Z_PRIMARYKEY` etc. — Core Data internals.

## Operating notes

- **Sim flakiness**: running this probe many times in one session
  exhausts the test-runner spawn cycle. Symptoms: XCUITest server
  ready-timeout, broken-pipe errors, sim shutting down on its own.
  Mitigation: hard reset (`killall -9 Simulator + simctl shutdown
  all + reboot`) between probe iterations.
- **`maps://?q=...` vs `?daddr=...`**: `?q=` lands the user on a
  search results screen + place card; `?daddr=` lands directly on
  route options (skips the duration-button-tap action). For our
  probe to reproduce the agent's path, use `?q=`.
- **`commit_route_via_duration_button` filter**: match only buttons
  whose label has `\d+ (min|hr).*(driving|walking|...)`. Don't match
  on just the mode word — the route options screen has mode-tab
  buttons (Drive/Walk/Transit/Cycle/Ride) that would otherwise
  shadow the real duration button.
