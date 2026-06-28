"""Per-model LLM pricing — turn token counts into a rough USD cost.

Pure stdlib. ``RATES`` is USD per 1,000,000 tokens as ``(input, output)``, from public list
pricing — **approximate and editable**; update as prices change. An unknown model falls back to a
conservative default (logged once) so cost reporting degrades gracefully instead of failing.

Tokens are stored on each ``Analysis`` (durable, price-independent); dollar figures are always
derived here, so re-pricing history is just a table edit.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)

# USD per 1M tokens: (input, output). Verified against public pricing 2026-06-19 — re-check when
# providers change rates or you switch models. Web search adds provider fees this table doesn't model.
RATES: dict[str, tuple[float, float]] = {
    # OpenAI (Responses API list prices)
    "gpt-5.5": (5.00, 30.00),
    "gpt-5": (1.25, 10.00),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4o": (2.50, 10.00),
    # Anthropic (Messages API list prices)
    "claude-fable-5": (10.00, 50.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
# Used when a model isn't in RATES (kept on the higher side so unknown models aren't under-counted).
FALLBACK_RATE: tuple[float, float] = (5.00, 25.00)

# Server-side web_search tool fee: $10 per 1,000 searches = $0.01 per search, billed on TOP of
# tokens (Anthropic + OpenAI both charge a per-search fee). Captured separately because it's not a
# token cost — without it, reported per-analysis cost undercounts real spend by the search fee.
WEB_SEARCH_FEE_USD: float = 0.01

_warned: set[str] = set()


def rate_for(model: str | None) -> tuple[float, float]:
    """(input, output) USD-per-1M for ``model``; the fallback (logged once) if unknown."""
    key = model or ""
    if key in RATES:
        return RATES[key]
    if key not in _warned:
        _log.warning("no pricing for model %r; using fallback rate %s/1M", model, FALLBACK_RATE)
        _warned.add(key)
    return FALLBACK_RATE


def cost_usd(
    model: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    cache_creation_tokens: int | None = 0,
    cache_read_tokens: int | None = 0,
    batch: bool = False,
    web_search_requests: int | None = 0,
) -> float:
    """Rough USD cost of one call given its token usage. 0.0 when usage is missing/zero.

    Anthropic prompt caching (5-min TTL): a cache **write** costs 1.25x the input rate, a cache
    **read** costs 0.1x. ``input_tokens`` is already the uncached remainder, so the three input
    components are additive. The cache args default to 0, so OpenAI calls and older 2-arg callers
    are unaffected. ``batch=True`` applies the Message Batches API's 50% discount to the **token**
    cost. ``web_search_requests`` adds the per-search tool fee (``WEB_SEARCH_FEE_USD`` each) on top
    — a tool fee, not a token cost, so the batch discount does NOT apply to it.
    """
    rate_in, rate_out = rate_for(model)
    it = input_tokens or 0
    ot = output_tokens or 0
    cc = cache_creation_tokens or 0
    cr = cache_read_tokens or 0
    input_cost = it * rate_in + cc * rate_in * 1.25 + cr * rate_in * 0.1
    token_total = (input_cost + ot * rate_out) / 1_000_000.0
    if batch:
        token_total *= 0.5
    return token_total + (web_search_requests or 0) * WEB_SEARCH_FEE_USD
