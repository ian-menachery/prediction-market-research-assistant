"""The dry-run cost preview (scanner.estimate_scan) must count spend without making LLM calls."""

from __future__ import annotations

from conftest import make_market

from research import scanner
from research.models import Analysis, ScanRequest


def _setup(monkeypatch, n_markets: int) -> dict:
    """Stub the exchange + analyzer so estimate/scan run offline; returns an analyze-call counter."""
    monkeypatch.setenv("ANALYSIS_DELAY_SECONDS", "0")
    markets = [make_market(id=f"m{i}", market_prob=0.5) for i in range(n_markets)]
    monkeypatch.setattr(scanner.exchanges, "fetch_active", lambda max_markets: markets)
    monkeypatch.setattr(scanner.exchanges, "fetch_book", lambda m: None)

    calls = {"n": 0}

    def fake_analyze(m):
        calls["n"] += 1
        return Analysis(market_id=m.id, claude_prob=0.9, model="test-model",
                        market_prob_at_analysis=0.5)

    monkeypatch.setattr(scanner.analyzer, "analyze_market", fake_analyze)
    return calls


def test_estimate_makes_no_llm_calls(temp_db, monkeypatch) -> None:
    calls = _setup(monkeypatch, n_markets=5)
    est = scanner.estimate_scan(ScanRequest(max_markets=5))
    assert calls["n"] == 0, "estimate must never call the LLM"
    assert est["candidates"] == 5
    assert est["fresh_analyses"] == 5  # cold cache → all would be analyzed
    assert est["cached"] == 0
    assert est["estimated_calls"] == 5


def test_estimate_counts_cached_as_free(temp_db, monkeypatch) -> None:
    calls = _setup(monkeypatch, n_markets=4)
    scanner.scan(ScanRequest(max_markets=4, max_llm_calls=0))  # warm the cache (4 real calls)
    assert calls["n"] == 4
    est = scanner.estimate_scan(ScanRequest(max_markets=4, max_age_hours=24))
    assert calls["n"] == 4, "estimate added no calls"
    assert est["cached"] == 4
    assert est["fresh_analyses"] == 0
    assert est["estimated_calls"] == 0
    assert est["estimated_cost_usd"] == 0.0


def test_estimate_applies_cap_and_refute_bound(temp_db, monkeypatch) -> None:
    _setup(monkeypatch, n_markets=10)
    est = scanner.estimate_scan(ScanRequest(max_markets=10, refute_top=3, max_llm_calls=4))
    assert est["fresh_analyses"] == 10
    assert est["refute_max"] == 3
    # uncapped would be 13; the cap clips the estimate to 4.
    assert est["estimated_calls"] == 4


def test_estimate_cost_uses_env_per_call(temp_db, monkeypatch) -> None:
    _setup(monkeypatch, n_markets=2)
    monkeypatch.setenv("COST_PER_LLM_CALL_USD", "0.50")
    est = scanner.estimate_scan(ScanRequest(max_markets=2))
    assert est["cost_per_call_usd"] == 0.50
    assert est["estimated_cost_usd"] == 1.0  # 2 calls * $0.50
