"""Pydantic data models for the Polymarket Claude Research Copilot.

All probabilities are stored as floats in the 0-1 range; the frontend renders
them as percentages. Market IDs are always strings. See ARCHITECTURE.md for the
SQLite schema these models map onto.
"""

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
    claude_prob: float | None = None  # 0-1
    confidence: Confidence | None = None
    edge: Edge | None = None
    edge_magnitude: float | None = None  # abs(claude_prob - market_prob)
    factors: list[str] = Field(default_factory=list)
    summary: str = ""
    resolved: bool | None = None
    resolution: bool | None = None  # True=YES won, False=NO won
    error: str | None = None


class MarketWithAnalysis(BaseModel):
    """A market paired with its most recent analysis (if any)."""

    market: Market
    latest_analysis: Analysis | None
    analysis_count: int


class ScanResult(BaseModel):
    """A market, its analysis, and the (uncalibrated) EV figures derived from them.

    EV uses the market mid price and Claude's *uncalibrated* estimate, so it is
    directional only until Phase 3 (calibration) and Phase 3.5 (executable
    bid/ask) land. ``side`` is the favorable side to bet (YES if Claude is higher
    than the market, NO if lower).
    """

    market: Market
    analysis: Analysis
    side: Literal["YES", "NO"] | None = None
    ev: float | None = None  # per-share edge on the favorable side = |claude - market|
    ev_pct: float | None = None  # ev / price_paid
    kelly: float | None = None  # full Kelly fraction = ev / (1 - price_paid); size with a fraction
    annualized_ev: float | None = None  # ev_pct * 365 / days_to_close (None below the days floor)
    days_to_close: float | None = None


class ScanRequest(BaseModel):
    """Parameters controlling a batch EV scan."""

    min_volume_24h: float = 10_000
    max_age_hours: float = 24.0
    min_divergence: float = 0.05
    category: str | None = None
    max_markets: int = 100
    min_liquidity: float = 0.0
    min_days_to_close: float = 7.0  # below this, annualized EV is noise — exclude
