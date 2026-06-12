"""Probe: stream the iOS Safari AX tree while a human (or agent)
interacts with our RSVP form.

Usage
-----
1. Boot SIBB-Demo sim. Make sure the XCUITest server is built (one-time
   `./sibb_xcuitest_setup.sh <UDID>` from sibb/simulator/).
2. In one terminal:
       python3 sibb/simulator/sibb_probe_safari_form_ax.py <UDID>
   That spawns a MockSite serving the RSVP form, opens Safari to it,
   and starts dumping AX snapshots every 500ms to
       /tmp/sibb_ax_probe.log
3. In another window, start a screen recording of the sim
   (Cmd+Shift+5 → record selected window).
4. Interact with the form on the sim: tap fields, type values, tap
   Submit. Note the wall-clock time at each step.
5. Stop the probe with Ctrl+C. Compare the recording to the AX log:
   * Does the submit button's `@(x,y)` change when Safari auto-zooms?
   * Does the button's position in the AX log match where it's
     painted on screen at the same timestamp?
   * What happens to the AX tree between the moment you tap submit
     and the moment the page transitions (if it does)?

What we expect to find: either
  (a) AX coords reflect the post-zoom position correctly — then the
      submit-not-firing bug is in WebKit's TAP→click→submit chain,
      not in coordinate accuracy. We move to a scaffold-side fix
      (e.g. fall back to keyboard Enter to submit).
  (b) AX coords lag or never update for the zoom — then the AX layer
      and the hit-testing layer in WKWebView are out of sync. We
      need to either re-poll AX with a delay, or add a "re-snapshot
      after pause" pass before TAP.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

# Repo paths.
_THIS = Path(__file__).resolve()
_ROOT = _THIS.parents[2]
sys.path.insert(0, str(_ROOT / "sibb" / "benchmark"))
sys.path.insert(0, str(_ROOT / "sibb" / "simulator"))

LOG_PATH = Path("/tmp/sibb_ax_probe.log")
POLL_INTERVAL_S = 0.5


async def main(udid: str) -> int:
    # 1. Spawn a MockSite with our rsvp_event page mounted at /event.
    # MockSite resolves callables from PAGE_REGISTRY (string template
    # names are reserved for the spec-apply path).
    import harness_pages  # noqa: F401 — registers rsvp_event
    from harness_layout import PAGE_REGISTRY
    from sibb_mock_site import MockSite, open_in_safari, list_sites
    rsvp_fn = PAGE_REGISTRY["rsvp_event"]
    site = MockSite(
        site_id="ax-probe",
        static_pages={"/event": rsvp_fn},
    )
    site.page_seed = 42  # same seed as the failing sim run
    site.start()
    print(f"[probe] MockSite up at {site.base_url}/event")

    # 2. Friendly URL via the DNS resolver if installed; else loopback.
    import sibb_dns_resolver
    sibb_dns_resolver.start_if_needed()
    if sibb_dns_resolver.resolver_is_installed():
        url = f"http://rsvp.test:{site.port}/event"
    else:
        url = f"{site.base_url}/event"
        print("[probe] /etc/resolver/test not installed — using "
              "numeric URL.")
    print(f"[probe] opening Safari → {url}")
    open_in_safari(udid, url)

    # 3. Connect XCUITest client + start polling AX.
    from sibb_xcuitest_client import XCUITestReader
    reader = XCUITestReader(udid, bundle_id="com.apple.mobilesafari")
    await reader.start()

    print(f"[probe] writing AX snapshots to {LOG_PATH}")
    print("[probe] now: start screen recording on the sim and "
          "interact with the form. Ctrl+C to stop.")

    LOG_PATH.write_text("")  # truncate
    start = time.time()
    try:
        while True:
            t_rel = time.time() - start
            try:
                snap = await reader.observe()
            except Exception as e:
                with LOG_PATH.open("a") as fh:
                    fh.write(f"\n=== t={t_rel:7.3f}s ERROR {e!r} ===\n")
                await asyncio.sleep(POLL_INTERVAL_S)
                continue

            # Flatten the snapshot into one line per element of interest.
            keywords = ("send", "submit", "confirm", "name", "email",
                         "phone", "attending", "rsvp", "response",
                         "address", "going", "registrant", "badge",
                         "contact", "form-error", "alert", "submitted")
            kb_top = (snap.keyboard_frame or {}).get("y") if hasattr(
                snap, "keyboard_frame") else None
            lines = [f"=== t={t_rel:7.3f}s els={len(snap.elements)} "
                     f"kb_visible={snap.keyboard_visible} "
                     f"kb_top={kb_top} ==="]
            for el in snap.elements:
                lbl = (el.label or "").lower()
                val = (el.value or "").lower()
                if not any(k in lbl or k in val for k in keywords):
                    continue
                fr = el.frame
                if fr is None:
                    continue
                cx = round(fr.center_x)
                cy = round(fr.center_y)
                lines.append(
                    f"  @{el.ref} [{el.role}] label={el.label!r} "
                    f"value={el.value!r} "
                    f"center=({cx},{cy}) "
                    f"frame=({fr.x:.0f},{fr.y:.0f} "
                    f"{fr.width:.0f}x{fr.height:.0f}) "
                    f"focused={el.focused} "
                    f"hittable={el.hittable}")
            with LOG_PATH.open("a") as fh:
                fh.write("\n".join(lines) + "\n")

            await asyncio.sleep(POLL_INTERVAL_S)
    except KeyboardInterrupt:
        print(f"\n[probe] stopped; AX log → {LOG_PATH}")
        return 0
    finally:
        try:
            await reader.close()
        except Exception:
            pass
        site.stop()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: sibb_probe_safari_form_ax.py <UDID>",
              file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1])))
