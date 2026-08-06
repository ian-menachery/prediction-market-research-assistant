> **Note:** early build doc from when this was framed as a trading tool. See [README.md](README.md) and [index.html](index.html) for the current framing — a falsification study.

# Calibration Tracker Integration

## Overview
This tool is designed as a companion to the existing Polymarket calibration tracker. The `Analysis` model uses a data contract compatible with the calibration tracker so that analyses from this tool can feed directly into calibration curves and Brier score tracking.

---

## Key integration points

### Shared primary key
Both tools use Polymarket's `id` field as the market identifier — a hex string like `0x1234abc...`. This ID is stable and doesn't change as market price moves. Use it consistently across both systems.

### Shared data contract
The fields that matter for calibration (don't rename these):

```python
Analysis:
    market_id: str          # Polymarket market ID — shared key
    created_at: datetime    # when prediction was made
    claude_prob: float      # 0–1 — the model's probability estimate
    resolved: bool | None   # None until market resolves
    resolution: bool | None # True = YES won, False = NO won
```

`(claude_prob, resolution)` pairs are what feed into Brier score and calibration curves. Everything else is display metadata.

---

## What to pull from the existing calibration tracker

When setting up the integration, look at the existing tracker for:

1. **`Resolution` or `Outcome` model** — map `resolution: bool` to your existing schema
2. **Existing market probability data** — any predictions already logged can seed the `analyses` table as historical records
3. **Brier score calculation** — keep this in the calibration tracker; this tool produces the raw data, the tracker calculates the metric
4. **Market ID format** — confirm both systems use the same ID format (hex string from Polymarket)

---

## Import script (design only — never built)

The original plan called for a `scripts/import_calibration.py` to import resolved analyses from the
existing tracker. It was **not built**: the calibration dataset was generated in-tool (live analyses
joined to resolved outcomes), not imported from the separate tracker, so the importer was never needed.
The intended shape, for the record, was:

1. Load existing resolved market data from the calibration tracker
2. Check if a matching market exists in the local DB by market ID
3. If market exists: insert a historical `Analysis` record with `resolved=True` and the known outcome
4. Log skipped records (market not found locally) to a file for review

---

## Resolution tracking workflow

What shipped is **automatic** resolution: the scheduler runs a periodic resolution sweep
(`AUTO_RESOLUTION_INTERVAL_HOURS`, free) that detects closed markets and marks all matching
`Analysis` records with `resolved=True, resolution=outcome`. The manual "Mark resolved" button
described in the original design was never built — auto-resolution replaced it. The
`PUT /markets/{id}/resolution` endpoint still exists as a manual override:

1. Market closes on the exchange
2. Auto-resolution sweep detects it and records the outcome, or a manual
   `PUT /markets/{id}/resolution` with `{"outcome": true|false}` sets it
3. All `Analysis` records for that market get `resolved=True, resolution=outcome`
4. Calibration reads these `(claude_prob, resolution)` pairs directly

---

## Auto-resolution (Phase 4)

When a market resolves, Polymarket sets:
- `closed: true`
- `outcomePrices`: the winning outcome price becomes `"1"`, losing becomes `"0"`

So to auto-detect: after fetching markets, check if `closed=true` and any price is `"1.0"` or `"1"`. The index of that price in `outcomePrices` corresponds to the index in `outcomes` — so `outcomes[i] = "Yes"` with `prices[i] = "1"` means YES won.

```python
def detect_resolution(raw: dict) -> bool | None:
    if not raw.get("closed"):
        return None
    prices = json.loads(raw.get("outcomePrices") or "[]")
    outcomes = json.loads(raw.get("outcomes") or "[]")
    for i, price in enumerate(prices):
        if float(price) >= 0.99:
            outcome_label = outcomes[i] if i < len(outcomes) else "?"
            return outcome_label.lower() in ("yes", "1", "true")
    return None
```

---

## Calibration export

When you want to feed data to the calibration tracker:

```bash
# Export all resolved analyses as CSV
GET /analyses?resolved=true&format=csv

# Or programmatically via db.py
analyses = db.get_all_resolved_analyses()   # sync — no asyncio anywhere in this project
# Each has: market_id, created_at, claude_prob, resolution
# Feed to calibration tracker's Brier score calculator
```

---

## Notes on edge cases

- **Markets analyzed multiple times**: use the `created_at` timestamp to know when each prediction was made. For calibration, each `(claude_prob, resolution)` pair at a specific timestamp is a valid data point.
- **Markets not yet resolved**: `resolved=NULL` in the DB. Don't include these in calibration calculations.
- **Very old analyses**: a prediction made 6 months before a market closed is a different quality of signal than one made 1 day before. Consider tracking `days_before_close = (end_date - created_at).days` for richer calibration analysis.
