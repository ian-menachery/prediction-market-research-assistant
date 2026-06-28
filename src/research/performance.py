"""Signal performance: turn the forward-signal P&L log into a track record.

Pure aggregation (stdlib only) — reads *settled* signals via ``db`` and never writes, mirroring
``calibration.py``. A signal's cost basis is its modeled VWAP fill (``price_paid * fill_shares``);
per-trade return is ``pnl / cost_basis``. The equity curve is cumulative realized P&L walked in
signal-entry order. Open (unresolved) signals are never counted toward realized stats — only their
count is surfaced, so the track record stays lookahead-free.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable

from research import db, pricing
from research.models import Signal


def _category_of(market_id: str) -> str:
    """Coarse category from a Kalshi ticker's series prefix — for per-category P&L breakdown.

    Tells you which kinds of markets actually make money (weather vs econ vs crypto), which feeds
    the discovery weighting / down-weight decisions. Unknown prefixes fall through to "other".
    """
    p = market_id.split("-", 1)[0].upper()
    if p.startswith("KXHIGH"):
        return "weather"
    if p.startswith(("KXBTC", "KXETH")):
        return "crypto"
    if any(p.startswith(e) for e in ("KXCPI", "KXPAYROLL", "KXFED", "KXGDP", "KXINITIAL", "KXJOBS")):
        return "econ"
    return "other"


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


def _breakdown(signals: list[Signal], key_fn: Callable[[Signal], object]) -> list[dict]:
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
        "equity_curve": [], "by_exchange": [], "by_side": [], "by_category": [],
    }


def build_report(signals: list[Signal], open_count: int = 0) -> dict:
    """Aggregate settled signals into a track-record report (pure — no I/O)."""
    settled = [s for s in signals if s.resolved and s.pnl is not None]
    n = len(settled)
    if n == 0:
        return _empty(open_count)

    # `settled` already filtered pnl is not None; `(s.pnl or 0.0)` re-narrows for the type
    # checker (and a 0.0 pnl behaves identically either way).
    wins = [s for s in settled if (s.pnl or 0.0) > 0]
    losses = [s for s in settled if (s.pnl or 0.0) <= 0]
    returns = [_trade_return(s) for s in settled]

    acc = 0.0
    curve: list[dict] = []
    for i, s in enumerate(settled):
        acc += s.pnl or 0.0
        curve.append({
            "i": i,
            "t": s.created_at.isoformat(),
            "cum_pnl": acc,
            "question": s.question,
        })

    total_pnl = sum(s.pnl or 0.0 for s in settled)
    total_cost = sum(_cost_basis(s) for s in settled)
    gross_win = sum(s.pnl or 0.0 for s in wins)
    gross_loss = -sum(s.pnl or 0.0 for s in losses)  # positive magnitude of losing P&L

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
        "by_category": _breakdown(settled, lambda s: _category_of(s.market_id)),
    }


def report() -> dict:
    """Track-record report from the persisted signal log (reads settled signals + open count)."""
    return build_report(db.get_resolved_signals(), db.signal_summary()["open"])


def total_credit_spend() -> float:
    """Total USD spent on LLM analysis to date, summed from each analysis's stored token usage.

    Re-priced from current ``pricing.RATES`` (durable token counts, price-independent). Note the
    batch 50% discount isn't stored per-analysis, so batched scans are counted at full price — a
    slight *over*-estimate, which keeps the ROI bar honest (conservative).
    """
    total = 0.0
    for r in db.get_analysis_cost_rows():
        total += pricing.cost_usd(
            r["model"], r["input_tokens"], r["output_tokens"],
            r["cache_creation_input_tokens"], r["cache_read_input_tokens"],
            web_search_requests=r["web_search_requests"],
        )
    return total


def divergence_review(threshold: float | None = None) -> list[dict]:
    """Extreme-divergence signals joined with the model's reasoning — the "why did it diverge?" loop.

    Surfaces signals whose |our YES prob − market mid| ≥ ``threshold`` (env ``EXTREME_DIVERGENCE``,
    default 0.40) with the analysis reasoning (summary/factors/confidence) + refutation verdict +
    outcome once resolved. Reviewing these — especially resolved-and-LOST ones — reveals recurring
    model mistakes (e.g. misread thresholds/dates) to fix in the analyzer prompt. Reads only.
    """
    thr = threshold if threshold is not None else float(os.getenv("EXTREME_DIVERGENCE", "0.40"))
    out: list[dict] = []
    for s in db.get_signals(limit=500):
        div = abs(s.calibrated_prob - s.market_prob)
        if div < thr:
            continue
        a = db.get_latest_analysis(s.market_id)
        out.append({
            "id": s.id, "question": s.question, "exchange": s.exchange, "side": s.side,
            "our_prob": s.calibrated_prob, "market_prob": s.market_prob, "divergence": round(div, 4),
            "ev": s.ev, "model": s.model,
            "verdict": s.adversarial_verdict, "refuter_model": s.refuter_model,
            "resolved": s.resolved, "resolution": s.resolution, "pnl": s.pnl,
            "confidence": a.confidence if a else None,
            "summary": a.summary if a else None,
            "factors": a.factors if a else None,
        })
    out.sort(key=lambda x: x["divergence"], reverse=True)
    return out


def roi() -> dict:
    """The headline ROI gauge: realized trading P&L vs Claude credit spend.

    ``net`` < 0 means the tool isn't paying for itself yet. ``open`` positions are excluded from
    realized P&L (lookahead-free) but surfaced so you know how much is still in flight.
    """
    summ = db.signal_summary()
    realized = summ["realized_pnl"]
    spend = total_credit_spend()
    return {
        "realized_pnl": round(realized, 2),
        "credit_spend": round(spend, 2),
        "net": round(realized - spend, 2),
        "covered": realized >= spend,
        "settled": summ["resolved"],
        "open": summ["open"],
    }
