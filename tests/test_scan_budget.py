"""The per-scan LLM-call cap (ScanRequest.max_llm_calls) must bound analyze() calls."""

from __future__ import annotations

import pytest
from conftest import make_market

from research import scanner
from research.models import Analysis, ScanRequest


def _setup(monkeypatch, n_markets: int) -> dict:
    """Stub the exchange + analyzer so scan() runs offline against the temp DB.

    Returns a counter dict whose ``n`` is the number of fresh analyze_market calls made.
    """
    monkeypatch.setenv("ANALYSIS_DELAY_SECONDS", "0")  # no sleeps in tests
    markets = [make_market(id=f"m{i}", market_prob=0.5) for i in range(n_markets)]
    monkeypatch.setattr(scanner.exchanges, "fetch_active", lambda max_markets: markets)
    monkeypatch.setattr(scanner.exchanges, "fetch_book", lambda m: None)  # mid-price fallback

    calls = {"n": 0}

    def fake_analyze(m):
        calls["n"] += 1
        # claude_prob 0.9 vs market 0.5 -> divergence 0.4 clears the default gate.
        return Analysis(market_id=m.id, claude_prob=0.9, model="test-model",
                        market_prob_at_analysis=0.5)

    monkeypatch.setattr(scanner.analyzer, "analyze_market", fake_analyze)
    return calls


def test_cap_limits_fresh_analyses(temp_db, monkeypatch) -> None:
    calls = _setup(monkeypatch, n_markets=5)
    results = scanner.scan(ScanRequest(max_markets=5, max_llm_calls=2))
    assert calls["n"] == 2, "cap should stop fresh analyses at max_llm_calls"
    assert len(results) == 2  # only the analyzed markets become results


def test_zero_means_uncapped(temp_db, monkeypatch) -> None:
    calls = _setup(monkeypatch, n_markets=5)
    scanner.scan(ScanRequest(max_markets=5, max_llm_calls=0))
    assert calls["n"] == 5, "0 = no cap: every candidate is analyzed"


def test_scan_with_stats_accumulates_real_cost(temp_db, monkeypatch) -> None:
    monkeypatch.setenv("ANALYSIS_DELAY_SECONDS", "0")
    markets = [make_market(id=f"m{i}", market_prob=0.5) for i in range(3)]
    monkeypatch.setattr(scanner.exchanges, "fetch_active", lambda max_markets: markets)
    monkeypatch.setattr(scanner.exchanges, "fetch_book", lambda m: None)
    monkeypatch.setattr(scanner.analyzer, "analyze_market", lambda m: Analysis(
        market_id=m.id, claude_prob=0.9, model="claude-sonnet-4-6",
        market_prob_at_analysis=0.5, input_tokens=1_000_000, output_tokens=0,
        cache_read_input_tokens=1_000_000,  # 1M cache read @ 0.1x of $3/1M = $0.30 per call
    ))
    _results, stats = scanner.scan_with_stats(ScanRequest(max_markets=3, refute_top=0))
    assert stats["fresh_analyses"] == 3
    assert stats["llm_calls"] == 3
    # per call: 1M input @$3 = $3.00 + 1M cache_read @0.3 = $0.30 -> $3.30; x3 = $9.90.
    assert stats["cost_usd"] == pytest.approx(9.90)
    assert stats["cache_read_tokens"] == 3_000_000  # aggregated across the 3 fresh calls
    assert stats["cache_creation_tokens"] == 0


def test_cached_analyses_do_not_consume_budget(temp_db, monkeypatch) -> None:
    # Pre-analyze every market so all are cache hits within max_age_hours.
    calls = _setup(monkeypatch, n_markets=4)
    scanner.scan(ScanRequest(max_markets=4, max_llm_calls=0))  # warm the cache (4 calls)
    assert calls["n"] == 4
    # Second scan with a tiny cap: all reused, so no fresh calls despite the low budget.
    scanner.scan(ScanRequest(max_markets=4, max_llm_calls=1, max_age_hours=24))
    assert calls["n"] == 4, "reused analyses are free and must not count against the cap"
