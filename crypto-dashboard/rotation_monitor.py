"""
Coin Rotation Monitor (Nadeau framework)
========================================

Tracks whether coins are rotating from hot money into strong hands — the
DeFi Report / Michael Nadeau read on when a cycle low can be trusted:

  "You don't hit that cycle low, or have conviction in it, until enough of
   these coins have rotated into stronger hands. A lot of coins changed
   hands in the final 90 days of the 2022 bear market."

Metrics (all free/keyless, from bitcoin-data.com — the BGeometrics API we
already use as the cycle monitor's on-chain fallback, since it isn't
blocked from GitHub runners like CoinMetrics):

  - LTH supply + 30d change   : long-term holders absorbing = strong hands
  - STH supply + 30d change   : hot money bleeding out
  - LTH share of supply       : how much of the float strong hands hold
  - Supply in profit %        : capitulation depth (prior lows ~40-55%)
  - STH realized price        : live version of the $69k reclaim referee

Leverage gauge (his actual first-principles trigger — "it all comes down
to leverage and credit"):

  - BTC futures open interest, 30d trend (OKX rubik stats, public)

The full cost-basis histogram (URPD) lives at charts.checkonchain.com —
linked from the panel rather than rebuilt.

Usage:
  python rotation_monitor.py     # prints summary, writes rotation_cache.json
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

CACHE_PATH = os.path.join(os.path.dirname(__file__), "rotation_cache.json")

BG = "https://bitcoin-data.com/v1"


def _get_json(url, params=None, timeout=45, retries=2):
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout,
                             headers={"User-Agent": "Mozilla/5.0 (rotation-monitor)"})
            if r.status_code == 429:
                time.sleep(10 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            if attempt == retries:
                print(f"  fetch failed: {url.split('?')[0]} -> {last_err}", file=sys.stderr)
                return None
            time.sleep(3)
    return None


_DATE_KEYS = ("d", "date", "day", "time", "theday")
_SKIP_KEYS = _DATE_KEYS + ("unixts", "timestamp")


def _parse_row(row):
    """(date_str, float_value) from a bitcoin-data.com row, tolerant of the
    per-metric value key ({"d": "2026-07-31", "lthSupply": "14500000"})."""
    if not isinstance(row, dict):
        return None
    date = None
    for k in _DATE_KEYS:
        for kk, vv in row.items():
            if kk.lower() == k and isinstance(vv, str):
                date = vv
                break
        if date:
            break
    for k, v in row.items():
        if k.lower() in _SKIP_KEYS:
            continue
        try:
            return (date, float(v))
        except (TypeError, ValueError):
            continue
    return None


def fetch_bg_series(paths, days=400):
    """Try candidate endpoint paths; return newest-last [(date, value)]."""
    for path in paths:
        data = _get_json(f"{BG}/{path}")
        if not isinstance(data, list) or not data:
            continue
        rows = [p for p in (_parse_row(r) for r in data) if p]
        if rows:
            return rows[-days:]
    return []


def fetch_okx_oi_series():
    """BTC futures open interest (USD), daily, newest-last [(ts_ms, oi)]."""
    data = _get_json("https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume",
                     params={"ccy": "BTC", "period": "1D"})
    rows = []
    for r in (data or {}).get("data", []):
        try:
            rows.append((int(r[0]), float(r[1])))
        except (IndexError, TypeError, ValueError):
            continue
    rows.sort(key=lambda x: x[0])
    return rows


def _delta_pct(series, days):
    """% change between the value `days` back and the latest."""
    if len(series) <= days:
        return None
    old, new = series[-days - 1][1], series[-1][1]
    return 100 * (new - old) / old if old else None


def run_monitor():
    lth = fetch_bg_series(["lth-supply", "long-term-holder-supply"])
    sth = fetch_bg_series(["sth-supply", "short-term-holder-supply"])
    profit = fetch_bg_series(["supply-in-profit", "percent-supply-in-profit",
                              "supply-profit"])
    sth_rp = fetch_bg_series(["sth-realized-price", "short-term-holder-realized-price"], days=5)
    oi = fetch_okx_oi_series()

    rows, metrics = [], {}

    lth_now = lth[-1][1] if lth else None
    sth_now = sth[-1][1] if sth else None
    lth_30d = _delta_pct(lth, 30)
    sth_30d = _delta_pct(sth, 30)

    # LTH supply — strong hands absorbing?
    if lth_now is not None:
        status = ("ACCUMULATING" if (lth_30d or 0) > 0.1 else
                  "FLAT" if (lth_30d or 0) > -0.1 else "DISTRIBUTING")
        rows.append({
            "name": "Long-term holder supply (strong hands)",
            "value": f"{lth_now / 1e6:.2f}M BTC",
            "delta": f"{lth_30d:+.2f}% /30d" if lth_30d is not None else "—",
            "status": status, "good": status == "ACCUMULATING",
            "note": "Rising LTH supply = coins rotating INTO strong hands — the precondition for a trustable low",
        })
        metrics["lth_supply"] = round(lth_now)
        metrics["lth_30d_pct"] = round(lth_30d, 3) if lth_30d is not None else None

    # STH supply — hot money bleeding out?
    if sth_now is not None:
        status = ("BLEEDING OUT" if (sth_30d or 0) < -0.5 else
                  "FLAT" if (sth_30d or 0) < 0.5 else "GROWING")
        rows.append({
            "name": "Short-term holder supply (hot money)",
            "value": f"{sth_now / 1e6:.2f}M BTC",
            "delta": f"{sth_30d:+.2f}% /30d" if sth_30d is not None else "—",
            "status": status, "good": status == "BLEEDING OUT",
            "note": "Falling STH supply = top-buyers capitulating out — Nadeau's 'coins changing hands'",
        })
        metrics["sth_supply"] = round(sth_now)
        metrics["sth_30d_pct"] = round(sth_30d, 3) if sth_30d is not None else None

    # LTH share of circulating (LTH + STH ~ circulating by construction)
    if lth_now and sth_now:
        share = 100 * lth_now / (lth_now + sth_now)
        rows.append({
            "name": "LTH share of supply",
            "value": f"{share:.1f}%",
            "delta": "—",
            "status": "HIGH" if share > 75 else "MID" if share > 65 else "LOW",
            "good": share > 75,
            "note": "2018/2022 lows printed with LTH share ~75-80%+ — most of the float in hands that don't sell",
        })
        metrics["lth_share_pct"] = round(share, 2)

    # Supply in profit — capitulation depth
    if profit:
        p_now = profit[-1][1]
        # served either as a percent or as raw BTC — normalize to percent
        if p_now > 100 and lth_now and sth_now:
            p_now = 100 * p_now / (lth_now + sth_now)
        if 0 < p_now <= 100:
            p_30d = _delta_pct(profit, 30)
            status = ("BOTTOM ZONE" if p_now < 55 else
                      "CLOSE" if p_now < 70 else "NOT THERE")
            rows.append({
                "name": "Supply in profit",
                "value": f"{p_now:.1f}%",
                "delta": f"{p_30d:+.1f}% /30d" if p_30d is not None else "—",
                "status": status, "good": status == "BOTTOM ZONE",
                "note": "2015/2018/2022 lows printed at ~40-55% of supply in profit — deep enough that sellers are exhausted",
            })
            metrics["supply_in_profit_pct"] = round(p_now, 2)

    # Live STH realized price vs the static $69k referee line
    if sth_rp:
        metrics["sth_realized_price"] = round(sth_rp[-1][1])

    # Leverage — his first-principles trigger
    if len(oi) > 30:
        oi_30d = 100 * (oi[-1][1] - oi[-31][1]) / oi[-31][1] if oi[-31][1] else None
        if oi_30d is not None:
            status = ("DELEVERAGING" if oi_30d < -5 else
                      "FLAT" if oi_30d < 5 else "RELEVERAGING")
            rows.append({
                "name": "BTC futures open interest (leverage)",
                "value": f"${oi[-1][1] / 1e9:.1f}B",
                "delta": f"{oi_30d:+.1f}% /30d",
                "status": status, "good": status == "DELEVERAGING",
                "note": "'It all comes down to leverage and credit' — washouts, not calendars, end bears. Falling OI = leverage leaving",
            })
            metrics["oi_30d_pct"] = round(oi_30d, 1)

    good = sum(1 for r in rows if r.get("good"))
    scored = len(rows)
    if scored == 0:
        verdict = "All rotation feeds down — panel will retry on the next nightly run."
    else:
        rotating = (metrics.get("lth_30d_pct") or 0) > 0.1 and (metrics.get("sth_30d_pct") or 0) < -0.5
        verdict = (f"{good}/{scored} rotation conditions met. "
                   + ("Hand-off IN PROGRESS: strong hands absorbing while hot money exits. "
                      if rotating else
                      "Hand-off NOT confirmed: LTH absorption and STH capitulation aren't both running. ")
                   + "Nadeau's bar: most coins changed hands in the FINAL 90 days of the 2022 bear — "
                     "watch for this panel going green inside the Oct 4 - Nov 20 window, and check the "
                     "56-66k band fattening on the URPD at charts.checkonchain.com.")

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rows": rows,
        "metrics": metrics,
        "good": good,
        "scored": scored,
        "verdict": verdict,
    }
    try:
        with open(CACHE_PATH, "w") as f:
            json.dump(result, f)
    except OSError:
        pass
    return result


if __name__ == "__main__":
    print("Fetching rotation data (10-30s)...")
    res = run_monitor()
    print(f"\n{'METRIC':<44} {'VALUE':>14} {'30D':>14}  STATUS")
    print("-" * 84)
    for r in res["rows"]:
        print(f"{r['name']:<44} {r['value']:>14} {r['delta']:>14}  {r['status']}")
    print(f"\n{res['verdict']}")
    print(f"\nCache written to {CACHE_PATH}")
