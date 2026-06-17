"""Kalshi trade-API client + market normalization.

The Kalshi counterpart to ``polymarket.py``: per CLAUDE.md module boundaries this
is the ONLY module (besides ``polymarket.py``) that imports ``httpx`` or talks to
an exchange API. It fetches *active* (tradeable) binary markets and normalizes them
into ``research.models.Market`` with ``exchange="kalshi"``.

The shape deliberately mirrors ``polymarket.py`` (sync ``httpx`` client → raw
Pydantic model with gotcha-safe validators → binary filter → ``fetch_markets`` /
``fetch_all_active``). The structural differences from Polymarket:

- **No auth for reads.** Kalshi's market-data endpoints are public; authentication
  (RSA-PSS request signing) is only required for trading/portfolio, which this
  read-only tool never calls. Optional signing scaffolding is included so a future
  trading use — or higher rate limits — can opt in via ``KALSHI_API_KEY`` +
  ``KALSHI_KEY_FILE`` (needs the ``cryptography`` package).
- **Prices are integer cents** (1–99), e.g. ``last_price=65`` → ``0.65`` — not the
  JSON-string decimals Polymarket returns, so no JSON-string array parsing.
- **Natively binary** — one YES/NO contract per market, so eligibility is a simple
  status/price check, not Polymarket's outcomes/negRisk handling.
- **No CLOB token id** — ``yes_token_id`` is always ``None`` for Kalshi, so the
  scanner's order-book/VWAP step is skipped and Kalshi edges price at the mid
  (``executable=False``). A by-ticker order-book fetch is left for future work.
- **Cursor pagination** (a ``cursor`` token per page), not offset-based.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import time
from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, field_validator

from research.models import Market

_log = logging.getLogger(__name__)

DEFAULT_API_HOST = "https://api.elections.kalshi.com"
API_PREFIX = "/trade-api/v2"

# Kalshi sometimes emits bare timezone offsets (e.g. `+00` instead of `+00:00`),
# which datetime.fromisoformat rejects — same gotcha handled in polymarket.py.
_BARE_OFFSET_RE = re.compile(r"[+-]\d{2}$")

# Kalshi market statuses that mean "tradeable now".
_ACTIVE_STATUSES = frozenset({"active", "open"})


def _sign_headers(key_id: str, key_path: str, method: str, path: str) -> dict[str, str]:
    """RSA-PSS signed auth headers for an authenticated Kalshi request.

    Only used when both ``KALSHI_API_KEY`` and ``KALSHI_KEY_FILE`` are set; public
    market-data reads never call this. Signs ``timestamp_ms + METHOD + path`` with the
    PEM private key at ``key_path`` (Kalshi's scheme). Raises a clear error if the
    optional ``cryptography`` dependency is missing.
    """
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError as e:  # pragma: no cover - only hit when auth is opted into without the dep
        raise RuntimeError(
            "Kalshi auth (KALSHI_API_KEY/KALSHI_KEY_FILE) requires the 'cryptography' "
            "package. Install it (it's in requirements.txt) or unset the keys to use the "
            "public read-only API."
        ) from e

    timestamp_ms = str(int(time.time() * 1000))
    message = (timestamp_ms + method.upper() + path).encode("utf-8")
    with open(key_path, "rb") as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None)
    signature = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
    }


class KalshiClient:
    """Synchronous Kalshi client with simple per-request rate limiting.

    No asyncio: a ``time.sleep`` before each request keeps us well under Kalshi's
    rate guidance. Use as a context manager. Reads are public; if ``KALSHI_API_KEY``
    and ``KALSHI_KEY_FILE`` are both set, requests are RSA-PSS signed (opt-in).
    """

    def __init__(
        self,
        api_host: str | None = None,
        request_delay_s: float = 0.2,
        timeout_s: float = 30.0,
    ) -> None:
        host = api_host or os.getenv("KALSHI_API_BASE", DEFAULT_API_HOST)
        self._client = httpx.Client(base_url=host + API_PREFIX, timeout=timeout_s)
        self._request_delay_s = request_delay_s
        self._key_id = os.getenv("KALSHI_API_KEY") or None
        self._key_path = os.getenv("KALSHI_KEY_FILE") or None

    def get(self, path: str, **params: Any) -> Any:
        time.sleep(self._request_delay_s)
        headers: dict[str, str] = {}
        if self._key_id and self._key_path:  # opt-in signing; public reads skip this
            headers = _sign_headers(self._key_id, self._key_path, "GET", API_PREFIX + path)
        r = self._client.get(path, params=params, headers=headers)
        r.raise_for_status()
        return r.json()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "KalshiClient":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


class KalshiMarket(BaseModel):
    """Subset of Kalshi /markets fields we care about for an *active* binary market.

    The live ``api.elections.kalshi.com`` schema reports prices as **decimal dollars**
    in the ``*_dollars`` fields (e.g. ``last_price_dollars=0.65`` ⇒ a 65% YES), already
    in 0–1 — so unlike the older cents-based docs, no ``/100`` is needed. Volume/liquidity
    come as fixed-point decimals (``*_fp`` / ``*_dollars``). ``close_time`` is the
    scheduled close (the Polymarket ``endDate`` analogue); ``expiration_time`` is
    settlement and ignored.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    ticker: str
    event_ticker: str = ""
    market_type: str = ""
    title: str = ""
    subtitle: str = ""
    yes_sub_title: str = ""
    status: str = ""
    yes_bid_dollars: float | None = None  # decimal dollars 0–1
    yes_ask_dollars: float | None = None  # decimal dollars 0–1
    last_price_dollars: float | None = None  # decimal dollars 0–1
    volume_fp: float | None = None  # lifetime volume (fixed-point decimal)
    volume_24h_fp: float | None = None  # 24h volume (fixed-point decimal)
    liquidity_dollars: float | None = None  # dollars; not directly comparable to Polymarket liquidity
    category: str = ""
    close_time: datetime | None = None

    # Pad bare timezone offsets (`+00` -> `+00:00`) and return None for garbage so a
    # single bad date filters the market out downstream rather than crashing the fetch.
    @field_validator("close_time", mode="before")
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


def _market_prob(km: KalshiMarket) -> float | None:
    """YES probability 0–1 (already in dollars): last trade if present, else bid/ask mid.

    Kalshi uses ``0``/``1`` as no-bid / no-ask sentinels (e.g. ``ask=1.0`` when nobody is
    offering), so the bid/ask mid is only used when both sides sit strictly inside (0, 1).
    """
    lp = km.last_price_dollars
    if lp is not None and 0 < lp < 1:
        return lp
    bid, ask = km.yes_bid_dollars, km.yes_ask_dollars
    if bid is not None and ask is not None and 0 < bid < 1 and 0 < ask < 1:
        return (bid + ask) / 2.0
    if bid is not None and 0 < bid < 1:
        return bid
    return None


def _is_eligible_binary(km: KalshiMarket) -> bool:
    """Keep tradeable binary markets with a usable live price.

    Kalshi markets are natively YES/NO; we only exclude non-binary (scalar) types,
    non-tradeable statuses, and markets with no parseable price.
    """
    if km.status.lower() not in _ACTIVE_STATUSES:
        return False
    if km.market_type and km.market_type.lower() != "binary":
        return False
    return _market_prob(km) is not None


def normalize_market(raw: dict) -> Market | None:
    """Validate + map a raw Kalshi row to our ``Market``; None if not eligible.

    The Polymarket counterpart records ``float(outcomePrices[0])``; here the YES price
    comes from cents (last trade or bid/ask mid). ``yes_token_id`` is always None — Kalshi
    has no CLOB token — so the scanner prices Kalshi edges at the mid.
    """
    km = KalshiMarket.model_validate(raw)
    if not _is_eligible_binary(km):
        return None

    return Market(
        id=km.ticker,
        exchange="kalshi",
        slug=km.ticker,  # NB: Kalshi trade URLs differ from Polymarket's /event/{slug}
        question=km.title or km.ticker,
        market_prob=_market_prob(km),
        volume_24h=km.volume_24h_fp or 0.0,
        volume_total=km.volume_fp,
        liquidity=km.liquidity_dollars,
        yes_token_id=None,  # no CLOB token on Kalshi
        end_date=km.close_time,
        tags=[km.category] if km.category else [],
        description=km.subtitle or km.yes_sub_title,
    )


def _fetch_page(
    client: KalshiClient, limit: int, cursor: str | None, status: str
) -> tuple[list[dict], str]:
    """Fetch one raw page of markets; return ``(raw_markets, next_cursor)``.

    ``next_cursor`` is the empty string on the last page (Kalshi's end-of-pages signal).
    """
    params: dict[str, Any] = {"limit": limit, "status": status}
    if cursor:
        params["cursor"] = cursor
    body = client.get("/markets", **params) or {}
    return body.get("markets") or [], body.get("cursor") or ""


def fetch_markets(
    limit: int = 100, cursor: str | None = None, status: str = "open"
) -> list[Market]:
    """Fetch a single page of active, eligible-binary Kalshi markets."""
    with KalshiClient() as client:
        raw_markets, _ = _fetch_page(client, limit=limit, cursor=cursor, status=status)
    return [m for m in (normalize_market(raw) for raw in raw_markets) if m is not None]


def fetch_all_active(
    max_markets: int = 500, min_volume: float = 1000.0, max_pages: int = 20
) -> list[Market]:
    """Paginate active markets, keeping only those with real trading activity.

    Cursor-paged (unlike Polymarket's offset paging): each page returns the cursor for
    the next, and an empty cursor marks the end.

    Kalshi's ``/markets`` has no server-side volume sort, so its early pages are
    dominated by auto-generated, zero-activity parlay markets. We therefore keep only
    markets whose **lifetime** volume (``volume_total``) clears ``min_volume`` and keep
    paging past the dead ones. (24h volume isn't usable: GetMarkets reports it as 0 for
    every market, so lifetime volume is the only populated liquidity signal.)

    Bounded three ways — ``max_markets`` eligible found, cursor exhausted, or ``max_pages``
    fetched — so a scan stays responsive even when liquid markets are sparse. Hitting the
    page cap is logged (never a silent truncation).
    """
    limit = 1000  # Kalshi allows up to 1000/page
    markets: list[Market] = []
    cursor: str | None = None
    pages = 0
    raw_seen = 0
    with KalshiClient() as client:
        while len(markets) < max_markets and pages < max_pages:
            raw_markets, cursor = _fetch_page(client, limit=limit, cursor=cursor, status="open")
            pages += 1
            raw_seen += len(raw_markets)
            if not raw_markets:
                break
            markets.extend(
                m
                for m in (normalize_market(raw) for raw in raw_markets)
                if m is not None and (m.volume_total or 0.0) >= min_volume
            )
            if not cursor:  # last page
                break
        else:
            if pages >= max_pages and len(markets) < max_markets:
                _log.warning(
                    "kalshi fetch hit max_pages=%d (scanned %d markets across %d pages, "
                    "kept %d with volume_total >= %s) before reaching max_markets=%d",
                    max_pages, raw_seen, pages, len(markets), min_volume, max_markets,
                )
    return markets[:max_markets]
