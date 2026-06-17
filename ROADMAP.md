# Roadmap

## Phase 1 — Working MVP
Goal: full pipeline working end-to-end, single market analysis.

- [ ] Backend scaffold (FastAPI, requirements.txt, Makefile)
- [ ] `models.py`: Market, Analysis, ScanResult, MarketWithAnalysis
- [ ] `db.py`: init_db, upsert_markets, save_analysis, get_latest_analysis
- [ ] `polymarket.py`: fetch_markets, fetch_all_active (with pagination)
- [ ] `analyzer.py`: analyze_market with Claude + web_search
- [ ] `main.py`: /health, /markets, /markets/{id}/analyze
- [ ] Frontend: MarketList + MarketCard with analyze button
- [ ] Frontend: divergence display (Market X% → Claude Y%, ±Npp badge)
- [ ] Frontend: tag filter chips
- [ ] Verify full pipeline: fetch → analyze → display divergence

## Phase 2 — Scanner
Goal: batch scan to surface top opportunities.

- [ ] `scanner.py`: scan() with semaphore + delay
- [ ] `main.py`: POST /scan endpoint
- [ ] Frontend: ScannerView with form + results table
- [ ] Filters: min volume, max analysis age, min divergence threshold
- [ ] Sort results by divergence magnitude
- [ ] "Export to CSV" button

## Phase 3 — Persistence & History
Goal: build up a log of analyses over time.

- [ ] `db.py`: get_analysis_history, get_analysis_age_hours
- [ ] `main.py`: /markets/{id} with full history, /analyses paginated
- [ ] Frontend: "analyzed N times" badge on MarketCard
- [ ] Frontend: history panel (past estimates over time)
- [ ] Frontend: re-analyze diff view (estimate shifted by X pp since last run)
- [ ] POST /markets/refresh: re-fetch markets from Polymarket API

## Phase 4 — Calibration Integration
Goal: track resolution and feed into calibration tracker.

- [ ] `db.py`: mark_resolution, get_all_resolved_analyses
- [ ] `main.py`: PUT /markets/{id}/resolution
- [ ] Frontend: "Mark resolved" button on closed markets
- [ ] Auto-resolution: detect closed markets on refresh, extract outcome from Polymarket
- [ ] `scripts/import_calibration.py`: import from existing tracker
- [ ] Export calibration data: claude_prob + resolution pairs as CSV/JSON
- [ ] Brier score calculation over rolling window
- [ ] Calibration curve view (bucketed accuracy chart)

## Phase 5 — Advanced
Goal: automation, intelligence, and portfolio simulation.

- [ ] Scheduled background fetching (APScheduler)
- [ ] Change detection: alert when a market's price moves >5pp since last analysis
- [ ] Re-analysis suggestions: flag markets where Claude's estimate may be stale
- [ ] Portfolio simulator: hypothetical P&L if you bet on every edge signal
- [ ] Backtesting: apply scanner to resolved markets, score against outcomes
- [ ] Multi-model comparison: run GPT-4o on same market, compare estimates
- [ ] Webhook/Slack notification for high-divergence markets

## Future polish
- [ ] Add log rotation or size cap for data/app.log (currently unbounded append)
- [x] Exclude analyses where claude_prob=1.0 and summary contains '1%' from calibration queries (parser-corrupted rows, id=14) — filtered via `db._NOT_CORRUPTED`

## Won't do (scope limits)
- Actual Polymarket trading / order placement (read-only by design)
- User authentication (local tool, single user)
- Deployment to cloud (designed for local use + Mac Mini via Tailscale)
