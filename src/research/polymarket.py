"""Polymarket Gamma API client + market normalization.

Per CLAUDE.md module boundaries, this is the ONLY module that imports ``httpx``
or talks to the Polymarket API. It fetches *active* markets and normalizes them
into ``research.models.Market``.

The client and the two field validators (JSON-string parsing, bare-offset date
repair) are adapted from the calibration tracker's ``polymarket/`` package. The
key inversion vs. that tool: it fetches *resolved* markets and records a ``won``
boolean; here we fetch *active* markets and record the live YES price
(``market_prob = float(outcomePrices[0])``).
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from research.models import Market

DEFAULT_BASE_URL = "https://gamma-api.polymarket.com"

# Polymarket sometimes emits bare timezone offsets (e.g. `+00` instead of
# `+00:00`), which datetime.fromisoformat rejects.
_BARE_OFFSET_RE = re.compile(r"[+-]\d{2}$")


class GammaClient:
    """Synchronous Gamma API client with simple per-request rate limiting.

    No asyncio: a ``time.sleep`` before each request keeps us well under
    Polymarket's ~50 req/min guidance. Use as a context manager.
    """

    def __init__(
        self,
        base_url: str | None = None,
        request_delay_s: float = 0.2,
        timeout_s: float = 30.0,
    ) -> None:
        base_url = base_url or os.getenv("POLYMARKET_API_BASE", DEFAULT_BASE_URL)
        self._client = httpx.Client(base_url=base_url, timeout=timeout_s)
        self._request_delay_s = request_delay_s

    def get(self, path: str, **params: Any) -> Any:
        time.sleep(self._request_delay_s)
        r = self._client.get(path, params=params)
        r.raise_for_status()
        return r.json()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GammaClient":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


class _GammaTag(BaseModel):
    """Inline tag on a /markets row. We only need the human-readable label."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")
    label: str = ""
    slug: str = ""


class GammaMarket(BaseModel):
    """Subset of Gamma /markets fields we care about for an *active* market.

    Adapted from the calibration tracker's GammaMarket: same JSON-string and
    bare-offset gotcha handling, but the field set targets live markets and
    ``end_date`` maps to ``endDate`` (scheduled close), not ``umaEndDate``
    (resolution time, which is null until a market resolves).
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str
    slug: str = ""
    question: str = ""
    outcomes: list[str] = Field(default_factory=list)
    outcome_prices: list[str] = Field(default_factory=list, alias="outcomePrices")
    clob_token_ids: list[str] = Field(default_factory=list, alias="clobTokenIds")
    neg_risk: bool = Field(default=False, alias="negRisk")
    closed: bool = False
    volume_24h: float | None = Field(default=None, alias="volume24hr")
    volume_num: float | None = Field(default=None, alias="volumeNum")
    liquidity: float | None = None
    description: str = ""
    end_date: datetime | None = Field(default=None, alias="endDate")
    tags: list[_GammaTag] = Field(default_factory=list)

    # outcomes / outcomePrices / clobTokenIds come back as JSON-encoded strings,
    # not arrays. Known Polymarket gotcha.
    @field_validator("outcomes", "outcome_prices", "clob_token_ids", mode="before")
    @classmethod
    def _parse_json_string(cls, v: object) -> object:
        if isinstance(v, str):
            return json.loads(v)
        return v

    # Pad bare timezone offsets (`+00` -> `+00:00`) and return None for garbage
    # values so a single bad date filters the market out downstream rather than
    # crashing the whole fetch.
    @field_validator("end_date", mode="before")
    @classmethod
    def _normalize_dt(cls, v: object) -> object:
        if not isinstance(v, str):
            return v
        candidate = v + ":00" if _BARE_OFFSET_RE.search(v) else v
        try:
            datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            return None
        return candidate


def _is_eligible_binary(gm: GammaMarket) -> bool:
    """Keep standalone, 2-outcome active markets with a parseable live price.

    Adapted from the calibration tracker's ``_is_eligible_binary`` with the
    resolved-state check (``sorted(prices) == ["0", "1"]``) removed — that marks
    a *finished* market, the opposite of what we want.
    """
    if gm.neg_risk:
        return False
    if len(gm.outcomes) != 2 or len(gm.clob_token_ids) != 2:
        return False
    if len(gm.outcome_prices) != 2:
        return False
    try:
        float(gm.outcome_prices[0])
    except (ValueError, TypeError):
        return False
    return True


def normalize_market(raw: dict) -> Market | None:
    """Validate + map a raw Gamma row to our ``Market``; None if not eligible.

    Adapted from the calibration tracker's ``_to_market``: instead of computing a
    ``won`` boolean, we record the live YES price ``float(outcomePrices[0])``.
    """
    gm = GammaMarket.model_validate(raw)
    if not _is_eligible_binary(gm):
        return None

    try:
        market_prob: float | None = float(gm.outcome_prices[0])
    except (ValueError, TypeError, IndexError):
        # Guarded though _is_eligible_binary already proved this parses;
        # belt-and-suspenders per ARCHITECTURE (malformed prices -> None).
        market_prob = None

    return Market(
        id=gm.id,
        slug=gm.slug,
        question=gm.question,
        market_prob=market_prob,
        volume_24h=gm.volume_24h or 0.0,
        volume_total=gm.volume_num,
        liquidity=gm.liquidity,
        yes_token_id=gm.clob_token_ids[0] if gm.clob_token_ids else None,
        end_date=gm.end_date,
        tags=[t.label for t in gm.tags if t.label],
        description=gm.description,
    )


def _fetch_page(
    client: GammaClient, limit: int, offset: int, tag: str | None
) -> list[dict]:
    """Fetch one raw page of active markets, highest volume first."""
    params: dict[str, Any] = {
        "active": "true",
        "closed": "false",
        "order": "volumeNum",
        "ascending": "false",
        "limit": limit,
        "offset": offset,
    }
    if tag:
        params["tag_slug"] = tag
    page = client.get("/markets", **params)
    return page or []


def fetch_markets(
    limit: int = 50, offset: int = 0, tag: str | None = None
) -> list[Market]:
    """Fetch a single page of active, eligible-binary markets."""
    with GammaClient() as client:
        raw_page = _fetch_page(client, limit=limit, offset=offset, tag=tag)
    return [m for m in (normalize_market(raw) for raw in raw_page) if m is not None]


def _detect_resolution(raw: dict) -> bool | None:
    """YES/NO winner for a *resolved* market, else None (see CALIBRATION_NOTES.md).

    A market is resolved when ``closed`` is true and one outcome's price has gone
    to ~1. Returns True if YES won, False if NO won, None if unresolved/disputed.
    """
    if not raw.get("closed"):
        return None
    try:
        prices = json.loads(raw.get("outcomePrices") or "[]")
        outcomes = json.loads(raw.get("outcomes") or "[]")
    except (json.JSONDecodeError, TypeError):
        return None
    for i, price in enumerate(prices):
        try:
            if float(price) >= 0.99:
                label = outcomes[i] if i < len(outcomes) else ""
                return str(label).lower() in ("yes", "1", "true")
        except (ValueError, TypeError):
            continue
    return None


def fetch_resolution(market_id: str) -> bool | None:
    """Look up a market by id (including closed) and return its YES/NO resolution.

    None if unresolved, disputed, or not found — the caller retries on the next
    refresh. Matches by id in the returned page so a server-side ignored filter
    degrades to a safe no-op rather than a wrong answer.
    """
    with GammaClient() as client:
        page = client.get("/markets", id=market_id)
    raws = page if isinstance(page, list) else [page] if page else []
    raw = next((m for m in raws if str(m.get("id")) == str(market_id)), None)
    if raw is None:
        return None
    return _detect_resolution(raw)


def fetch_all_active(max_markets: int = 500) -> list[Market]:
    """Paginate active markets until ``max_markets`` eligible ones or the last page.

    Pagination advances by the *raw* page size, not the filtered count — binary
    filtering shrinks each page, so paging by kept-count would skip markets.
    """
    limit = 50
    markets: list[Market] = []
    offset = 0
    with GammaClient() as client:
        while len(markets) < max_markets:
            raw_page = _fetch_page(client, limit=limit, offset=offset, tag=None)
            if not raw_page:
                break
            markets.extend(
                m for m in (normalize_market(raw) for raw in raw_page) if m is not None
            )
            offset += len(raw_page)
            if len(raw_page) < limit:  # last page
                break
    return markets[:max_markets]
