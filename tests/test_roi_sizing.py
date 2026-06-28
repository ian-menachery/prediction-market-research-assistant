"""Tests for manual-trade sizing, record-fill + actual-fill P&L, and the ROI gauge."""

from __future__ import annotations

import pytest

from conftest import make_market

from research import performance, scanner
from research.models import Analysis, Signal


def _sig(**over) -> Signal:
    base = dict(
        market_id="KX-1", exchange="kalshi", question="Will it?", side="YES",
        calibrated_prob=0.60, market_prob=0.50, price_paid=0.50,
        ev=0.10, kelly=0.20, fill_shares=100.0, target_position_usd=50.0,
    )
    base.update(over)
    return Signal(**base)  # type: ignore[arg-type]


class TestRecommendedStake:
    def test_fractional_kelly(self, monkeypatch) -> None:
        monkeypatch.setenv("BANKROLL_USD", "200")
        monkeypatch.setenv("KELLY_FRACTION", "0.25")
        monkeypatch.delenv("MAX_POSITION_USD", raising=False)
        # 0.25 * 0.20 * 200 = $10; depth cap = 100 * 0.50 = $50 (not binding)
        assert scanner.recommended_stake_usd(_sig(kelly=0.20)) == pytest.approx(10.0)

    def test_hard_position_cap_binds(self, monkeypatch) -> None:
        monkeypatch.setenv("BANKROLL_USD", "200")
        monkeypatch.setenv("KELLY_FRACTION", "0.25")
        monkeypatch.setenv("MAX_POSITION_USD", "10")
        # kelly stake = 0.25 * 0.8 * 200 = $40, depth ample, but the $10 hard cap binds.
        s = _sig(kelly=0.8, fill_shares=1000.0, price_paid=0.50)
        assert scanner.recommended_stake_usd(s) == pytest.approx(10.0)

    def test_depth_cap_binds(self, monkeypatch) -> None:
        monkeypatch.setenv("BANKROLL_USD", "200")
        monkeypatch.setenv("KELLY_FRACTION", "0.25")
        # kelly stake = 0.25*1.0*200 = $50, but book only filled 20 * 0.50 = $10
        s = _sig(kelly=1.0, fill_shares=20.0, price_paid=0.50)
        assert scanner.recommended_stake_usd(s) == pytest.approx(10.0)

    @pytest.mark.parametrize("kelly", [None, 0.0, -0.3])
    def test_no_edge_is_zero(self, monkeypatch, kelly) -> None:
        monkeypatch.setenv("BANKROLL_USD", "200")
        assert scanner.recommended_stake_usd(_sig(kelly=kelly)) == 0.0

    def test_extreme_divergence_withholds_stake(self, monkeypatch) -> None:
        monkeypatch.setenv("BANKROLL_USD", "200")
        monkeypatch.setenv("KELLY_FRACTION", "0.25")
        monkeypatch.setenv("EXTREME_DIVERGENCE", "0.40")
        # 92% vs 7% mid = 85pp gap -> likely a misread -> no auto-stake, even with positive kelly.
        s = _sig(kelly=0.5, calibrated_prob=0.92, market_prob=0.07)
        assert scanner.is_extreme_divergence(s) is True
        assert scanner.recommended_stake_usd(s) == 0.0

    def test_normal_divergence_not_flagged(self, monkeypatch) -> None:
        monkeypatch.setenv("EXTREME_DIVERGENCE", "0.40")
        s = _sig(kelly=0.2, calibrated_prob=0.60, market_prob=0.50)  # 10pp
        assert scanner.is_extreme_divergence(s) is False


class TestRecordFill:
    def test_round_trip(self, temp_db) -> None:
        db = temp_db
        db.upsert_markets([make_market(id="KX-1", exchange="kalshi")])
        sid = db.save_signal(_sig())
        db.record_signal_fill(sid, stake_usd=12.0, price=0.48, shares=25.0)
        got = db.get_signal(sid)
        assert got is not None
        assert got.actual_stake_usd == pytest.approx(12.0)
        assert got.actual_price == pytest.approx(0.48)
        assert got.actual_shares == pytest.approx(25.0)


class TestActualFillPnl:
    def _setup(self, db, **sig_over):
        db.upsert_markets([make_market(id="KX-1", exchange="kalshi")])
        db.save_analysis(Analysis(market_id="KX-1", model="claude-sonnet-4-6", claude_prob=0.6))
        return db.save_signal(_sig(**sig_over))

    def test_uses_actual_fill_when_present(self, temp_db, monkeypatch) -> None:
        db = temp_db
        sid = self._setup(db)
        db.record_signal_fill(sid, stake_usd=12.0, price=0.40, shares=30.0)
        monkeypatch.setattr(scanner.exchanges, "fetch_resolution", lambda m: True)  # YES wins
        scanner.sweep_resolutions()
        got = db.get_signal(sid)
        assert got is not None
        # YES won: pnl = shares * (1 - price) = 30 * 0.60 = 18.0 (actual, not modeled 100*0.5)
        assert got.pnl == pytest.approx(18.0)

    def test_falls_back_to_modeled_fill(self, temp_db, monkeypatch) -> None:
        db = temp_db
        sid = self._setup(db)
        monkeypatch.setattr(scanner.exchanges, "fetch_resolution", lambda m: False)  # NO wins, YES bet loses
        scanner.sweep_resolutions()
        got = db.get_signal(sid)
        assert got is not None
        # modeled: lost YES -> -fill_shares * price_paid = -100 * 0.50 = -50.0
        assert got.pnl == pytest.approx(-50.0)


class TestRoi:
    def test_net_is_realized_minus_spend(self, temp_db) -> None:
        db = temp_db
        db.upsert_markets([make_market(id="KX-1", exchange="kalshi")])
        sid = db.save_signal(_sig())
        db.resolve_signal(sid, outcome=True, pnl=7.5)
        out = performance.roi()
        assert out["realized_pnl"] == pytest.approx(7.5)
        assert out["credit_spend"] == pytest.approx(0.0)  # no analyses with token usage
        assert out["net"] == pytest.approx(7.5)
        assert out["covered"] is True
