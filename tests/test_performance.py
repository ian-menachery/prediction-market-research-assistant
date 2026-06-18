"""Tests for the signal performance / track-record aggregation (pure stdlib)."""

from __future__ import annotations

import pytest

from research import performance
from research.models import Signal


def make_signal(
    *,
    pnl: float | None,
    price_paid: float = 0.5,
    fill_shares: float = 100.0,
    side: str = "YES",
    exchange: str = "polymarket",
    resolved: bool = True,
) -> Signal:
    """A minimal settled (or open) Signal for pure-logic tests."""
    return Signal(
        market_id="m",
        exchange=exchange,
        question="Will it happen?",
        side=side,
        calibrated_prob=0.6,
        market_prob=0.5,
        price_paid=price_paid,
        fill_shares=fill_shares,
        target_position_usd=50.0,
        resolved=resolved,
        resolution=(pnl is not None and pnl > 0),
        pnl=pnl,
    )


class TestMaxDrawdown:
    def test_empty_is_zero(self) -> None:
        assert performance.max_drawdown([]) == 0.0

    def test_monotonic_up_is_zero(self) -> None:
        assert performance.max_drawdown([10.0, 20.0, 30.0]) == 0.0

    def test_peak_to_trough(self) -> None:
        # peak 30 then down to 10 => drawdown 20.
        assert performance.max_drawdown([10.0, 30.0, 10.0, 25.0]) == 20.0

    def test_dip_below_zero_from_flat_start(self) -> None:
        # running peak starts at 0, so a first negative point is a drawdown.
        assert performance.max_drawdown([-5.0, -8.0]) == 8.0


class TestSharpe:
    def test_under_two_trades_is_none(self) -> None:
        assert performance.sharpe([0.1]) is None

    def test_zero_variance_is_none(self) -> None:
        assert performance.sharpe([0.2, 0.2, 0.2]) is None

    def test_positive_for_consistent_gains(self) -> None:
        s = performance.sharpe([0.1, 0.12, 0.08, 0.11])
        assert s is not None and s > 0.0


class TestBuildReport:
    def test_empty_has_valid_shape_no_invented_zeros(self) -> None:
        rep = performance.build_report([], open_count=3)
        assert rep["settled"] == 0
        assert rep["open"] == 3
        assert rep["total_return"] is None
        assert rep["win_rate"] is None
        assert rep["sharpe"] is None
        assert rep["equity_curve"] == []
        assert rep["by_exchange"] == []

    def test_open_signals_excluded_from_realized_stats(self) -> None:
        rep = performance.build_report([make_signal(pnl=None, resolved=False)], open_count=1)
        assert rep["settled"] == 0

    def test_totals_and_win_rate(self) -> None:
        # two winners (+50 each), one loser (-50). cost basis = 0.5 * 100 = 50 each.
        sigs = [
            make_signal(pnl=50.0),
            make_signal(pnl=50.0),
            make_signal(pnl=-50.0),
        ]
        rep = performance.build_report(sigs)
        assert rep["settled"] == 3
        assert rep["wins"] == 2
        assert rep["losses"] == 1
        assert rep["total_pnl"] == pytest.approx(50.0)
        assert rep["total_cost"] == pytest.approx(150.0)
        assert rep["total_return"] == pytest.approx(50.0 / 150.0)
        assert rep["win_rate"] == pytest.approx(2 / 3)
        assert rep["avg_win"] == pytest.approx(50.0)
        assert rep["avg_loss"] == pytest.approx(-50.0)
        assert rep["profit_factor"] == pytest.approx(2.0)  # 100 gross win / 50 gross loss

    def test_equity_curve_is_cumulative_in_order(self) -> None:
        sigs = [make_signal(pnl=10.0), make_signal(pnl=-4.0), make_signal(pnl=6.0)]
        rep = performance.build_report(sigs)
        assert [pt["cum_pnl"] for pt in rep["equity_curve"]] == [10.0, 6.0, 12.0]
        assert rep["max_drawdown"] == pytest.approx(4.0)

    def test_breakdowns_partition_by_key(self) -> None:
        sigs = [
            make_signal(pnl=50.0, exchange="polymarket", side="YES"),
            make_signal(pnl=-50.0, exchange="kalshi", side="NO"),
            make_signal(pnl=30.0, exchange="kalshi", side="YES"),
        ]
        rep = performance.build_report(sigs)
        by_ex = {r["key"]: r for r in rep["by_exchange"]}
        assert by_ex["kalshi"]["n"] == 2
        assert by_ex["kalshi"]["pnl"] == pytest.approx(-20.0)
        assert by_ex["polymarket"]["pnl"] == pytest.approx(50.0)
        by_side = {r["key"]: r for r in rep["by_side"]}
        assert by_side["YES"]["n"] == 2
        assert by_side["NO"]["n"] == 1
