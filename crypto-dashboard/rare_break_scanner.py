#!/usr/bin/env python3
"""
rare_break_scanner.py  --  v1.0

Scans a stock universe for names that RARELY trade below their long-term
moving average, and are below it RIGHT NOW.

Two metrics do the work, and they do different jobs:

  1. pct_days_below   -- SELECTION. What fraction of its history has this
                         name spent below its 200 DMA? Low = "rarely breaks
                         trend". This defines the universe. Run it once.

  2. dist_pctile      -- TRIGGER. Today's distance from the 200 DMA, ranked
                         against that stock's OWN history of distances.
                         Low percentile = as dislocated as it ever gets.
                         This is the Mayer Multiple, percentile-ranked.

Raw distance is useless cross-sectionally: -8% from the 200 DMA is a routine
Tuesday for one name and a five-year event for another. The percentile
normalizes that away.

READ THE CAVEATS AT THE BOTTOM OF THIS FILE BEFORE TRADING OFF IT.

Usage:
    pip install yfinance pandas numpy
    python rare_break_scanner.py                          # S&P 500, defaults
    python rare_break_scanner.py --ma-type ema            # 200 EMA instead
    python rare_break_scanner.py --max-pct-below 5        # stricter "rare"
    python rare_break_scanner.py --tickers-file mine.txt  # your own universe
    python rare_break_scanner.py --include-all            # dump every name
    python rare_break_scanner.py --selftest               # verify the math
"""

import argparse
import sys
import time

import numpy as np
import pandas as pd

# ----------------------------------------------------------------------------
# Universe (S&P 500, embedded so the script has no runtime scraping dependency)
# Yahoo-style tickers: BRK.B -> BRK-B. Refresh occasionally; constituents drift.
# Last refreshed 2026-08-03 from the Wikipedia constituents table.
# ----------------------------------------------------------------------------
SP500 = [
    'A', 'AAPL', 'ABBV', 'ABNB', 'ABT', 'ACGL', 'ACN', 'ADBE', 'ADI', 'ADM',
    'ADP', 'ADSK', 'AEE', 'AEP', 'AES', 'AFL', 'AIG', 'AIZ', 'AJG', 'AKAM',
    'ALB', 'ALGN', 'ALL', 'ALLE', 'AMAT', 'AMCR', 'AMD', 'AME', 'AMGN',
    'AMP', 'AMT', 'AMZN', 'ANET', 'AON', 'AOS', 'APA', 'APD', 'APH', 'APO',
    'APP', 'APTV', 'ARE', 'ARES', 'ATO', 'AVB', 'AVGO', 'AVY', 'AWK', 'AXON',
    'AXP', 'AZO', 'BA', 'BAC', 'BALL', 'BAX', 'BBY', 'BDX', 'BEN', 'BF-B',
    'BG', 'BIIB', 'BKNG', 'BKR', 'BLDR', 'BLK', 'BMY', 'BNY', 'BR', 'BRK-B',
    'BRO', 'BSX', 'BX', 'BXP', 'C', 'CAH', 'CARR', 'CASY', 'CAT', 'CB',
    'CBOE', 'CBRE', 'CCI', 'CCL', 'CDNS', 'CDW', 'CEG', 'CF', 'CFG', 'CHD',
    'CHRW', 'CHTR', 'CI', 'CIEN', 'CINF', 'CL', 'CLX', 'CMCSA', 'CME', 'CMG',
    'CMI', 'CMS', 'CNC', 'CNP', 'COF', 'COHR', 'COIN', 'COO', 'COP', 'COR',
    'COST', 'CPAY', 'CPRT', 'CPT', 'CRH', 'CRL', 'CRM', 'CRWD', 'CSCO',
    'CSGP', 'CSX', 'CTAS', 'CTSH', 'CTVA', 'CVNA', 'CVS', 'CVX', 'D', 'DAL',
    'DASH', 'DD', 'DDOG', 'DE', 'DECK', 'DELL', 'DG', 'DGX', 'DHI', 'DHR',
    'DIS', 'DLR', 'DLTR', 'DOC', 'DOV', 'DOW', 'DPZ', 'DRI', 'DTE', 'DUK',
    'DVA', 'DVN', 'DXCM', 'EA', 'EBAY', 'ECHO', 'ECL', 'ED', 'EFX', 'EG',
    'EIX', 'EL', 'ELV', 'EME', 'EMR', 'EOG', 'EQIX', 'EQR', 'EQT', 'ERIE',
    'ES', 'ESS', 'ETN', 'ETR', 'EVRG', 'EW', 'EXC', 'EXE', 'EXPD', 'EXPE',
    'EXR', 'F', 'FANG', 'FAST', 'FCX', 'FDS', 'FDX', 'FDXF', 'FE', 'FFIV',
    'FICO', 'FIS', 'FISV', 'FITB', 'FIX', 'FLEX', 'FOX', 'FOXA', 'FRT',
    'FSLR', 'FTNT', 'FTV', 'GD', 'GDDY', 'GE', 'GEHC', 'GEN', 'GEV', 'GILD',
    'GIS', 'GL', 'GLW', 'GM', 'GNRC', 'GOOG', 'GOOGL', 'GPC', 'GPN', 'GRMN',
    'GS', 'GWW', 'HAL', 'HAS', 'HBAN', 'HCA', 'HD', 'HIG', 'HII', 'HLT',
    'HON', 'HONA', 'HOOD', 'HPE', 'HPQ', 'HRL', 'HSIC', 'HST', 'HSY', 'HUBB',
    'HUM', 'HWM', 'IBKR', 'IBM', 'ICE', 'IDXX', 'IEX', 'IFF', 'INCY', 'INTC',
    'INTU', 'INVH', 'IP', 'IQV', 'IR', 'IRM', 'ISRG', 'IT', 'ITW', 'IVZ',
    'J', 'JBHT', 'JBL', 'JCI', 'JKHY', 'JNJ', 'JPM', 'KDP', 'KEY', 'KEYS',
    'KHC', 'KIM', 'KKR', 'KLAC', 'KMB', 'KMI', 'KO', 'KR', 'KVUE', 'L',
    'LDOS', 'LEN', 'LH', 'LHX', 'LII', 'LIN', 'LITE', 'LLY', 'LMT', 'LNT',
    'LOW', 'LRCX', 'LULU', 'LUV', 'LVS', 'LYB', 'LYV', 'MA', 'MAA', 'MAR',
    'MAS', 'MCD', 'MCHP', 'MCK', 'MCO', 'MDLZ', 'MDT', 'MET', 'META', 'MGM',
    'MKC', 'MLM', 'MMM', 'MNST', 'MO', 'MOS', 'MPC', 'MPWR', 'MRK', 'MRNA',
    'MRSH', 'MRVL', 'MS', 'MSCI', 'MSFT', 'MSI', 'MTB', 'MTD', 'MU', 'NCLH',
    'NDAQ', 'NDSN', 'NEE', 'NEM', 'NFLX', 'NI', 'NKE', 'NOC', 'NOW', 'NRG',
    'NSC', 'NTAP', 'NTRS', 'NUE', 'NVDA', 'NVR', 'NWS', 'NWSA', 'NXPI', 'O',
    'ODFL', 'OKE', 'OMC', 'ON', 'ORCL', 'ORLY', 'OTIS', 'OXY', 'PANW',
    'PAYX', 'PCAR', 'PCG', 'PEG', 'PEP', 'PFE', 'PFG', 'PG', 'PGR', 'PH',
    'PHM', 'PKG', 'PLD', 'PLTR', 'PM', 'PNC', 'PNR', 'PNW', 'PODD', 'PPG',
    'PPL', 'PRU', 'PSA', 'PSKY', 'PSX', 'PTC', 'PWR', 'PYPL', 'Q', 'QCOM',
    'RCL', 'REG', 'REGN', 'RF', 'RJF', 'RL', 'RMD', 'ROK', 'ROL', 'ROP',
    'ROST', 'RSG', 'RTX', 'RVTY', 'SBAC', 'SBUX', 'SCHW', 'SHW', 'SJM',
    'SLB', 'SMCI', 'SNA', 'SNDK', 'SNPS', 'SO', 'SOLV', 'SPG', 'SPGI', 'SRE',
    'STE', 'STLD', 'STT', 'STX', 'STZ', 'SW', 'SWK', 'SWKS', 'SYF', 'SYK',
    'SYY', 'T', 'TAP', 'TDG', 'TDY', 'TECH', 'TEL', 'TER', 'TFC', 'TGT',
    'TJX', 'TKO', 'TMO', 'TMUS', 'TPL', 'TPR', 'TRGP', 'TRMB', 'TROW', 'TRV',
    'TSCO', 'TSLA', 'TSN', 'TT', 'TTD', 'TTWO', 'TXN', 'TXT', 'TYL', 'UAL',
    'UBER', 'UDR', 'UHS', 'ULTA', 'UNH', 'UNP', 'UPS', 'URI', 'USB', 'V',
    'VEEV', 'VICI', 'VLO', 'VLTO', 'VMC', 'VRSK', 'VRSN', 'VRT', 'VRTX',
    'VST', 'VTR', 'VTRS', 'VZ', 'WAB', 'WAT', 'WBD', 'WDAY', 'WDC', 'WEC',
    'WELL', 'WFC', 'WM', 'WMB', 'WMT', 'WRB', 'WSM', 'WST', 'WTW', 'WY',
    'WYNN', 'XEL', 'XOM', 'XYL', 'XYZ', 'YUM', 'ZBH', 'ZBRA', 'ZTS'
]


# ----------------------------------------------------------------------------
# Metrics -- pure functions, no I/O, unit-tested by --selftest
# ----------------------------------------------------------------------------

# 200-week overlay, expressed in trading days (200 x 5). Computed alongside the
# primary MA as a severity marker: a blue chip below its 200-WEEK average is a
# once-in-years event. Below a RISING 200-week MA is the strongest form of the
# dip setup this screen looks for; below a FALLING one is the strongest
# de-rating warning (the ACN/INTC shape). See caveat 7.
WK200_LEN = 1000


def compute_ma(close: pd.Series, ma_len: int, ma_type: str) -> pd.Series:
    """200-period SMA or EMA. Both masked until ma_len bars exist."""
    if ma_type == 'ema':
        ma = close.ewm(span=ma_len, adjust=False).mean()
        ma.iloc[:ma_len - 1] = np.nan
        return ma
    return close.rolling(ma_len, min_periods=ma_len).mean()


def break_episodes(below: pd.Series) -> list:
    """
    Convert a below/above boolean series into a list of (start_idx, length)
    episodes. An episode = one contiguous run below the MA. Counting EPISODES
    not DAYS is what makes n_breaks a meaningful sample size.
    """
    vals = below.to_numpy(dtype=bool)
    episodes = []
    i, n = 0, len(vals)
    while i < n:
        if vals[i]:
            j = i
            while j < n and vals[j]:
                j += 1
            episodes.append((i, j - i))
            i = j
        else:
            i += 1
    return episodes


def analyze_symbol(close: pd.Series, volume: pd.Series, ma_len: int,
                   ma_type: str, fwd_days: list) -> dict | None:
    """Compute the full metric set for one symbol. Returns None if unusable."""
    close = close.dropna()
    if len(close) < ma_len + 60:
        return None

    ma = compute_ma(close, ma_len, ma_type)
    mask = ma.notna()
    c = close[mask]
    m = ma[mask]
    n = len(c)
    if n < 250:                      # need ~1y of post-warmup history minimum
        return None

    dist = (c / m) - 1.0             # Mayer Multiple minus 1
    below = c < m

    cur_dist = float(dist.iloc[-1])
    # Percentile rank of TODAY's distance vs this stock's own history.
    dist_pctile = float((dist < cur_dist).mean() * 100.0)

    episodes = break_episodes(below)
    n_breaks = len(episodes)

    # Is the current bar inside an in-progress break?
    in_break = bool(below.iloc[-1])
    days_in_break = episodes[-1][1] if (in_break and episodes) else 0

    # Typical severity of a break: extra decline from break start to episode
    # trough. Tells you how much pain a "buy the dip" historically involved.
    c_arr = c.to_numpy(dtype=float)
    dd = [(c_arr[s:s + L].min() / c_arr[s] - 1.0) * 100.0
          for s, L in episodes if L >= 2]
    median_break_dd = float(np.median(dd)) if dd else np.nan
    lens = [L for _, L in episodes]
    median_break_days = float(np.median(lens)) if lens else np.nan

    # Forward returns from PRIOR break starts. Only completed windows count,
    # so the in-progress break drops out automatically.
    fwd = {}
    fwd_n = 0
    for d in fwd_days:
        rets = [(c_arr[s + d] / c_arr[s] - 1.0) * 100.0
                for s, _ in episodes if s + d < n]
        fwd[f'fwd_{d}d_median_pct'] = float(np.median(rets)) if rets else np.nan
        fwd[f'fwd_{d}d_win_rate'] = (
            float(np.mean([r > 0 for r in rets]) * 100.0) if rets else np.nan)
        fwd_n = max(fwd_n, len(rets))

    # Is the trend itself still intact? A rolling-over 200 DMA is the single
    # best warning that this is a de-rating, not a dip.
    slope = (float(m.iloc[-1] / m.iloc[-61] - 1.0) * 100.0) if n > 61 else np.nan

    dv = (close * volume).reindex(c.index).tail(60)
    med_dollar_vol = float(dv.median()) if dv.notna().any() else np.nan

    # 200-week overlay. Needs ~4y of warmup plus 61 bars for a slope, so it is
    # None/NaN on shorter histories rather than a hard requirement.
    wk = compute_ma(close, WK200_LEN, 'sma').dropna()
    if len(wk) > 61:
        wk_last = float(wk.iloc[-1])
        below_200w = bool(float(c.iloc[-1]) < wk_last)
        dist_200w = (float(c.iloc[-1]) / wk_last - 1.0) * 100.0
        slope_200w = float(wk.iloc[-1] / wk.iloc[-61] - 1.0) * 100.0
    else:
        below_200w, dist_200w, slope_200w = None, np.nan, np.nan

    row = {
        'price': float(c.iloc[-1]),
        'ma': float(m.iloc[-1]),
        'dist_pct': cur_dist * 100.0,
        'dist_pctile': dist_pctile,
        'is_below': in_break,
        'days_in_break': int(days_in_break),
        'pct_days_below': float(below.mean() * 100.0),
        'n_breaks': n_breaks,
        'median_break_days': median_break_days,
        'median_break_dd_pct': median_break_dd,
        'ma_slope_60d_pct': slope,
        'below_200w': below_200w,
        'dist_200w_pct': dist_200w,
        'ma200w_slope_60d_pct': slope_200w,
        'years_history': round(n / 252.0, 1),
        'med_dollar_vol_musd': med_dollar_vol / 1e6,
        'fwd_samples': fwd_n,
    }
    row.update(fwd)
    return row


# ----------------------------------------------------------------------------
# Data fetch
# ----------------------------------------------------------------------------

def fetch(tickers: list, years: int, chunk: int = 100) -> dict:
    """Download adjusted OHLCV via yfinance in chunks. Returns {sym: DataFrame}."""
    try:
        import yfinance as yf
    except ImportError:
        sys.exit("yfinance not installed. Run: pip install yfinance pandas numpy")

    out = {}
    period = f'{years}y'
    for i in range(0, len(tickers), chunk):
        batch = tickers[i:i + chunk]
        print(f'  fetching {i + 1}-{i + len(batch)} of {len(tickers)}...',
              file=sys.stderr)
        try:
            df = yf.download(batch, period=period, interval='1d',
                             auto_adjust=True, group_by='ticker',
                             threads=True, progress=False, timeout=60)
        except Exception as e:                       # noqa: BLE001
            print(f'  batch failed ({e}); skipping', file=sys.stderr)
            continue
        for sym in batch:
            try:
                sub = df[sym] if isinstance(df.columns, pd.MultiIndex) else df
                if sub is None or sub.empty or 'Close' not in sub:
                    continue
                if sub['Close'].dropna().empty:
                    continue
                out[sym] = sub
            except (KeyError, TypeError):
                continue
        time.sleep(1.0)                              # be polite to the endpoint
    return out


# ----------------------------------------------------------------------------
# Self-test
# ----------------------------------------------------------------------------

def selftest() -> int:
    fails = []

    def check(name, cond):
        print(f'  {"PASS" if cond else "FAIL"}  {name}')
        if not cond:
            fails.append(name)

    print('SMA / EMA')
    s = pd.Series(np.arange(1, 401, dtype=float))
    sma = compute_ma(s, 200, 'sma')
    check('sma warmup masked', bool(sma.iloc[:199].isna().all()))
    check('sma[199] == mean(1..200)', abs(sma.iloc[199] - 100.5) < 1e-9)
    check('sma[399] == mean(201..400)', abs(sma.iloc[399] - 300.5) < 1e-9)
    ema = compute_ma(s, 200, 'ema')
    check('ema warmup masked', bool(ema.iloc[:199].isna().all()))
    check('ema lags rising series', bool(ema.iloc[399] < s.iloc[399]))

    print('\nEpisode detection')
    b = pd.Series([False, True, True, False, False, True, False, True, True, True])
    eps = break_episodes(b)
    check('3 episodes found', len(eps) == 3)
    check('episode starts correct', [s for s, _ in eps] == [1, 5, 7])
    check('episode lengths correct', [L for _, L in eps] == [2, 1, 3])
    check('all-above -> 0 episodes', break_episodes(pd.Series([False] * 5)) == [])
    check('all-below -> 1 episode', break_episodes(pd.Series([True] * 5)) == [(0, 5)])

    print('\nStrong uptrend (should look "rare-break")')
    rng = np.random.default_rng(42)
    n = 2600
    # Trend-to-noise must be high to model a real compounder (COST/MSFT-like).
    # Weak drift vs vol produces ~33% days below -- an ordinary stock, not a
    # rare-break one. Sharpe here is ~3, which is what "rarely breaks" means.
    up = pd.Series(100 * np.exp(np.cumsum(
        rng.normal(0.0011, 0.006, n))), index=pd.RangeIndex(n))
    vol = pd.Series(1e6, index=up.index)
    r = analyze_symbol(up, vol, 200, 'sma', [60, 120])
    check('analysed', r is not None)
    check(f'pct_days_below low ({r["pct_days_below"]:.1f}%)',
          r['pct_days_below'] < 25)
    check('ma slope positive', r['ma_slope_60d_pct'] > 0)
    check('above 200-week MA', r['below_200w'] is False)
    check('200-week slope positive', r['ma200w_slope_60d_pct'] > 0)
    check('200-week dist positive', r['dist_200w_pct'] > 0)

    print('\nFlat random walk (should look "always breaking")')
    flat = pd.Series(100 * np.exp(np.cumsum(
        rng.normal(0.0, 0.012, n))), index=pd.RangeIndex(n))
    r2 = analyze_symbol(flat, vol, 200, 'sma', [60, 120])
    check(f'pct_days_below near 50 ({r2["pct_days_below"]:.1f}%)',
          25 < r2['pct_days_below'] < 75)
    check('more breaks than uptrend', r2['n_breaks'] > r['n_breaks'])

    print('\nPercentile trigger')
    # Force the last bar to be the most dislocated in the series.
    crash = up.copy()
    crash.iloc[-1] = crash.iloc[-1] * 0.55
    r3 = analyze_symbol(crash, vol, 200, 'sma', [60])
    check(f'dist_pctile ~0 on record dislocation ({r3["dist_pctile"]:.2f})',
          r3['dist_pctile'] < 1.0)
    check('flagged below MA', r3['is_below'] is True)
    check('dist_pct negative', r3['dist_pct'] < 0)
    check('crash is below 200-week MA too', r3['below_200w'] is True)
    check('200-week dist negative', r3['dist_200w_pct'] < 0)

    print('\nGuards')
    check('short series rejected',
          analyze_symbol(pd.Series(np.ones(50)), pd.Series(np.ones(50)),
                         200, 'sma', [60]) is None)
    r4 = analyze_symbol(up.iloc[:800], vol.iloc[:800], 200, 'sma', [60])
    check('200-week overlay None when history too short',
          r4 is not None and r4['below_200w'] is None)

    print(f'\n{"ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}"}')
    return 1 if fails else 0


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(
        description='Find blue chips that rarely break their 200 DMA -- '
                    'and are below it now.')
    p.add_argument('--tickers-file', help='newline-separated tickers; '
                                          'overrides the built-in S&P 500')
    p.add_argument('--ma-len', type=int, default=200)
    p.add_argument('--ma-type', choices=['sma', 'ema'], default='sma')
    p.add_argument('--years', type=int, default=10, help='history to pull')
    # Default calibrated against the first live scan (2026-08-03): over a real
    # 2016-2026 decade -- 2018, 2020, 2022 bears plus the current drawdown --
    # the RAREST breakers in the S&P 500 sit at 16-25% of days below the 200
    # DMA. The synthetic-data era default of 10% returns an empty screen.
    p.add_argument('--max-pct-below', type=float, default=25.0,
                   help='SELECTION gate: max %% of days spent below the MA')
    p.add_argument('--min-years', type=float, default=8.0)
    p.add_argument('--min-dollar-vol', type=float, default=5.0,
                   help='min median 60d dollar volume, $M')
    p.add_argument('--forward-days', default='60,120,250')
    p.add_argument('--include-all', action='store_true',
                   help='write every analysed name, not just qualifiers')
    p.add_argument('--out', default='rare_break_scan.csv')
    p.add_argument('--json-out', default=None,
                   help='also write JSON for the web frontend, '
                        'e.g. site/data/scan.json (always includes ALL names)')
    p.add_argument('--selftest', action='store_true')
    a = p.parse_args()

    if a.selftest:
        return selftest()

    fwd = [int(x) for x in a.forward_days.split(',') if x.strip()]

    if a.tickers_file:
        with open(a.tickers_file) as fh:
            tickers = [ln.strip().upper().replace('.', '-')
                       for ln in fh if ln.strip() and not ln.startswith('#')]
    else:
        tickers = SP500
    print(f'Universe: {len(tickers)} tickers', file=sys.stderr)

    data = fetch(tickers, a.years)
    print(f'Got data for {len(data)}', file=sys.stderr)

    rows = []
    for sym, df in data.items():
        try:
            r = analyze_symbol(df['Close'], df.get('Volume', pd.Series(dtype=float)),
                               a.ma_len, a.ma_type, fwd)
        except Exception as e:                       # noqa: BLE001
            print(f'  {sym}: {e}', file=sys.stderr)
            continue
        if r is None:
            continue
        r['symbol'] = sym
        r['qualifies'] = bool(
            r['is_below']
            and r['pct_days_below'] <= a.max_pct_below
            and r['years_history'] >= a.min_years
            and (np.isnan(r['med_dollar_vol_musd'])
                 or r['med_dollar_vol_musd'] >= a.min_dollar_vol))
        rows.append(r)

    if not rows:
        print('No usable data.', file=sys.stderr)
        return 1

    df = pd.DataFrame(rows)
    cols = ['symbol', 'qualifies', 'price', 'ma', 'dist_pct', 'dist_pctile',
            'is_below', 'days_in_break', 'pct_days_below', 'n_breaks',
            'median_break_days', 'median_break_dd_pct', 'ma_slope_60d_pct',
            'below_200w', 'dist_200w_pct', 'ma200w_slope_60d_pct',
            'fwd_samples'] + \
           [c for c in df.columns if c.startswith('fwd_') and c != 'fwd_samples'] + \
           ['years_history', 'med_dollar_vol_musd']
    df = df[[c for c in cols if c in df.columns]]
    df = df.sort_values(['qualifies', 'dist_pctile'], ascending=[False, True])
    df = df.round(2)

    out = df if a.include_all else df[df['qualifies']]
    out.to_csv(a.out, index=False)

    if a.json_out:
        import json
        import os
        payload = {
            'generated_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'demo': False,
            'params': {
                'ma_len': a.ma_len, 'ma_type': a.ma_type, 'years': a.years,
                'max_pct_below': a.max_pct_below, 'min_years': a.min_years,
                'min_dollar_vol': a.min_dollar_vol,
                'universe_size': len(tickers), 'analysed': len(rows),
                'forward_days': fwd,
            },
            'rows': df.replace({np.nan: None}).to_dict(orient='records'),
        }
        os.makedirs(os.path.dirname(a.json_out) or '.', exist_ok=True)
        with open(a.json_out, 'w') as fh:
            json.dump(payload, fh, separators=(',', ':'))
        print(f'Wrote {a.json_out}')

    hits = df[df['qualifies']]
    print(f'\n{len(hits)} names qualify '
          f'(below {a.ma_len} {a.ma_type.upper()}, '
          f'below it <={a.max_pct_below}% of history)\n')
    if len(hits):
        show = ['symbol', 'price', 'dist_pct', 'dist_pctile', 'pct_days_below',
                'n_breaks', 'days_in_break', 'ma_slope_60d_pct']
        print(hits[show].head(30).to_string(index=False))
        thin = hits[hits['n_breaks'] < 5]
        if len(thin):
            print(f'\n!! Thin sample (n_breaks < 5): '
                  f'{", ".join(thin["symbol"].tolist())}')
            print('   Their forward-return stats are anecdote, not evidence.')
        rolling = hits[hits['ma_slope_60d_pct'] < 0]
        if len(rolling):
            print(f'\n!! 200 {a.ma_type.upper()} is ROLLING OVER: '
                  f'{", ".join(rolling["symbol"].tolist())}')
            print('   Trend may be breaking, not dipping. Check fundamentals.')
    print(f'\nWrote {a.out}')
    return 0


# ----------------------------------------------------------------------------
# CAVEATS -- read these.
#
# 1. SURVIVORSHIP BIAS. The universe is TODAY's S&P 500. Names that broke
#    their 200 DMA and never recovered got deleted from the index, so they
#    are invisible here. Every historical forward-return number in the output
#    is therefore biased upward. This is not a fixable flaw of the script; it
#    is a property of screening a current index membership list.
#
# 2. THE SELECTION GATE IS A STRENGTH-CHASING FILTER. "Rarely below its
#    200 DMA" mechanically selects names that have already compounded hard.
#    INTC, GE, CSCO and NOK all had this profile once. Each eventually broke
#    the 200 DMA and STAYED broken -- and the break was the signal that the
#    secular story had changed, not that the stock was on sale.
#
# 3. n_breaks IS YOUR REAL SAMPLE SIZE. A name that has broken trend four
#    times in ten years gives you n=4. Treat fwd_*_median_pct on such names
#    as anecdote. The script prints a warning; do not ignore it.
#
# 4. ma_slope_60d_pct IS THE MOST IMPORTANT COLUMN AFTER dist_pctile.
#    Price below a still-RISING 200 DMA is a dip. Price below a FALLING
#    200 DMA is a downtrend. The metric cannot tell these apart. The slope
#    can, imperfectly.
#
# 5. NO FUNDAMENTAL GATE EXISTS HERE. Nothing in this file distinguishes a
#    quality compounder in a macro drawdown from a business that is
#    de-rating. Something has to, and it isn't this script. The output is a
#    candidate list, not a decision.
#
# 6. Prices are yfinance auto_adjust=True (split- and dividend-adjusted).
#    Adjusted series change history retroactively after each dividend, so
#    re-running on an old date will not reproduce old output exactly.
#
# 7. THE 200-WEEK OVERLAY (below_200w / dist_200w_pct / ma200w_slope_60d_pct)
#    IS A SEVERITY MARKER, NOT A SEPARATE SIGNAL. It has no episode statistics
#    behind it -- no percentile rank, no forward returns. Below a rising
#    200-week MA is the strongest form of the dip this screen looks for; below
#    a falling one is the strongest de-rating warning. The first live scan
#    (2026-08-03) found a quarter of the S&P 500 below its 200-week MA,
#    concentrated in one sector -- a reminder that this state often means the
#    market has repriced a story, not marked down a sale.
#
# Not investment advice.
# ----------------------------------------------------------------------------

if __name__ == '__main__':
    sys.exit(main())
