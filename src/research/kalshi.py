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
- **Decimal-dollar prices.** The live ``api.elections.kalshi.com`` schema reports
  prices as decimal dollars in the ``*_dollars`` fields (already 0–1), so no
  JSON-string array parsing and no cents scaling for the market list. (The *order
  book* endpoint still returns integer cents — scaled in ``_orderbook_side``.)
- **Natively binary** — one YES/NO contract per market, so eligibility is a status /
  two-sided-quote / price check, not Polymarket's outcomes/negRisk handling.
- **No CLOB token id** — ``yes_token_id`` is always ``None``; the order book is fetched
  by ticker (``fetch_book`` → ``/markets/{ticker}/orderbook``) instead, so Kalshi edges
  price off the live book and VWAP fill just like Polymarket.
- **Discovery via /events, not /markets.** The raw ``/markets`` listing is ~99%
  auto-generated MVE parlay markets; ``fetch_all_active`` discovers through ``/events``
  (with nested markets) and ``_is_eligible_binary`` drops MVE/one-sided stragglers.
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
from research.polymarket import Book, retry_http  # reuse YES-centric Book + shared HTTP retry

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
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
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
    # Kalshi's scheme uses an RSA key with PSS padding; narrow the broad loader return type.
    assert isinstance(private_key, rsa.RSAPrivateKey), "Kalshi signing key must be RSA"
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
        host = api_host or os.getenv("KALSHI_API_BASE") or DEFAULT_API_HOST
        self._client = httpx.Client(base_url=host + API_PREFIX, timeout=timeout_s)
        self._request_delay_s = request_delay_s
        self._key_id = os.getenv("KALSHI_API_KEY") or None
        self._key_path = os.getenv("KALSHI_KEY_FILE") or None

    def get(self, path: str, **params: Any) -> Any:
        time.sleep(self._request_delay_s)
        headers: dict[str, str] = {}
        if self._key_id and self._key_path:  # opt-in signing; public reads skip this
            headers = _sign_headers(self._key_id, self._key_path, "GET", API_PREFIX + path)

        def do() -> Any:
            r = self._client.get(path, params=params, headers=headers)
            r.raise_for_status()
            return r.json()

        return retry_http(do)

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
    # MVE = "multivariate event": Kalshi auto-generates huge numbers of provisional
    # multi-leg parlay markets (e.g. KXMVESPORTSMULTIGAMEEXTENDED). They carry these
    # fields, have no live two-sided quote, and flood the /markets listing — we exclude
    # them in _is_eligible_binary so the scanner only sees real single-event markets.
    mve_collection_ticker: str = ""
    mve_selected_legs: list[Any] = []
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


def _is_mve(km: KalshiMarket) -> bool:
    """True for auto-generated multivariate-event (parlay) markets — see KalshiMarket."""
    return bool(km.mve_selected_legs) or bool(km.mve_collection_ticker) or km.ticker.startswith("KXMVE")


def _has_two_sided_quote(km: KalshiMarket) -> bool:
    """True only if both YES bid and ask sit strictly inside (0, 1).

    Kalshi uses 0/1 as no-bid / no-ask sentinels, so a one-sided book shows up as
    bid=0 or ask=1. Requiring both inside (0, 1) keeps only markets with a live,
    two-sided market — which is also what an executable forward signal needs.
    """
    bid, ask = km.yes_bid_dollars, km.yes_ask_dollars
    return bid is not None and ask is not None and 0 < bid < 1 and 0 < ask < 1


def _is_eligible_binary(km: KalshiMarket) -> bool:
    """Keep real, tradeable binary markets with a live two-sided quote.

    Excludes non-binary (scalar) types, non-tradeable statuses, auto-generated MVE
    parlay markets (which flood the listing and have no real book), and markets with
    no parseable price or no two-sided quote.
    """
    if km.status.lower() not in _ACTIVE_STATUSES:
        return False
    if km.market_type and km.market_type.lower() != "binary":
        return False
    if _is_mve(km):
        return False
    if not _has_two_sided_quote(km):
        return False
    return _market_prob(km) is not None


def normalize_market(raw: dict) -> Market | None:
    """Validate + map a raw Kalshi row to our ``Market``; None if not eligible.

    The Polymarket counterpart records ``float(outcomePrices[0])``; here the YES price
    comes from the decimal-dollar fields (last trade or bid/ask mid). ``yes_token_id`` is
    always None — Kalshi has no CLOB token — but the scanner still prices off the live
    book via ``fetch_book`` (by ticker), not the mid.
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


def _orderbook_side(levels: Any, *, invert: bool) -> list[tuple[float, float]]:
    """Parse one Kalshi ``[[price_cents, count], ...]`` side into ``(price, size)`` pairs.

    Kalshi prices are integer **cents** (1–99); we scale to 0–1 dollars. ``invert=True``
    flips a NO-bid price to its YES-ask equivalent (a NO bid at ``c``¢ lets you BUY YES at
    ``100 - c``¢). Levels with an out-of-range price or non-positive size are dropped.
    Sorted best-first: YES bids descending, YES asks ascending.
    """
    parsed: list[tuple[float, float]] = []
    for lvl in levels or []:
        try:
            cents = float(lvl[0])
            size = float(lvl[1])
        except (TypeError, ValueError, IndexError):
            continue
        if not (0 < cents < 100) or size <= 0:
            continue
        price = (100.0 - cents) / 100.0 if invert else cents / 100.0
        parsed.append((price, size))
    parsed.sort(key=lambda ps: ps[0], reverse=not invert)  # asks ascending, bids descending
    return parsed


def _parse_orderbook(book: dict) -> Book | None:
    """Build a YES-centric ``Book`` from Kalshi's ``{"yes": [...], "no": [...]}`` payload.

    ``yes`` are resting bids to buy YES (our YES bids); ``no`` are resting bids to buy NO,
    which become YES asks once inverted. Returns None for a one-sided book so the scanner
    falls back to the mid — matching ``polymarket.fetch_book``.
    """
    bids = _orderbook_side(book.get("yes"), invert=False)
    asks = _orderbook_side(book.get("no"), invert=True)
    if not bids or not asks:
        return None
    best_bid, best_ask = bids[0][0], asks[0][0]
    bid_depth = sum(s for p, s in bids if p == best_bid)
    ask_depth = sum(s for p, s in asks if p == best_ask)
    return Book(bids, asks, best_bid, best_ask, bid_depth, ask_depth)


def fetch_book(ticker: str) -> Book | None:
    """Live order book for a Kalshi market (by ticker), or None on failure/one-sided book.

    Reads ``GET /markets/{ticker}/orderbook`` and parses it into the same YES-centric
    ``Book`` Polymarket returns, so the scanner's VWAP-fill logic is exchange-agnostic.
    """
    try:
        with KalshiClient() as client:
            body = client.get(f"/markets/{ticker}/orderbook") or {}
    except (httpx.HTTPError, ValueError, TypeError):
        return None
    return _parse_orderbook(body.get("orderbook") or {})


def fetch_resolution(ticker: str) -> bool | None:
    """YES/NO resolution for a settled Kalshi market (by ticker), else ``None``.

    Reads ``GET /markets/{ticker}`` and maps the ``result`` field (``"yes"``/``"no"``) to a bool.
    ``None`` when the market isn't settled yet, the result is void/unknown, or the lookup fails —
    the caller retries on the next sweep. Mirrors ``polymarket.fetch_resolution``'s contract so the
    resolution sweep is exchange-agnostic.
    """
    try:
        with KalshiClient() as client:
            body = client.get(f"/markets/{ticker}") or {}
    except (httpx.HTTPError, ValueError, TypeError):
        return None
    result = str((body.get("market") or {}).get("result") or "").strip().lower()
    if result == "yes":
        return True
    if result == "no":
        return False
    return None  # "" (still open), "void", or anything unexpected


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


def _fetch_events_page(
    client: KalshiClient, limit: int, cursor: str | None
) -> tuple[list[dict], str]:
    """Fetch one page of open *events* with their nested markets flattened out.

    Discovery goes through ``/events`` (not ``/markets``) on purpose: the raw
    ``/markets`` listing is overwhelmingly auto-generated MVE parlay markets, which bury
    the real single-event markets past any sane page budget. ``/events`` groups the real
    markets and keeps the MVE collections out of the way, so flattening their nested
    markets surfaces genuine, liquid markets directly. ``next_cursor`` is the empty
    string on the last page (Kalshi's end-of-pages signal).
    """
    params: dict[str, Any] = {"limit": limit, "status": "open", "with_nested_markets": True}
    if cursor:
        params["cursor"] = cursor
    body = client.get("/events", **params) or {}
    events = body.get("events") or []
    raw_markets = [m for e in events for m in (e.get("markets") or [])]
    return raw_markets, body.get("cursor") or ""


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
    """Paginate active markets via ``/events``, keeping only real trading activity.

    Cursor-paged (unlike Polymarket's offset paging): each page returns the cursor for
    the next, and an empty cursor marks the end.

    Discovery goes through the events endpoint (see ``_fetch_events_page``) because the
    raw ``/markets`` listing is ~99% auto-generated MVE parlay markets that bury the real
    ones. ``_is_eligible_binary`` then drops any MVE/one-sided stragglers, and we keep
    only markets whose **lifetime** volume (``volume_total``) clears ``min_volume``. (24h
    volume isn't usable: Kalshi reports it as 0 for every market, so lifetime volume is
    the only populated liquidity signal.)

    Bounded three ways — ``max_markets`` eligible found, cursor exhausted, or ``max_pages``
    fetched — so a scan stays responsive even when liquid markets are sparse. Hitting the
    page cap is logged (never a silent truncation).
    """
    limit = 200  # Kalshi /events allows up to 200/page
    markets: list[Market] = []
    cursor: str | None = None
    pages = 0
    raw_seen = 0
    with KalshiClient() as client:
        while len(markets) < max_markets and pages < max_pages:
            raw_markets, cursor = _fetch_events_page(client, limit=limit, cursor=cursor)
            pages += 1
            raw_seen += len(raw_markets)
            if not raw_markets and not cursor:
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
