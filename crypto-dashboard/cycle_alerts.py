"""
Diff two cycle.json snapshots and emit an alert when anything changed state:
a bottom-checklist signal firing or un-firing, a gate closing/reopening/being
killed, or a top-level flag (all-closed flip, kill, starter, early warning)
toggling. The workflows run this after every cycle refresh and deliver any
alert as a GitHub issue (GitHub emails watchers) plus optional ntfy push.

CLOSE<->NOT_FIRED churn on the bottom checklist is deliberately ignored —
only transitions in or out of FIRED are worth waking anyone for.

Usage: python cycle_alerts.py <prev.json> <new.json> <out.md>
Writes out.md (first line = alert title) only if something changed.
Always exits 0 — alerting must never fail the scan.
"""

import json
import sys


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def by_key(container, field):
    return {s["key"]: s.get(field) for s in (container or [])
            if isinstance(s, dict) and "key" in s}


def main():
    prev_path, new_path, out_path = sys.argv[1:4]
    prev, new = load(prev_path), load(new_path)
    if not prev or not new or new.get("error"):
        print("nothing to diff")
        return 0

    lines = []

    # Bottom checklist: only in/out of FIRED
    ps = by_key(prev.get("signals"), "status")
    names = by_key(new.get("signals"), "name")
    vals = by_key(new.get("signals"), "value")
    for k, now in by_key(new.get("signals"), "status").items():
        was = ps.get(k)
        if was is not None and was != now and "FIRED" in (was, now):
            lines.append(f"- BOTTOM SIGNAL {names.get(k, k)}: {was} -> {now} (now {vals.get(k, '?')})")

    # Gates: every status change matters (OPEN/CLOSED/PENDING/KILLED)
    pgates, ngates = (prev.get("gates") or {}), (new.get("gates") or {})
    pg = by_key(pgates.get("gates"), "status")
    gnames = by_key(ngates.get("gates"), "name")
    gvals = by_key(ngates.get("gates"), "value")
    for k, now in by_key(ngates.get("gates"), "status").items():
        was = pg.get(k)
        if was is not None and was != now:
            lines.append(f"- GATE {gnames.get(k, k)}: {was} -> {now} ({gvals.get(k, '?')})")

    # Top-level flags
    flags = [("all_closed", "ALL FOUR GATES CLOSED - the flip condition"),
             ("killed", "GATES KILLED - inverse tripped, lower zones live again"),
             ("starter", "STARTER armed (2nd weekly close above the 50W EMA)"),
             ("early_warning", "EARLY WARNING (spot-led grind + rising ETF weeks)")]
    for flag, label in flags:
        was, now = pgates.get(flag), ngates.get(flag)
        if was is not None and now is not None and was != now:
            lines.append(f"- {label}: {'ON' if now else 'off'}")

    if not lines:
        print("no state changes")
        return 0

    price = new.get("price") or 0
    title = (f"Cycle alert: {len(lines)} change{'s' if len(lines) > 1 else ''} - "
             f"BTC ${price:,.0f}, gates {ngates.get('closed', '?')}/4, "
             f"bottom {new.get('fired', '?')}/{new.get('scored', '?')}")
    body = [title, ""]
    body += lines
    body += ["", f"Gates verdict: {ngates.get('verdict', '')}",
             f"Checklist verdict: {new.get('verdict', '')}",
             "", "Dashboard: https://cryptoclauderesearch.netlify.app/",
             "", "_Automated by the cycle refresh. Not investment advice._"]
    with open(out_path, "w") as f:
        f.write("\n".join(body) + "\n")
    print("\n".join(body))
    return 0


if __name__ == "__main__":
    sys.exit(main())
