"""Tests for the per-model pricing table (pure)."""

from __future__ import annotations

import pytest

from research import pricing


class TestRateFor:
    def test_known_model(self) -> None:
        assert pricing.rate_for("claude-sonnet-4-6") == (3.00, 15.00)

    def test_unknown_model_uses_fallback(self) -> None:
        assert pricing.rate_for("some-future-model") == pricing.FALLBACK_RATE

    def test_none_uses_fallback(self) -> None:
        assert pricing.rate_for(None) == pricing.FALLBACK_RATE


class TestCostUsd:
    def test_zero_and_none_tokens(self) -> None:
        assert pricing.cost_usd("gpt-5.5", 0, 0) == 0.0
        assert pricing.cost_usd("gpt-5.5", None, None) == 0.0

    def test_known_model_math(self) -> None:
        # gpt-5.5 = (5.00, 30.00)/1M; 1M input + 0.5M output = 5.00 + 15.00 = 20.00.
        assert pricing.cost_usd("gpt-5.5", 1_000_000, 500_000) == pytest.approx(20.00)

    def test_fallback_model_still_priced(self) -> None:
        ri, ro = pricing.FALLBACK_RATE
        assert pricing.cost_usd("mystery", 1_000_000, 1_000_000) == pytest.approx(ri + ro)

    def test_cache_read_and_write_multipliers(self) -> None:
        # claude-sonnet-4-6 = (3.00, 15.00)/1M. 1M uncached input = 3.00; 1M cache write @1.25x =
        # 3.75; 1M cache read @0.1x = 0.30 -> 7.05 total (output 0).
        cost = pricing.cost_usd("claude-sonnet-4-6", 1_000_000, 0, 1_000_000, 1_000_000)
        assert cost == pytest.approx(3.00 + 3.75 + 0.30)

    def test_cache_args_default_to_zero(self) -> None:
        # Omitting the cache args must equal passing them as 0 (back-compat for 2-arg callers).
        assert pricing.cost_usd("gpt-5.5", 1000, 500) == pricing.cost_usd("gpt-5.5", 1000, 500, 0, 0)

    def test_batch_flag_halves_cost(self) -> None:
        full = pricing.cost_usd("claude-sonnet-4-6", 1_000_000, 1_000_000)
        assert pricing.cost_usd("claude-sonnet-4-6", 1_000_000, 1_000_000, batch=True) == pytest.approx(
            full * 0.5
        )

    def test_web_search_fee_added_on_top(self) -> None:
        # 5 searches @ $0.01 add $0.05 over the token cost.
        tokens_only = pricing.cost_usd("claude-sonnet-4-6", 1000, 500)
        with_search = pricing.cost_usd("claude-sonnet-4-6", 1000, 500, web_search_requests=5)
        assert with_search - tokens_only == pytest.approx(5 * pricing.WEB_SEARCH_FEE_USD)

    def test_batch_discount_does_not_apply_to_search_fee(self) -> None:
        # The 50% batch discount halves TOKENS only; the per-search tool fee is charged in full.
        tokens = pricing.cost_usd("claude-sonnet-4-6", 1_000_000, 1_000_000)
        fee = 4 * pricing.WEB_SEARCH_FEE_USD
        got = pricing.cost_usd("claude-sonnet-4-6", 1_000_000, 1_000_000, batch=True, web_search_requests=4)
        assert got == pytest.approx(tokens * 0.5 + fee)
