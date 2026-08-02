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
from datetime import datetime, timedelta, timezone

import requests

CACHE_PATH = os.path.join(os.path.dirname(__file__), "rotation_cache.json")

# BGeometrics free tier: 8 requests/HOUR per IP (429 past that). The cycle
# monitor's on-chain fallback uses 3 — this script must stay at ~3 total.
BG = "https://bitcoin-data.com/v1"


def _get_json(url, params=None, timeout=45, retries=1):
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout,
                             headers={"User-Agent": "Mozilla/5.0 (rotation-monitor)"})
            if r.status_code == 429:
                if attempt == retries:
                    print(f"  RATE LIMITED (429): {url.split('?')[0]} — free tier is 8 req/hour",
                          file=sys.stderr)
                    return None
                time.sleep(20)
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


def fetch_bg_series(path, days=400):
    """One endpoint, one request (quota!). startday keeps the payload small.
    Returns newest-last [(date, value)]."""
    start = (datetime.now(timezone.utc) - timedelta(days=days + 5)).strftime("%Y-%m-%d")
    data = _get_json(f"{BG}/{path}", params={"startday": start})
    if isinstance(data, dict):                      # tolerate a wrapper object
        for k in ("data", "values", "series", "rows"):
            if isinstance(data.get(k), list):
                data = data[k]
                break
    if not isinstance(data, list) or not data:
        return []
    rows = [p for p in (_parse_row(r) for r in data) if p]
    return rows[-days:]


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


CIRCULATING_FALLBACK = 19.9e6  # BTC, close enough for share-of-supply math


def _fmt_supply(v):
    """Value may arrive as absolute BTC or % of supply — format either."""
    return f"{v / 1e6:.2f}M BTC" if v > 1e5 else f"{v:.1f}% of supply"


def run_monitor():
    # exactly 3 BGeometrics requests — endpoint names verified in the wild:
    # long-term-hodler-supply-btc (sic, "hodler") and utxo-profit
    lth = fetch_bg_series("long-term-hodler-supply-btc")
    sth = fetch_bg_series("short-term-hodler-supply-btc")
    profit = fetch_bg_series("utxo-profit")
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
            "value": _fmt_supply(lth_now),
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
            "value": _fmt_supply(sth_now),
            "delta": f"{sth_30d:+.2f}% /30d" if sth_30d is not None else "—",
            "status": status, "good": status == "BLEEDING OUT",
            "note": "Falling STH supply = top-buyers capitulating out — Nadeau's 'coins changing hands'",
        })
        metrics["sth_supply"] = round(sth_now)
        metrics["sth_30d_pct"] = round(sth_30d, 3) if sth_30d is not None else None

    # LTH share of circulating (LTH + STH ~ circulating by construction).
    # Series may already BE a share (%); otherwise derive from absolutes,
    # falling back to ~19.9M circulating if the STH series is missing.
    share = None
    if lth_now is not None and lth_now <= 100:
        share = lth_now
    elif lth_now and sth_now and sth_now > 1e5:
        share = 100 * lth_now / (lth_now + sth_now)
    elif lth_now and lth_now > 1e5:
        share = 100 * lth_now / CIRCULATING_FALLBACK
    if share is not None:
        rows.append({
            "name": "LTH share of supply",
            "value": f"{share:.1f}%",
            "delta": "—",
            "status": "HIGH" if share > 75 else "MID" if share > 65 else "LOW",
            "good": share > 75,
            "note": "2018/2022 lows printed with LTH share ~75-80%+ — most of the float in hands that don't sell",
        })
        metrics["lth_share_pct"] = round(share, 2)

    # UTXOs in profit — capitulation depth
    if profit:
        p_now = profit[-1][1]
        # utxo-profit may be a percent already or a raw UTXO count — a raw
        # count is useless without total UTXOs, so only score a percent
        if 0 < p_now <= 100:
            p_30d = _delta_pct(profit, 30)
            status = ("BOTTOM ZONE" if p_now < 55 else
                      "CLOSE" if p_now < 70 else "NOT THERE")
            rows.append({
                "name": "UTXOs in profit",
                "value": f"{p_now:.1f}%",
                "delta": f"{p_30d:+.1f}% /30d" if p_30d is not None else "—",
                "status": status, "good": status == "BOTTOM ZONE",
                "note": "2015/2018/2022 lows printed ~40-55% in profit — deep enough that sellers are exhausted",
            })
            metrics["utxo_profit_pct"] = round(p_now, 2)

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
