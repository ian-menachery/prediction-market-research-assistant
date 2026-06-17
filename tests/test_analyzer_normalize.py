"""Tests for the pure parsing/normalization logic in analyzer.py."""

from __future__ import annotations

import pytest

from conftest import make_market
from research import analyzer


class TestNormalizeProb:
    def test_integer_one_is_one_percent(self) -> None:
        # Regression for bf1183c: the model's integer 1 means 1%, not 100%.
        assert analyzer._normalize_prob({"probability": 1}) == pytest.approx(0.01)

    def test_fraction_below_one_treated_as_fraction(self) -> None:
        # 0.04 returned in 0-1 space => 4%.
        assert analyzer._normalize_prob({"probability": 0.04}) == pytest.approx(0.04)

    def test_mid_range_integer_is_percent(self) -> None:
        assert analyzer._normalize_prob({"probability": 55}) == pytest.approx(0.55)

    def test_hundred_is_one(self) -> None:
        assert analyzer._normalize_prob({"probability": 100}) == pytest.approx(1.0)

    def test_clamps_above_hundred(self) -> None:
        assert analyzer._normalize_prob({"probability": 150}) == pytest.approx(1.0)

    def test_clamps_negative(self) -> None:
        assert analyzer._normalize_prob({"probability": -5}) == pytest.approx(0.0)

    def test_string_number_coerced(self) -> None:
        assert analyzer._normalize_prob({"probability": "55"}) == pytest.approx(0.55)


class TestExtractJson:
    def test_plain_json(self) -> None:
        assert analyzer._extract_json('{"probability": 42}') == {"probability": 42}

    def test_json_in_markdown_fence(self) -> None:
        text = 'Here is my answer:\n```json\n{"probability": 42}\n```\nThanks!'
        assert analyzer._extract_json(text) == {"probability": 42}

    def test_greedy_match_spans_nested_braces(self) -> None:
        text = '{"probability": 42, "meta": {"k": "v"}}'
        assert analyzer._extract_json(text) == {"probability": 42, "meta": {"k": "v"}}

    def test_missing_json_raises(self) -> None:
        with pytest.raises(ValueError):
            analyzer._extract_json("no json here at all")


class TestDeriveEdge:
    def test_no_market_price_returns_none_pair(self) -> None:
        assert analyzer._derive_edge(0.7, None) == (None, None)

    def test_within_fair_band_is_fair(self) -> None:
        edge, mag = analyzer._derive_edge(0.50, 0.49)  # 1pp < 3pp band
        assert edge == "fair"
        assert mag == pytest.approx(0.01)

    def test_claude_higher_is_underpriced(self) -> None:
        edge, mag = analyzer._derive_edge(0.70, 0.50)
        assert edge == "underpriced"
        assert mag == pytest.approx(0.20)

    def test_claude_lower_is_overpriced(self) -> None:
        edge, _ = analyzer._derive_edge(0.30, 0.50)
        assert edge == "overpriced"

    def test_just_inside_band_is_fair_just_outside_is_edge(self) -> None:
        # The band is inclusive (<=); check classification straddling it.
        inside, _ = analyzer._derive_edge(0.50 + analyzer.FAIR_BAND - 0.005, 0.50)
        outside, _ = analyzer._derive_edge(0.50 + analyzer.FAIR_BAND + 0.005, 0.50)
        assert inside == "fair"
        assert outside == "underpriced"


class TestParseAnalysis:
    def test_happy_path(self) -> None:
        market = make_market(market_prob=0.50)
        text = (
            '{"probability": 70, "confidence": "high", '
            '"factors": ["a", "b"], "summary": "looks cheap"}'
        )
        a = analyzer._parse_analysis(text, market, model="test-model")
        assert a.claude_prob == pytest.approx(0.70)
        assert a.confidence == "high"
        assert a.edge == "underpriced"
        assert a.model == "test-model"
        assert a.market_prob_at_analysis == pytest.approx(0.50)
        assert a.factors == ["a", "b"]

    def test_invalid_confidence_dropped(self) -> None:
        market = make_market()
        a = analyzer._parse_analysis(
            '{"probability": 50, "confidence": "extreme"}', market, model="m"
        )
        assert a.confidence is None

    def test_factors_capped_at_four(self) -> None:
        market = make_market()
        text = '{"probability": 50, "factors": ["1","2","3","4","5","6"]}'
        a = analyzer._parse_analysis(text, market, model="m")
        assert a.factors == ["1", "2", "3", "4"]

    def test_missing_summary_is_empty_string(self) -> None:
        market = make_market()
        a = analyzer._parse_analysis('{"probability": 50}', market, model="m")
        assert a.summary == ""
