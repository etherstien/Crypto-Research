import os
import hmac
import hashlib
import base64
import time
import json
import requests
from datetime import datetime, timezone
from flask import Flask, jsonify, render_template
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _prices_for(symbols: list[str]) -> dict[str, float]:
    """Fetch USD prices from CoinGecko (free, no key required)."""
    COIN_MAP = {
        "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
        "XRP": "ripple", "ADA": "cardano", "DOGE": "dogecoin",
        "DOT": "polkadot", "MATIC": "matic-network", "AVAX": "avalanche-2",
        "LINK": "chainlink", "UNI": "uniswap", "ATOM": "cosmos",
        "LTC": "litecoin", "BCH": "bitcoin-cash", "ALGO": "algorand",
        "XLM": "stellar", "VET": "vechain", "NEAR": "near",
        "FTM": "fantom", "SAND": "the-sandbox", "MANA": "decentraland",
        "SHIB": "shiba-inu", "TRX": "tron", "ETC": "ethereum-classic",
        "XMR": "monero", "AAVE": "aave", "MKR": "maker",
        "COMP": "compound-governance-token", "SUSHI": "sushi",
        "YFI": "yearn-finance", "SNX": "havven", "CRV": "curve-dao-token",
        "1INCH": "1inch", "BAL": "balancer", "GRT": "the-graph",
        "FIL": "filecoin", "CHZ": "chiliz", "ENJ": "enjincoin",
        "AXS": "axie-infinity", "ICP": "internet-computer",
        "THETA": "theta-token", "EOS": "eos", "ZEC": "zcash",
        "DASH": "dash", "BAT": "basic-attention-token",
        "ZIL": "zilliqa", "QTUM": "qtum", "OMG": "omisego",
        "IOTA": "iota", "NEO": "neo", "WAVES": "waves",
        "USDT": "tether", "USDC": "usd-coin", "BUSD": "binance-usd",
        "DAI": "dai", "TUSD": "true-usd",
    }
    ids = [COIN_MAP[s] for s in symbols if s in COIN_MAP]
    if not ids:
        return {}
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": ",".join(ids), "vs_currencies": "usd"},
            timeout=10,
        )
        data = resp.json()
        result = {}
        for sym in symbols:
            cg_id = COIN_MAP.get(sym)
            if cg_id and cg_id in data:
                result[sym] = data[cg_id]["usd"]
        return result
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# OKX
# ---------------------------------------------------------------------------

def _okx_sign(timestamp: str, method: str, path: str, body: str = "") -> str:
    msg = timestamp + method.upper() + path + body
    secret = os.getenv("OKX_API_SECRET", "")
    return base64.b64encode(
        hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()


def fetch_okx() -> dict:
    api_key = os.getenv("OKX_API_KEY", "")
    passphrase = os.getenv("OKX_PASSPHRASE", "")
    if not api_key:
        return {"exchange": "OKX", "error": "API credentials not configured", "positions": []}

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    path = "/api/v5/account/balance"
    sig = _okx_sign(ts, "GET", path)

    headers = {
        "OK-ACCESS-KEY": api_key,
        "OK-ACCESS-SIGN": sig,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
    }
    try:
        resp = requests.get(f"https://www.okx.com{path}", headers=headers, timeout=10)
        data = resp.json()
        if data.get("code") != "0":
            return {"exchange": "OKX", "error": data.get("msg", "Unknown error"), "positions": []}

        details = data["data"][0].get("details", [])
        positions = []
        for d in details:
            qty = _safe_float(d.get("cashBal"))
            if qty > 0:
                positions.append({"symbol": d["ccy"], "amount": qty, "usd_value": _safe_float(d.get("eqUsd"))})
        return {"exchange": "OKX", "positions": positions}
    except Exception as e:
        return {"exchange": "OKX", "error": str(e), "positions": []}


# ---------------------------------------------------------------------------
# Binance
# ---------------------------------------------------------------------------

def fetch_binance() -> dict:
    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    if not api_key:
        return {"exchange": "Binance", "error": "API credentials not configured", "positions": []}

    ts = int(time.time() * 1000)
    query = f"timestamp={ts}"
    sig = hmac.new(api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"https://api.binance.com/api/v3/account?{query}&signature={sig}"
    headers = {"X-MBX-APIKEY": api_key}

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()
        if "code" in data and data["code"] < 0:
            return {"exchange": "Binance", "error": data.get("msg", "Unknown error"), "positions": []}

        balances = data.get("balances", [])
        symbols = [b["asset"] for b in balances if _safe_float(b.get("free", 0)) + _safe_float(b.get("locked", 0)) > 0]
        prices = _prices_for(symbols)

        positions = []
        for b in balances:
            qty = _safe_float(b.get("free", 0)) + _safe_float(b.get("locked", 0))
            if qty > 0:
                sym = b["asset"]
                price = prices.get(sym, 0)
                positions.append({"symbol": sym, "amount": qty, "usd_value": qty * price if price else None})
        return {"exchange": "Binance", "positions": positions}
    except Exception as e:
        return {"exchange": "Binance", "error": str(e), "positions": []}


# ---------------------------------------------------------------------------
# Kraken
# ---------------------------------------------------------------------------

# Kraken uses non-standard ticker symbols; map them to display names
KRAKEN_TICKER_MAP = {
    "XXBT": "BTC", "XETH": "ETH", "XLTC": "LTC", "XXRP": "XRP",
    "XXLM": "XLM", "XZEC": "ZEC", "XXMR": "XMR", "XEOS": "EOS",
    "XETC": "ETC", "XXDG": "DOGE", "ZUSD": "USD", "ZEUR": "EUR",
    "ZGBP": "GBP", "ZCAD": "CAD", "ZJPY": "JPY",
}


def _kraken_sign(path: str, data: dict, secret: str) -> str:
    nonce = data["nonce"]
    post_data = "&".join([f"{k}={v}" for k, v in data.items()])
    encoded = (str(nonce) + post_data).encode()
    msg = path.encode() + hashlib.sha256(encoded).digest()
    secret_bytes = base64.b64decode(secret)
    return base64.b64encode(hmac.new(secret_bytes, msg, hashlib.sha512).digest()).decode()


def fetch_kraken() -> dict:
    api_key = os.getenv("KRAKEN_API_KEY", "")
    api_secret = os.getenv("KRAKEN_API_SECRET", "")
    if not api_key:
        return {"exchange": "Kraken", "error": "API credentials not configured", "positions": []}

    path = "/0/private/Balance"
    nonce = str(int(time.time() * 1000))
    data = {"nonce": nonce}
    sig = _kraken_sign(path, data, api_secret)
    headers = {"API-Key": api_key, "API-Sign": sig}

    try:
        resp = requests.post(f"https://api.kraken.com{path}", headers=headers, data=data, timeout=10)
        result = resp.json()
        errors = result.get("error", [])
        if errors:
            return {"exchange": "Kraken", "error": ", ".join(errors), "positions": []}

        balances = result.get("result", {})
        symbols_raw = [k for k, v in balances.items() if _safe_float(v) > 0]
        display_symbols = [KRAKEN_TICKER_MAP.get(s, s) for s in symbols_raw]
        prices = _prices_for(display_symbols)

        positions = []
        for raw_sym, qty_str in balances.items():
            qty = _safe_float(qty_str)
            if qty > 0:
                sym = KRAKEN_TICKER_MAP.get(raw_sym, raw_sym)
                price = prices.get(sym, 0)
                positions.append({"symbol": sym, "amount": qty, "usd_value": qty * price if price else None})
        return {"exchange": "Kraken", "positions": positions}
    except Exception as e:
        return {"exchange": "Kraken", "error": str(e), "positions": []}


# ---------------------------------------------------------------------------
# Robinhood
# ---------------------------------------------------------------------------

def fetch_robinhood() -> dict:
    username = os.getenv("ROBINHOOD_USERNAME", "")
    password = os.getenv("ROBINHOOD_PASSWORD", "")
    totp_key = os.getenv("ROBINHOOD_TOTP_KEY", "")
    if not username:
        return {"exchange": "Robinhood", "error": "Credentials not configured", "positions": []}

    try:
        import robin_stocks.robinhood as rh
        import pyotp

        mfa = None
        if totp_key:
            totp = pyotp.TOTP(totp_key)
            mfa = totp.now()

        rh.login(username, password, mfa_code=mfa, store_session=False)
        holdings = rh.get_crypto_positions()

        symbols = [h["currency"]["code"] for h in holdings if _safe_float(h.get("quantity", 0)) > 0]
        prices = _prices_for(symbols)

        positions = []
        for h in holdings:
            qty = _safe_float(h.get("quantity", 0))
            if qty > 0:
                sym = h["currency"]["code"]
                price = prices.get(sym, 0)
                positions.append({"symbol": sym, "amount": qty, "usd_value": qty * price if price else None})

        rh.logout()
        return {"exchange": "Robinhood", "positions": positions}
    except ImportError:
        return {"exchange": "Robinhood", "error": "robin_stocks / pyotp not installed", "positions": []}
    except Exception as e:
        return {"exchange": "Robinhood", "error": str(e), "positions": []}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/positions")
def positions():
    results = []
    for fetch_fn in [fetch_okx, fetch_binance, fetch_kraken, fetch_robinhood]:
        results.append(fetch_fn())
    return jsonify({"exchanges": results, "fetched_at": datetime.utcnow().isoformat() + "Z"})


@app.route("/api/positions/<exchange>")
def positions_single(exchange: str):
    fn_map = {
        "okx": fetch_okx,
        "binance": fetch_binance,
        "kraken": fetch_kraken,
        "robinhood": fetch_robinhood,
    }
    fn = fn_map.get(exchange.lower())
    if not fn:
        return jsonify({"error": "Unknown exchange"}), 404
    return jsonify(fn())


if __name__ == "__main__":
    app.run(debug=True, port=5000)
