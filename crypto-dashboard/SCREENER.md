# Altcoin Cycle Screener

Nightly scan of the top ~1,000 coins, scored on the factors that historically
preceded 10x–50x bull-cycle runs. Built July 2026 while BTC (~$64k) grinds
toward an expected Q4 2026 cycle low.

## Running it

```bash
# one-off scan from the terminal (writes screener_cache.json)
cd crypto-dashboard
python screener.py            # prints top 30
python screener.py --top 50

# or from the dashboard: the Screener section loads the cached scan;
# click "Refresh scan" (calls /api/screener?refresh=1) to re-scan.
```

Optional: get a free CoinGecko **Demo** API key (10,000 calls/month) and add
`COINGECKO_API_KEY=...` to `.env` — the scan drops from ~90s to ~40s.
Everything else (DeFiLlama, Binance exchangeInfo, Fear & Greed) is keyless.

## What the research found (July 2026)

**Base rates are brutal.** Only 14 of the 2018 top-100 ever reclaimed their
highs. >70% of coins listed in 2021 were dead by 2024. 93% of >$100M tokens
launched since 2024 sit below launch price (median −95.7%). A random alt has
a ~10–15% chance of a new ATH next cycle. The screener's job is to exclude
the structurally dead 90%, not to "pick the winner."

**What past 10–50x winners had beforehand** (SOL was an $80M mcap in 2020,
AXS $30M, TAO $30→$759, HYPE $4→$70):

- market cap $30M–$500M at entry
- under ~2 years old — each cycle's winners were seeded in the prior bear
- high float (MC/FDV > 50%), community distribution; the low-float VC class
  was a massacre (all 37 of Binance's 2024 listings went negative; zero of
  the $1B+-FDV 2025 launches are green)
- real usage rising while price was flat (fees/revenue > dev activity alone)
- not yet listed on Binance/Coinbase (listing = +41% avg day-one catalyst)
- attached to the *next* cycle's narrative, not the last one's leader
  (expected 2027: AI agents, RWA, perp DEXs, DePIN, prediction markets,
  stablecoin infra)

**Quant factors with real evidence:** unlock overhang (90% of 16,000 unlock
events were price-negative), float ratio, 4–12-week momentum vs BTC
(Journal-of-Finance-grade factor; decays past a month), size,
active-addresses-per-mcap (the one "value" metric that works — TVL ratios
show no alpha), revenue capture, bear-market relative strength, holder
concentration, liquidity depth.

**Timing:** alt/BTC ratios bottom *after* BTC's USD bottom — if BTC bottoms
Q4 2026, broad alt outperformance is likely a 2027 story. The sequence every
cycle: BTC new ATH first → dominance rolls over (~52–54% break) → large caps
→ mid caps → small-cap blow-off (4–8 weeks) → crash. 2024–25 had no
synchronized altseason — rallies were ~20-day narrative bursts, so the
screener pairs with fast alerting (the TradingView tranche dashboards).

## How the score works

Kill filters (excluded before scoring): mcap outside $25M–$2B · MC/FDV < 35%
· 24h volume/mcap < 0.5% · stables/wrapped/staked derivatives.

FDV basis (fixed 2026-08-03): float is computed against `max(FDV, price ×
max_supply)`. CoinGecko derives FDV from *total* supply — emitted-so-far for
continuous-emission tokens — so Bittensor subnet tokens read as perfect 1.0
floats while ~3/4 of max supply is still to come (SN53 scored #2 this way;
against max supply its true float is ~0.24, below the kill filter).

| Factor | Weight | Direction |
|---|---|---|
| Float ratio (MC/FDV) | 20 | higher = better (unlock-overhang proxy) |
| Revenue yield (ann. fees / mcap, DeFiLlama) | 20 | higher = better |
| 30d momentum vs BTC | 15 | higher = better |
| 200d relative strength vs BTC | 10 | higher = better |
| 2027 narrative membership | 15 | member = better |
| Listing headroom (not on Binance) | 10 | unlisted = better |
| Liquidity (volume/mcap percentile) | 10 | higher = better |

Regime gauge: BTC dominance + % of top-100 beating BTC over 30d (altseason
proxy) + Fear & Greed → **ACCUMULATE / ROTATE / DISTRIBUTE**. Distribute when
the proxy holds >75 — euphoria windows last weeks, not months, and most
positions must eventually be sold.

## Phase 2 candidates (not built yet)

- Token unlock calendar (best free option gone in 2026 — test DeFiLlama's
  legacy `/emissions` route, else a manual table for the shortlist)
- Active-address growth via Santiment free tier (1k calls/mo → shortlist only)
- Upbit/Coinbase listing-announcement diffing for listing-pop alerts
- Auto-export of shortlist → Pine Script tranche-dashboard rows

## Disclaimers

Not financial advice. Percentile scores are relative to the day's surviving
universe, so a high score means "best of a bad bunch" in a bear market.
The screener finds candidates for *research*, then the existing pipeline
takes over: deep-dive the top names → set T1/T2/T3 ladders in the Pine
dashboards → TradingView zone alerts fire on entry.
