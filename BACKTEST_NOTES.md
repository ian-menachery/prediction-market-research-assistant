# PMRA — Backtest & Edge-Search Notes

Durable record of the post-wind-down edge-search work. Nothing here is load-bearing for the running
app (it stays wound down); this exists so the analysis, data, and reasoning are **not lost** and the
conclusions can be re-audited. Two work packages are recorded:

1. **Edge search** (2026-07-12) — decompose existing P&L, test bias inversion, characterize the
   untested market universe, and check for structural arbitrage.
2. **Politics gate backtest** (2026-07-30) — the one lead the edge search flagged (near-dated
   Politics), tested as a go/no-go gate for forward paper-testing.

**Both came back NULL. The verdict is unchanged: no accessible edge — stay wound down.**

**Total real LLM spend across all of this work: $0.70** (7 web-search analyses in the Politics probe;
everything else used free Kalshi public reads + the local SQLite DB, $0).

---

## 0. Where the raw data lives (so nothing is lost)

- `backtest/politics_gate_probe_out.jsonl` — raw output of the 6-market look-ahead probe (model prob,
  confidence, web-search count, cost, full summary + factors, actual outcome). The 7th pilot analysis
  (`KXCABLEAVE`, $0.155) is described inline below; it duplicates the batch's `KXCABLEAVE` row.
- `backtest/politics_gate_probe.py` — the exact script that produced the probe (reproducible; note it
  **spends credits** and, because the markets are post-cutoff, will re-leak the outcome).
- `data/polymarket.db` (gitignored) — the 24 signals / 71 analyses the Part 1–2 autopsy reads. The
  autopsy numbers below are copied out so they survive independent of the DB.
- Parts 3–4 and the Politics market baseline read **live** Kalshi endpoints; those numbers are a
  snapshot as of the dates noted and will drift. They are recorded here as they stood.

Ground rule used for all P&L: **gross** = entry priced at the market mid; **net** = entry priced at the
realistic far touch (ask/bid VWAP already stored as `price_paid`) + Kalshi `0.07·p·(1−p)` fee. Held to
resolution, so no exit spread. An edge smaller than (spread + fee) is a **null**, not an edge.

---

## 1. Edge search (2026-07-12)

### Part 1 — Per-category autopsy of the 13 settled signals

| Slice | n | Dir. acc | Base rate | Brier | Gross P&L | Net P&L |
|---|--:|--:|--:|--:|--:|--:|
| **OVERALL** | 13 | 0.08 | 0.15 | 0.505 | **−$579.58** | **−$586.82** |
| econ (payrolls) | 8 | 0.00 | 0.00 | 0.501 | −$400.00 | −$400.00 |
| weather | 5 | 0.20 | 0.40 | 0.512 | −$179.58 | −$186.82 |
| — KXHIGHCHI | 1 | 0.00 | 0.00 | 0.846 | −$50.00 | −$50.00 |
| — KXHIGHLAX | 2 | 0.00 | 0.50 | 0.741 | −$100.00 | −$100.00 |
| — KXHIGHMIA | 1 | 0.00 | 1.00 | 0.230 | −$50.00 | −$50.00 |
| — KXHIGHTSEA | 1 | 1.00 | 0.00 | 0.002 | +$20.42 | +$13.18 |

- **No slice is net-positive at n ≥ 15.** Largest slice is econ (n=8). The only positive cell
  (KXHIGHTSEA, +$13.18 net) is **n=1 — insufficient data, not an edge.**
- **Negative result is uniform, not concentrated:** econ 0/8 (Brier 0.501) and weather 1/5 (0.512)
  are near-identical and both catastrophic.
- **The spread did not kill this — being wrong did.** Gross vs net differ by only **$7.24 across 13
  trades**, because 12/13 lost the full stake regardless of entry price. This is a forecasting-skill
  failure, not an execution-cost failure.

Per-signal (side, our YES prob `cp`, mid, far-touch fill, outcome, gross/net at $50):

```
id 8 weather  KXHIGHCHI   YES cp=0.92 mid=0.07 fill=0.11 res=0 gross=-50.00 net=-50.00
id 9 weather  KXHIGHLAX   YES cp=0.90 mid=0.39 fill=0.42 res=0 gross=-50.00 net=-50.00
id10 econ     KXPAYROLLS  YES cp=0.28 mid=0.17 fill=0.16 res=0 gross=-50.00 net=-50.00
id11 econ     KXPAYROLLS  YES cp=0.38 mid=0.19 fill=0.24 res=0 gross=-50.00 net=-50.00
id12 econ     KXPAYROLLS  YES cp=0.85 mid=0.62 fill=0.62 res=0 gross=-50.00 net=-50.00
id13 econ     KXPAYROLLS  YES cp=0.78 mid=0.63 fill=0.64 res=0 gross=-50.00 net=-50.00
id15 econ     KXPAYROLLS  YES cp=0.62 mid=0.46 fill=0.46 res=0 gross=-50.00 net=-50.00
id16 econ     KXPAYROLLS  YES cp=0.88 mid=0.69 fill=0.68 res=0 gross=-50.00 net=-50.00
id19 econ     KXPAYROLLS  YES cp=0.72 mid=0.59 fill=0.59 res=0 gross=-50.00 net=-50.00
id21 weather  KXHIGHLAX   NO  cp=0.18 mid=0.44 fill=0.59 res=1 gross=-50.00 net=-50.00
id22 weather  KXHIGHTSEA  NO  cp=0.04 mid=0.29 fill=0.78 res=0 gross=+20.42 net=+13.18
id23 econ     KXPAYROLLS  YES cp=0.88 mid=0.74 fill=0.74 res=0 gross=-50.00 net=-50.00
id24 weather  KXHIGHMIA   NO  cp=0.52 mid=0.63 fill=0.38 res=1 gross=-50.00 net=-50.00
```

Broader view over all 24 resolved *analyses* (includes unbet markets): econ n=14 Brier 0.290,
weather n=8 Brier 0.331 — both worse than a 0.25 coin flip.

### Part 2 — Confidence-bias inversion check

Confidence × side buckets (net P&L, as bet):

| Confidence | Side | n | Wins | Net P&L |
|---|---|--:|--:|--:|
| high | YES | 5 | 0 | −$250 |
| high | NO | 2 | 1 | −$37 |
| medium | YES | 5 | 0 | −$250 |
| medium | NO | 1 | 0 | −$50 |

- **Every YES bet lost (0/10), at both confidence levels** — "confidence" carries no discriminating info.
- Counterfactual inversions: high-conf (>0.85) YES (n=4, all NO) → as-bet −$200, **inverted ≈ +$264**.
  Full inversion of all 13 → as-bet −$586.82, **inverted ≈ +$550.47** (NO far-touch approximated with a
  symmetric spread haircut).
- **Not exploitable — small-n noise.** The 13 signals span only **5 distinct underlying events** (4
  city-days of weather + one June payrolls report; the 8 payroll legs are thresholds on the *same*
  jobs number = one correlated bet). Inverting a negative-skill model over ~5 events is overfitting;
  the profitable "inversions" are just NO bets on outcomes the market already favored. Correct
  inference: **don't bet on this model's output**, not "systematically fade it."

### Part 3 — Universe expansion (free market/book data; no LLM spend)

Full live Kalshi open universe as of 2026-07-12, characterized by two-sided-quote count, liquidity,
and spread (top-of-book pulled inline from `/events?with_nested_markets`):

| Category | 2-sided mkts | vol≥$1k | Median spread (liquid) | Depth≥$10 & spread≤3c | Efficiency prior |
|---|--:|--:|--:|--:|---|
| Elections | 9,451 | 2,169 | 0.030 | 1,507 | mixed (long-dated ≈ efficient) |
| Sports | 4,931 | 1,404 | 0.010 | 746 | efficient — excluded |
| Entertainment | 3,487 | 1,375 | 0.050 | 283 | plausibly inefficient; thin depth (~$2.5) |
| Financials | 1,894 | 510 | 0.020 | 733 | efficient — excluded |
| Politics | 1,367 | 976 | 0.031 | 299 | plausibly inefficient; most liquid (med vol $4.7k) |
| Economics (ex-CPI/payrolls) | 1,383 | 549 | 0.050 | 178 | already-disproven family (nowcast-priced) |
| Companies | 467 | 242 | 0.060 | 67 | plausibly inefficient; wide spread |
| Science & Tech | 381 | 250 | 0.050 | 44 | long-dated lottery — unforecastable |
| Mentions | 750 | 206 | 0.070 | 41 | novelty; wide spread |
| Crypto | 226 | 132 | 0.010 | 34 | efficient — excluded |

Break-even bar: a 3c spread + up to ~1.75c fee means the model must beat the market by **>~5pp net**.
Ranked plausible-edge leads (all low-conviction): **1) Politics** (most liquid, 3c spread,
news/judgment resolution) → tested in §2; 2) near-dated Elections; 3) Companies; 4) Entertainment;
skip Economics-ex/Sci-Tech/Mentions. Tight-spread categories (Sports/Crypto/Financials) are the most
efficient — no LLM edge.

### Part 4 — Intra-venue structural mispricing (live books, free)

Scanned **1,796 live mutually-exclusive Kalshi events** (`mutually_exclusive=True`, ≥2 quoted legs):

- Sum of YES **asks** (buy the set): median **1.07** → a **7% buy-side overround**. No buy-side arb.
- Sum of YES **bids** (sell the set): median **0.95** → a **5% sell-side underround**.
- **Executable sell-side arbs (sum bid > 1 + fees, all legs quoted): ZERO.**
- The 32 apparent "buy-side arbs" (e.g. `KXLAPRIMARY`, sum-ask 0.06) are **false positives —
  non-exhaustive longshot subsets** (the field winner isn't among the listed legs). The 3 events with
  sum(bid) > 1.03 are illiquid ~33-leg longshot markets (`KXNFLCOTY`, sum-bid 1.070) where per-leg NO
  fees + missing bids + microscopic depth erase the ~0.4c/contract gap.
- No crossed books. **Clean null** — reconfirms the archived "taker arbitrage is dead" finding with
  fresh data: the overround belongs to whoever posts it.

---

## 2. Politics gate backtest (2026-07-30)

**Goal:** gate — only forward-test near-dated Politics if the model shows positive Brier skill AND
positive net P&L on already-resolved markets at n ≥ 30. Read-only + free re-analysis; cheapest path.

### Sample (free)

- **401 settled Kalshi Politics markets**; **232 near-dated** (≤30-day lifespan, volume ≥ $100) —
  above the n ≥ 30 target. Sub-types: confirmations, cabinet departures, "by-date" legislative/legal
  events, appointments. Base rate ≈ 0.37–0.39.
- **Fatal constraint: all 232 opened Apr–Jul 2026 — entirely AFTER the model's ~Jan 2026 training
  cutoff; zero straddle it.** So:
  - *With web search (the actual product):* the model reads the June–July 2026 outcome → total
    look-ahead contamination.
  - *Without web search:* the model has no knowledge these post-cutoff events exist → blind guess.
  - *No date-fenceable middle:* the Anthropic `web_search` tool has no reliable pre-date filter, and no
    market exists in the open < cutoff < close window.
  → **A valid look-ahead-free backtest of the web-search product is structurally impossible on this
  sample.** The very feature that made Politics "plausibly less-dominated" (resolution on dispersed
  public news) is exactly what makes post-hoc web search read the answer.

### Look-ahead demonstrated empirically (probe, $0.70 total)

Ran PMRA's actual analyzer on 6 resolved markets (raw: `backtest/politics_gate_probe_out.jsonl`).
**6 of 6 leaked the outcome** — every summary cites post-open, often post-resolution facts:

| Market | Model | Conf | Actual | Leak evidence (verbatim) |
|---|--:|---|---|---|
| Cabinet member leave before Jun? | 0.99 | high | **NO** | "Gabbard announced her resignation on May 22" |
| SCOTUS rehearing petition filed? | 0.02 | high | NO | "SCOTUSblog confirmed *as of July 28*…" |
| Netanyahu out before Jul 1? | 0.02 | high | NO | "confirmed actively serving as PM *as late as July 5*" |
| Ken Martin out as DNC chair? | 0.02 | high | NO | "remains chair *as of July 30, 2026*" |
| Gabbard out as DNI before Jun 29? | 0.99 | high | YES | "officially left the DNI role *on June 20*" |
| Fable 5 access restored by Jun 15? | 0.02 | high | NO | "not restored until *July 1*" |

- The model is **retrieving the answer**, not forecasting. Contaminated Brier = **0.164**.
- Even while reading answers it **still didn't beat the market's own 0.10 baseline**, and it **misread
  the resolution criteria** on the first market (confused Gabbard's May 22 *announcement* with the
  departure the contract required → confident 0.99 YES on a NO). Same criteria-misread failure mode
  seen in weather/econ.

### The decisive VALID evidence — the market is already efficient (free, n ≥ 30)

Market's own forecast from free candlestick history (41 near-dated markets had usable price history):

| Market's forecast priced at… | n | Brier | Dir. acc | Base rate | Brier skill vs base rate |
|---|--:|--:|--:|--:|--:|
| mid-life | 41 | 0.128 | 0.80 | 0.37 | **+0.45** |
| ~24h before close | 41 | 0.095 | 0.88 | 0.37 | **+0.59** |
| ~7d before close (subset) | 28 | 0.110 | 0.82 | 0.36 | +0.52 |

The Kalshi crowd prices near-dated Politics at **Brier ~0.10–0.13, 80–88% accuracy, well before
resolution** — sharper than the model has ever been. Edge would require beating an already-sharp price
by > spread — the same wall the tool lost to on weather (Brier 0.51) and econ (0.50).

### Gate verdict — NULL (do not soften)

- **Model Brier beats 0.25?** Cannot be validly established. The only obtainable number (0.164) is
  look-ahead-contaminated (6/6 leak) → worthless as evidence of skill. **Not a pass.**
- **Net P&L positive?** Cannot be validly computed; the one contaminated run already misfired
  (0.99 → NO = full-stake loss).
- **Gate FAILS.** Two independent honest reasons: (1) a look-ahead-free web-search backtest is
  structurally impossible here, not a fixable gap; (2) the one clean free measurement shows the
  category is **efficiently priced** — the same efficient-market wall as before.
- The only uncontaminated test left is a **true forward paper-test on currently-open markets** (nothing
  to leak). The free evidence (sharp market + a tool with a documented criteria-misreading habit and a
  negative track record) **does not justify funding one.** Absent a fundamentally different strategy
  (not "generic LLM + web search vs. a liquid market"), the honest call is: **stay wound down.**

---

## Bottom line

Forecasting lost (Part 1–2), the untested-universe leads collapse to an efficient market or an
un-runnable backtest (Part 3 + §2), and structural arbitrage is dead (Part 4). Every path checked ends
at Kalshi's spread or the market's efficiency. **No accessible edge for a manual LLM-taker. Wound down.**
