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
    # Extended
    "BNB": "binancecoin", "OP": "optimism", "ARB": "arbitrum",
    "APT": "aptos", "SUI": "sui", "SEI": "sei-network",
    "INJ": "injective-protocol", "TIA": "celestia", "JUP": "jupiter-exchange-solana",
    "PYTH": "pyth-network", "WIF": "dogwifcoin", "BONK": "bonk",
    "PEPE": "pepe", "FLOKI": "floki", "WLD": "worldcoin-wld",
    "APE": "apecoin", "LDO": "lido-dao", "RUNE": "thorchain",
    "RENDER": "render-token", "RNDR": "render-token",
    "ENA": "ethena", "ETHENA": "ethena",
    "SUI": "sui", "BLUR": "blur", "IMX": "immutable-x",
    "DYDX": "dydx-chain", "GMX": "gmx", "PENDLE": "pendle",
    "STRK": "starknet", "MANTA": "manta-network", "ALT": "altlayer",
    "JTO": "jito-governance-token", "ONDO": "ondo-finance",
    "W": "wormhole", "BOME": "book-of-meme", "SLERF": "slerf",
    "MOTHER": "mother-iggy", "MEW": "cat-in-a-dogs-world",
    "CC": "clash-of-clans-cc",
}

# Cache for dynamically resolved symbols
_dyn_symbol_cache: dict[str, str | None] = {}


def _resolve_unknown(symbol: str) -> str | None:
    """Search CoinGecko for a symbol not in the static map."""
    if symbol in _dyn_symbol_cache:
        return _dyn_symbol_cache[symbol]
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/search",
            params={"query": symbol},
            timeout=5,
        )
        coins = resp.json().get("coins", [])
        exact = [c for c in coins if c.get("symbol", "").upper() == symbol.upper()]
        if exact:
            best = min(exact, key=lambda c: c.get("market_cap_rank") or 99999)
            _dyn_symbol_cache[symbol] = best["id"]
            return best["id"]
    except Exception:
        pass
    _dyn_symbol_cache[symbol] = None
    return None


def _prices_for(symbols: list[str]) -> dict[str, float]:
    """Fetch USD prices from CoinGecko; resolves unknown symbols dynamically."""
    known_ids = {}
    unknown = []
    for s in symbols:
        if s in COIN_MAP:
            known_ids[s] = COIN_MAP[s]
        elif s not in ("USD", "EUR", "GBP", "JPY", "CAD"):
            unknown.append(s)

    # Resolve unknown symbols via CoinGecko search
    for s in unknown:
        cg_id = _resolve_unknown(s)
        if cg_id:
            known_ids[s] = cg_id

    if not known_ids:
        return {}

    ids = list(set(known_ids.values()))
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": ",".join(ids), "vs_currencies": "usd"},
            timeout=10,
        )
        data = resp.json()
        result = {}
        for sym, cg_id in known_ids.items():
            if cg_id in data:
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


def _load_manual(exchange: str) -> list[dict] | None:
    path = os.path.join(os.path.dirname(__file__), "manual_positions.json")
    try:
        with open(path) as f:
            data = json.load(f)
        return data.get(exchange)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def fetch_okx() -> dict:
    api_key   = os.getenv("OKX_API_KEY",    "").strip()
    api_secret = os.getenv("OKX_API_SECRET", "").strip()
    passphrase = os.getenv("OKX_PASSPHRASE", "").strip()
    if not api_key or not api_secret:
        manual = _load_manual("OKX")
        if manual:
            return {"exchange": "OKX", "positions": manual, "source": "manual"}
        return {"exchange": "OKX", "error": "API credentials not configured", "positions": []}

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    path = "/api/v5/account/balance"

    msg = ts + "GET" + path
    sig = base64.b64encode(
        hmac.new(api_secret.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()

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
            return {"exchange": "OKX", "error": f"[{data.get('code')}] {data.get('msg', 'Unknown error')}", "positions": []}

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
    api_key    = os.getenv("BINANCE_API_KEY",    "").strip()
    api_secret = os.getenv("BINANCE_API_SECRET", "").strip()
    tld = os.getenv("BINANCE_TLD", "com").strip()
    if not api_key or not api_secret:
        return {"exchange": "Binance", "error": "API credentials not configured", "positions": []}

    base = f"https://api.binance.{tld}"
    try:
        # Use Binance server time to avoid clock-skew errors
        ts = requests.get(f"{base}/api/v3/time", timeout=5).json()["serverTime"]
    except Exception:
        ts = int(time.time() * 1000)

    query = f"timestamp={ts}"
    sig = hmac.new(api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{base}/api/v3/account?{query}&signature={sig}"
    headers = {"X-MBX-APIKEY": api_key}

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()
        if "code" in data and data["code"] < 0:
            return {"exchange": "Binance", "error": data.get("msg", "Unknown error"), "positions": []}

        balances = data.get("balances", [])
        non_zero = [b for b in balances if _safe_float(b.get("free", 0)) + _safe_float(b.get("locked", 0)) > 0]
        symbols = [b["asset"] for b in non_zero]
        prices = _prices_for(symbols)

        positions = []
        for b in non_zero:
            qty = _safe_float(b.get("free", 0)) + _safe_float(b.get("locked", 0))
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
    api_key    = os.getenv("KRAKEN_API_KEY",    "").strip()
    api_secret = os.getenv("KRAKEN_API_SECRET", "").strip()
    if not api_key or not api_secret:
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
    username = os.getenv("ROBINHOOD_USERNAME", "").strip()
    password = os.getenv("ROBINHOOD_PASSWORD", "").strip()
    totp_key = os.getenv("ROBINHOOD_TOTP_KEY", "").strip()
    if not username or not password:
        return {"exchange": "Robinhood", "error": "Credentials not configured", "positions": []}
    try:
        import robin_stocks.robinhood as rh
        mfa = None
        if totp_key:
            import pyotp
            mfa = pyotp.TOTP(totp_key).now()
        # store_session=True saves the token to disk; after first device approval
        # subsequent logins reuse the token automatically with no prompt.
        rh.login(username, password, mfa_code=mfa, store_session=True, expiresIn=86400)

        holdings = rh.get_crypto_positions()
        non_zero = [h for h in holdings if _safe_float(h.get("quantity", 0)) > 0]
        symbols = [h["currency"]["code"] for h in non_zero]
        prices = _prices_for(symbols)

        positions = []
        for h in non_zero:
            qty = _safe_float(h.get("quantity", 0))
            sym = h["currency"]["code"]
            price = prices.get(sym, 0)
            positions.append({"symbol": sym, "amount": qty, "usd_value": qty * price if price else None})
        return {"exchange": "Robinhood", "positions": positions}
    except ImportError:
        return {"exchange": "Robinhood", "error": "robin_stocks not installed — run: pip install robin_stocks pyotp", "positions": []}
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
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
    fns = [fetch_okx, fetch_binance, fetch_kraken, fetch_robinhood]
    names = ["OKX", "Binance", "Kraken", "Robinhood"]
    results = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(fn): name for fn, name in zip(fns, names)}
        for fut, name in futures.items():
            try:
                results.append(fut.result(timeout=20))
            except FuturesTimeout:
                results.append({"exchange": name, "error": "Request timed out", "positions": []})
            except Exception as e:
                results.append({"exchange": name, "error": str(e), "positions": []})
    results.sort(key=lambda r: names.index(r["exchange"]))
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
