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


def fetch_stock(stooq_sym):
    """Last two daily closes from stooq history CSV -> (price, chg_pct, date)."""
    try:
        r = requests.get("https://stooq.com/q/d/l/", params={"s": stooq_sym, "i": "d"},
                         timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        rows = list(csv.DictReader(io.StringIO(r.text)))
        closes = [(row["Date"], float(row["Close"])) for row in rows if row.get("Close")]
        if not closes:
            return None
        date, price = closes[-1]
        chg = None
        if len(closes) >= 2:
            prev = closes[-2][1]
            if prev:
                chg = round(100 * (price - prev) / prev, 2)
        return {"price": price, "chg_pct": chg, "date": date}
    except Exception as e:
        print(f"  {stooq_sym}: {e}", file=sys.stderr)
        return None


def main():
    out = {}
    for sym, stooq_sym in STOCKS.items():
        q = fetch_stock(stooq_sym)
        if q:
            out[sym] = q
            print(f"  {sym}: ${q['price']} ({q['chg_pct']}%)")
    with open(CACHE_PATH, "w") as f:
        json.dump(out, f)
    print(f"{len(out)}/{len(STOCKS)} stocks fetched -> {CACHE_PATH}")


if __name__ == "__main__":
    main()
