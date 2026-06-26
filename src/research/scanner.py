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

import json
import logging
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from research import analyzer, calibration, db, exchanges, polymarket
from research.models import Analysis, Market, ScanRequest, ScanResult, Signal

_log = logging.getLogger(__name__)

REFUTE_BAND = float(os.getenv("REFUTE_BAND", "0.03"))  # refuter must still diverge from the market by this much to "hold"


def _refute_verdict(
    side: str | None, market_prob: float | None, cal_refuter: float | None
) -> Literal["holds", "refuted"] | None:
    """holds if the (recalibrated) refuter still backs the original side past REFUTE_BAND."""
    if market_prob is None or cal_refuter is None or side is None:
        return None
    if side == "YES":
        return "holds" if cal_refuter > market_prob + REFUTE_BAND else "refuted"
    return "holds" if cal_refuter < market_prob - REFUTE_BAND else "refuted"


def _days_to_close(market: Market) -> float | None:
    if market.end_date is None:
        return None
    return (market.end_date - datetime.now(timezone.utc)).total_seconds() / 86400.0


def _ev_fields(
    market: Market, prob: float, yes_cost: float | None, no_cost: float | None, min_days: float
) -> dict:
    """EV on the favorable side, priced at the executable book when available.

    Side is the directional intent (calibrated ``prob`` vs the market mid). ``yes_cost``
    and ``no_cost`` are the executable per-share costs already on the correct side — the
    VWAP fill to deploy the target position (BUY YES at the ask VWAP; bet NO at the
    ``1 - bid`` VWAP, since buying NO == selling YES into the bids). When both are
    supplied the position is priced off the book; otherwise it falls back to the mid
    price and ``executable`` is False. ``ev`` can be negative once depth is included —
    that's surfaced, not dropped.
    """
    mp = market.market_prob
    assert mp is not None  # caller (scan) gates this; a mid price is required to price EV
    dtc = _days_to_close(market)
    side = "YES" if prob > mp else "NO"
    executable = yes_cost is not None and no_cost is not None

    if side == "YES":
        price_paid = yes_cost if (executable and yes_cost is not None) else mp
        ev = prob - price_paid
    else:
        price_paid = no_cost if (executable and no_cost is not None) else (1.0 - mp)
        ev = (1.0 - prob) - price_paid

    valid = price_paid is not None and 0.0 < price_paid < 1.0
    ev_pct = ev / price_paid if valid else None
    kelly = ev / (1.0 - price_paid) if valid else None
    annualized = ev_pct * 365.0 / dtc if (ev_pct is not None and dtc is not None and dtc >= min_days) else None
    return {"side": side, "ev": ev if valid else None, "ev_pct": ev_pct, "kelly": kelly,
            "annualized_ev": annualized, "days_to_close": dtc,
            "price_paid": price_paid, "executable": executable}


def _book_fills(book: polymarket.Book, target: float) -> dict:
    """Top-of-book + VWAP fills to deploy ``target`` USD on each side.

    BUY YES into the asks; bet NO into the bids (NO cost per share = 1 - yes bid). Both
    ladders are best-first. Pure given a ``Book`` — testable without a network call.
    """
    yes_fill = polymarket.vwap_fill([(p, s) for p, s in book.asks], target)
    no_fill = polymarket.vwap_fill([(1.0 - p, s) for p, s in book.bids], target)
    return {
        "best_bid": book.best_bid, "best_ask": book.best_ask,
        "bid_depth": book.bid_depth, "ask_depth": book.ask_depth,
        "yes_fill": yes_fill, "no_fill": no_fill,
        "yes_cost": yes_fill.price if yes_fill else None,
        "no_cost": no_fill.price if no_fill else None,
    }


def _passes_pre(m: Market, req: ScanRequest, kalshi_min_volume: float) -> bool:
    """Cheap pre-filter — bounds how many paid Claude calls a scan makes.

    Kalshi reports no 24h volume, so it's gated on lifetime ``volume_total`` against its own
    floor; Polymarket keeps the per-scan 24h-volume gate.
    """
    dtc = _days_to_close(m)
    if m.exchange == "kalshi":
        vol_ok = (m.volume_total or 0.0) >= kalshi_min_volume
    else:
        vol_ok = (m.volume_24h or 0.0) >= req.min_volume_24h
    return (
        vol_ok
        and (m.liquidity or 0.0) >= req.min_liquidity
        and dtc is not None
        and dtc >= req.min_days_to_close
        and (req.category is None or req.category in m.tags)
    )


def _analyze_or_reuse(
    m: Market, max_age_hours: float, delay: float, allow_fresh: bool
) -> tuple[Analysis | None, bool]:
    """Reuse a recent stored analysis (free) or run a fresh one (one LLM call).

    Returns ``(analysis, made_call)``. A cache hit returns ``(analysis, False)`` — no API cost.
    When ``allow_fresh`` is False and there's no fresh-enough cache, returns ``(None, False)``
    without calling the LLM (the per-scan budget is spent). A fresh call that errors returns
    ``(None, True)`` — it was attempted, so it still counts against the budget.
    """
    age = db.get_analysis_age_hours(m.id)
    if age is not None and age <= max_age_hours:
        return db.get_latest_analysis(m.id), False  # reuse recent — no API cost
    if not allow_fresh:
        return None, False  # per-scan LLM budget exhausted — skip without spending
    analysis = analyzer.analyze_market(m)
    if analysis.error:
        return None, True  # call was made (spent) even though it failed; don't persist
    db.save_analysis(analysis)
    time.sleep(delay)
    return analysis, True


def _pack_result(
    m: Market, analysis: Analysis, calibrated_p: float, req: ScanRequest, target: float
) -> ScanResult | None:
    """Price the favorable side off the live book (mid fallback) and build a ScanResult.

    Returns None when EV can't be annualized (below the days floor — defensive; pre-filtered).
    Only called for markets that already cleared the cheap mid-divergence gate.
    """
    book = exchanges.fetch_book(m)
    best_bid = best_ask = bid_depth = ask_depth = None
    yes_cost = no_cost = None
    yes_fill = no_fill = None
    if book:
        f = _book_fills(book, target)
        best_bid, best_ask = f["best_bid"], f["best_ask"]
        bid_depth, ask_depth = f["bid_depth"], f["ask_depth"]
        yes_fill, no_fill = f["yes_fill"], f["no_fill"]
        yes_cost, no_cost = f["yes_cost"], f["no_cost"]

    ev = _ev_fields(m, calibrated_p, yes_cost, no_cost, req.min_days_to_close)
    if ev["annualized_ev"] is None:
        return None
    chosen_fill = yes_fill if ev["side"] == "YES" else no_fill
    return ScanResult(
        market=m, analysis=analysis, calibrated_prob=calibrated_p,
        best_bid=best_bid, best_ask=best_ask, bid_depth=bid_depth, ask_depth=ask_depth,
        fill_shares=chosen_fill.shares if chosen_fill else None,
        fully_filled=chosen_fill.fully_filled if chosen_fill else False,
        target_position_usd=target if ev["executable"] else None,
        **ev,
    )


def _refute_top(
    results: list[ScanResult],
    req: ScanRequest,
    recals: dict[str, calibration.Recalibrator],
    delay: float,
    budget: int,
    calls_so_far: int,
) -> int:
    """Adversarial second pass over the top-ranked edges; returns the LLM calls it made.

    Honors the per-scan ``budget`` (0 = unlimited): stops once the scan's total LLM calls reach
    it, so refutations share the same spend cap as analyses. Mutates each result's ``refutation``
    in place — flag-only; edges are never dropped.
    """
    made = 0
    for r in results[: req.refute_top]:
        if budget and (calls_so_far + made) >= budget:
            break  # per-scan LLM budget reached — stop spending on refutations
        if r.calibrated_prob is None:  # packed results always carry one; defensive
            continue
        ref = analyzer.refute_edge(r.market, r.calibrated_prob, original_model=r.analysis.model)
        made += 1
        time.sleep(delay)
        if ref.error is None and ref.refuter_prob is not None:
            recal = recals.get(r.analysis.model or "") or calibration.identity_recalibrator(r.analysis.model)
            ref.verdict = _refute_verdict(r.side, r.market.market_prob, recal.apply(ref.refuter_prob))
        r.refutation = ref
    return made


def scan(req: ScanRequest) -> list[ScanResult]:
    """Run a batch EV scan and return results sorted by annualized EV (desc)."""
    recals = calibration.build_recalibrators()  # one per model; fit once, applied per market
    markets = exchanges.fetch_active(req.max_markets)
    db.upsert_markets(markets)  # persist fetched markets (also satisfies the analyses FK)

    kalshi_min_volume = float(os.getenv("KALSHI_MIN_VOLUME", "5000"))
    candidates = [m for m in markets if _passes_pre(m, req, kalshi_min_volume)]
    delay = float(os.getenv("ANALYSIS_DELAY_SECONDS", "1.5"))
    target = float(os.getenv("TARGET_POSITION_USD", "50"))  # VWAP fill sizes EV to this
    budget = req.max_llm_calls  # cap on fresh LLM calls this scan (0 = unlimited); see ScanRequest
    calls_made = 0
    results: list[ScanResult] = []

    for m in candidates:
        allow_fresh = budget == 0 or calls_made < budget
        analysis, made_call = _analyze_or_reuse(m, req.max_age_hours, delay, allow_fresh)
        calls_made += int(made_call)
        if analysis is None or analysis.claude_prob is None:
            continue

        recal = recals.get(analysis.model or "") or calibration.identity_recalibrator(analysis.model)
        calibrated_p = recal.apply(analysis.claude_prob)

        mp = m.market_prob
        if mp is None or calibrated_p is None:
            continue
        # Gate on mid divergence FIRST — cheap, so we make no order-book call for markets
        # that don't clear the bar.
        if abs(calibrated_p - mp) < req.min_divergence:
            continue

        result = _pack_result(m, analysis, calibrated_p, req, target)
        if result is not None:
            results.append(result)

    results.sort(key=lambda r: r.annualized_ev or -1.0, reverse=True)
    calls_made += _refute_top(results, req, recals, delay, budget, calls_made)
    if budget and calls_made >= budget:
        _log.info(
            "scan reached the LLM-call cap (max_llm_calls=%d); remaining markets/refutations were "
            "skipped to bound spend", budget,
        )
    return results


def sweep_resolutions() -> int:
    """Record resolutions for analyzed-but-unresolved markets; return the count.

    One Gamma call per market (fine at MVP scale). Resolutions are lost once a market
    drops out of the active fetch, so this runs on every refresh / scheduled scan to
    capture them while they're live. Per-market errors are swallowed (next sweep retries).
    """
    resolved = 0
    for market_id in db.get_unresolved_analyzed_market_ids():
        market = db.get_market(market_id)
        if market is None:  # analyzed market no longer in the table (shouldn't happen) — skip
            continue
        try:
            outcome = exchanges.fetch_resolution(market)  # exchange-aware (Polymarket or Kalshi)
        except Exception as e:  # noqa: BLE001 — transient; next sweep retries
            _log.warning("resolution fetch failed for %s: %s", market_id, e)
            continue
        if outcome is not None:
            db.mark_resolution(market_id, outcome)
            resolved += 1
            # Settle any open forward signals on this market. P&L is modeled at the
            # recorded VWAP fill: a winning side pays (1 - price_paid) per share, a
            # losing side forfeits price_paid per share. (Formula lives here, not db.)
            for sig in db.get_open_signals_for_market(market_id):
                assert sig.id is not None  # persisted rows always carry an id
                won = (sig.side == "YES") == outcome
                pnl = sig.fill_shares * (1.0 - sig.price_paid) if won else -sig.fill_shares * sig.price_paid
                db.resolve_signal(sig.id, outcome, pnl)
    return resolved


def reanalyze_stale() -> int:
    """Re-analyze markets whose price drifted past the stale threshold; return the count.

    Stale = ``db.is_stale`` flagged (current price moved > ``STALE_THRESHOLD`` since the
    latest analysis). Bounded by ``STALE_REANALYZE_MAX`` (default 20) so one run can't blow
    the LLM budget; the cap being hit is logged (never a silent truncation). Failed analyses
    aren't persisted (graceful — matches ``scan``).
    """
    cap = int(os.getenv("STALE_REANALYZE_MAX", "20"))
    delay = float(os.getenv("ANALYSIS_DELAY_SECONDS", "1.5"))
    stale = [mwa.market for mwa in db.get_markets_with_latest_analysis() if mwa.stale]
    reanalyzed = 0
    for m in stale[:cap]:
        analysis = analyzer.analyze_market(m)
        if analysis.error:
            continue
        db.save_analysis(analysis)
        reanalyzed += 1
        time.sleep(delay)
    if len(stale) > cap:
        _log.info("reanalyze_stale: %d stale markets found, capped at %d this run", len(stale), cap)
    return reanalyzed


def persist_signals(results: list[ScanResult]) -> int:
    """Log the actionable subset of a scan as forward signals; return the count saved.

    Actionable = priced off the live book (``executable``) with a positive per-share
    edge above ``SIGNAL_MIN_EV`` (env, default 0.0). Deduped against open signals so at
    most one open position exists per ``(market_id, side)`` — including within this batch.
    Prices/EV are frozen here; realized P&L is filled in by ``sweep_resolutions``.
    """
    min_ev = float(os.getenv("SIGNAL_MIN_EV", "0.0"))
    open_keys = db.get_open_signal_keys()
    saved = 0
    for r in results:
        if not (r.executable and r.ev is not None and r.ev > min_ev):
            continue
        # An executable result always carries these (set together in _pack_result); assert so
        # the type checker (and a future refactor) sees the invariant the filter guarantees.
        assert (
            r.side is not None and r.calibrated_prob is not None and r.market.market_prob is not None
            and r.price_paid is not None and r.fill_shares is not None
            and r.target_position_usd is not None
        )
        key = (r.market.id, r.side)
        if key in open_keys:
            continue
        db.save_signal(Signal(
            market_id=r.market.id,
            exchange=r.market.exchange,
            question=r.market.question,
            model=r.analysis.model,
            side=r.side,
            calibrated_prob=r.calibrated_prob,
            market_prob=r.market.market_prob,
            price_paid=r.price_paid,
            ev=r.ev,
            ev_pct=r.ev_pct,
            kelly=r.kelly,
            annualized_ev=r.annualized_ev,
            fill_shares=r.fill_shares,
            target_position_usd=r.target_position_usd,
            days_to_close=r.days_to_close,
            adversarial_verdict=r.refutation.verdict if r.refutation else None,
            refuter_model=r.refutation.refuter_model if r.refutation else None,
        ))
        open_keys.add(key)  # so a repeat (market, side) in the same batch isn't double-logged
        saved += 1
    return saved


# --- high-divergence alerts ----------------------------------------------------


def _alerts_path() -> Path:
    override = os.getenv("ALERT_LOG_PATH")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "data" / "alerts.jsonl"


def _post_webhook(url: str, payload: dict) -> None:
    """Best-effort POST of an alert via stdlib urllib (httpx is confined to polymarket.py).

    Fire-and-forget: short timeout, all errors swallowed, so a flaky webhook never
    delays or breaks a scan.
    """
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=5).close()
    except Exception as e:  # noqa: BLE001 — best-effort; never raise
        _log.debug("webhook POST failed: %s", e)


def emit_alerts(results: list[ScanResult]) -> int:
    """Write a structured alert per actionable, high-divergence edge; return the count.

    Alerts on edges whose calibrated estimate diverges from the market mid by at least
    ``ALERT_MIN_DIVERGENCE`` (env, default 0.15) and that are actionable (``executable``
    with ``ev > 0``). Dedup: skip a market alerted within ``ALERT_COOLDOWN_HOURS`` (env,
    default 24) — read from the existing ``alerts.jsonl``. Each new alert appends one JSON
    line, logs a warning, and — if ``ALERT_WEBHOOK_URL`` is set — fires a best-effort POST.
    """
    min_div = float(os.getenv("ALERT_MIN_DIVERGENCE", "0.15"))
    cooldown_h = float(os.getenv("ALERT_COOLDOWN_HOURS", "24"))
    webhook = os.getenv("ALERT_WEBHOOK_URL")
    path = _alerts_path()

    # Cooldown index: most recent alert time per market (whole-file read, like read_alerts).
    recent: dict[str, datetime] = {}
    for rec in read_alerts(limit=10_000):
        ts, mid = rec.get("timestamp"), rec.get("market_id")
        if not ts or not mid:
            continue
        try:
            t = datetime.fromisoformat(ts)
        except ValueError:
            continue
        if mid not in recent or t > recent[mid]:
            recent[mid] = t

    now = datetime.now(timezone.utc)
    new_records: list[dict] = []
    for r in results:
        mp = r.market.market_prob
        if mp is None or r.calibrated_prob is None:
            continue
        divergence = r.calibrated_prob - mp
        if abs(divergence) < min_div:
            continue
        if not (r.executable and r.ev is not None and r.ev > 0):
            continue
        last = recent.get(r.market.id)
        if last is not None and (now - last).total_seconds() < cooldown_h * 3600.0:
            continue
        rec = {
            "timestamp": now.isoformat(),
            "market_id": r.market.id,
            "question": r.market.question,
            "slug": r.market.slug,
            "side": r.side,
            "calibrated_prob": r.calibrated_prob,
            "market_prob": mp,
            "divergence": divergence,
            "ev": r.ev,
            "annualized_ev": r.annualized_ev,
            "price_paid": r.price_paid,
            "trade_url": f"https://polymarket.com/event/{r.market.slug}",
        }
        new_records.append(rec)
        recent[r.market.id] = now  # dedup within this batch too
        _log.warning(
            "high-divergence edge: %s %s div=%+.2f ev=%.3f (%s)",
            r.side, r.market.question, divergence, r.ev, rec["trade_url"],
        )
        if webhook:
            _post_webhook(webhook, rec)

    if new_records:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                for rec in new_records:
                    f.write(json.dumps(rec) + "\n")
        except OSError as e:
            _log.warning("alerts.jsonl write failed: %s", e)
    return len(new_records)


def read_alerts(limit: int = 50) -> list[dict]:
    """Newest-first tail of alerts.jsonl (skip blank/malformed lines)."""
    path = _alerts_path()
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    out.reverse()
    return out[:limit]
