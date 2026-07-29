"""
ntfy push notifications for the nightly scan.

Compares tonight's cycle.json / data.json against the previous run and
POSTs to https://ntfy.sh/<NTFY_TOPIC> only when something changed:

  - a cycle bottom-signal newly flips to FIRED
  - BTC crosses a referee line (reclaim ~$69k / capitulation = realized price)
  - the Oct 4 - Nov 20 template window opens
  - the screener regime changes (ACCUMULATE / ROTATE / DISTRIBUTE)
  - a new coin enters the screener top 10

Silent when nothing changed. No-ops entirely if NTFY_TOPIC is unset, so the
workflow is safe to run before the secret exists.

Setup (one time):
  1. Install the ntfy app (iOS/Android) or use ntfy.sh in a browser.
  2. Subscribe to a topic with a random, unguessable name (the topic name
     IS the password — e.g. dmn-crypto-x7Q9v2Lp4T).
  3. Add it as a GitHub Actions secret named NTFY_TOPIC.

Bonus: point your TradingView Pine alerts' Webhook URL (paid TV plans) at
https://ntfy.sh/<same topic> and zone entries land in the same feed.
"""

import json
import os
import sys

import requests

BASE = os.path.dirname(__file__)
WEB = os.path.join(BASE, "..", "web")


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _normalize_topic(raw):
    """Accept a bare topic OR a pasted full URL; return the bare topic."""
    t = raw.strip().rstrip("/")
    if "/" in t:
        t = t.split("/")[-1]
    return t


def _push(topic, title, message, priority="default", tags=""):
    try:
        r = requests.post(f"https://ntfy.sh/{topic}", data=message.encode("utf-8"),
                          headers={"Title": title, "Priority": priority, "Tags": tags},
                          timeout=15)
        if r.status_code == 200:
            print(f"ntfy sent (HTTP 200): {title}")
        else:
            print(f"ntfy REJECTED (HTTP {r.status_code}): {title} — {r.text[:120]}")
    except Exception as e:
        print(f"ntfy send failed: {e}", file=sys.stderr)


def main():
    topic = _normalize_topic(os.getenv("NTFY_TOPIC", ""))
    if not topic:
        print("NTFY_TOPIC not set — skipping notifications")
        return
    print(f"topic loaded ({len(topic)} chars)")

    if os.getenv("NTFY_TEST") == "1":
        _push(topic, "Test: crypto pipeline connected",
              "If you can read this, the GitHub -> ntfy path works end-to-end. "
              "Real alerts fire on cycle signals, referee lines, regime changes "
              "and screener top-10 movers.", priority="high", tags="white_check_mark")
        return

    cyc = _load(os.path.join(WEB, "cycle.json"))
    cyc_prev = _load(os.path.join(WEB, "cycle_prev.json"))
    scr = _load(os.path.join(WEB, "data.json"))
    scr_prev = _load(os.path.join(WEB, "data_prev.json"))

    # ── Cycle monitor events ────────────────────────────────────────────
    if cyc and not cyc.get("error"):
        prev_status = {s["key"]: s["status"] for s in (cyc_prev or {}).get("signals", [])}
        for s in cyc.get("signals", []):
            if s["status"] == "FIRED" and prev_status.get(s["key"]) not in (None, "FIRED"):
                _push(topic, f"CYCLE SIGNAL FIRED: {s['name']}",
                      f"{s['value']} — {s['detail']}\n"
                      f"BTC ${cyc['price']:,.0f} · {cyc['fired']}/{cyc['scored']} signals fired",
                      priority="high", tags="rotating_light")

        ref = cyc.get("referee", {})
        price = cyc.get("price")
        prev_price = (cyc_prev or {}).get("price")
        if price and prev_price:
            if price >= ref.get("reclaim_line", 9e9) > prev_price:
                _push(topic, "BTC RECLAIMED THE $69K LINE",
                      f"BTC ${price:,.0f} closed above the STH cost basis. "
                      "With sustained ETF inflows this favors 'bottom already in' — "
                      "review T1 deployment.", priority="urgent", tags="chart_with_upwards_trend")
            if price <= ref.get("capitulation_line", 0) < prev_price:
                _push(topic, "BTC BROKE THE CAPITULATION LINE",
                      f"BTC ${price:,.0f} below realized price ${ref['capitulation_line']:,.0f}. "
                      "Bear path confirmed — the $45-55k template zone is live. "
                      "T2/T3 ladders are the plan.", priority="urgent", tags="rotating_light")

        w = cyc.get("window", {})
        if w.get("in_window") and not (cyc_prev or {}).get("window", {}).get("in_window"):
            _push(topic, "Q4 TEMPLATE WINDOW OPEN",
                  f"Oct 4 - Nov 20 bottom window has begun. BTC ${price:,.0f}, "
                  f"{cyc['fired']}/{cyc['scored']} bottom signals fired. "
                  "Valuation signals firing inside the window = maximum-deployment zone.",
                  priority="high", tags="calendar")

    # ── Screener events ─────────────────────────────────────────────────
    if scr and not scr.get("error"):
        reg = (scr.get("regime") or {}).get("label")
        reg_prev = ((scr_prev or {}).get("regime") or {}).get("label")
        if reg and reg_prev and reg != reg_prev:
            _push(topic, f"REGIME CHANGE: {reg_prev} → {reg}",
                  (scr.get("regime") or {}).get("note", ""),
                  priority="high", tags="arrows_counterclockwise")

        top_now = [(r["id"], r["symbol"]) for r in (scr.get("rows") or [])[:10]]
        prev_ids = {r["id"] for r in ((scr_prev or {}).get("rows") or [])[:10]}
        if prev_ids:
            new = [sym for cid, sym in top_now if cid not in prev_ids]
            if new:
                _push(topic, f"Screener top-10 new entrant{'s' if len(new) > 1 else ''}: {', '.join(new)}",
                      "Climbed into the top 10 overnight — research-queue candidates.",
                      tags="mag")

    print("notify pass complete")


if __name__ == "__main__":
    main()
