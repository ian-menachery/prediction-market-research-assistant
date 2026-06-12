"""Portfolio simulation (read-only) — fade-to-0.5 fractional-Kelly backtest.

Simulates a mechanical betting strategy over the resolved binaries in the companion
calibration tracker's database, using the *crowd's* YES price at T-7d (recorded before
resolution, so lookahead-free) and the eventual outcome. It validates the whole
edge -> Kelly-sizing -> P&L pipeline on real data before any capital is risked.

Strategy (fade-to-0.5): treat every market's fair YES probability as 0.5; the crowd's
deviation from 50% is the mispricing we fade. For each market with a 7d price:
  - act only if |price - 0.5| > DIVERGENCE_THRESHOLD;
  - bet the CHEAP (contrarian) side toward 50%: price>0.5 -> buy NO (cost 1-price),
    price<0.5 -> buy YES (cost price). cost c = min(price, 1-price), edge = 0.5 - c;
  - size by fractional Kelly: f = KELLY_FRACTION * (0.5 - c)/(1 - c);
  - P&L on a FIXED 1.0 notional (no compounding, so P&L is additive across categories
    and order-independent): win -> +f * (1-c)/c ; loss -> -f.

IMPORTANT: this measures the CROWD signal on real outcomes — a pipeline/strategy sanity
check, NOT a forward guarantee. Extreme-price bets (near 0/1) carry huge odds and
dominate the variance. This script is strictly READ-ONLY and writes nothing, anywhere.

Run:  PYTHONPATH=src python scripts/portfolio_sim.py [db_path] [threshold] [kelly_fraction]
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_DEFAULT_DB = Path(__file__).resolve().parents[1].parent / "calibration-tracker" / "data" / "markets.db"

# Single-bucket category rules: first rule whose anchor tags intersect a market's tag
# set wins; markets with no match fall to "Other". Ordered specific -> general; edit
# freely. Geopolitics precedes Politics so e.g. "Trump strikes Iran" lands in Geopolitics.
CATEGORY_RULES: list[tuple[str, set[str]]] = [
    ("Crypto", {"crypto", "crypto-prices", "bitcoin", "ethereum", "hit-price"}),
    ("Sports", {"sports", "games", "nba", "basketball", "nfl", "nhl", "hockey"}),
    ("Geopolitics", {"geopolitics", "iran", "israel", "ukraine", "middle-east", "world", "military-strikes"}),
    ("Politics", {"politics", "us-election", "trump", "trump-presidency"}),
    ("Finance", {"finance", "pre-market"}),
    ("Pop-culture", {"pop-culture"}),
]

# Optional cost floor to suppress extreme-longshot bets (price near 0/1). Off by default
# so the honest, full-variance result is the default; raise it (e.g. 0.05) to clamp.
MIN_COST = 0.0


def categorize(tags: set[str]) -> str:
    """Assign one category by first-matching anchor-tag rule; else 'Other'."""
    for name, anchors in CATEGORY_RULES:
        if tags & anchors:
            return name
    return "Other"


def kelly_fraction(c: float, frac: float) -> float:
    """Fractional Kelly stake for the cheap side at cost ``c`` (belief = 0.5).

    Full Kelly = edge/(1-c) = (0.5 - c)/(1 - c); scaled by ``frac`` (e.g. 0.25).
    """
    return frac * (0.5 - c) / (1.0 - c)


def bet_pnl(c: float, won: bool, f: float) -> float:
    """Per-bet P&L on a fixed 1.0 notional: +f*(1-c)/c on win, -f on loss."""
    return f * (1.0 - c) / c if won else -f


def max_drawdown(pnls: list[float]) -> float:
    """Max peak-to-trough drop (absolute, bankroll units) of the running cumulative
    P&L over the given (already time-ordered) sequence. 0.0 if never below a prior peak."""
    cum = 0.0
    peak = 0.0
    worst = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        worst = max(worst, peak - cum)
    return worst


def _load_markets(db_path: str) -> list[tuple[str, float, str, float]]:
    """(market_id, crowd_7d_price, observed_at, resolved_value) for resolved binaries. READ-ONLY.

    Module-boundary note: this is the only raw SQL in the project outside db.py. It is
    read-only (mode=ro) and targets the EXTERNAL calibration-tracker DB, never our
    data/polymarket.db — research.db is not imported here.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            """
            SELECT s.market_id, s.price, s.observed_at, m.resolved_value
            FROM price_snapshots s
            JOIN markets m ON s.market_id = m.market_id
            WHERE s.snapshot_type = '7d'
              AND m.resolved_value IS NOT NULL
              AND s.price IS NOT NULL
            """
        ).fetchall()
    finally:
        conn.close()
    return [(str(mid), float(price), str(obs), float(rv)) for mid, price, obs, rv in rows]


def _load_tags(db_path: str) -> dict[str, set[str]]:
    """market_id -> set of tag_slug. READ-ONLY (same external-DB caveat as _load_markets)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT market_id, tag_slug FROM market_tags").fetchall()
    finally:
        conn.close()
    tags: dict[str, set[str]] = {}
    for mid, slug in rows:
        tags.setdefault(str(mid), set()).add(str(slug))
    return tags


def simulate(
    rows: list[tuple[str, float, str, float]],
    tags: dict[str, set[str]],
    threshold: float,
    frac: float,
) -> list[dict]:
    """Apply gate -> cheap side -> Kelly -> P&L. One record per bet placed."""
    bets: list[dict] = []
    for market_id, price, observed_at, resolved_value in rows:
        if abs(price - 0.5) <= threshold:
            continue  # crowd not confident enough; skip
        # Bet the cheap (contrarian) side toward 50%.
        if price > 0.5:
            side, c, won = "NO", 1.0 - price, resolved_value == 0.0
        else:
            side, c, won = "YES", price, resolved_value == 1.0
        if c < MIN_COST:
            continue  # extreme longshot clamped out (MIN_COST off by default)
        f = kelly_fraction(c, frac)
        bets.append({
            "category": categorize(tags.get(market_id, set())),
            "observed_at": observed_at,
            "side": side,
            "cost": c,
            "won": won,
            "stake": f,
            "pnl": bet_pnl(c, won, f),
        })
    return bets


def _aggregate(bets: list[dict]) -> dict:
    """Per-group totals for the summary row (bets assumed pre-filtered to the group)."""
    ordered = sorted(bets, key=lambda b: b["observed_at"])
    n = len(ordered)
    wins = sum(1 for b in ordered if b["won"])
    total_pnl = sum(b["pnl"] for b in ordered)
    total_stake = sum(b["stake"] for b in ordered)
    return {
        "bets": n,
        "hit_pct": 100.0 * wins / n if n else 0.0,
        "total_pnl": total_pnl,
        "avg_pnl": total_pnl / n if n else 0.0,
        "roi": total_pnl / total_stake if total_stake else 0.0,
        "max_dd": max_drawdown([b["pnl"] for b in ordered]),
    }


def main() -> None:
    db_path = sys.argv[1] if len(sys.argv) > 1 else str(_DEFAULT_DB)
    threshold = float(sys.argv[2]) if len(sys.argv) > 2 else 0.10
    frac = float(sys.argv[3]) if len(sys.argv) > 3 else 0.25

    rows = _load_markets(db_path)
    considered = len(rows)
    if considered == 0:
        print(f"No resolved markets with a 7d snapshot (db={db_path}).")
        return
    tags = _load_tags(db_path)
    bets = simulate(rows, tags, threshold, frac)

    # --- invariants ---
    assert all(0.0 < b["cost"] < 0.5 for b in bets), "cheap-side cost must be in (0, 0.5)"
    by_cat: dict[str, list[dict]] = {}
    for b in bets:
        by_cat.setdefault(b["category"], []).append(b)
    assert sum(len(v) for v in by_cat.values()) == len(bets), "category split must partition bets"

    overall = _aggregate(bets)
    cats = {name: _aggregate(group) for name, group in by_cat.items()}
    # Additivity check: per-category P&L sums to overall (fixed-fraction => order-independent).
    assert abs(sum(c["total_pnl"] for c in cats.values()) - overall["total_pnl"]) < 1e-9

    print("=" * 84)
    print("PORTFOLIO SIM  (fade-to-0.5, fractional Kelly, crowd 7d price — CROWD not Claude)")
    print("  pipeline/strategy sanity check on real outcomes; read-only; nothing written")
    print("=" * 84)
    print(f"db             : {db_path}")
    print(f"fair value     : 0.50  (belief; deviation is the mispricing we fade)")
    print(f"threshold      : {threshold:.3f}   (bet only if |price-0.5| > threshold)")
    print(f"Kelly fraction : {frac:.2f}   (stake = frac * (0.5-c)/(1-c), fixed 1.0 notional)")
    print(f"markets        : considered {considered}   bet {len(bets)}   skipped {considered - len(bets)}")
    print("-" * 84)
    hdr = f"{'Category':<13} {'Bets':>5} {'Hit%':>6} {'TotP&L':>9} {'Avg/bet':>9} {'ROI':>8} {'MaxDD':>8}"
    print(hdr)
    print("-" * 84)

    def _line(name: str, a: dict) -> str:
        return (f"{name:<13} {a['bets']:>5} {a['hit_pct']:>5.1f}% {a['total_pnl']:>9.3f} "
                f"{a['avg_pnl']:>9.4f} {a['roi']:>7.1%} {a['max_dd']:>8.3f}")

    for name in sorted(cats, key=lambda k: cats[k]["total_pnl"], reverse=True):
        print(_line(name, cats[name]))
    print("-" * 84)
    print(_line("OVERALL", overall))
    print("=" * 84)
    print("Reminder: CROWD signal on real outcomes — a sanity check, not a forward guarantee.")
    print("Extreme-price bets (cost near 0) carry huge odds and dominate the variance/drawdown.")


if __name__ == "__main__":
    main()
