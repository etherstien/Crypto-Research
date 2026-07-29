"""
Nightly stock quotes for the website watchlist (crypto is fetched live in
the browser via CoinGecko; stocks have no free CORS API, so the workflow
pulls them from stooq.com's free CSV endpoint instead).

Writes stocks_cache.json: {SYM: {price, chg_pct, date}}.
"""

import csv
import io
import json
import os
import sys

import requests

CACHE_PATH = os.path.join(os.path.dirname(__file__), "stocks_cache.json")

STOCKS = {
    "COIN": "coin.us", "CRCL": "crcl.us", "MSFT": "msft.us", "GOOGL": "googl.us",
    "HOOD": "hood.us", "GLXY": "glxy.us", "SPCX": "spcx.us", "ORBS": "orbs.us",
}


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
    prev = meta.get("chartPreviousClose") or (closes[-2] if len(closes) >= 2 else None)
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
    out = {}
    for sym, stooq_sym in STOCKS.items():
        q = fetch_stock(sym, stooq_sym)
        if q:
            out[sym] = q
            print(f"  {sym}: ${q['price']} ({q['chg_pct']}%)")
    with open(CACHE_PATH, "w") as f:
        json.dump(out, f)
    print(f"{len(out)}/{len(STOCKS)} stocks fetched -> {CACHE_PATH}")


if __name__ == "__main__":
    main()
