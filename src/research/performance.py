"""Signal performance: turn the forward-signal P&L log into a track record.

Pure aggregation (stdlib only) — reads *settled* signals via ``db`` and never writes, mirroring
``calibration.py``. A signal's cost basis is its modeled VWAP fill (``price_paid * fill_shares``);
per-trade return is ``pnl / cost_basis``. The equity curve is cumulative realized P&L walked in
signal-entry order. Open (unresolved) signals are never counted toward realized stats — only their
count is surfaced, so the track record stays lookahead-free.
"""

from __future__ import annotations

import math

from research import db
from research.models import Signal


def _cost_basis(s: Signal) -> float:
    """USD actually deployed on the modeled fill."""
    return s.price_paid * s.fill_shares


def _trade_return(s: Signal) -> float:
    cost = _cost_basis(s)
    return (s.pnl / cost) if (cost and s.pnl is not None) else 0.0


def max_drawdown(cum_pnl: list[float]) -> float:
    """Largest peak-to-trough drop ($) along the cumulative-P&L curve. 0.0 if monotonic/empty.

    The running peak starts at 0 (flat before the first trade), so a curve that only ever rises
    has zero drawdown.
    """
    peak = 0.0
    mdd = 0.0
    for v in cum_pnl:
        peak = max(peak, v)
        mdd = max(mdd, peak - v)
    return mdd


def sharpe(returns: list[float]) -> float | None:
    """Per-trade Sharpe: mean / sample-stdev of per-trade returns (no annualization).

    ``None`` with fewer than two trades or zero variance — undefined rather than an invented 0.
    """
    n = len(returns)
    if n < 2:
        return None
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    if var < 1e-12:  # flat (or fp-noise-flat) returns => risk-adjusted return is undefined
        return None
    return mean / math.sqrt(var)


def _breakdown(signals: list[Signal], key_fn) -> list[dict]:
    """Group settled signals by a key, with count / P&L / win-rate per group (P&L desc)."""
    groups: dict[str, list[Signal]] = {}
    for s in signals:
        groups.setdefault(str(key_fn(s)), []).append(s)
    out: list[dict] = []
    for key, sigs in groups.items():
        wins = sum(1 for s in sigs if (s.pnl or 0.0) > 0)
        out.append({
            "key": key,
            "n": len(sigs),
            "pnl": sum(s.pnl or 0.0 for s in sigs),
            "win_rate": wins / len(sigs) if sigs else None,
        })
    out.sort(key=lambda e: e["pnl"], reverse=True)
    return out


def _empty(open_count: int) -> dict:
    """Valid empty shape — no invented zeros for ratios that are undefined with no data."""
    return {
        "settled": 0, "open": open_count,
        "total_pnl": 0.0, "total_cost": 0.0, "total_return": None,
        "wins": 0, "losses": 0, "win_rate": None,
        "avg_win": None, "avg_loss": None, "profit_factor": None,
        "sharpe": None, "max_drawdown": 0.0,
        "equity_curve": [], "by_exchange": [], "by_side": [],
    }


def build_report(signals: list[Signal], open_count: int = 0) -> dict:
    """Aggregate settled signals into a track-record report (pure — no I/O)."""
    settled = [s for s in signals if s.resolved and s.pnl is not None]
    n = len(settled)
    if n == 0:
        return _empty(open_count)

    wins = [s for s in settled if s.pnl > 0]
    losses = [s for s in settled if s.pnl <= 0]
    returns = [_trade_return(s) for s in settled]

    acc = 0.0
    curve: list[dict] = []
    for i, s in enumerate(settled):
        acc += s.pnl
        curve.append({
            "i": i,
            "t": s.created_at.isoformat(),
            "cum_pnl": acc,
            "question": s.question,
        })

    total_pnl = sum(s.pnl for s in settled)
    total_cost = sum(_cost_basis(s) for s in settled)
    gross_win = sum(s.pnl for s in wins)
    gross_loss = -sum(s.pnl for s in losses)  # positive magnitude of losing P&L

    return {
        "settled": n,
        "open": open_count,
        "total_pnl": total_pnl,
        "total_cost": total_cost,
        "total_return": (total_pnl / total_cost) if total_cost else None,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / n,
        "avg_win": (gross_win / len(wins)) if wins else None,
        "avg_loss": (-gross_loss / len(losses)) if losses else None,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "sharpe": sharpe(returns),
        "max_drawdown": max_drawdown([p["cum_pnl"] for p in curve]),
        "equity_curve": curve,
        "by_exchange": _breakdown(settled, lambda s: s.exchange),
        "by_side": _breakdown(settled, lambda s: s.side),
    }


def report() -> dict:
    """Track-record report from the persisted signal log (reads settled signals + open count)."""
    return build_report(db.get_resolved_signals(), db.signal_summary()["open"])
