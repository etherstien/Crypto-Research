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
                                -> MVRV, NUPL, realized price
                                (403-blocks GitHub CI runner IPs)
  - bitcoin-data.com          : MVRV / NUPL / realized price fallback
                                (BGeometrics free API) when CM is blocked
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

# ── Upside-confirmation gates (Aug-2026 "four gates" framework) ─────────────
# The bottom checklist only fires if price goes LOWER — in a V-recovery it
# reads 1/9 forever and the model can never say "get in". These gates are the
# other half: what must be TRUE to declare the low in and redeploy with the
# lower zones unfilled. All four closing = flip. Thresholds are thesis
# parameters from the Aug-2026 read ($7.2B shorts liquidated = squeeze fuel
# spent; whatever moves price next is closer to real demand) — revisit as the
# structure moves.
G1_WEEKLY_EMA_LEN = 50          # 50-week EMA, the 2022-fakeout killer line
G1_CLOSES_NEEDED = 3            # 2022 managed exactly 2 closes above; 3 has no bear precedent
G1_SUPPLY_SHELF = 84500.0       # low-$80s overhead supply; weekly close above = sellers absorbed
G2_ETF_WEEKLY_USD_M = 1000.0    # ETF net inflows >= $1B/week...
G2_ETF_WEEKS_NEEDED = 3         # ...for 3 consecutive completed weeks (regime, not spike)
# Perp funding sits at ~+0.01%/8h (~11% ann.) when longs/shorts are BALANCED —
# that baseline is what "flat" means. Spot-led = at/below baseline while price
# holds; re-levered shows as 20%+ ann. (Recalibrated 2026-08-26 from 5.0,
# which was stricter than neutral and near-unpassable in a rising market.)
G2_FUNDING_FLAT_MAX = 12.0
G3_EVENTS = [                   # (date, name, floor BTC must hold after it)
    ("2026-08-28", "PCE print", 75000.0),
    ("2026-09-16", "FOMC decision", 70000.0),
]
G4_HOLD_LINE = 75000.0          # still above this at the deadline = bears out of window
G4_DEADLINE = "2026-09-30"      # Q4-flush window (Cowen/Brandt/Martinez cluster early-mid Oct)
KILL_WEEKLY_CLOSE = 72000.0     # weekly close below = Gate 1 dead, lower zones live again
KILL_FUNDING_ANN = 20.0         # ~2x neutral with price flat = re-levered, not demand


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


def _ema_series(vals, n):
    """EMA value aligned to every index (None during warmup)."""
    if len(vals) < n:
        return [None] * len(vals)
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n
    out = [None] * (n - 1) + [e]
    for v in vals[n:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

def fetch_binance_klines(interval, limit):
    """BTC closing prices. Primary: Binance's public data mirror
    (data-api.binance.vision — NOT geo-blocked like api.binance.com, which
    returns 451 from US CI runners). Fallback: Kraken OHLC (720 candles)."""
    for host in ("https://data-api.binance.vision", "https://api.binance.com"):
        data = _get_json(f"{host}/api/v3/klines",
                         params={"symbol": "BTCUSDT", "interval": interval, "limit": limit})
        if isinstance(data, list) and data:
            return [float(k[4]) for k in data if isinstance(k, list) and len(k) > 4]
    kr_interval = {"1d": 1440, "1w": 10080}.get(interval)
    if kr_interval:
        data = _get_json("https://api.kraken.com/0/public/OHLC",
                         params={"pair": "XBTUSD", "interval": kr_interval})
        result = (data or {}).get("result", {})
        for key, rows in result.items():
            if key != "last" and isinstance(rows, list):
                closes = [float(r[4]) for r in rows if isinstance(r, list) and len(r) > 4]
                return closes[-limit:]
    return []


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


def _bg_last(path):
    """bitcoin-data.com /v1/<metric>/last -> float, tolerant of field naming
    ({"d": "...", "unixTs": "...", "nupl": "0.47"} — the metric key varies)."""
    data = _get_json(f"https://bitcoin-data.com/v1/{path}/last")
    if isinstance(data, list) and data:
        data = data[-1]
    if not isinstance(data, dict):
        return None
    for k, v in data.items():
        if k.lower() in ("d", "date", "unixts", "time", "timestamp"):
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def fetch_onchain_metrics():
    """MVRV / NUPL / realized price with fallback chain:
    CoinMetrics community API (403s GitHub runners) -> bitcoin-data.com."""
    cm = fetch_coinmetrics_latest()
    if cm:
        mvrv = cm["mcap"] / cm["rcap"]
        return {"mvrv": mvrv, "nupl": 1 - 1 / mvrv,
                "realized_price": cm["rcap"] * cm["price"] / cm["mcap"],
                "source": "coinmetrics"}
    mvrv = _bg_last("mvrv")
    nupl = _bg_last("nupl")
    rp = _bg_last("realized-price")
    if nupl is not None and abs(nupl) > 1.5:   # served as % instead of fraction
        nupl /= 100.0
    if nupl is None and mvrv:
        nupl = 1 - 1 / mvrv
    if mvrv is None and nupl is not None and nupl < 1:
        mvrv = 1 / (1 - nupl)
    if rp is not None and not (5000 < rp < 500000):   # garbage guard
        rp = None
    if mvrv is not None or nupl is not None or rp is not None:
        return {"mvrv": mvrv, "nupl": nupl, "realized_price": rp,
                "source": "bitcoin-data.com"}
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


def fetch_funding_rates():
    """8h funding-rate history, oldest first. Binance first, OKX fallback."""
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
        rates.reverse()   # OKX serves newest first
    return rates


def _funding_ann(rates):
    """Average 8h funding as annualized %; None on empty."""
    if not rates:
        return None
    return round(sum(rates) / len(rates) * 3 * 365 * 100, 2)


def fetch_etf_flows(sessions=30):
    """Best-effort scrape of Farside's BTC ETF flow table -> last N daily totals
    (30 by default so the gates can aggregate weekly; the checklist uses 5)."""
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
        return flows[-sessions:] if flows else None
    except Exception:
        return None


def fetch_open_interest_7d(daily):
    """7d % change in BTC-denominated futures open interest.
    Binance OI history first (451-blocked from some IPs); OKX rubik fallback,
    whose USD-denominated OI is de-priced against the 7d price change so a
    price move alone doesn't read as a positioning change.
    Returns (pct_change, source) or (None, None)."""
    data = _get_json("https://fapi.binance.com/futures/data/openInterestHist",
                     params={"symbol": "BTCUSDT", "period": "1d", "limit": 10})
    if isinstance(data, list) and len(data) >= 8:
        try:
            vals = [float(r["sumOpenInterest"]) for r in data]
            return (vals[-1] / vals[-8] - 1) * 100, "binance"
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            pass
    data = _get_json("https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume",
                     params={"ccy": "BTC", "period": "1D"})
    rows = (data or {}).get("data", [])
    try:
        rows = sorted(rows, key=lambda r: int(r[0]))          # oldest first
        oi_usd = [float(r[1]) for r in rows]
        if len(oi_usd) >= 8 and len(daily) >= 8 and oi_usd[-8] and daily[-8]:
            usd_chg = oi_usd[-1] / oi_usd[-8] - 1
            px_chg = daily[-1] / daily[-8] - 1
            return ((1 + usd_chg) / (1 + px_chg) - 1) * 100, "okx"
    except (TypeError, ValueError, IndexError, ZeroDivisionError):
        pass
    return None, None


def fetch_coinbase_premium():
    """Coinbase BTC-USD vs Binance BTC-USDT spot, in %. The classic detector
    of a US institutional/whale bid: big US buyers execute on Coinbase, and a
    persistent positive premium during a rally separates institutional spot
    accumulation from offshore perp-driven moves. Snapshot, so noisy — read
    the sign, not the second decimal. (USDT/USD drift adds ~±0.1% noise.)"""
    cb = _get_json("https://api.exchange.coinbase.com/products/BTC-USD/ticker")
    bn = _get_json("https://data-api.binance.vision/api/v3/ticker/price",
                   params={"symbol": "BTCUSDT"})
    try:
        return round((float(cb["price"]) / float(bn["price"]) - 1) * 100, 3)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def _etf_weekly_sums(flows):
    """Completed calendar weeks only, oldest first: [(iso_week_label, sum_usd_m)]."""
    if not flows:
        return []
    by_week = {}
    this_week = datetime.now(timezone.utc).date().isocalendar()[:2]
    for f in flows:
        try:
            d = datetime.strptime(f["date"], "%d %b %Y").date()
        except ValueError:
            continue
        wk = d.isocalendar()[:2]
        if wk == this_week:
            continue                      # in-progress week: not a regime datapoint yet
        by_week.setdefault(wk, 0.0)
        by_week[wk] += f["net_usd_m"]
    return [(f"{y}-W{w:02d}", round(v, 0)) for (y, w), v in sorted(by_week.items())]


# ---------------------------------------------------------------------------
# Signal engine
# ---------------------------------------------------------------------------

def _sig(key, name, value, status, detail):
    return {"key": key, "name": name, "value": value, "status": status, "detail": detail}


# ---------------------------------------------------------------------------
# Upside gates — the "you'd better get in" half of the model
# ---------------------------------------------------------------------------

def evaluate_gates(daily, weekly, funding_rates, etf_flows,
                   oi_7d=None, oi_src=None, premium=None):
    """The four upside-confirmation gates. Statuses: CLOSED (condition met),
    OPEN (not yet), PENDING (event still ahead), KILLED (inverse tripped).
    all_closed=True is the flip: low declared in, redeploy off the new range."""
    price = daily[-1]
    today = datetime.now(timezone.utc).date()
    closed_w = weekly[:-1]                    # drop the in-progress weekly candle

    # ── Gate 1: price proves the level ──
    emas = _ema_series(closed_w, G1_WEEKLY_EMA_LEN)
    ema50w = emas[-1] if emas else None
    consec = 0
    for c, e in zip(reversed(closed_w), reversed(emas)):
        if e is None or c <= e:
            break
        consec += 1
    shelf_closed = closed_w[-1] > G1_SUPPLY_SHELF
    shelf_holding = price > G1_SUPPLY_SHELF
    g1_killed = closed_w[-1] < KILL_WEEKLY_CLOSE
    g1_ok = consec >= G1_CLOSES_NEEDED and shelf_closed and shelf_holding
    g1 = _sig("g1", "Price proves the level",
              f"{consec} wkly closes > 50W EMA (${ema50w:,.0f})" if ema50w else "n/a",
              "KILLED" if g1_killed else ("CLOSED" if g1_ok else "OPEN"),
              f"Need {G1_CLOSES_NEEDED} consecutive — {max(0, G1_CLOSES_NEEDED - consec)} more to go "
              f"(2022's fakeout died at 2) — then a weekly "
              f"close over the ${G1_SUPPLY_SHELF/1000:.1f}k supply shelf that holds. "
              f"Shelf close: {'yes' if shelf_closed else 'no'}; holding now: "
              f"{'yes' if shelf_holding else 'no'}. Weekly close < ${KILL_WEEKLY_CLOSE/1000:.0f}k kills it.")

    # ── Gate 2: demand replaces the squeeze ──
    funding_14d = _funding_ann(funding_rates[-42:]) if funding_rates else None
    px_14d = (daily[-1] / daily[-15] - 1) * 100 if len(daily) >= 15 else None
    spot_led = (funding_14d is not None and px_14d is not None
                and funding_14d < G2_FUNDING_FLAT_MAX and px_14d > -2)
    weeks = _etf_weekly_sums(etf_flows)
    etf_consec = 0
    for _, v in reversed(weeks):
        if v >= G2_ETF_WEEKLY_USD_M:
            etf_consec += 1
        else:
            break
    g2_killed = (funding_14d is not None and px_14d is not None
                 and funding_14d > KILL_FUNDING_ANN and px_14d < 2)
    g2_ok = etf_consec >= G2_ETF_WEEKS_NEEDED and spot_led
    wk_txt = ", ".join(f"{v:+,.0f}M" for _, v in weeks[-4:]) or "no flow data"
    g2 = _sig("g2", "Demand replaces the squeeze",
              f"ETF wks {wk_txt} · funding {funding_14d:+.1f}% ann"
              if funding_14d is not None else f"ETF wks {wk_txt} · funding n/a",
              "KILLED" if g2_killed else ("CLOSED" if g2_ok else "OPEN"),
              f"Need {G2_ETF_WEEKS_NEEDED} straight completed weeks >= ${G2_ETF_WEEKLY_USD_M:,.0f}M "
              f"(now {etf_consec}) AND 14d funding under {G2_FUNDING_FLAT_MAX:.0f}% ann. — i.e. at/below "
              f"the ~11% neutral baseline — while price holds (spot-led, not re-levered). "
              f"CryptoQuant spot+futures demand staying positive "
              f"through late Sep is the manual third check — no free feed. Funding spiking with "
              f"price flat kills it.")

    # ── Gate 3: the events don't break it ──
    ev_parts, ev_all_passed, ev_failed, ev_pending = [], True, False, False
    for dstr, name, floor in G3_EVENTS:
        d = datetime.strptime(dstr, "%Y-%m-%d").date()
        if today <= d:
            ev_parts.append(f"{name} in {(d - today).days}d ({dstr})")
            ev_pending = True
            ev_all_passed = False
        else:
            days = (today - d).days
            since = daily[-(days + 1):] if days + 1 <= len(daily) else daily
            held = min(since) >= floor
            ev_parts.append(f"{name}: {'held' if held else 'BROKE'} ${floor/1000:.0f}k")
            if not held:
                ev_all_passed = False
                ev_failed = True
    g3 = _sig("g3", "The events don't break it",
              " · ".join(ev_parts),
              "CLOSED" if ev_all_passed else ("OPEN" if ev_failed else "PENDING"),
              "PCE then the Sep 16 FOMC, absorbed without losing the floor on a close — a hawkish "
              "print absorbed would be the strongest signal of all. Clarity Act vote is a bonus "
              "accelerant, not a requirement.")

    # ── Gate 4: time runs out on the bears ──
    deadline = datetime.strptime(G4_DEADLINE, "%Y-%m-%d").date()
    days_left = (deadline - today).days
    on_track = price > G4_HOLD_LINE and closed_w[-1] > G4_HOLD_LINE
    if days_left > 0:
        g4_status = "PENDING"
        g4_val = f"{days_left}d to {G4_DEADLINE}; {'on track' if on_track else 'NOT holding'} ${G4_HOLD_LINE/1000:.0f}k"
    else:
        days_since = min(-days_left + 30, len(daily) - 1)
        held_month = min(daily[-(days_since + 1):]) >= G4_HOLD_LINE
        g4_status = "CLOSED" if held_month else "OPEN"
        g4_val = f"deadline passed; {'held' if held_month else 'lost'} ${G4_HOLD_LINE/1000:.0f}k through it"
    g4 = _sig("g4", "Time runs out on the bears", g4_val, g4_status,
              "The Q4-flush thesis is a WINDOW (early-mid Oct cluster), not just a target. A flush "
              "needs momentum to feed on; every week above the 50W EMA subtracts one from the "
              "bears. Above the line at the deadline = no mechanism left.")

    gates = [g1, g2, g3, g4]
    n_closed = sum(1 for g in gates if g["status"] == "CLOSED")
    killed = any(g["status"] == "KILLED" for g in gates)
    all_closed = n_closed == 4 and not killed
    early_warning = (spot_led and (px_14d or 0) > 0 and len(weeks) >= 2
                     and weeks[-1][1] > 0 and weeks[-1][1] >= weeks[-2][1])
    starter = consec >= 2 and not killed

    if killed:
        verdict = ("GATES KILLED — the inverse tripped. Lower tranche zones are live again; "
                   "the bottom checklist above is the map.")
    elif all_closed:
        verdict = ("ALL FOUR GATES CLOSED — flip: treat the low as in, deploy at market, "
                   "re-anchor the tranche ladder off the new range. The unfilled zones are sunk cost.")
    else:
        verdict = (f"{n_closed}/4 gates closed."
                   + (" EARLY WARNING live: funding flat while price grinds higher on rising ETF "
                      "inflows — squeezes exhaust, spot bids persist; gates are closing early."
                      if early_warning else "")
                   + (f" Starter condition met ({consec} weekly closes > 50W EMA): the 10-15% "
                      "pre-commitment tranche is armed." if starter else ""))

    # Supporting turn-signals: not scored, not gates — context that belongs on
    # the recovery side of the page rather than in the capitulation checklist.
    supporting = []
    last5 = (etf_flows or [])[-5:]
    if last5:
        net5 = sum(f["net_usd_m"] for f in last5)
        supporting.append(_sig("etf5", "Spot ETF flows (last 5 sessions)",
                               f"{net5:+,.0f}M USD",
                               "POSITIVE" if net5 > 0 else "NEGATIVE",
                               "Daily-resolution demand read — a turn signal, not a bottom "
                               "signal, which is why it lives on this side of the page. Inflow "
                               "resumption marked prior local lows; the scored weekly regime "
                               "version is Gate 2 above."))

    # Who's driving the move? OI-delta decomposition — the whale-vs-squeeze
    # read. Price up while futures positions CLOSE is short covering; price
    # up on NEW leverage is a chase; price up with positioning flat and
    # funding at baseline means the move is happening in SPOT — the whale
    # signature (with the caveat that OTC accumulation is invisible to all
    # exchange data). Heuristic thresholds: |3%| price and |4%| OI over 7d.
    px_7d = (daily[-1] / daily[-8] - 1) * 100 if len(daily) >= 8 else None
    drivers = None
    if px_7d is not None and oi_7d is not None and funding_14d is not None:
        if px_7d > 3:
            if oi_7d <= -4:
                label, expl = "SQUEEZE", ("price up while futures positions close — "
                                          "short covering, not proven new demand")
            elif oi_7d >= 4 and funding_14d > G2_FUNDING_FLAT_MAX:
                label, expl = "LEVERED CHASE", ("price up on new leverage with funding above "
                                                "baseline — fragile, liquidation-prone")
            elif funding_14d <= G2_FUNDING_FLAT_MAX:
                label, expl = "SPOT-LED", ("price up with futures positioning flat and funding "
                                           "at/below baseline — the move is in spot: the whale "
                                           "signature")
            else:
                label, expl = "MIXED", "price up but leverage and positioning give no clean read"
        elif px_7d < -3:
            if oi_7d <= -4:
                label, expl = "LONG FLUSH", "price down while positions close — longs liquidating"
            elif oi_7d >= 4:
                label, expl = "SHORTS PRESSING", ("price down on new positions — shorts opening "
                                                  "into weakness")
            else:
                label, expl = "SPOT SELLOFF", "price down with futures positioning flat — spot selling"
        else:
            label, expl = "QUIET", "no 7d impulse either way"
        if label == "SPOT-LED" and premium is not None:
            if premium >= 0.03:
                expl += ". Coinbase premium positive — US institutional bid confirms"
            elif premium <= -0.05:
                expl += ". Coinbase premium negative — offshore-led, weaker confirmation"
        drivers = {"label": label, "px_7d": round(px_7d, 1), "oi_7d": round(oi_7d, 1),
                   "funding_14d_ann_pct": funding_14d,
                   "coinbase_premium_pct": premium, "oi_source": oi_src, "detail": expl}

    return {
        "gates": gates, "closed": n_closed, "all_closed": all_closed,
        "supporting": supporting, "drivers": drivers,
        "killed": killed, "early_warning": early_warning, "starter": starter,
        "ema50w": round(ema50w, 0) if ema50w else None,
        "weeks_above_50w": consec,
        "etf_weekly": [{"week": w, "net_usd_m": v} for w, v in weeks[-6:]],
        "funding_14d_ann_pct": funding_14d,
        "verdict": verdict,
        "params": {"closes_needed": G1_CLOSES_NEEDED, "supply_shelf": G1_SUPPLY_SHELF,
                   "etf_weekly_usd_m": G2_ETF_WEEKLY_USD_M, "etf_weeks": G2_ETF_WEEKS_NEEDED,
                   "hold_line": G4_HOLD_LINE, "deadline": G4_DEADLINE,
                   "kill_weekly_close": KILL_WEEKLY_CLOSE},
    }


REALIZED_PRICE_FALLBACK = 52500.0  # Jul-2026 research value, used if CM is down


def run_monitor():
    daily = fetch_binance_klines("1d", 1000)    # covers 200DMA, 471SMA, 150EMA
    weekly = fetch_binance_klines("1w", 300)    # covers 200-week MA
    if len(daily) < 500 or len(weekly) < 200:
        return {"error": "Binance price fetch failed — cannot compute signals"}
    price = daily[-1]

    oc = fetch_onchain_metrics()
    if oc:
        mvrv, nupl = oc.get("mvrv"), oc.get("nupl")
        rp_live = oc.get("realized_price") is not None
        realized_price = oc["realized_price"] if rp_live else REALIZED_PRICE_FALLBACK
    else:
        mvrv = nupl = None
        realized_price = REALIZED_PRICE_FALLBACK
        rp_live = False

    # Short-term-holder cost basis (the reclaim line) fetched live so it
    # tracks the cohort as the rally ages; static constant as fallback.
    sth_live = _bg_last("sth-realized-price")
    if sth_live is not None and not (5000 < sth_live < 500000):
        sth_live = None
    sth = sth_live if sth_live is not None else STH_REALIZED_PRICE

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
    funding_rates = fetch_funding_rates()
    funding = _funding_ann(funding_rates)
    etf = fetch_etf_flows()            # ~30 sessions for the gates' weekly sums
    etf5 = etf[-5:] if etf else None   # the bottom checklist keeps its 5-day view

    # Notes are LIVE: current reading, the level where the signal fires, and
    # the distance from here — the history is the anchor, not the whole note.
    signals = []
    to_rp = (realized_price / price - 1) * 100
    signals.append(_sig("mvrv", "MVRV < 1 (price tags realized price)",
                        f"{mvrv:.2f}" if mvrv is not None else "n/a (feed down)",
                        "NA" if mvrv is None else
                        ("FIRED" if mvrv < 1 else ("CLOSE" if mvrv < 1.1 else "NOT_FIRED")),
                        f"Fires at price ≤ realized ${realized_price:,.0f} ({to_rp:+.1f}% from here)"
                        + ("" if rp_live else " (static research value)")
                        + " — fired at every 2015/2018/2022 low"))
    wma_gap = (price / wma200 - 1) * 100 if wma200 else None
    signals.append(_sig("wma200", "200-week MA touch",
                        f"${wma200:,.0f}" if wma200 else "n/a",
                        "FIRED" if (wma200 and price < wma200 * 1.02) else
                        ("CLOSE" if (wma200 and price < wma200 * 1.08) else "NOT_FIRED"),
                        (f"Price {wma_gap:+.1f}% vs the line; fires within +2% of it. "
                         if wma_gap is not None else "")
                        + "Every prior bear bottomed at/below it (2022 pierced -25%)"))
    signals.append(_sig("nupl", "NUPL < 0 (aggregate capitulation)",
                        f"{nupl:.2f}" if nupl is not None else "n/a (feed down)",
                        "NA" if nupl is None else
                        ("FIRED" if nupl < 0 else ("CLOSE" if nupl < 0.1 else "NOT_FIRED")),
                        f"Hits 0 when price tags realized ${realized_price:,.0f} ({to_rp:+.1f}% from "
                        "here); prior lows printed -0.20 to -0.25"))
    signals.append(_sig("puell", "Puell Multiple < 0.5",
                        f"{puell:.2f}" if puell else "n/a",
                        "NA" if puell is None else
                        ("FIRED" if puell < 0.5 else ("CLOSE" if puell < 0.8 else "NOT_FIRED")),
                        (f"Miner revenue must fall {(1 - 0.5 / puell) * 100:.0f}% from today's rate to "
                         "fire. " if puell and puell > 0.5 else "")
                        + "0.3-0.4 at prior lows; <1 = miner stress"))
    mayer_fire = 0.6 * dma200 if dma200 else None
    signals.append(_sig("mayer", "Mayer Multiple < 0.6",
                        f"{mayer:.2f}" if mayer else "n/a",
                        "NA" if mayer is None else
                        ("FIRED" if mayer < 0.6 else ("CLOSE" if mayer < 0.85 else "NOT_FIRED")),
                        (f"Fires at ${mayer_fire:,.0f} ({(mayer_fire / price - 1) * 100:+.1f}% from "
                         "here). " if mayer_fire else "")
                        + "Cycle bottoms printed ~0.5-0.6"))
    if ribbons:
        rib_gap = ((ribbons["sma30"] / ribbons["sma60"] - 1) * 100
                   if ribbons.get("sma60") else None)
        signals.append(_sig("ribbons", "Hash ribbons capitulation → recovery",
                            ribbons["state"].replace("_", " "),
                            "FIRED" if ribbons["state"] == "recovery_buy" else
                            ("CLOSE" if ribbons["state"] == "capitulation" else "NOT_FIRED"),
                            (f"30d hashrate {rib_gap:+.1f}% vs 60d. " if rib_gap is not None else "")
                            + "Fires on the capitulation→recovery cross; marked every bottom "
                            "since 2015 (can run early)"))
    pi_ratio = (ema150 / (0.745 * sma471)) if (ema150 and sma471) else None
    signals.append(_sig("pi", "Pi Cycle Bottom (150EMA < 0.745×471SMA)",
                        "in process" if pi_bottom else "no",
                        "FIRED" if pi_bottom else "NOT_FIRED",
                        (f"150EMA sits {pi_ratio:.2f}× the trigger line (fires under 1.00). "
                         if pi_ratio else "")
                        + "Lows form between the down-cross and the re-cross"))
    if fng:
        signals.append(_sig("fng", "Fear & Greed sustained < 25",
                            f"{fng['value']} ({fng['label']})",
                            "FIRED" if fng["sub25_streak_days"] >= 14 else
                            ("CLOSE" if fng["value"] < 30 else "NOT_FIRED"),
                            f"Needs 14 straight days under 25; now {fng['value']} with a "
                            f"{fng['sub25_streak_days']}d streak — regime gauge, not a timer"))
    if funding is not None:
        signals.append(_sig("funding", "Negative 30d avg funding",
                            f"{funding:+.1f}% ann.",
                            "FIRED" if funding < 0 else ("CLOSE" if funding < 3 else "NOT_FIRED"),
                            f"Longs paying {funding:+.1f}% ann. now; fires below zero. The 46-day "
                            "negative streak into Apr 2026 was the leverage washout"))
    # NOTE: spot ETF flows are deliberately NOT in this checklist. Inflow
    # resumption is a TURN signal, not a capitulation signal — it renders as
    # a supporting row in the Upside Gates card (and its scored weekly
    # version is Gate 2). Mixing it in here made the fired-count read like
    # bottom evidence when it was actually recovery evidence.

    scored = [s for s in signals if s["status"] != "NA"]
    fired = sum(1 for s in scored if s["status"] == "FIRED")

    today = datetime.now(timezone.utc).date()
    ws = datetime.strptime(WINDOW_START, "%Y-%m-%d").date()
    we = datetime.strptime(WINDOW_END, "%Y-%m-%d").date()
    in_window = ws <= today <= we

    behavioral = {"ribbons", "fng", "funding", "wma200"}
    beh_fired = sum(1 for s in scored if s["key"] in behavioral and s["status"] == "FIRED")
    val_fired = sum(1 for s in scored if s["key"] not in behavioral and s["status"] == "FIRED")
    verdict = (f"{fired}/{len(scored)} signals fired — "
               f"behavioral {beh_fired} fired, valuation {val_fired} fired. "
               + ("INSIDE the Oct 4 - Nov 20 template window: valuation signals firing now = maximum-deployment zone."
                  if in_window else
                  f"Template window opens {WINDOW_START} ({(ws - today).days} days). "
                  f"Reclaim of the ${sth / 1000:.0f}k STH cost-basis line before then favors "
                  "'bottom already in'; a close below realized price favors the Q4 capitulation path."))

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "price": round(price, 0),
        "referee": {
            "reclaim_line": round(sth, 0),
            "reclaim_line_live": sth_live is not None,
            "reclaim_distance_pct": round(100 * (sth - price) / price, 1),
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
            "onchain_source": (oc or {}).get("source"),
            "wma200": round(wma200, 0) if wma200 else None,
            "dma200": round(dma200, 0) if dma200 else None,
            "mayer": round(mayer, 3) if mayer else None,
            "puell": round(puell, 3) if puell else None,
            "funding_30d_ann_pct": funding,
            "fear_greed": fng,
            "etf_last5": etf5,
        },
        "signals": signals,
        "fired": fired,
        "scored": len(scored),
        "verdict": verdict,
        "gates": evaluate_gates(daily, weekly, funding_rates, etf,
                                *fetch_open_interest_7d(daily),
                                premium=fetch_coinbase_premium()),
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
    g = res.get("gates")
    if g:
        print(f"\nUPSIDE GATES ({g['closed']}/4 closed)")
        print("-" * 76)
        for s in g["gates"]:
            print(f"{s['name']:<34} {s['status']:>8}  {s['value']}")
        for s in g.get("supporting", []):
            print(f"{s['name']:<34} {s['status']:>8}  {s['value']}  [supporting]")
        d = g.get("drivers")
        if d:
            print(f"\nWho's driving: {d['label']} — 7d px {d['px_7d']:+.1f}%, "
                  f"OI {d['oi_7d']:+.1f}% ({d['oi_source']}), funding "
                  f"{d['funding_14d_ann_pct']:+.1f}% ann, CB premium "
                  f"{d['coinbase_premium_pct'] if d['coinbase_premium_pct'] is not None else 'n/a'}%"
                  f"\n  {d['detail']}")
        print(f"\n{g['verdict']}")
    print(f"\nCache written to {CACHE_PATH}")
