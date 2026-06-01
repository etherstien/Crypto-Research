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

STABLECOINS = {"USD", "USDT", "USDC", "BUSD", "DAI", "TUSD", "USDP", "GUSD", "EUR", "GBP", "JPY", "CAD"}


def _load_cost_basis() -> dict:
    path = os.path.join(os.path.dirname(__file__), "cost_basis.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _attach_returns(positions: list[dict], exchange: str) -> list[dict]:
    """Merge cost basis into positions and calculate return $ and %."""
    basis = _load_cost_basis().get(exchange, {})
    for p in positions:
        sym        = p["symbol"]
        cost       = basis.get(sym)
        usd_val    = p.get("usd_value")
        qty        = p.get("amount", 0)
        # Derive current price from usd_value / qty when available
        cur_price  = (usd_val / qty) if (usd_val and qty) else None
        if cost and cur_price:
            p["cost_price"] = cost
            p["pnl"]        = round((cur_price - cost) * qty, 2)
            p["pnl_pct"]    = round((cur_price - cost) / cost * 100, 2)
        else:
            p.setdefault("cost_price", cost)
            p.setdefault("pnl",        None)
            p.setdefault("pnl_pct",    None)
    return positions


# ---------------------------------------------------------------------------
# OKX
# ---------------------------------------------------------------------------

def _load_manual(exchange: str) -> list[dict] | None:
    path = os.path.join(os.path.dirname(__file__), "manual_positions.json")
    try:
        with open(path) as f:
            data = json.load(f)
        return data.get(exchange)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _okx_live_prices(symbols: list[str]) -> dict[str, float]:
    """Fetch live prices from OKX public market API — no auth required."""
    prices = {}
    for sym in symbols:
        if sym in STABLECOINS:
            prices[sym] = 1.0
            continue
        try:
            resp = requests.get(
                "https://www.okx.com/api/v5/market/ticker",
                params={"instId": f"{sym}-USDT"},
                timeout=5,
            )
            data = resp.json()
            if data.get("code") == "0" and data["data"]:
                prices[sym] = _safe_float(data["data"][0].get("last"))
        except Exception:
            pass
    return prices


def fetch_okx() -> dict:
    api_key    = os.getenv("OKX_API_KEY",    "").strip()
    api_secret = os.getenv("OKX_API_SECRET", "").strip()
    passphrase = os.getenv("OKX_PASSPHRASE", "").strip()

    # Always try authenticated API first
    if api_key and api_secret:
        ts      = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        bal_path = "/api/v5/account/balance"
        sig     = base64.b64encode(
            hmac.new(api_secret.encode(), (ts + "GET" + bal_path).encode(), hashlib.sha256).digest()
        ).decode()
        headers = {
            "OK-ACCESS-KEY": api_key,
            "OK-ACCESS-SIGN": sig,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": passphrase,
            "Content-Type": "application/json",
        }
        try:
            resp = requests.get(f"https://www.okx.com{bal_path}", headers=headers, timeout=10)
            data = resp.json()
            if data.get("code") == "0":
                positions = []
                for d in data["data"][0].get("details", []):
                    qty = _safe_float(d.get("cashBal"))
                    if qty > 0:
                        positions.append({
                            "symbol":    d["ccy"],
                            "amount":    qty,
                            "usd_value": _safe_float(d.get("eqUsd")) or None,
                        })
                return {"exchange": "OKX", "positions": _attach_returns(positions, "OKX")}
        except Exception:
            pass  # fall through to manual mode

    # Fall back to manual balances + live OKX public prices
    manual = _load_manual("OKX")
    if not manual:
        return {"exchange": "OKX", "error": "API credentials not configured", "positions": []}

    symbols  = [p["symbol"] for p in manual]
    prices   = _okx_live_prices(symbols)
    basis    = _load_cost_basis().get("OKX", {})

    positions = []
    for p in manual:
        sym   = p["symbol"]
        qty   = _safe_float(p.get("amount", 0))
        price = prices.get(sym, 1.0 if sym in STABLECOINS else None)
        cost  = p.get("cost_price") or basis.get(sym)
        usd   = round(qty * price, 4) if price else None
        pos   = {"symbol": sym, "amount": qty, "usd_value": usd}
        if cost and price:
            pos["cost_price"] = cost
            pos["pnl"]        = round((price - cost) * qty, 2)
            pos["pnl_pct"]    = round((price - cost) / cost * 100, 2)
        positions.append(pos)

    return {"exchange": "OKX", "positions": positions, "source": "manual"}


# ---------------------------------------------------------------------------
# Binance
# ---------------------------------------------------------------------------

def fetch_binance() -> dict:
    api_key    = os.getenv("BINANCE_API_KEY",    "").strip()
    api_secret = os.getenv("BINANCE_API_SECRET", "").strip()
    tld        = os.getenv("BINANCE_TLD", "com").strip()
    if not api_key or not api_secret:
        return {"exchange": "Binance", "error": "API credentials not configured", "positions": []}

    base = f"https://api.binance.{tld}"
    try:
        ts = requests.get(f"{base}/api/v3/time", timeout=5).json()["serverTime"]
    except Exception:
        ts = int(time.time() * 1000)

    query = f"timestamp={ts}"
    sig   = hmac.new(api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    headers = {"X-MBX-APIKEY": api_key}

    try:
        resp = requests.get(f"{base}/api/v3/account?{query}&signature={sig}", headers=headers, timeout=10)
        data = resp.json()
        if "code" in data and data["code"] < 0:
            return {"exchange": "Binance", "error": data.get("msg", "Unknown error"), "positions": []}

        non_zero = [b for b in data.get("balances", [])
                    if _safe_float(b.get("free", 0)) + _safe_float(b.get("locked", 0)) > 0]
        if not non_zero:
            return {"exchange": "Binance", "positions": []}

        # Fetch all ticker prices directly from Binance (public, no auth)
        ticker_resp = requests.get(f"{base}/api/v3/ticker/price", timeout=10)
        all_prices  = {t["symbol"]: _safe_float(t["price"]) for t in ticker_resp.json()}

        def binance_price(sym: str) -> float | None:
            if sym in STABLECOINS:
                return 1.0
            for quote in ("USDT", "USD", "USDC", "BUSD"):
                p = all_prices.get(f"{sym}{quote}")
                if p:
                    return p
            return None

        positions = []
        for b in non_zero:
            qty = _safe_float(b.get("free", 0)) + _safe_float(b.get("locked", 0))
            sym   = b["asset"]
            price = binance_price(sym)
            positions.append({"symbol": sym, "amount": qty,
                               "usd_value": round(qty * price, 4) if price else None})
        return {"exchange": "Binance", "positions": _attach_returns(positions, "Binance")}
    except Exception as e:
        return {"exchange": "Binance", "error": str(e), "positions": []}


# ---------------------------------------------------------------------------
# Kraken
# ---------------------------------------------------------------------------

KRAKEN_TICKER_MAP = {
    "XXBT": "BTC", "XETH": "ETH", "XLTC": "LTC", "XXRP": "XRP",
    "XXLM": "XLM", "XZEC": "ZEC", "XXMR": "XMR", "XEOS": "EOS",
    "XETC": "ETC", "XXDG": "DOGE", "ZUSD": "USD", "ZEUR": "EUR",
    "ZGBP": "GBP", "ZCAD": "CAD", "ZJPY": "JPY",
}


def _kraken_sign(path: str, data: dict, secret: str) -> str:
    nonce     = data["nonce"]
    post_data = "&".join([f"{k}={v}" for k, v in data.items()])
    encoded   = (str(nonce) + post_data).encode()
    msg       = path.encode() + hashlib.sha256(encoded).digest()
    return base64.b64encode(hmac.new(base64.b64decode(secret), msg, hashlib.sha512).digest()).decode()


def fetch_kraken() -> dict:
    api_key    = os.getenv("KRAKEN_API_KEY",    "").strip()
    api_secret = os.getenv("KRAKEN_API_SECRET", "").strip()
    if not api_key or not api_secret:
        return {"exchange": "Kraken", "error": "API credentials not configured", "positions": []}

    path  = "/0/private/Balance"
    nonce = str(int(time.time() * 1000))
    payload = {"nonce": nonce}
    headers = {"API-Key": api_key, "API-Sign": _kraken_sign(path, payload, api_secret)}

    try:
        resp   = requests.post(f"https://api.kraken.com{path}", headers=headers, data=payload, timeout=10)
        result = resp.json()
        if result.get("error"):
            return {"exchange": "Kraken", "error": ", ".join(result["error"]), "positions": []}

        balances = {KRAKEN_TICKER_MAP.get(k, k): _safe_float(v)
                    for k, v in result.get("result", {}).items()}
        non_zero = {sym: qty for sym, qty in balances.items() if qty > 0}
        if not non_zero:
            return {"exchange": "Kraken", "positions": []}

        # Build Kraken ticker pairs for non-stable assets, query prices directly
        pairs = [f"{sym}USD" for sym in non_zero if sym not in STABLECOINS]
        prices: dict[str, float] = {}
        if pairs:
            t_resp = requests.get(
                "https://api.kraken.com/0/public/Ticker",
                params={"pair": ",".join(pairs)},
                timeout=10,
            )
            for pair_name, info in t_resp.json().get("result", {}).items():
                # 'c' = [last_trade_price, lot_volume]
                price = _safe_float(info["c"][0])
                # map back to base symbol: strip USD/EUR suffix
                base = pair_name.replace("XBT", "BTC")
                for suffix in ("USD", "EUR", "USDT"):
                    base = base.removesuffix(suffix)
                prices[base] = price

        positions = []
        for sym, qty in non_zero.items():
            if sym in STABLECOINS:
                usd_val = qty
            else:
                p = prices.get(sym)
                usd_val = round(qty * p, 4) if p else None
            positions.append({"symbol": sym, "amount": qty, "usd_value": usd_val})
        return {"exchange": "Kraken", "positions": _attach_returns(positions, "Kraken")}
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
        rh.login(username, password, mfa_code=mfa, store_session=True, expiresIn=86400)

        positions = []

        # --- Equities (stocks/ETFs) via build_holdings() ---
        # build_holdings returns avg buy price, current price, quantity all in one call
        try:
            stock_holdings = rh.build_holdings()
            for sym, h in stock_holdings.items():
                qty       = _safe_float(h.get("quantity", 0))
                price     = _safe_float(h.get("price", 0))
                avg_cost  = _safe_float(h.get("average_buy_price", 0))
                usd_val   = round(qty * price, 2) if price else None
                pos = {"symbol": sym, "amount": qty, "usd_value": usd_val, "asset_type": "equity"}
                if avg_cost and price:
                    pos["cost_price"] = avg_cost
                    pos["pnl"]        = round((price - avg_cost) * qty, 2)
                    pos["pnl_pct"]    = round((price - avg_cost) / avg_cost * 100, 2)
                else:
                    pos["pnl"] = pos["pnl_pct"] = None
                positions.append(pos)
        except Exception:
            pass

        # --- Crypto ---
        crypto_holdings = rh.get_crypto_positions()
        for h in crypto_holdings:
            qty = _safe_float(h.get("quantity", 0))
            if qty <= 0:
                continue
            sym = h["currency"]["code"]
            try:
                quote = rh.crypto.get_crypto_quote(sym)
                price = _safe_float(quote.get("mark_price") or quote.get("bid_price"))
            except Exception:
                price = 0

            # Robinhood stores total cost_basis per lot; derive average price
            avg_cost = None
            try:
                lots      = h.get("cost_bases", [])
                total_cost = sum(_safe_float(l.get("direct_cost_basis", 0)) for l in lots)
                total_qty  = sum(_safe_float(l.get("direct_quantity",   0)) for l in lots)
                if total_qty > 0:
                    avg_cost = total_cost / total_qty
            except Exception:
                pass

            usd_val = round(qty * price, 4) if price else None
            pos = {"symbol": sym, "amount": qty, "usd_value": usd_val, "asset_type": "crypto"}
            if avg_cost and price:
                pos["cost_price"] = round(avg_cost, 6)
                pos["pnl"]        = round((price - avg_cost) * qty, 2)
                pos["pnl_pct"]    = round((price - avg_cost) / avg_cost * 100, 2)
            else:
                pos["pnl"] = pos["pnl_pct"] = None
            positions.append(pos)

        # --- Cash balance ---
        try:
            account = rh.load_account_profile()
            cash = _safe_float(account.get("cash") or account.get("buying_power"))
            if cash > 0:
                positions.append({
                    "symbol":     "USD",
                    "amount":     cash,
                    "usd_value":  cash,
                    "asset_type": "cash",
                    "pnl":        None,
                    "pnl_pct":    None,
                })
        except Exception:
            pass

        return {"exchange": "Robinhood", "positions": positions}
    except ImportError:
        return {"exchange": "Robinhood", "error": "robin_stocks not installed", "positions": []}
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


@app.route("/api/debug/okx")
def debug_okx():
    """Shows masked credentials and raw OKX API response to diagnose auth issues."""
    api_key    = os.getenv("OKX_API_KEY",    "").strip()
    api_secret = os.getenv("OKX_API_SECRET", "").strip()
    passphrase = os.getenv("OKX_PASSPHRASE", "").strip()

    def mask(s): return s[:4] + "..." + s[-4:] if len(s) > 8 else ("(empty)" if not s else "(too short)")

    # Test 1: public endpoint (no auth) — confirms basic connectivity to OKX
    try:
        pub = requests.get("https://www.okx.com/api/v5/public/time", timeout=5).json()
    except Exception as e:
        pub = {"exception": str(e)}

    # Test 2: account/balance (requires auth)
    ts   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    path = "/api/v5/account/balance"
    sig  = base64.b64encode(hmac.new(api_secret.encode(), (ts + "GET" + path).encode(), hashlib.sha256).digest()).decode()
    headers = {
        "OK-ACCESS-KEY": api_key,
        "OK-ACCESS-SIGN": sig,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
    }
    try:
        auth = requests.get(f"https://www.okx.com{path}", headers=headers, timeout=10).json()
    except Exception as e:
        auth = {"exception": str(e)}

    # Test 3: asset/balances (funding account — different permission scope)
    ts2  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    path2 = "/api/v5/asset/balances"
    sig2  = base64.b64encode(hmac.new(api_secret.encode(), (ts2 + "GET" + path2).encode(), hashlib.sha256).digest()).decode()
    headers2 = {**headers, "OK-ACCESS-SIGN": sig2, "OK-ACCESS-TIMESTAMP": ts2}
    try:
        asset = requests.get(f"https://www.okx.com{path2}", headers=headers2, timeout=10).json()
    except Exception as e:
        asset = {"exception": str(e)}

    return jsonify({
        "credentials_loaded": {
            "OKX_API_KEY":       mask(api_key),
            "OKX_API_SECRET":    mask(api_secret),
            "OKX_PASSPHRASE":    mask(passphrase),
            "key_length":        len(api_key),
            "secret_length":     len(api_secret),
            "passphrase_length": len(passphrase),
        },
        "test_1_public_time":      pub,
        "test_2_account_balance":  auth,
        "test_3_asset_balances":   asset,
    })


if __name__ == "__main__":
    app.run(debug=True, port=5000)
