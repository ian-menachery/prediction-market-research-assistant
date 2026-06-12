"""Crowd-calibration backtest (read-only) — pipeline validation + market baseline.

Fits the calibration machinery on the *crowd's* forward probability — the market's
YES price at T-7d, recorded before resolution — paired with the eventual outcome,
over the resolved binaries in the companion calibration tracker's database.

This is lookahead-free (the 7d price predates resolution) and serves two purposes:
  1. validate research.calibration's temperature fit + reliability curve on real data;
  2. measure how well-calibrated the *crowd* is at T-7d.

IMPORTANT: this measures the CROWD, not Claude. The pairs here must NEVER be written
into our analyses table or fed to the per-model recalibrators — that would corrupt
Phase-3 (Claude) calibration. This script is strictly read-only and writes nothing.

Run:  PYTHONPATH=src python scripts/backtest_crowd_calibration.py [db_path] [horizon]
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# research.calibration provides the math; we reimplement none of it.
from research import calibration as cal

_DEFAULT_DB = Path(__file__).resolve().parents[1].parent / "calibration-tracker" / "data" / "markets.db"


def _load_pairs(db_path: str, horizon: str = "7d") -> list[tuple[float, bool]]:
    """(crowd_price_at_horizon, outcome_bool) for resolved binaries. READ-ONLY.

    Module-boundary note: this is the only raw SQL in the project outside db.py. It
    is read-only (mode=ro) and targets the EXTERNAL calibration-tracker DB, never our
    data/polymarket.db — research.db is not imported here.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            """
            SELECT s.price, m.resolved_value
            FROM price_snapshots s
            JOIN markets m ON s.market_id = m.market_id
            WHERE s.snapshot_type = ?
              AND m.resolved_value IS NOT NULL
              AND s.price IS NOT NULL
            """,
            (horizon,),
        ).fetchall()
    finally:
        conn.close()

    pairs: list[tuple[float, bool]] = []
    for price, value in rows:
        p = float(price)
        if 0.0 <= p <= 1.0:  # defensive: skip any out-of-range snapshot
            pairs.append((p, value == 1.0))
    return pairs


def main() -> None:
    db_path = sys.argv[1] if len(sys.argv) > 1 else str(_DEFAULT_DB)
    horizon = sys.argv[2] if len(sys.argv) > 2 else "7d"

    pairs = _load_pairs(db_path, horizon)
    n = len(pairs)
    if n == 0:
        print(f"No pairs found (db={db_path}, horizon={horizon}).")
        return

    # --- invariants ---
    assert all(0.0 <= p <= 1.0 for p, _ in pairs), "price out of [0,1]"
    assert all(isinstance(y, bool) for _, y in pairs), "non-bool outcome"

    base_rate = sum(1 for _, y in pairs if y) / n
    mean_price = sum(p for p, _ in pairs) / n

    raw_brier = cal.brier_score(pairs)
    raw_ll = cal.log_loss(pairs)

    temperature = cal.fit_temperature(pairs)
    cal_pairs = [(cal.apply_temperature(p, temperature), y) for p, y in pairs]
    cal_brier = cal.brier_score(cal_pairs)
    cal_ll = cal.log_loss(cal_pairs)

    # Temperature minimizes log-loss with T=1 in range, so the optimum can't be worse.
    assert cal_ll <= raw_ll + 1e-9, (cal_ll, raw_ll)

    curve = cal.calibration_curve(pairs)
    assert sum(b["count"] for b in curve) == n, "curve counts must sum to N"

    if temperature > 1.05:
        tnote = "T > 1 -> crowd is overconfident (prices pushed toward extremes)"
    elif temperature < 0.95:
        tnote = "T < 1 -> crowd is underconfident (prices too close to 50%)"
    else:
        tnote = "T ~ 1 -> crowd is already well-calibrated at this horizon"

    print("=" * 72)
    print("CROWD calibration backtest  (market price at T-{}, NOT Claude)".format(horizon))
    print("  pipeline validation + crowd baseline; read-only; nothing written")
    print("=" * 72)
    print(f"db            : {db_path}")
    print(f"N pairs       : {n}")
    print(f"base rate YES : {base_rate:.3f}")
    print(f"mean price    : {mean_price:.3f}")
    print("-" * 72)
    print(f"raw   : Brier {raw_brier:.4f}   log-loss {raw_ll:.4f}")
    print(f"temp T: {temperature:.3f}   ({tnote})")
    print(f"post-T: Brier {cal_brier:.4f}   log-loss {cal_ll:.4f}   "
          f"(log-loss improvement {raw_ll - cal_ll:+.4f})")
    print("-" * 72)
    print("reliability (10 bins):")
    print(f"  {'bin':>9}  {'predicted':>9}  {'actual':>7}  {'count':>6}")
    for b in curve:
        lo, hi = round(b["bin_lo"] * 100), round(b["bin_hi"] * 100)
        pm = "-" if b["predicted_mean"] is None else f"{b['predicted_mean']*100:.0f}%"
        er = "-" if b["empirical_rate"] is None else f"{b['empirical_rate']*100:.0f}%"
        print(f"  {lo:>3}-{hi:<3}%  {pm:>9}  {er:>7}  {b['count']:>6}")
    print("=" * 72)
    print("Reminder: CROWD calibration only. Do not feed these into analyses / Claude recalibrators.")


if __name__ == "__main__":
    main()
