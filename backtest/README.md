# backtest/ — archived edge-search artifacts

Frozen, historical record of the 2026-07-30 edge search + Politics-gate backtest. **Nothing here is
live or meant to be re-run.** The analysis and conclusions live in [`../BACKTEST_NOTES.md`](../BACKTEST_NOTES.md);
the project verdict is in [`../CLAUDE.md`](../CLAUDE.md) (top). Result: **NULL — no accessible edge; stay wound down.**

## `archive/` — Politics gate probe (see BACKTEST_NOTES.md §2)
- **`politics_gate_probe.py`** — the exact one-shot script that ran PMRA's analyzer on 6 resolved
  Politics markets. **DO NOT RUN:** it spends Anthropic credits and, because the target markets are
  post-training-cutoff, re-leaks the outcome (so it can't produce a valid backtest anyway).
- **`politics_gate_probe_out.jsonl`** — the frozen raw output of that probe (model prob, confidence,
  web-search count, cost, full summary + factors, actual outcome). Preserved so the $0.70 of probe
  results is never lost. 6/6 rows show the model reading the outcome off the web (look-ahead leak).

## `behavioral/` — mechanical/behavioral mispricing study (see BACKTEST_NOTES.md §3)
The largest pass: 4,610 resolved markets / 1,901 events, event-clustered, testing longshot bias,
timing, structure. **$0 / read-only.** Result: favorite-longshot bias is real *gross* but net-negative
after the ~7% overround; nothing exploitable survives.
- **`priced_rows.jsonl`** — the frozen 4,610-market dataset (outcome + entry prices with far-touch
  bid/ask at four horizons). The ground truth behind §3.
- **`settled_sample.json`** — the input sample (with open/close timestamps) `priced_rows.jsonl` was built from.
- **`analyze.py`** — reproduces **every §3 number offline from `priced_rows.jsonl`** ($0, no network).
  This is the runnable one: `python analyze.py`.
- **`build_sample.py`** / **`fetch_prices.py`** — rebuild the sample from live Kalshi and attach
  candlestick prices. Free but hit the API and re-snapshot a drifting universe; the frozen files above
  are what was actually analyzed.
