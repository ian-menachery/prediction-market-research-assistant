"""Tests for ScanRequest validation bounds (reject pathological scan params)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from research.models import ScanRequest


class TestScanRequestBounds:
    def test_defaults_valid(self) -> None:
        req = ScanRequest()
        assert req.max_markets == 100
        assert req.min_divergence == pytest.approx(0.05)

    def test_max_markets_upper_bound(self) -> None:
        with pytest.raises(ValidationError):
            ScanRequest(max_markets=1_000_000)

    def test_max_markets_lower_bound(self) -> None:
        with pytest.raises(ValidationError):
            ScanRequest(max_markets=0)

    def test_negative_divergence_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScanRequest(min_divergence=-0.1)

    def test_divergence_above_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScanRequest(min_divergence=1.5)

    def test_negative_volume_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScanRequest(min_volume_24h=-1)

    def test_refute_top_bounds(self) -> None:
        assert ScanRequest(refute_top=50).refute_top == 50
        with pytest.raises(ValidationError):
            ScanRequest(refute_top=51)
        with pytest.raises(ValidationError):
            ScanRequest(refute_top=-1)

    def test_max_llm_calls_default_is_uncapped(self, monkeypatch) -> None:
        # Hermetic: the default reads MAX_LLM_CALLS_PER_SCAN, which a local .env (loaded via
        # load_dotenv during the session) may set. Clear it so the test asserts the code default.
        monkeypatch.delenv("MAX_LLM_CALLS_PER_SCAN", raising=False)
        assert ScanRequest().max_llm_calls == 0

    def test_max_llm_calls_bounds(self) -> None:
        assert ScanRequest(max_llm_calls=20).max_llm_calls == 20
        with pytest.raises(ValidationError):
            ScanRequest(max_llm_calls=-1)
        with pytest.raises(ValidationError):
            ScanRequest(max_llm_calls=1001)

    def test_max_llm_calls_defaults_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("MAX_LLM_CALLS_PER_SCAN", "15")
        assert ScanRequest().max_llm_calls == 15  # env default
        assert ScanRequest(max_llm_calls=3).max_llm_calls == 3  # explicit value wins
