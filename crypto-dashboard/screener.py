"""
Altcoin Cycle Screener — Phase 1
================================

Nightly scan of the top ~1000 coins, scored on the factors that historically
preceded 10x-50x bull-cycle runs (research: July 2026):

  KILL FILTERS (excluded before scoring):
    - market cap outside $25M .. $2B (winners started $30M-$500M; >$2B has
      limited 10x headroom)
    - float ratio MC/FDV < 0.35 (2024's 12.3%-float class was a massacre:
      all 37 Binance 2024 listings went negative)
    - 24h volume / mcap < 0.5% (illiquidity → untradeable + rug correlate)
    - stablecoins / wrapped / staked-derivative tickers

  COMPOSITE SCORE 0-100 (weight — evidence):
    - float ratio        20  MC/FDV higher = better (unlock overhang proxy:
                             90% of 16k unlock events were price-negative)
    - revenue yield      20  annualized protocol fees / mcap (DeFiLlama);
                             the "real revenue" cohort outperformed 2025-26
    - momentum vs BTC    15  30d relative strength (JoF-grade priced factor,
                             decays past ~1 month → refresh often)
    - bear-market RS     10  200d vs BTC (cycle-horizon survivor signal)
    - narrative          15  member of expected 2027 leaders (AI, RWA,
                             perp-DEX, DePIN, prediction markets, stablecoin
                             infra); prior-cycle leaders excluded
    - listing headroom   10  NOT yet on Binance = future catalyst (+41% avg
                             day-one pop; never buy the pump itself)
    - liquidity          10  volume/mcap percentile (tradability gate)

  REGIME GAUGE:
    BTC dominance + %% of top-100 alts beating BTC over 30d (altseason
    proxy) + Fear & Greed → ACCUMULATE / ROTATE / DISTRIBUTE banner.
    Timing research: alt/BTC ratios bottom AFTER BTC's USD bottom; the
    explosive small-cap window is ~4-8 weeks after BTC's new ATH once
    dominance rolls under ~52-54%%. Distribute when altseason proxy >75.

Data sources (all free):
  - CoinGecko (optional demo key via COINGECKO_API_KEY in .env: 10k calls/mo,
    30/min; keyless works at ~5-15/min — the scan sleeps between calls)
  - DeFiLlama /protocols + /overview/fees (no key)
  - Binance /api/v3/exchangeInfo (listing detection, no key)
  - alternative.me Fear & Greed (no key)

Usage:
  python screener.py            # run a scan, print top 30, write cache
  python screener.py --top 50   # print more rows
  Flask: GET /api/screener      # serve cached scan
         GET /api/screener?refresh=1  # re-scan (takes ~60-90s keyless)

NOT FINANCIAL ADVICE. The screener shrinks the universe; it does not pick
winners. Base rate: ~10-15%% of alts make a new ATH in the next cycle.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

try:  # pull COINGECKO_API_KEY from .env when run standalone (CLI / cron)
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass

CACHE_PATH = os.path.join(os.path.dirname(__file__), "screener_cache.json")

CG_BASE = "https://api.coingecko.com/api/v3"
LLAMA_BASE = "https://api.llama.fi"

# Kill-filter bounds
MCAP_MIN = 25_000_000
MCAP_MAX = 2_000_000_000
FLOAT_MIN = 0.35
VOL_MCAP_MIN = 0.005

# CoinGecko category slugs for the expected 2027 narratives.
# Unknown/renamed slugs are skipped gracefully.
NARRATIVE_CATEGORIES = [
    "artificial-intelligence",
    "ai-agents",
    "real-world-assets-rwa",
    "decentralized-perpetuals",
    "depin",
    "prediction-markets",
    "stablecoin-protocol",
]

# Hard-excluded categories: tokenized equities/ETFs track their underlying
# stock — they cannot 10x from token dynamics and pollute the RWA narrative.
EXCLUDED_CATEGORIES = [
    "tokenized-stock",
    "tokenized-stocks",
    "tokenized-assets",
    "tokenized-etfs",
    "tokenized-treasury-bonds",
    "tokenized-gold",
    "tokenized-commodities",
]

# Commodity/asset wrappers that sometimes escape category tagging
EXCLUDED_SYMBOLS = {"paxg", "xaut", "kau", "kag"}

# 30d moves beyond this (vs BTC, in %) are treated as listing/repricing data
# artifacts and excluded; survivors are clamped so one outlier can't own the
# top momentum percentile.
RS_ARTIFACT_LIMIT = 1000.0
RS_CLAMP_HI = 300.0
RS_CLAMP_LO = -95.0

STABLE_SYMBOLS = {
    "usdt", "usdc", "dai", "tusd", "usdp", "gusd", "busd", "frax", "usde",
    "fdusd", "pyusd", "usdd", "usds", "usd1", "usdx", "eurc", "eurt", "rlusd",
}
WRAPPED_HINTS = ("wrapped", "staked", "bridged", "restaked", "peg", "binance-peg",
                 "xstock", "tokenized")


def _cg_headers():
    key = os.getenv("COINGECKO_API_KEY", "").strip()
    return {"x-cg-demo-api-key": key} if key else {}


def _get(url, params=None, headers=None, timeout=20, retries=2):
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code == 429:
                time.sleep(15 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception:
            if attempt == retries:
                return None
            time.sleep(3)
    return None


def _cg_sleep():
    # Keyless CG allows ~5-15 calls/min; a demo key allows 30/min.
    time.sleep(2.5 if os.getenv("COINGECKO_API_KEY", "").strip() else 6.0)


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

def fetch_markets(pages=4):
    """Top pages*250 coins with price changes needed for RS factors."""
    coins = []
    for page in range(1, pages + 1):
        data = _get(f"{CG_BASE}/coins/markets", params={
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": 250,
            "page": page,
            "price_change_percentage": "24h,7d,30d,200d",
        }, headers=_cg_headers())
        if not data:
            break
        coins.extend(data)
        _cg_sleep()
    return coins


def fetch_global():
    data = _get(f"{CG_BASE}/global", headers=_cg_headers())
    _cg_sleep()
    return (data or {}).get("data", {})


def fetch_narrative_members():
    """coin_id -> set of narrative category slugs it belongs to."""
    members = {}
    for slug in NARRATIVE_CATEGORIES:
        data = _get(f"{CG_BASE}/coins/markets", params={
            "vs_currency": "usd",
            "category": slug,
            "order": "market_cap_desc",
            "per_page": 250,
            "page": 1,
        }, headers=_cg_headers())
        if data:
            for c in data:
                members.setdefault(c["id"], set()).add(slug)
        _cg_sleep()
    return members


def fetch_excluded_ids():
    """coin_ids in hard-excluded categories (tokenized stocks/ETFs/bonds)."""
    excluded = set()
    for slug in EXCLUDED_CATEGORIES:
        data = _get(f"{CG_BASE}/coins/markets", params={
            "vs_currency": "usd",
            "category": slug,
            "order": "market_cap_desc",
            "per_page": 250,
            "page": 1,
        }, headers=_cg_headers())
        if data:
            excluded.update(c["id"] for c in data)
        _cg_sleep()
    return excluded


def fetch_llama_fees():
    """gecko_id -> annualized protocol fees (USD), best-effort mapping."""
    fees_by_gecko = {}
    protocols = _get(f"{LLAMA_BASE}/protocols") or []
    name_to_gecko = {}
    for p in protocols:
        gid = p.get("gecko_id")
        if gid:
            name_to_gecko[(p.get("name") or "").lower()] = gid
    # dataType=dailyRevenue = what accrues to the protocol/holders, NOT gross
    # fees (Lido-style staking passthrough inflates plain fees ~10x).
    overview = _get(f"{LLAMA_BASE}/overview/fees", params={
        "excludeTotalDataChart": "true",
        "excludeTotalDataChartBreakdown": "true",
        "dataType": "dailyRevenue",
    }) or {}
    for proto in overview.get("protocols", []):
        gid = proto.get("gecko_id") or name_to_gecko.get((proto.get("name") or "").lower())
        if not gid:
            continue
        fees_30d = proto.get("total30d")
        fees_24h = proto.get("total24h")
        annual = None
        if isinstance(fees_30d, (int, float)) and fees_30d > 0:
            annual = fees_30d * 12.17
        elif isinstance(fees_24h, (int, float)) and fees_24h > 0:
            annual = fees_24h * 365
        if annual:
            fees_by_gecko[gid] = fees_by_gecko.get(gid, 0) + annual
    return fees_by_gecko


def fetch_exchange_listings():
    """base symbol (lower) -> (exchange, tv_symbol).

    Fetched in priority order (Binance > Coinbase > Bybit > OKX > Kraken >
    MEXC > Gate); first hit wins, so tv_symbol points at the deepest venue.
    All endpoints are free/keyless. Matching is BY TICKER SYMBOL, so ticker
    collisions across venues are possible — always price-check the feed.
    """
    listings = {}

    def add(base, exch, tv):
        s = (base or "").lower()
        if s and s not in listings:
            listings[s] = (exch, tv)

    # data-api.binance.vision = official public mirror; api.binance.com
    # returns 451 (geo-block) from US CI runners, silently breaking the
    # "listed on Binance" factor
    data = _get("https://data-api.binance.vision/api/v3/exchangeInfo") \
        or _get("https://api.binance.com/api/v3/exchangeInfo")
    for s in (data or {}).get("symbols", []):
        if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING":
            add(s.get("baseAsset"), "BINANCE", f"BINANCE:{s.get('baseAsset')}USDT")

    data = _get("https://api.exchange.coinbase.com/products")
    for p in (data or []):
        if p.get("quote_currency") == "USD" and p.get("status") == "online":
            add(p.get("base_currency"), "COINBASE", f"COINBASE:{p.get('base_currency')}USD")

    cursor = ""
    for _ in range(5):
        data = _get("https://api.bybit.com/v5/market/instruments-info",
                    params={"category": "spot", "limit": 1000, "cursor": cursor})
        result = (data or {}).get("result", {})
        for s in result.get("list", []):
            if s.get("quoteCoin") == "USDT" and s.get("status") == "Trading":
                add(s.get("baseCoin"), "BYBIT", f"BYBIT:{s.get('baseCoin')}USDT")
        cursor = result.get("nextPageCursor") or ""
        if not cursor:
            break

    data = _get("https://www.okx.com/api/v5/public/instruments", params={"instType": "SPOT"})
    for s in (data or {}).get("data", []):
        if s.get("quoteCcy") == "USDT" and s.get("state") == "live":
            add(s.get("baseCcy"), "OKX", f"OKX:{s.get('baseCcy')}USDT")

    data = _get("https://api.kraken.com/0/public/AssetPairs")
    for p in ((data or {}).get("result") or {}).values():
        ws = p.get("wsname") or ""
        if ws.endswith("/USD"):
            base = ws.split("/")[0]
            add(base, "KRAKEN", f"KRAKEN:{base}USD")

    data = _get("https://api.mexc.com/api/v3/exchangeInfo")
    for s in (data or {}).get("symbols", []):
        if s.get("quoteAsset") == "USDT" and s.get("isSpotTradingAllowed"):
            add(s.get("baseAsset"), "MEXC", f"MEXC:{s.get('baseAsset')}USDT")

    data = _get("https://api.gateio.ws/api/v4/spot/currency_pairs")
    for p in (data or []):
        if p.get("quote") == "USDT" and p.get("trade_status") == "tradable":
            add(p.get("base"), "GATEIO", f"GATEIO:{p.get('base')}USDT")

    return listings


def fetch_fear_greed():
    data = _get("https://api.alternative.me/fng/", params={"limit": 1})
    try:
        row = data["data"][0]
        return {"value": int(row["value"]), "label": row["value_classification"]}
    except Exception:
        return {"value": None, "label": "unavailable"}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _is_excluded_asset(coin):
    sym = (coin.get("symbol") or "").lower()
    name = (coin.get("name") or "").lower()
    cid = (coin.get("id") or "").lower()
    if sym in STABLE_SYMBOLS or sym in EXCLUDED_SYMBOLS:
        return True
    if any(h in name or h in cid for h in WRAPPED_HINTS):
        return True
    # price pinned to $1 with negligible movement = stable we don't know about
    price = coin.get("current_price") or 0
    chg = abs(coin.get("price_change_percentage_24h") or 0)
    if 0.97 <= price <= 1.03 and chg < 0.3:
        return True
    return False


def _percentile_scores(values):
    """value list -> 0..1 percentile per index (None-safe, higher = better)."""
    idx = [i for i, v in enumerate(values) if v is not None]
    ranked = sorted(idx, key=lambda i: values[i])
    out = [0.0] * len(values)
    n = max(len(ranked) - 1, 1)
    for rank, i in enumerate(ranked):
        out[i] = rank / n
    return out


def run_scan():
    started = time.time()
    markets = fetch_markets()
    if not markets:
        return {"error": "CoinGecko markets fetch failed — check connectivity / rate limits"}

    glob = fetch_global()
    narratives = fetch_narrative_members()
    excluded_ids = fetch_excluded_ids()
    fees = fetch_llama_fees()
    listings = fetch_exchange_listings()
    binance_listed = {s for s, (exch, _) in listings.items() if exch == "BINANCE"}
    fng = fetch_fear_greed()

    btc = next((c for c in markets if c["id"] == "bitcoin"), {})
    btc_30d = btc.get("price_change_percentage_30d_in_currency") or 0
    btc_200d = btc.get("price_change_percentage_200d_in_currency") or 0

    # ── Regime gauge ──────────────────────────────────────────────────────
    top100 = [c for c in markets[:110] if not _is_excluded_asset(c) and c["id"] != "bitcoin"][:100]
    beat_btc = [
        c for c in top100
        if (c.get("price_change_percentage_30d_in_currency") or -999) > btc_30d
    ]
    alt_pct = round(100 * len(beat_btc) / max(len(top100), 1), 1)
    btc_dominance = round((glob.get("market_cap_percentage") or {}).get("btc", 0), 2)

    if alt_pct >= 75:
        regime, regime_note = "DISTRIBUTE", "Altseason proxy >75 — historically the euphoria window lasts weeks, not months. Take profits into strength."
    elif alt_pct >= 50:
        regime, regime_note = "ROTATE", "Alts broadly beating BTC — rotation underway. Add only names passing the screen; tighten stops on laggards."
    else:
        regime, regime_note = "ACCUMULATE", "BTC-dominant / bear regime — build tranche positions in screened names; broad altseason historically needs BTC.D under ~52-54%."

    # Float ratio on the most conservative supply basis. CoinGecko computes
    # FDV from total_supply, which for continuous-emission tokens (Bittensor
    # subnets and kin) equals emitted-so-far — so a token with 3/4 of its max
    # supply still to be emitted reads as a perfect 1.0 float. When
    # max_supply is declared, price × max_supply is the honest FDV ceiling;
    # use whichever basis is larger.
    def fr(c):
        mcap = c.get("market_cap")
        if not mcap:
            return None
        fdv = c.get("fully_diluted_valuation") or 0
        price, mx = c.get("current_price"), c.get("max_supply")
        if price and mx:
            fdv = max(fdv, price * mx)
        return (mcap / fdv) if fdv else None

    # ── Kill filters ──────────────────────────────────────────────────────
    candidates = []
    for c in markets:
        if c["id"] in ("bitcoin", "ethereum"):
            continue
        if c["id"] in excluded_ids:
            continue
        if _is_excluded_asset(c):
            continue
        mcap = c.get("market_cap") or 0
        if not (MCAP_MIN <= mcap <= MCAP_MAX):
            continue
        float_ratio = fr(c)
        if float_ratio is not None and float_ratio < FLOAT_MIN:
            continue
        vol = c.get("total_volume") or 0
        if mcap and (vol / mcap) < VOL_MCAP_MIN:
            continue
        chg30 = c.get("price_change_percentage_30d_in_currency")
        if chg30 is not None and (chg30 - btc_30d) > RS_ARTIFACT_LIMIT:
            continue  # listing/repricing data artifact, not tradeable momentum
        candidates.append(c)

    # ── Factor arrays ─────────────────────────────────────────────────────
    float_ratios = [fr(c) for c in candidates]
    rev_yields = []
    for c in candidates:
        annual = fees.get(c["id"])
        rev_yields.append((annual / c["market_cap"]) if (annual and c.get("market_cap")) else None)
    def _clamp_rs(v):
        return None if v is None else max(RS_CLAMP_LO, min(RS_CLAMP_HI, v))

    mom_30 = [
        _clamp_rs(c.get("price_change_percentage_30d_in_currency") - btc_30d)
        if c.get("price_change_percentage_30d_in_currency") is not None else None
        for c in candidates
    ]
    rs_200 = [
        _clamp_rs(c.get("price_change_percentage_200d_in_currency") - btc_200d)
        if c.get("price_change_percentage_200d_in_currency") is not None else None
        for c in candidates
    ]
    vol_ratios = [
        (c["total_volume"] / c["market_cap"]) if c.get("market_cap") else None
        for c in candidates
    ]

    p_float = _percentile_scores(float_ratios)
    p_rev = _percentile_scores(rev_yields)
    p_mom = _percentile_scores(mom_30)
    p_rs = _percentile_scores(rs_200)
    p_vol = _percentile_scores(vol_ratios)

    rows = []
    for i, c in enumerate(candidates):
        narr = sorted(narratives.get(c["id"], set()))
        sym_lower = (c.get("symbol") or "").lower()
        on_binance = sym_lower in binance_listed
        exch, tv_symbol = listings.get(sym_lower, (None, None))
        has_rev = rev_yields[i] is not None

        score = (
            20 * (p_float[i] if float_ratios[i] is not None else 0.5)  # unknown FDV = neutral
            + 20 * (p_rev[i] if has_rev else 0.0)
            + 15 * p_mom[i]
            + 10 * p_rs[i]
            + 15 * (1.0 if narr else 0.0)
            + 10 * (1.0 if not on_binance else 0.0)
            + 10 * p_vol[i]
        )

        rows.append({
            "id": c["id"],
            "symbol": (c.get("symbol") or "").upper(),
            "name": c.get("name"),
            "score": round(score, 1),
            "price": c.get("current_price"),
            "mcap": c.get("market_cap"),
            "mcap_rank": c.get("market_cap_rank"),
            "float_ratio": round(float_ratios[i], 3) if float_ratios[i] is not None else None,
            "rev_yield_pct": round(100 * rev_yields[i], 2) if rev_yields[i] is not None else None,
            "chg_30d_vs_btc": round(mom_30[i], 1) if mom_30[i] is not None else None,
            "chg_200d_vs_btc": round(rs_200[i], 1) if rs_200[i] is not None else None,
            "ath_drawdown_pct": round(c.get("ath_change_percentage"), 1) if c.get("ath_change_percentage") is not None else None,
            "narratives": narr,
            "on_binance": on_binance,
            "exchange": exch,
            "tv_symbol": tv_symbol,
            "vol_mcap_pct": round(100 * vol_ratios[i], 2) if vol_ratios[i] is not None else None,
        })

    rows.sort(key=lambda r: r["score"], reverse=True)

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scan_seconds": round(time.time() - started, 1),
        "universe": len(markets),
        "passed_filters": len(rows),
        "regime": {
            "label": regime,
            "note": regime_note,
            "btc_dominance": btc_dominance,
            "altseason_proxy_pct": alt_pct,
            "fear_greed": fng,
            "btc_30d_pct": round(btc_30d, 1),
        },
        "rows": rows[:100],
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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    top_n = 30
    if "--top" in sys.argv:
        try:
            top_n = int(sys.argv[sys.argv.index("--top") + 1])
        except (IndexError, ValueError):
            pass

    print("Scanning (60-90s keyless, ~40s with a CoinGecko demo key)...")
    res = run_scan()
    if res.get("error"):
        print("ERROR:", res["error"])
        sys.exit(1)

    reg = res["regime"]
    print(f"\n=== REGIME: {reg['label']} ===")
    print(f"BTC dominance {reg['btc_dominance']}% | altseason proxy {reg['altseason_proxy_pct']}% "
          f"| F&G {reg['fear_greed']['value']} ({reg['fear_greed']['label']})")
    print(reg["note"])
    print(f"\nUniverse {res['universe']} | passed filters {res['passed_filters']} "
          f"| scan {res['scan_seconds']}s\n")

    hdr = f"{'#':>3} {'SYM':<10} {'SCORE':>5} {'MCAP $M':>9} {'FLOAT':>6} {'REV%':>6} {'30dRS':>7} {'200dRS':>7} {'BIN':>4}  NARRATIVES"
    print(hdr)
    print("-" * len(hdr))
    for i, r in enumerate(res["rows"][:top_n], 1):
        print(f"{i:>3} {r['symbol']:<10} {r['score']:>5} "
              f"{(r['mcap'] or 0) / 1e6:>9,.0f} "
              f"{r['float_ratio'] if r['float_ratio'] is not None else '—':>6} "
              f"{r['rev_yield_pct'] if r['rev_yield_pct'] is not None else '—':>6} "
              f"{r['chg_30d_vs_btc'] if r['chg_30d_vs_btc'] is not None else '—':>7} "
              f"{r['chg_200d_vs_btc'] if r['chg_200d_vs_btc'] is not None else '—':>7} "
              f"{'yes' if r['on_binance'] else 'NO':>4}  "
              f"{','.join(n.split('-')[0] for n in r['narratives']) or '—'}")
    print(f"\nCache written to {CACHE_PATH}")
