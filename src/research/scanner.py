"""Batch EV scanner — fetch markets, analyze, rank by (uncalibrated) annualized EV.

Sequential (no asyncio); a ``time.sleep`` between Claude calls keeps us within
rate limits. Markets analyzed within ``max_age_hours`` reuse their stored analysis
instead of paying for a fresh call.

IMPORTANT: the EV figures here use the market **mid** price and Claude's
**uncalibrated** estimate. They are directional only until Phase 3 (calibration)
and Phase 3.5 (executable bid/ask). Treat the ranking as "where to look," not
"guaranteed +EV."
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from research import analyzer, calibration, db, polymarket
from research.models import Market, ScanRequest, ScanResult


def _days_to_close(market: Market) -> float | None:
    if market.end_date is None:
        return None
    return (market.end_date - datetime.now(timezone.utc)).total_seconds() / 86400.0


def _ev_fields(
    market: Market, prob: float, bid: float | None, ask: float | None, min_days: float
) -> dict:
    """EV on the favorable side, priced at the executable book when available.

    Side is the directional intent (calibrated ``prob`` vs the market mid). When a
    two-sided book is supplied (``bid`` and ``ask``), the cost is what you'd fill at:
    BUY YES at the ask, or bet NO at ``1 - bid`` (buying NO == selling YES at the
    bid). Otherwise it falls back to the mid price and ``executable`` is False.
    ``ev`` can be negative once the spread is included — that's surfaced, not dropped.
    """
    mp = market.market_prob
    dtc = _days_to_close(market)
    side = "YES" if prob > mp else "NO"
    executable = bid is not None and ask is not None

    if side == "YES":
        price_paid = ask if executable else mp
        ev = prob - price_paid
    else:
        price_paid = (1.0 - bid) if executable else (1.0 - mp)
        ev = (1.0 - prob) - price_paid

    valid = price_paid is not None and 0.0 < price_paid < 1.0
    ev_pct = ev / price_paid if valid else None
    kelly = ev / (1.0 - price_paid) if valid else None
    annualized = ev_pct * 365.0 / dtc if (ev_pct is not None and dtc is not None and dtc >= min_days) else None
    return {"side": side, "ev": ev if valid else None, "ev_pct": ev_pct, "kelly": kelly,
            "annualized_ev": annualized, "days_to_close": dtc,
            "best_bid": bid, "best_ask": ask, "price_paid": price_paid, "executable": executable}


def scan(req: ScanRequest) -> list[ScanResult]:
    """Run a batch EV scan and return results sorted by annualized EV (desc)."""
    recals = calibration.build_recalibrators()  # one per model; fit once, applied per market
    markets = polymarket.fetch_all_active(max_markets=req.max_markets)
    db.upsert_markets(markets)  # persist fetched markets (also satisfies the analyses FK)

    # Cheap pre-filters first — they bound how many paid Claude calls we make.
    def passes_pre(m: Market) -> bool:
        dtc = _days_to_close(m)
        return (
            (m.volume_24h or 0.0) >= req.min_volume_24h
            and (m.liquidity or 0.0) >= req.min_liquidity
            and dtc is not None
            and dtc >= req.min_days_to_close
            and (req.category is None or req.category in m.tags)
        )

    candidates = [m for m in markets if passes_pre(m)]
    delay = float(os.getenv("ANALYSIS_DELAY_SECONDS", "1.5"))
    results: list[ScanResult] = []

    for m in candidates:
        age = db.get_analysis_age_hours(m.id)
        if age is not None and age <= req.max_age_hours:
            analysis = db.get_latest_analysis(m.id)  # reuse recent — no API cost
        else:
            analysis = analyzer.analyze_market(m)
            if analysis.error:
                continue  # don't persist failures; skip
            db.save_analysis(analysis)
            time.sleep(delay)

        if analysis is None or analysis.claude_prob is None:
            continue

        recal = recals.get(analysis.model) or calibration.identity_recalibrator(analysis.model)
        calibrated_p = recal.apply(analysis.claude_prob)

        mp = m.market_prob
        if mp is None or calibrated_p is None:
            continue
        # Gate on mid divergence FIRST — cheap, so we make no CLOB call for markets
        # that don't clear the bar.
        if abs(calibrated_p - mp) < req.min_divergence:
            continue

        # Only survivors hit the order book; fall back to mid if it's unavailable.
        book = None
        if m.yes_token_id:
            try:
                book = polymarket.fetch_best_bid_ask(m.yes_token_id)
            except Exception:  # noqa: BLE001 — a CLOB hiccup shouldn't kill the scan
                book = None
        bid, ask = book if book else (None, None)

        ev = _ev_fields(m, calibrated_p, bid, ask, req.min_days_to_close)
        if ev["annualized_ev"] is None:  # below the days floor (defensive; pre-filtered)
            continue
        results.append(ScanResult(market=m, analysis=analysis, calibrated_prob=calibrated_p, **ev))

    results.sort(key=lambda r: r.annualized_ev or -1.0, reverse=True)
    return results
