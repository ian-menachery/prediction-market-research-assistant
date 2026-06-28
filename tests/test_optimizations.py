"""Tests for the optimization round: depth guard, no-Polymarket-spend, categories, health check."""

from __future__ import annotations

import pytest

from conftest import make_market

from research import kalshi, performance, scanner
from research.models import Analysis, ScanResult


def _exec_result(m, **over) -> ScanResult:
    base = dict(
        market=m, analysis=Analysis(market_id=m.id, claude_prob=0.7, model="x"),
        calibrated_prob=0.7, side="YES", ev=0.1, ev_pct=0.2, kelly=0.2, annualized_ev=0.5,
        fill_shares=100.0, price_paid=0.5, target_position_usd=50.0, executable=True,
        days_to_close=5.0,
    )
    base.update(over)
    return ScanResult(**base)


class TestDepthGuard:
    def test_thin_book_not_logged(self, temp_db, monkeypatch) -> None:
        monkeypatch.setenv("MIN_BOOK_DEPTH_USD", "20")
        monkeypatch.setenv("SIGNAL_MIN_EV", "0.0")
        m = make_market(id="KX-1", exchange="kalshi", market_prob=0.5)
        temp_db.upsert_markets([m])
        # 10 shares * $0.50 = $5 executable < $20 floor -> skipped
        assert scanner.persist_signals([_exec_result(m, fill_shares=10.0)]) == 0

    def test_deep_book_logged(self, temp_db, monkeypatch) -> None:
        monkeypatch.setenv("MIN_BOOK_DEPTH_USD", "20")
        monkeypatch.setenv("SIGNAL_MIN_EV", "0.0")
        m = make_market(id="KX-2", exchange="kalshi", market_prob=0.5)
        temp_db.upsert_markets([m])
        # 100 * $0.50 = $50 >= $20 -> logged
        assert scanner.persist_signals([_exec_result(m, fill_shares=100.0)]) == 1


class TestNoPolymarketSpend:
    def test_reanalyze_skips_untradeable_exchange(self, temp_db, monkeypatch) -> None:
        monkeypatch.setenv("EXCHANGE", "kalshi")
        monkeypatch.setenv("STALE_THRESHOLD", "0.01")
        temp_db.upsert_markets([
            make_market(id="0xPOLY", exchange="polymarket", market_prob=0.5),
            make_market(id="KX-9", exchange="kalshi", market_prob=0.5),
        ])
        for mid in ("0xPOLY", "KX-9"):  # at-analysis far from current -> stale
            temp_db.save_analysis(Analysis(
                market_id=mid, model="x", claude_prob=0.5, market_prob_at_analysis=0.1))
        analyzed: list[str] = []
        monkeypatch.setattr(
            scanner.analyzer, "analyze_market",
            lambda mkt: (analyzed.append(mkt.id), Analysis(market_id=mkt.id, model="x", claude_prob=0.6))[1],
        )
        scanner.reanalyze_stale()
        assert "0xPOLY" not in analyzed  # untradeable Polymarket market never analyzed
        assert "KX-9" in analyzed

    def test_tradeable_exchanges_both(self, monkeypatch) -> None:
        monkeypatch.setenv("EXCHANGE", "both")
        assert scanner._tradeable_exchanges() == {"polymarket", "kalshi"}
        monkeypatch.setenv("EXCHANGE", "kalshi")
        assert scanner._tradeable_exchanges() == {"kalshi"}


class TestCategory:
    @pytest.mark.parametrize("ticker,cat", [
        ("KXHIGHNY-26JUN28-T84", "weather"),
        ("KXBTCD-26JUN-T60", "crypto"),
        ("KXETHD-26JUN-T15", "crypto"),
        ("KXCPIYOY-26NOV-T3", "econ"),
        ("KXPAYROLLS-26JUN", "econ"),
        ("KXNEWPOPE-70-PPAR", "other"),
    ])
    def test_category_of(self, ticker, cat) -> None:
        assert performance._category_of(ticker) == cat


class TestHealthCheck:
    def _patch(self, monkeypatch, *, book):
        m = make_market(id="KXHIGHNY-1", exchange="kalshi")
        monkeypatch.setattr(kalshi, "fetch_all_active", lambda **k: [m])
        monkeypatch.setattr(kalshi, "fetch_book", lambda t: book)
        monkeypatch.setattr(kalshi, "fetch_resolution", lambda t: None)

    def test_all_ok(self, monkeypatch) -> None:
        self._patch(monkeypatch, book=object())  # any non-None book
        h = kalshi.health_check()
        assert h["discovery_ok"] and h["book_ok"] and h["resolution_ok"]

    def test_book_drift_flagged(self, monkeypatch) -> None:
        self._patch(monkeypatch, book=None)  # book schema broke
        h = kalshi.health_check()
        assert h["discovery_ok"] and not h["book_ok"]
