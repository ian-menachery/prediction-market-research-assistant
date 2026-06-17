"""Tests for the pure EV / verdict helpers in scanner.py."""

from __future__ import annotations

import pytest

from conftest import make_market
from research import scanner


class TestEvFields:
    def test_side_yes_when_prob_above_mid(self) -> None:
        m = make_market(market_prob=0.50)
        out = scanner._ev_fields(m, prob=0.70, yes_cost=None, no_cost=None, min_days=1.0)
        assert out["side"] == "YES"

    def test_side_no_when_prob_below_mid(self) -> None:
        m = make_market(market_prob=0.50)
        out = scanner._ev_fields(m, prob=0.30, yes_cost=None, no_cost=None, min_days=1.0)
        assert out["side"] == "NO"

    def test_mid_fallback_not_executable(self) -> None:
        m = make_market(market_prob=0.50)
        out = scanner._ev_fields(m, prob=0.70, yes_cost=None, no_cost=None, min_days=1.0)
        assert out["executable"] is False
        assert out["price_paid"] == pytest.approx(0.50)
        assert out["ev"] == pytest.approx(0.20)  # prob 0.70 - mid 0.50

    def test_executable_uses_book_cost(self) -> None:
        m = make_market(market_prob=0.50)
        out = scanner._ev_fields(m, prob=0.70, yes_cost=0.55, no_cost=0.45, min_days=1.0)
        assert out["executable"] is True
        assert out["price_paid"] == pytest.approx(0.55)
        assert out["ev"] == pytest.approx(0.15)  # 0.70 - 0.55

    def test_no_side_ev_uses_complement(self) -> None:
        m = make_market(market_prob=0.50)
        out = scanner._ev_fields(m, prob=0.30, yes_cost=None, no_cost=None, min_days=1.0)
        # NO side mid cost = 1 - 0.50 = 0.50; ev = (1-0.30) - 0.50 = 0.20
        assert out["price_paid"] == pytest.approx(0.50)
        assert out["ev"] == pytest.approx(0.20)

    def test_negative_ev_surfaced_not_dropped(self) -> None:
        m = make_market(market_prob=0.50)
        out = scanner._ev_fields(m, prob=0.55, yes_cost=0.80, no_cost=0.20, min_days=1.0)
        assert out["ev"] == pytest.approx(0.55 - 0.80)
        assert out["ev"] < 0

    def test_annualized_none_when_below_min_days(self) -> None:
        m = make_market(market_prob=0.50, days_to_close=0.5)
        out = scanner._ev_fields(m, prob=0.70, yes_cost=None, no_cost=None, min_days=1.0)
        assert out["annualized_ev"] is None
        assert out["days_to_close"] == pytest.approx(0.5, abs=0.05)

    def test_annualized_present_when_above_min_days(self) -> None:
        m = make_market(market_prob=0.50, days_to_close=30.0)
        out = scanner._ev_fields(m, prob=0.70, yes_cost=None, no_cost=None, min_days=1.0)
        assert out["annualized_ev"] is not None

    def test_degenerate_price_paid_yields_none_metrics(self) -> None:
        # An executable cost of exactly 1.0 is out of (0,1) => ev_pct/kelly None.
        m = make_market(market_prob=0.50)
        out = scanner._ev_fields(m, prob=0.70, yes_cost=1.0, no_cost=0.0, min_days=1.0)
        assert out["ev_pct"] is None
        assert out["kelly"] is None
        assert out["annualized_ev"] is None


class TestRefuteVerdict:
    def test_none_guards(self) -> None:
        assert scanner._refute_verdict("YES", None, 0.6) is None
        assert scanner._refute_verdict("YES", 0.5, None) is None
        assert scanner._refute_verdict(None, 0.5, 0.6) is None

    def test_yes_holds_when_refuter_above_band(self) -> None:
        # market 0.50, band 0.03; refuter must exceed 0.53 to hold.
        assert scanner._refute_verdict("YES", 0.50, 0.60) == "holds"

    def test_yes_refuted_when_refuter_inside_band(self) -> None:
        assert scanner._refute_verdict("YES", 0.50, 0.51) == "refuted"

    def test_no_holds_when_refuter_below_band(self) -> None:
        assert scanner._refute_verdict("NO", 0.50, 0.40) == "holds"

    def test_no_refuted_when_refuter_inside_band(self) -> None:
        assert scanner._refute_verdict("NO", 0.50, 0.49) == "refuted"


class TestDaysToClose:
    def test_none_when_no_end_date(self) -> None:
        m = make_market(days_to_close=None)
        assert scanner._days_to_close(m) is None

    def test_positive_for_future_close(self) -> None:
        m = make_market(days_to_close=10.0)
        dtc = scanner._days_to_close(m)
        assert dtc == pytest.approx(10.0, abs=0.05)
