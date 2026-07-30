# PMRA — Prediction Market Research Assistant (CLAUDE.md)

## Working agreement on planning

Plan-first is **relaxed** (owner's call, 2026-06-13): commit changes directly to `main`
and make code edits without stating a plan or waiting for confirmation first.

Still state a brief plan when a change is large, risky, ambiguous, or hard to reverse —
otherwise just implement. Surface contradictions and unexpected findings as before.

## What this is

A local tool whose **goal is real ROI: trading P&L > the Claude credits it spends.** It fetches
live prediction markets, has an LLM (web search enabled) estimate each market's probability against
the current price, and surfaces executable, net-of-fee edges as **forward signals** with a
recommended (fractional-Kelly) stake. You place the bets by hand and log the fills; calibration and a
P&L/ROI track record build as markets resolve.

**Kalshi-only for trading** (owner is US-based; Polymarket blocks US users). Polymarket remains a
*free signal source* the engine can still read, but `EXCHANGE=kalshi` so we never spend LLM budget on
markets we can't trade. The tool does **not** place orders — it finds + sizes edges; execution is manual.

Pipeline: **series discovery → resolution-grounded LLM analysis → executable signal (net-of-fee,
deduped) → trade-ticket (capped stake) → ROI scoreboard**, with calibration + a "why did it diverge?"
review loop building over time.

## ⚖️ VERDICT (2026-07-11; re-confirmed 2026-07-30) — WOUND DOWN. No accessible edge; project archived.

**Status: archived on `main`. Spending is off (`SCAN_INTERVAL_HOURS=` empty); the app is not running.**
(Auto-launch note, verified 2026-07-30: a boot-triggered Scheduled Task `PolymarketResearchCopilot`
still exists and is *enabled*, but its action points at the **stale pre-rename path**
`...\polymarket-claude\scripts\start_app.bat` — which no longer exists after the rename to `PMRA` — so
it **fails on boot (LastTaskResult=1) and never launches the app**. It is inert cruft; recommend
deleting it. Even if it did launch, spending stays off because `SCAN_INTERVAL_HOURS` is empty.)
Every way to make money was tested and failed — forecasting, taker
arbitrage, and (2026-07-30) a per-category edge search + a Politics-category backtest gate. Total real
cost of the whole experiment: ~$6.97 in credits (~$6.27 original + $0.70 Politics probe), no real bets.
Full edge-search + backtest data: **`BACKTEST_NOTES.md`** (raw artifacts in `backtest/`). To revive: set
`SCAN_INTERVAL_HOURS` and `make run`, but read this verdict first.

**Forecasting (out-predict the market) — the model lost, decisively:**
- **1 win / 13 resolved · −$585.85 modeled P&L** (at the $50 modeled position; no real bets were placed —
  only ~$6.27 of credits was actually spent).
- **Econ 0/8 (−$400):** the model bet "June payrolls will be high" with 85–88% confidence across every
  threshold — all wrong (jobs came in weak). It has no forecasting edge on a number professional
  nowcasts already price.
- **Weather 1/5 (−$186):** confirmed the "fabricated edge" bug with real outcomes (wrong resolution
  station; fixed after, unverified at scale).
- **Calibration: Brier 0.305 over 22 pairs — WORSE than a 0.25 coin flip.** Negative skill, not just
  "no edge."

**Conclusion:** a generic LLM + web search has **no durable edge** on liquid Kalshi markets (weather/econ
are efficiently forecast-priced), and is over-confident + directionally wrong. The one win here was the
**process**: paper-validation cost $6, not $585. Spending is paused (`SCAN_INTERVAL_HOURS=` empty).
Do NOT fund more forecasting scans on this evidence without a strategy change (see next section).

**Structural / taker arbitrage — ALSO tested (free probe, 2026-07-11) and DEAD.** For 10 liquid Kalshi
weather events, buying every outcome's YES costs **$1.05–1.11** for a $1 payout (a 5–11% overround), and
the rare sell-side gap ($1.03–1.05) is eaten by ~$0.07 in per-contract fees. As a *taker* you always pay
the market-maker's spread, and it's wider than any internal inconsistency. The edge inside the spread
belongs to whoever *posts* it (market-making — capital/speed/inventory infra this tool doesn't have).

→ Both forecasting AND clean arbitrage lose to Kalshi's spread. **Conclusion stands: no accessible edge
for a manual LLM-taker — this is the "accept the base rate" outcome.**

**Edge search + Politics gate — ALSO tested (2026-07-12 & 2026-07-30) and NULL.** Full data:
`BACKTEST_NOTES.md` (+ raw probe artifacts in `backtest/`). Summary:
- **Per-category autopsy:** the −$586 loss is **uniform** (econ 0/8, weather 1/5, near-identical Brier
  ~0.50), NOT a good pocket hidden in the average. The spread cost only ~$7 of the loss — **being wrong
  killed it, not execution cost.** No slice is net-positive at n≥15.
- **Confidence-inversion "edge" is a mirage:** the 13 signals are only ~5 independent events (8 payroll
  legs = one jobs number); "invert the model" is small-n overfitting, not exploitable.
- **Politics gate FAILED (the one flagged lead):** a look-ahead-free web-search backtest is
  **structurally impossible** — the 232 near-dated resolved markets are all post-cutoff, so the analyzer
  reads the outcome off the web (demonstrated 6/6 leak; contaminated Brier 0.164 that still didn't beat
  the market). The one clean free measurement — the market's own price — is **already efficient**
  (Brier ~0.10, skill +0.5). Same efficient-market wall.

## If ever revived — directions now MOSTLY disproven too (very low odds, real cost)
1. ~~**Target under-followed, text/knowledge-resolution markets (niche political/legal/news).**~~
   **TESTED 2026-07-30 → dead-end.** Near-dated Politics is the most liquid such category and it is
   *efficiently priced* (market Brier ~0.10); worse, its news-resolution nature makes any resolved-market
   backtest impossible to run leak-free. The only untainted test is a live forward paper-test, which the
   free evidence does not justify funding. See `BACKTEST_NOTES.md` §2.
2. **Only bet with a specific, verifiable reason the market is wrong.** Gate a signal on the model naming
   a concrete fact the market plausibly missed — not just "my estimate differs." Make the adversarial
   refutation actually KILL bets (currently it only flags); shrink estimates toward the market.
   (Untested, but note the model *misreads resolution criteria* even when handed the answer — see the
   probe's confident 0.99→NO. A "verifiable reason" gate leans on the same weak criteria-reading.)
3. ~~**Prove per-category edge via a resolved-market backtest before trading it.**~~ Attempted for
   Politics — **the backtest itself is unrunnable leak-free** for any category that resolves on public
   news/text (exactly the "plausibly inefficient" kind). Backtesting only works for numeric-resolution
   categories, which are the efficient ones the model already lost on. This route is a catch-22.

Absent a fundamentally different strategy (not "generic LLM + web search vs. a liquid market"), the
honest answer is settled: **no edge — stay wound down.**

## Stack (locked)
- `flask`, `httpx` (sync), `anthropic`, `openai`, `pydantic`, `sqlite3` (stdlib), `python-dotenv`
- No `asyncio`. No `aiosqlite`. No React build pipeline (frontend is CDN React + Babel). No Docker. No Postgres.
- New dependencies require asking first.

## LLM provider (dual-provider)
`analyzer.py` supports **OpenAI and Anthropic**, selected by `LLM_PROVIDER`. Each `Analysis` records
its `model`, so calibration stays **per-model** across a switch (a switch starts a fresh per-model
history — uncalibrated until `CALIBRATION_MIN_N` resolved pairs).
- **Primary: Anthropic** — `.env`: `LLM_PROVIDER=anthropic`, `ANALYSIS_MODEL=claude-sonnet-4-6`
  (uses the server-side `web_search` tool). **OpenAI is the reversible fallback** (`LLM_PROVIDER=openai`,
  `OPENAI_MODEL`) — but its credits are exhausted, so it latches `openai_exhausted` and errors rather
  than silently falling back. Keep cross-model refutation OFF (`CROSS_MODEL_ADVERSARIAL` blank) → Claude
  self-refutes, avoiding the exhausted-OpenAI path.

## Quick start / resume
```bash
make install
cp .env.example .env     # set LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY (and Kalshi optional — public reads work)
make run                 # → http://localhost:5000   (PYTHONPATH=src python -m research.app)
```
On this box `python`/`py` aren't on PATH; use `$LOCALAPPDATA/Programs/Python/Python312/python.exe` with
`PYTHONPATH=src`. To keep it running unattended (survives logoff), the owner runs a Windows
`schtasks /SC ONLOGON` task pointing at `run_copilot.bat`.

## Project structure
```
PMRA/                         ← folder (was polymarket-claude)
├── CLAUDE.md                 ← you are here
├── ARCHITECTURE.md / ROADMAP.md / API_REFERENCE.md / CALIBRATION_NOTES.md  ← (older docs; partly stale)
├── run_copilot.bat           ← launcher for the unattended Scheduled Task
├── src/research/
│   ├── models.py             ← Pydantic models (Market, Analysis, Signal, ScanRequest, ...)
│   ├── db.py                 ← sqlite3 access layer (sync; the only raw-SQL module)
│   ├── polymarket.py         ← Polymarket API client (httpx) + shared Book/VWAP/retry helpers
│   ├── kalshi.py             ← Kalshi API client: series discovery, order book, resolution, health_check
│   ├── exchanges.py          ← dispatch between polymarket/kalshi by EXCHANGE / market.exchange
│   ├── analyzer.py           ← LLM analysis engine (resolution-grounded prompt) + refutation
│   ├── scanner.py            ← scan/EV, signals, sizing, fees, dedup, resolution sweep, alerts
│   ├── calibration.py        ← per-model temperature scaling + Brier/log-loss leaderboard
│   ├── performance.py        ← signal track record, by_category, ROI (P&L − credit spend), divergence_review
│   ├── pricing.py            ← per-model token → USD cost
│   ├── scheduler.py          ← stdlib threading.Timer cadences (scan / resolution sweep / health heartbeat)
│   └── app.py                ← Flask app + all routes
├── frontend/
│   ├── index.html            ← shell + CSS, served at /
│   └── js/{api.js, views.js, app.js}  ← CDN-React UI (no build step)
├── data/                     ← gitignored: <db>.db, scan_log.jsonl, health.jsonl, alerts.jsonl, app.log
├── .env / .env.example       ← .env is gitignored (holds API keys)
└── Makefile
```

## Operating the flywheel (runbook)

**Cadences** (`scheduler.py`, armed in `app.py __main__`): batched scan every `SCAN_INTERVAL_HOURS`
(24), resolution sweep + Kalshi health heartbeat every `AUTO_RESOLUTION_INTERVAL_HOURS` (6, free).

**Key env knobs** (full annotated list in `.env.example`):
- *Discovery:* `EXCHANGE=kalshi`; `KALSHI_SERIES` (ordered weather→econ→crypto = analysis priority);
  `KALSHI_MIN_VOLUME`.
- *Cost:* `MAX_LLM_CALLS_PER_SCAN` (10), `SPEND_CAP_USD` (scheduler pauses scans past it), `SCAN_INTERVAL_HOURS`.
  Real cost ≈ **$0.16/analysis** (web search + cached context), not the old token guess — the estimator
  now uses real recent cost.
- *Quality:* `SIGNAL_MIN_EV`, `MIN_BOOK_DEPTH_USD`, `EXTREME_DIVERGENCE` (flag misreads, withhold stake),
  `REFUTE_TOP` (adversarial pass), `KALSHI_FEE_RATE` (netted into EV), `MAX_SIGNALS_PER_EVENT` (dedup).
- *Near-dated bias:* `SCAN_MIN_DAYS_TO_CLOSE` / `SCAN_MAX_DAYS_TO_CLOSE` (resolve fast → calibrate fast).
- *Sizing:* `BANKROLL_USD`, `KELLY_FRACTION` (¼-Kelly), `MAX_POSITION_USD` (hard cap).

**Reading the UI** (`http://localhost:5000`): **Signals** = actionable trades (recommended stake +
Kalshi link), the ROI scoreboard (realized P&L − credit spend), and the "⚠ Extreme divergences —
review" diagnosis panel; **Calibration** = progress toward 50 resolved pairs; **Performance** =
by_category P&L. `/api/health` (+ `data/health.jsonl` heartbeat) shows Kalshi schema liveness.

**The trade loop:** pick a sanity-checked Signal → place the small bet on Kalshi → **Log fill** →
realized P&L lands when it resolves.

**Size-up rule:** keep stakes tiny until `claude-sonnet-4-6` crosses **50 resolved pairs AND net ROI is
positive**; only then raise `MAX_POSITION_USD`.

**Open loose ends:** the Kalshi "Trade ↗" link uses the series-page URL (`/markets/{series}`) — confirm
it resolves from a real browser and refine to a deep link if needed. Stale far-dated/untradeable open
signals are **hidden in the UI, not deleted** (a true DB purge is blocked by the destructive-action guard).

## Module boundaries (enforce these)
- All exchange API calls live in `polymarket.py` / `kalshi.py` — the only `httpx` importers; `exchanges.py`
  dispatches between them. No `httpx` elsewhere.
- All DB operations live in `db.py` (the only raw-SQL module). No business logic there — it only reads/writes.
- Flask routes in `app.py` call the modules — they don't contain logic.

## Code conventions
- Type hints on every function in Python
- All probabilities stored as float 0–1 in DB; displayed as % in frontend
- Market IDs are always strings
- Never delete or overwrite analysis records — only append
- When adding a `Market`/`Signal` field: update the model **and** the DB column + migration + row-mapper
  + the INSERT placeholder count (`len(_*_COLUMNS.split(","))`).

## Things you should not do
- Do not add infrastructure speculatively (no FastAPI upgrade, no Redis, no Celery)
- Do not "clean up" code I didn't ask you to clean up — mention it, don't fix it silently
- Do not invent data. If an API call fails, surface the error — never fill with zeros
- Do not use `asyncio` anywhere. Use `time.sleep()` for rate limiting.
- Do not hardcode API keys (`OPENAI_API_KEY` / `ANTHROPIC_API_KEY`) — read them from env

## Things you should do
- After each substantive change, suggest a commit message (short, imperative); commit + push to `main`
- When you finish a piece of work, summarize what changed in 2–3 sentences
- Keep changes data-driven: further features should wait until resolved-market data shows what's needed

## Relationship to calibration tracker
Separate project. `polymarket.py` borrows patterns (API normalization, gotchas) from the calibration
tracker's `polymarket/` module but doesn't import it. Shared data contract: CALIBRATION_NOTES.md.
