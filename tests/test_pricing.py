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
