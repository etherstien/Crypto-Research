"""
BTC Cycle Monitor
=================

Computes the bottom-detection checklist from the July 2026 cycle research:
which signals that marked the 2015 / 2018 / 2022 bear-market lows have fired
this cycle, the two "referee lines" that resolve the bottomed-already vs
Q4-2026-low debate, and the historical template window (top + 363-410 days
= Oct 4 - Nov 20, 2026).

All data sources are free and keyless:
  - CoinMetrics Community API : price, market cap, realized cap
                                -> MVRV, NUPL, realized price, 200WMA,
                                   Mayer Multiple, Pi Cycle Bottom
  - blockchain.info charts    : hash rate (hash ribbons), miner revenue
                                (Puell Multiple)
  - alternative.me            : Fear & Greed value + sub-25 streak
  - Binance/OKX futures       : 30d average funding rate (best-effort)
  - Farside Investors         : recent spot-ETF net flows (best-effort
                                scrape; NA if blocked)

Referee lines (constants below, revisit as on-chain levels drift):
  - RECLAIM  ~$69k  short-term-holder realized price. Weekly closes above,
    with sustained ETF inflows => the Jul 1 low ($57.7k) was the bottom.
  - CAPITULATION = live realized price (~$52.5k). Weekly close below =>
    bear case vindicated; $45-55k template zone in play.
  - DEEP FLOOR ~$49.7k long-term-holder realized price (2015/2018/2022
    capitulation marker).

Usage:
  python cycle_monitor.py       # prints the checklist, writes cycle_cache.json
  Flask: GET /api/cycle
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass

CACHE_PATH = os.path.join(os.path.dirname(__file__), "cycle_cache.json")

# Referee lines from the Jul-2026 research pass (update as cohort data moves)
STH_REALIZED_PRICE = 69000.0    # short-term-holder cost basis (reclaim line)
LTH_REALIZED_PRICE = 49700.0    # long-term-holder cost basis (deep floor)
CYCLE_LOW = 57717.0             # Jul 1 2026 low
CYCLE_TOP = 126296.0            # Oct 6 2025 top
WINDOW_START = "2026-10-04"     # top + 363 days
WINDOW_END = "2026-11-20"       # top + 410 days


def _get(url, params=None, timeout=30, retries=2, headers=None):
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout,
                             headers=headers or {"User-Agent": "Mozilla/5.0 (cycle-monitor)"})
            if r.status_code == 429:
                time.sleep(10 * (attempt + 1))
                continue
            r.raise_for_status()
            return r
        except Exception as e:
            last_err = e
            if attempt == retries:
                print(f"  fetch failed: {url.split('?')[0]} -> {last_err}", file=sys.stderr)
                return None
            time.sleep(3)
    return None


def _get_json(url, params=None, **kw):
    r = _get(url, params, **kw)
    try:
        return r.json() if r is not None else None
    except ValueError:
        return None


def _sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def _ema(vals, n):
    if len(vals) < n:
        return None
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n
    for v in vals[n:]:
        e = v * k + e * (1 - k)
    return e


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

def fetch_binance_klines(interval, limit):
    """Closing prices from Binance spot (keyless; proven reachable from CI)."""
    data = _get_json("https://api.binance.com/api/v3/klines",
                     params={"symbol": "BTCUSDT", "interval": interval, "limit": limit})
    if not isinstance(data, list):
        return []
    return [float(k[4]) for k in data if isinstance(k, list) and len(k) > 4]


def fetch_coinmetrics_latest():
    """Latest market cap + realized cap (light call). None if unreachable."""
    data = _get_json("https://community-api.coinmetrics.io/v4/timeseries/asset-metrics",
                     params={"assets": "btc",
                             "metrics": "CapMrktCurUSD,CapRealUSD,PriceUSD",
                             "frequency": "1d", "page_size": 10,
                             "sort": "time", "limit_per_asset": 10})
    rows = (data or {}).get("data", [])
    for r in reversed(rows):
        try:
            return {"price": float(r["PriceUSD"]),
                    "mcap": float(r["CapMrktCurUSD"]),
                    "rcap": float(r["CapRealUSD"])}
        except (KeyError, TypeError, ValueError):
            continue
    return None


def fetch_blockchain_chart(chart, timespan="2years"):
    data = _get_json(f"https://api.blockchain.info/charts/{chart}",
                     params={"timespan": timespan, "format": "json", "cors": "true"})
    vals = [(v.get("x"), v.get("y")) for v in (data or {}).get("values", [])
            if v.get("y") is not None]
    vals.sort(key=lambda x: x[0])
    return [v[1] for v in vals]


def fetch_fear_greed():
    data = _get_json("https://api.alternative.me/fng/", params={"limit": 120, "format": "json"})
    rows = (data or {}).get("data", [])
    if not rows:
        return None
    latest = int(rows[0]["value"])
    streak = 0
    for r in rows:
        if int(r["value"]) < 25:
            streak += 1
        else:
            break
    return {"value": latest, "label": rows[0].get("value_classification", ""),
            "sub25_streak_days": streak}


def fetch_funding_30d():
    """30d average funding, annualized %. Binance first, OKX fallback."""
    data = _get_json("https://fapi.binance.com/fapi/v1/fundingRate",
                     params={"symbol": "BTCUSDT", "limit": 90})
    rates = []
    if isinstance(data, list):
        rates = [float(r["fundingRate"]) for r in data if r.get("fundingRate")]
    if not rates:
        data = _get_json("https://www.okx.com/api/v5/public/funding-rate-history",
                         params={"instId": "BTC-USD-SWAP", "limit": 100})
        rates = [float(r["fundingRate"]) for r in (data or {}).get("data", [])
                 if r.get("fundingRate")]
    if not rates:
        return None
    avg8h = sum(rates) / len(rates)
    return round(avg8h * 3 * 365 * 100, 2)  # annualized %


def fetch_etf_flows():
    """Best-effort scrape of Farside's BTC ETF flow table -> last 5 daily totals."""
    r = _get("https://farside.co.uk/btc/")
    if r is None:
        return None
    try:
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", r.text, re.S)
        flows = []
        for row in rows:
            cells = [re.sub(r"<[^>]+>", "", c).replace("&nbsp;", " ").strip()
                     for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]
            if not cells or not re.match(r"\d{1,2} \w{3} \d{4}", cells[0]):
                continue
            total = cells[-1].replace(",", "").replace("(", "-").replace(")", "")
            try:
                flows.append({"date": cells[0], "net_usd_m": float(total)})
            except ValueError:
                pass
        return flows[-5:] if flows else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Signal engine
# ---------------------------------------------------------------------------

def _sig(key, name, value, status, detail):
    return {"key": key, "name": name, "value": value, "status": status, "detail": detail}


REALIZED_PRICE_FALLBACK = 52500.0  # Jul-2026 research value, used if CM is down


def run_monitor():
    daily = fetch_binance_klines("1d", 1000)    # covers 200DMA, 471SMA, 150EMA
    weekly = fetch_binance_klines("1w", 300)    # covers 200-week MA
    if len(daily) < 500 or len(weekly) < 200:
        return {"error": "Binance price fetch failed — cannot compute signals"}
    price = daily[-1]

    cm = fetch_coinmetrics_latest()
    if cm:
        mvrv = cm["mcap"] / cm["rcap"]
        nupl = 1 - 1 / mvrv
        realized_price = cm["rcap"] * cm["price"] / cm["mcap"]
        rp_live = True
    else:
        mvrv = nupl = None
        realized_price = REALIZED_PRICE_FALLBACK
        rp_live = False

    wma200 = _sma(weekly, 200)
    dma200 = _sma(daily, 200)
    mayer = price / dma200 if dma200 else None
    ema150 = _ema(daily[-800:], 150)
    sma471 = _sma(daily, 471)
    pi_bottom = (ema150 is not None and sma471 is not None
                 and ema150 < sma471 * 0.745)

    hashrate = fetch_blockchain_chart("hash-rate")
    ribbons = None
    if len(hashrate) >= 60:
        h30, h60 = _sma(hashrate, 30), _sma(hashrate, 60)
        capit_recent = any(_sma(hashrate[:i], 30) is not None
                           and _sma(hashrate[:i], 30) < _sma(hashrate[:i], 60)
                           for i in range(max(61, len(hashrate) - 120), len(hashrate)))
        ribbons = {"sma30": h30, "sma60": h60,
                   "state": "capitulation" if h30 < h60 else
                            ("recovery_buy" if capit_recent else "normal")}

    revenue = fetch_blockchain_chart("miners-revenue")
    puell = None
    if len(revenue) >= 365:
        puell = revenue[-1] / _sma(revenue, 365)

    fng = fetch_fear_greed()
    funding = fetch_funding_30d()
    etf = fetch_etf_flows()

    signals = []
    signals.append(_sig("mvrv", "MVRV < 1 (price tags realized price)",
                        f"{mvrv:.2f}" if mvrv is not None else "n/a (feed down)",
                        "NA" if mvrv is None else
                        ("FIRED" if mvrv < 1 else ("CLOSE" if mvrv < 1.1 else "NOT_FIRED")),
                        f"Realized price ${realized_price:,.0f}"
                        + ("" if rp_live else " (static research value)")
                        + " — fired at every 2015/2018/2022 low"))
    signals.append(_sig("wma200", "200-week MA touch",
                        f"${wma200:,.0f}" if wma200 else "n/a",
                        "FIRED" if (wma200 and price < wma200 * 1.02) else
                        ("CLOSE" if (wma200 and price < wma200 * 1.08) else "NOT_FIRED"),
                        "Every prior bear bottomed at/below this line (2022 pierced it -25%)"))
    signals.append(_sig("nupl", "NUPL < 0 (aggregate capitulation)",
                        f"{nupl:.2f}" if nupl is not None else "n/a (feed down)",
                        "NA" if nupl is None else
                        ("FIRED" if nupl < 0 else ("CLOSE" if nupl < 0.1 else "NOT_FIRED")),
                        "Went negative at all three prior lows (-0.20 to -0.25)"))
    signals.append(_sig("puell", "Puell Multiple < 0.5",
                        f"{puell:.2f}" if puell else "n/a",
                        "NA" if puell is None else
                        ("FIRED" if puell < 0.5 else ("CLOSE" if puell < 0.8 else "NOT_FIRED")),
                        "0.3-0.4 at prior lows; <1 = miner stress"))
    signals.append(_sig("mayer", "Mayer Multiple < 0.6",
                        f"{mayer:.2f}" if mayer else "n/a",
                        "NA" if mayer is None else
                        ("FIRED" if mayer < 0.6 else ("CLOSE" if mayer < 0.85 else "NOT_FIRED")),
                        "Cycle bottoms printed ~0.5-0.6"))
    if ribbons:
        signals.append(_sig("ribbons", "Hash ribbons capitulation → recovery",
                            ribbons["state"].replace("_", " "),
                            "FIRED" if ribbons["state"] == "recovery_buy" else
                            ("CLOSE" if ribbons["state"] == "capitulation" else "NOT_FIRED"),
                            "Buy signal marked every bottom since 2015 (fired Q1 2026; can run early)"))
    signals.append(_sig("pi", "Pi Cycle Bottom (150EMA < 0.745×471SMA)",
                        "in process" if pi_bottom else "no",
                        "FIRED" if pi_bottom else "NOT_FIRED",
                        "Curve-fit confirmation overlay — lows form between down-cross and re-cross"))
    if fng:
        signals.append(_sig("fng", "Fear & Greed sustained < 25",
                            f"{fng['value']} ({fng['label']})",
                            "FIRED" if fng["sub25_streak_days"] >= 14 else
                            ("CLOSE" if fng["value"] < 30 else "NOT_FIRED"),
                            f"Current sub-25 streak: {fng['sub25_streak_days']}d — regime gauge, not a timer"))
    if funding is not None:
        signals.append(_sig("funding", "Negative 30d avg funding",
                            f"{funding:+.1f}% ann.",
                            "FIRED" if funding < 0 else ("CLOSE" if funding < 3 else "NOT_FIRED"),
                            "46-day negative streak into Apr 2026 = leverage washout; watch for repeat at new lows"))
    if etf:
        recent = sum(f["net_usd_m"] for f in etf)
        signals.append(_sig("etf", "Spot ETF flows (last 5 sessions)",
                            f"{recent:+,.0f}M USD",
                            "FIRED" if recent > 0 else "NOT_FIRED",
                            "Outflow-capitulation → inflow-resumption marked recent local lows"))

    scored = [s for s in signals if s["status"] != "NA"]
    fired = sum(1 for s in scored if s["status"] == "FIRED")

    today = datetime.now(timezone.utc).date()
    ws = datetime.strptime(WINDOW_START, "%Y-%m-%d").date()
    we = datetime.strptime(WINDOW_END, "%Y-%m-%d").date()
    in_window = ws <= today <= we

    behavioral = {"ribbons", "fng", "funding", "etf", "wma200"}
    beh_fired = sum(1 for s in scored if s["key"] in behavioral and s["status"] == "FIRED")
    val_fired = sum(1 for s in scored if s["key"] not in behavioral and s["status"] == "FIRED")
    verdict = (f"{fired}/{len(scored)} signals fired — "
               f"behavioral {beh_fired} fired, valuation {val_fired} fired. "
               + ("INSIDE the Oct 4 - Nov 20 template window: valuation signals firing now = maximum-deployment zone."
                  if in_window else
                  f"Template window opens {WINDOW_START} ({(ws - today).days} days). "
                  "Reclaim of the $69k line before then favors 'bottom already in'; "
                  "a close below realized price favors the Q4 capitulation path."))

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "price": round(price, 0),
        "referee": {
            "reclaim_line": STH_REALIZED_PRICE,
            "reclaim_distance_pct": round(100 * (STH_REALIZED_PRICE - price) / price, 1),
            "capitulation_line": round(realized_price, 0),
            "capitulation_distance_pct": round(100 * (price - realized_price) / price, 1),
            "lth_floor": LTH_REALIZED_PRICE,
            "cycle_low": CYCLE_LOW,
            "cycle_top": CYCLE_TOP,
            "drawdown_pct": round(100 * (price - CYCLE_TOP) / CYCLE_TOP, 1),
        },
        "window": {
            "start": WINDOW_START, "end": WINDOW_END,
            "in_window": in_window,
            "days_to_start": max(0, (ws - today).days),
            "days_to_end": (we - today).days,
        },
        "metrics": {
            "mvrv": round(mvrv, 3) if mvrv is not None else None,
            "nupl": round(nupl, 3) if nupl is not None else None,
            "realized_price": round(realized_price, 0),
            "realized_price_live": rp_live,
            "wma200": round(wma200, 0) if wma200 else None,
            "dma200": round(dma200, 0) if dma200 else None,
            "mayer": round(mayer, 3) if mayer else None,
            "puell": round(puell, 3) if puell else None,
            "funding_30d_ann_pct": funding,
            "fear_greed": fng,
            "etf_last5": etf,
        },
        "signals": signals,
        "fired": fired,
        "scored": len(scored),
        "verdict": verdict,
    }

    try:
        with open(CACHE_PATH, "w") as f:
            json.dump(result, f)
    except OSError:
        pass
    return result


def load_cache():
    try:
        with open(CACHE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


if __name__ == "__main__":
    print("Fetching cycle data (20-40s)...")
    res = run_monitor()
    if res.get("error"):
        print("ERROR:", res["error"])
        sys.exit(1)
    ref = res["referee"]
    print(f"\nBTC ${res['price']:,.0f}  ({ref['drawdown_pct']}% from top; cycle low ${ref['cycle_low']:,.0f})")
    print(f"Referee lines: reclaim ${ref['reclaim_line']:,.0f} (+{ref['reclaim_distance_pct']}%) | "
          f"capitulation ${ref['capitulation_line']:,.0f} (-{ref['capitulation_distance_pct']}%) | "
          f"LTH floor ${ref['lth_floor']:,.0f}")
    w = res["window"]
    print(f"Template window: {w['start']} → {w['end']}"
          + (" [IN WINDOW]" if w["in_window"] else f" (opens in {w['days_to_start']}d)"))
    print(f"\n{'SIGNAL':<44} {'VALUE':>16}  STATUS")
    print("-" * 76)
    for s in res["signals"]:
        print(f"{s['name']:<44} {s['value']:>16}  {s['status']}")
    print(f"\n{res['verdict']}")
    print(f"\nCache written to {CACHE_PATH}")
