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

    ``slug``, ``volume_total``, and ``liquidity`` extend the base research model
    to support acting on an edge: ``slug`` builds the polymarket.com trade URL,
    and ``volume_total``/``liquidity`` indicate whether a meaningful bet can
    actually be filled.
    """

    id: str
    slug: str
    question: str
    market_prob: float | None  # YES probability 0-1; None if unavailable/malformed
    volume_24h: float
    volume_total: float | None = None
    liquidity: float | None = None
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
    """One market and the analysis produced for it during a scan."""

    market: Market
    analysis: Analysis


class ScanRequest(BaseModel):
    """Parameters controlling a batch divergence scan."""

    min_volume_24h: float = 10_000
    max_age_hours: float = 24.0
    min_divergence: float = 0.05
    category: str | None = None
    max_markets: int = 100
