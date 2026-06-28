"""Tests for the optimization round: depth guard, no-Polymarket-spend, categories, health check."""

from __future__ import annotations

import pytest

from conftest import make_market

from research import analyzer, kalshi, performance, scanner, scheduler
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


class TestResolutionRules:
    def test_kalshi_normalize_captures_rules(self) -> None:
        raw = {
            "ticker": "KXHIGHNY-26JUN28-T84", "market_type": "binary", "status": "active",
            "title": "Will the high temp in NYC be >84 on Jun 28, 2026?",
            "yes_bid_dollars": "0.30", "yes_ask_dollars": "0.34", "last_price_dollars": "0.32",
            "volume_fp": "12000", "close_time": "2026-06-29T00:00:00Z",
            "rules_primary": "Resolves Yes if the NWS Central Park high for Jun 28 2026 is > 84.",
        }
        m = kalshi.normalize_market(raw)
        assert m is not None and "Central Park" in m.resolution_rules

    def test_user_prompt_includes_rules(self) -> None:
        m = make_market(market_prob=0.5, resolution_rules="NWS Central Park high > 84 on Jun 28")
        p = analyzer._user_prompt(m)
        assert "Resolution criteria" in p and "Central Park" in p

    def test_db_round_trips_resolution_rules(self, temp_db) -> None:
        m = make_market(id="KX-R", exchange="kalshi", resolution_rules="BLS one-decimal CPI > 3.9%")
        temp_db.upsert_markets([m])
        got = temp_db.get_market("KX-R")
        assert got is not None and got.resolution_rules == "BLS one-decimal CPI > 3.9%"


class TestKalshiFeeInEv:
    def test_fee_reduces_kalshi_ev_only(self, monkeypatch) -> None:
        monkeypatch.setenv("KALSHI_FEE_RATE", "0.07")
        k = make_market(market_prob=0.5, exchange="kalshi")
        p = make_market(market_prob=0.5, exchange="polymarket")
        kal = scanner._ev_fields(k, 0.7, yes_cost=0.55, no_cost=0.45, min_days=0.0)
        poly = scanner._ev_fields(p, 0.7, yes_cost=0.55, no_cost=0.45, min_days=0.0)
        fee = 0.07 * 0.55 * (1 - 0.55)
        assert kal["ev"] == pytest.approx(poly["ev"] - fee)
        assert poly["ev"] == pytest.approx(0.7 - 0.55)  # polymarket unchanged


class TestEventDedup:
    def test_keeps_one_signal_per_event(self, temp_db, monkeypatch) -> None:
        monkeypatch.setenv("MAX_SIGNALS_PER_EVENT", "1")
        monkeypatch.setenv("MIN_BOOK_DEPTH_USD", "0")
        monkeypatch.setenv("SIGNAL_MIN_EV", "0.0")
        # Two strikes of the same CPI event -> same group "KXCPIYOY-26NOV"; keep the higher-EV one.
        lo = make_market(id="KXCPIYOY-26NOV-T39", exchange="kalshi", market_prob=0.5)
        hi = make_market(id="KXCPIYOY-26NOV-T40", exchange="kalshi", market_prob=0.5)
        temp_db.upsert_markets([lo, hi])
        n = scanner.persist_signals([
            _exec_result(lo, ev=0.05, annualized_ev=0.2),
            _exec_result(hi, ev=0.20, annualized_ev=0.9),
        ])
        assert n == 1
        sigs = temp_db.get_signals()
        assert len(sigs) == 1 and sigs[0].market_id == "KXCPIYOY-26NOV-T40"  # higher EV kept

    def test_event_group(self) -> None:
        assert scanner._event_group("KXCPIYOY-26NOV-T3.9") == "KXCPIYOY-26NOV"

    def test_existing_open_signal_blocks_same_event(self, temp_db, monkeypatch) -> None:
        # An already-open signal in the event group blocks a new strike of the SAME event (cross-scan).
        monkeypatch.setenv("MAX_SIGNALS_PER_EVENT", "1")
        monkeypatch.setenv("MIN_BOOK_DEPTH_USD", "0")
        monkeypatch.setenv("SIGNAL_MIN_EV", "0.0")
        a = make_market(id="KXCPIYOY-26NOV-T39", exchange="kalshi", market_prob=0.5)
        b = make_market(id="KXCPIYOY-26NOV-T40", exchange="kalshi", market_prob=0.5)
        temp_db.upsert_markets([a, b])
        assert scanner.persist_signals([_exec_result(a, ev=0.1)]) == 1   # first one logs
        assert scanner.persist_signals([_exec_result(b, ev=0.2)]) == 0   # same event already open


class TestObservedCostEstimate:
    def test_uses_real_recent_cost(self, temp_db) -> None:
        temp_db.upsert_markets([make_market(id="m1", exchange="kalshi")])
        temp_db.save_analysis(Analysis(
            market_id="m1", model="claude-sonnet-4-6", claude_prob=0.5,
            input_tokens=1_000_000, output_tokens=0,  # 1M input @ $3/1M = $3.00
        ))
        assert scanner._observed_cost_per_call("claude-sonnet-4-6") == pytest.approx(3.0)

    def test_none_without_priced_rows(self, temp_db) -> None:
        # An analysis with no token usage isn't priceable -> None -> caller falls back to assumed.
        temp_db.upsert_markets([make_market(id="m2", exchange="kalshi")])
        temp_db.save_analysis(Analysis(market_id="m2", model="claude-sonnet-4-6", claude_prob=0.5))
        assert scanner._observed_cost_per_call("claude-sonnet-4-6") is None


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


class TestPeriodicHealthCheck:
    def test_failure_warns_and_writes_heartbeat(self, tmp_path, monkeypatch, caplog) -> None:
        monkeypatch.setenv("HEALTH_LOG_PATH", str(tmp_path / "health.jsonl"))
        monkeypatch.setattr(scheduler.kalshi, "health_check",
                            lambda: {"discovery_ok": False, "book_ok": False, "markets_found": 0})
        with caplog.at_level("WARNING"):
            rec = scheduler.run_health_check_once()
        assert rec["discovery_ok"] is False
        assert (tmp_path / "health.jsonl").read_text(encoding="utf-8").strip()  # heartbeat written
        assert "health FAILED" in caplog.text

    def test_ok_writes_heartbeat_no_warning(self, tmp_path, monkeypatch, caplog) -> None:
        monkeypatch.setenv("HEALTH_LOG_PATH", str(tmp_path / "health.jsonl"))
        monkeypatch.setattr(scheduler.kalshi, "health_check",
                            lambda: {"discovery_ok": True, "book_ok": True, "markets_found": 9})
        with caplog.at_level("WARNING"):
            rec = scheduler.run_health_check_once()
        assert rec["markets_found"] == 9
        assert (tmp_path / "health.jsonl").read_text(encoding="utf-8").strip()
        assert "FAILED" not in caplog.text
