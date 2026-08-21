"""
Stock quotes for the website watchlist (crypto is fetched live in the
browser via CoinGecko; stocks have no free CORS API, so a workflow pulls
them server-side: Yahoo's keyless chart endpoint, stooq CSV as fallback).

Runs in the nightly screener scan AND in the stock-quotes workflow every
30 minutes during US market hours (.github/workflows/stocks.yml) -- a
once-a-day quote left rows showing midnight prices through 5-10% sessions.

Writes stocks_cache.json: {"_generated_at": iso, SYM: {price, chg_pct, date}}.
"""

import csv
import io
import json
import os
import sys
import time

import requests

HERE = os.path.dirname(__file__)
CACHE_PATH = os.path.join(HERE, "stocks_cache.json")
WATCHLIST_PATH = os.path.join(HERE, "..", "web", "watchlist.json")

# Fallback list; the live list is every type=stock row in web/watchlist.json
# so adding a stock to the site automatically gets it quoted.
STOCKS = {
    "COIN": "coin.us", "CRCL": "crcl.us", "MSFT": "msft.us", "GOOGL": "googl.us",
    "HOOD": "hood.us", "GLXY": "glxy.us", "SPCX": "spcx.us", "ORBS": "orbs.us",
    "NVDA": "nvda.us",
}


def watchlist_stocks():
    """{SYM: stooq_sym} for every stock row on the site, plus the fallbacks."""
    syms = dict(STOCKS)
    try:
        with open(WATCHLIST_PATH) as f:
            cfg = json.load(f)
        for g in cfg.get("groups", []):
            for r in g.get("rows", []):
                if r.get("type") == "stock" and r.get("sym"):
                    syms.setdefault(r["sym"].upper(), f"{r['sym'].lower()}.us")
    except Exception as e:  # noqa: BLE001
        print(f"  watchlist.json not read ({e}); using built-in list")
    return syms


def fetch_yahoo(sym):
    """Yahoo v8 chart endpoint — keyless, works from CI runners."""
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                     params={"range": "5d", "interval": "1d"}, timeout=20,
                     headers={"User-Agent": "Mozilla/5.0 (watchlist)"})
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    meta = res.get("meta", {})
    closes = [c for c in res["indicators"]["quote"][0].get("close", []) if c is not None]
    price = meta.get("regularMarketPrice") or (closes[-1] if closes else None)
    # closes[-2] = yesterday's close (true 1-day change); chartPreviousClose
    # is relative to the 5d range start and overstates the move
    prev = (closes[-2] if len(closes) >= 2 else None) or meta.get("chartPreviousClose")
    if price is None:
        return None
    chg = round(100 * (price - prev) / prev, 2) if prev else None
    return {"price": round(float(price), 2), "chg_pct": chg, "date": "live"}


def fetch_stooq(stooq_sym):
    """Fallback: stooq daily-history CSV (rate-limited from datacenter IPs)."""
    r = requests.get("https://stooq.com/q/d/l/", params={"s": stooq_sym, "i": "d"},
                     timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(r.text)))
    closes = [(row["Date"], float(row["Close"])) for row in rows if row.get("Close")]
    if not closes:
        return None
    date, price = closes[-1]
    chg = None
    if len(closes) >= 2 and closes[-2][1]:
        chg = round(100 * (price - closes[-2][1]) / closes[-2][1], 2)
    return {"price": price, "chg_pct": chg, "date": date}


def fetch_stock(sym, stooq_sym):
    for fn, arg in ((fetch_yahoo, sym), (fetch_stooq, stooq_sym)):
        try:
            q = fn(arg)
            if q:
                return q
        except Exception as e:
            print(f"  {sym} via {fn.__name__}: {e}")
    return None


def main():
    stocks = watchlist_stocks()
    out = {"_generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    n = 0
    for sym, stooq_sym in stocks.items():
        q = fetch_stock(sym, stooq_sym)
        if q:
            out[sym] = q
            n += 1
            print(f"  {sym}: ${q['price']} ({q['chg_pct']}%)")
    if n == 0:
        # Don't write an empty cache over a good one; let the workflow keep
        # the previous stocks.json.
        sys.exit("no stock quotes fetched; cache left untouched")
    with open(CACHE_PATH, "w") as f:
        json.dump(out, f)
    print(f"{n}/{len(stocks)} stocks fetched -> {CACHE_PATH}")


if __name__ == "__main__":
    main()
