"""Pydantic data models for the Polymarket Claude Research Copilot.

All probabilities are stored as floats in the 0-1 range; the frontend renders
them as percentages. Market IDs are always strings. See ARCHITECTURE.md for the
SQLite schema these models map onto.
"""

import os
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

# Probability/edge labels Claude returns. Typed as Literals so Pydantic rejects
# malformed analyzer output at parse time rather than letting it reach the DB.
Confidence = Literal["low", "medium", "high"]
Edge = Literal["underpriced", "overpriced", "fair"]


def _utcnow() -> datetime:
    """Timezone-aware UTC now.

    Used instead of the deprecated ``datetime.utcnow()``. Market ``end_date``
    values are parsed timezone-aware from the Gamma API, so our own timestamps
    must be aware too — otherwise time-to-close comparisons raise
    "can't compare offset-naive and offset-aware datetimes".
    """
    return datetime.now(timezone.utc)


class Market(BaseModel):
    """A normalized Polymarket market.

    ``slug``, ``volume_total``, ``liquidity``, and ``yes_token_id`` extend the
    base research model to support acting on an edge: ``slug`` builds the
    polymarket.com trade URL, ``volume_total``/``liquidity`` indicate whether a
    meaningful bet can actually be filled, and ``yes_token_id`` is the CLOB token
    for the YES outcome (enables future live price-history / order-book depth).
    """

    id: str
    exchange: Literal["polymarket", "kalshi"] = "polymarket"  # which exchange this market came from
    slug: str
    question: str
    market_prob: float | None  # YES probability 0-1; None if unavailable/malformed
    volume_24h: float
    volume_total: float | None = None
    liquidity: float | None = None
    yes_token_id: str | None = None  # clobTokenIds[0]; for future CLOB price-history / depth
    end_date: datetime | None
    tags: list[str]
    description: str
    fetched_at: datetime = Field(default_factory=_utcnow)


class Analysis(BaseModel):
    """A single Claude analysis of a market.

    The Claude-derived fields are nullable so a *failed* analysis is
    representable: on error the analyzer returns an ``Analysis`` carrying only
    ``market_id`` and ``error``, never inventing zeros for the missing estimate.
    """

    id: int | None = None
    market_id: str
    created_at: datetime = Field(default_factory=_utcnow)
    model: str | None = None  # which LLM produced this estimate (for per-model calibration)
    claude_prob: float | None = None  # 0-1 (the model's estimate; name kept per CALIBRATION_NOTES)
    market_prob_at_analysis: float | None = None  # market YES mid when this analysis ran (staleness)
    confidence: Confidence | None = None
    edge: Edge | None = None
    edge_magnitude: float | None = None  # abs(claude_prob - market_prob)
    factors: list[str] = Field(default_factory=list)
    summary: str = ""
    resolved: bool | None = None
    resolution: bool | None = None  # True=YES won, False=NO won
    error: str | None = None
    input_tokens: int | None = None  # LLM tokens consumed producing this analysis (cost accounting)
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None  # Anthropic prompt-cache write/read (0 if no cache)
    cache_read_input_tokens: int | None = None
    web_search_requests: int | None = None  # server-side web_search calls (billed per-search, not in tokens)


class MarketWithAnalysis(BaseModel):
    """A market paired with its most recent analysis (if any)."""

    market: Market
    latest_analysis: Analysis | None
    analysis_count: int
    stale: bool = False  # current market_prob moved > STALE_THRESHOLD since the latest analysis


class Refutation(BaseModel):
    """A skeptical second-pass review of an edge (see analyzer.refute_edge).

    The refuter argues the market is right; ``verdict`` (holds/refuted) is derived by
    the scanner from ``refuter_prob`` vs the market price, not self-reported.
    """

    refuter_prob: float | None = None  # the skeptic's own YES estimate (0-1), uncalibrated
    refuter_model: str | None = None  # which model ran the refutation (may differ when cross-model)
    verdict: Literal["holds", "refuted"] | None = None
    resolution_risk: bool = False
    counterpoints: list[str] = Field(default_factory=list)
    summary: str = ""
    error: str | None = None
    input_tokens: int | None = None  # LLM tokens consumed by this refutation (cost accounting)
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None  # Anthropic prompt-cache write/read (0 if no cache)
    cache_read_input_tokens: int | None = None
    web_search_requests: int | None = None  # server-side web_search calls (billed per-search, not in tokens)


class ScanResult(BaseModel):
    """A market, its analysis, and the (uncalibrated) EV figures derived from them.

    ``side`` is the favorable side to bet (YES if Claude is higher than the market, NO
    if lower). When the live book is available, ``price_paid`` is the **volume-weighted
    average fill** to deploy ``target_position_usd`` on that side (depth-aware), not the
    top-of-book price — so a thin book yields a worse, truer cost; ``best_bid``/``best_ask``
    remain the top-of-book reference. Falls back to the mid price (``executable=False``)
    when no two-sided book is available.
    """

    market: Market
    analysis: Analysis
    calibrated_prob: float | None = None  # claude_prob after recalibration (raw if uncalibrated)
    side: Literal["YES", "NO"] | None = None
    ev: float | None = None  # executable per-share edge on the favorable side
    ev_pct: float | None = None  # ev / price_paid
    kelly: float | None = None  # full Kelly fraction = ev / (1 - price_paid); size with a fraction
    annualized_ev: float | None = None  # ev_pct * 365 / days_to_close (None below the days floor)
    days_to_close: float | None = None
    best_bid: float | None = None  # CLOB best bid for the YES token (top of book)
    bid_depth: float | None = None  # shares resting at best_bid
    best_ask: float | None = None  # CLOB best ask for the YES token (top of book)
    ask_depth: float | None = None  # shares resting at best_ask
    price_paid: float | None = None  # VWAP fill cost/share on the chosen side for target_position_usd (else mid)
    fill_shares: float | None = None  # shares the VWAP walk filled toward the target
    fully_filled: bool = False  # False if the book was too thin to reach target_position_usd
    target_position_usd: float | None = None  # USD position the VWAP priced (None on mid fallback)
    executable: bool = False  # True if priced off the live book; False = mid-price fallback
    refutation: Refutation | None = None  # skeptical second pass (top edges only; None otherwise)


class Signal(BaseModel):
    """A forward, lookahead-free record of an actionable edge the scanner surfaced.

    Persisted at scan time from a ``ScanResult`` (executable, EV past the floor) so the
    tool's own calls can be scored once the market resolves — the real calibration
    flywheel, distinct from the crowd backtest. Prices/EV are frozen at log time and
    never updated; ``resolved``/``resolution``/``pnl`` are filled in by the resolution
    sweep. ``pnl`` is computed from the modeled VWAP fill (``fill_shares`` * payoff),
    not a notional.
    """

    id: int | None = None
    market_id: str
    exchange: Literal["polymarket", "kalshi"] = "polymarket"  # exchange the signal's market is on
    question: str
    created_at: datetime = Field(default_factory=_utcnow)
    model: str | None = None  # the LLM whose estimate drove this signal (per-model attribution)
    side: Literal["YES", "NO"]
    calibrated_prob: float  # our estimate on the chosen side at log time
    market_prob: float  # market YES mid at log time
    price_paid: float  # VWAP fill cost/share on the chosen side
    ev: float | None = None  # executable per-share edge at log time
    ev_pct: float | None = None
    kelly: float | None = None
    annualized_ev: float | None = None
    fill_shares: float  # shares the VWAP walk filled toward the target
    target_position_usd: float  # USD position the VWAP priced
    days_to_close: float | None = None
    adversarial_verdict: Literal["holds", "refuted"] | None = None  # from refutation, None if not run
    refuter_model: str | None = None  # which model ran the refutation (None if not run)
    resolved: bool | None = None
    resolution: bool | None = None  # True=YES won, False=NO won
    pnl: float | None = None  # realized $ on resolution; None while open
    # Manual execution: the real bet you placed (None until recorded via /api/signals/<id>/fill).
    # When set, the resolution sweep realizes P&L from these instead of the modeled VWAP fill —
    # so the track record reflects actual fills, not the $50 model.
    actual_stake_usd: float | None = None
    actual_price: float | None = None  # your avg fill price/share on the chosen side
    actual_shares: float | None = None  # contracts you actually got filled


class CalibrationBin(BaseModel):
    """One reliability bin: predicted mean vs empirical resolve-rate."""

    bin_lo: float
    bin_hi: float
    predicted_mean: float | None
    empirical_rate: float | None
    count: int


class CalibrationReport(BaseModel):
    """Calibration status + metrics for one model (/api/calibration returns a list)."""

    model: str
    n: int
    calibrated: bool
    temperature: float
    min_n: int
    brier: float
    log_loss: float
    curve: list[CalibrationBin]


class ScanRequest(BaseModel):
    """Parameters controlling a batch EV scan.

    Bounds reject pathological requests (e.g. ``max_markets=10**6``, negative gates) with a
    422/400 instead of exhausting memory or LLM budget. Defaults are unchanged.
    """

    # The gate defaults read from env so manual scans (/api/scan) and the scheduled auto-scan
    # apply the SAME gates without the caller having to repeat them; an explicit value in the
    # request body still overrides. (Mirrors how max_llm_calls defaults from MAX_LLM_CALLS_PER_SCAN.)
    min_volume_24h: float = Field(
        default_factory=lambda: float(os.getenv("SCAN_MIN_VOLUME_24H", "10000")), ge=0
    )
    max_age_hours: float = Field(default=24.0, ge=0)
    min_divergence: float = Field(default=0.05, ge=0, le=1)
    category: str | None = None
    max_markets: int = Field(default=100, ge=1, le=1000)
    min_liquidity: float = Field(default=0.0, ge=0)
    min_days_to_close: float = Field(  # below this, annualized EV is noise
        default_factory=lambda: float(os.getenv("SCAN_MIN_DAYS_TO_CLOSE", "7")), ge=0
    )
    # Upper bound on days-to-close (0 = no cap). Biases a scan toward near-dated markets so
    # resolved (estimate, outcome) pairs accrue quickly — useful while building calibration.
    max_days_to_close: float = Field(
        default_factory=lambda: float(os.getenv("SCAN_MAX_DAYS_TO_CLOSE", "0")), ge=0
    )
    # Refute top-N ranked edges (0 = off). Defaults from REFUTE_TOP so manual and scheduled scans
    # pressure-test consistently; an explicit request value overrides.
    refute_top: int = Field(default_factory=lambda: int(os.getenv("REFUTE_TOP", "0")), ge=0, le=50)
    # Hard ceiling on fresh LLM calls (market analyses + refutations) for ONE scan, so a single
    # scan can't burn through the API budget. Reused/cached analyses are free and don't count;
    # 0 = no cap. Defaults from MAX_LLM_CALLS_PER_SCAN so scheduled scans honor it too; an
    # explicit value in the request overrides the env default.
    max_llm_calls: int = Field(
        default_factory=lambda: int(os.getenv("MAX_LLM_CALLS_PER_SCAN", "0")), ge=0, le=1000
    )
