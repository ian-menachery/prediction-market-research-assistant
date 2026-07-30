# backtest/ — archived edge-search artifacts

Frozen, historical record of the 2026-07-30 edge search + Politics-gate backtest. **Nothing here is
live or meant to be re-run.** The analysis and conclusions live in [`../BACKTEST_NOTES.md`](../BACKTEST_NOTES.md);
the project verdict is in [`../CLAUDE.md`](../CLAUDE.md) (top). Result: **NULL — no accessible edge; stay wound down.**

## `archive/`
- **`politics_gate_probe.py`** — the exact one-shot script that ran PMRA's analyzer on 6 resolved
  Politics markets. **DO NOT RUN:** it spends Anthropic credits and, because the target markets are
  post-training-cutoff, re-leaks the outcome (so it can't produce a valid backtest anyway).
- **`politics_gate_probe_out.jsonl`** — the frozen raw output of that probe (model prob, confidence,
  web-search count, cost, full summary + factors, actual outcome). Preserved so the $0.70 of probe
  results is never lost. 6/6 rows show the model reading the outcome off the web (look-ahead leak).
