"""Tests for the Pydantic parsing/validation gotchas in polymarket.py and kalshi.py."""

from __future__ import annotations

import pytest

from research import kalshi, polymarket


def _gamma_raw(**overrides: object) -> dict:
    raw: dict = {
        "id": "0x1",
        "slug": "will-x-happen",
        "question": "Will X happen?",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.62", "0.38"]',
        "clobTokenIds": '["tok-yes", "tok-no"]',
        "endDate": "2026-12-31T00:00:00Z",
        "volume24hr": 5000.0,
    }
    raw.update(overrides)
    return raw


class TestGammaParsing:
    def test_json_string_fields_parsed_to_lists(self) -> None:
        gm = polymarket.GammaMarket.model_validate(_gamma_raw())
        assert gm.outcomes == ["Yes", "No"]
        assert gm.outcome_prices == ["0.62", "0.38"]
        assert gm.clob_token_ids == ["tok-yes", "tok-no"]

    def test_bare_offset_datetime_repaired(self) -> None:
        gm = polymarket.GammaMarket.model_validate(_gamma_raw(endDate="2026-12-31T00:00:00+00"))
        assert gm.end_date is not None
        assert gm.end_date.year == 2026

    def test_garbage_datetime_becomes_none(self) -> None:
        gm = polymarket.GammaMarket.model_validate(_gamma_raw(endDate="not-a-date"))
        assert gm.end_date is None

    def test_z_suffix_datetime_parsed(self) -> None:
        gm = polymarket.GammaMarket.model_validate(_gamma_raw(endDate="2026-06-01T12:00:00Z"))
        assert gm.end_date is not None


class TestNormalizeMarket:
    def test_eligible_binary_normalized(self) -> None:
        m = polymarket.normalize_market(_gamma_raw())
        assert m is not None
        assert m.market_prob == pytest.approx(0.62)
        assert m.yes_token_id == "tok-yes"
        assert m.exchange == "polymarket"

    def test_neg_risk_filtered_out(self) -> None:
        assert polymarket.normalize_market(_gamma_raw(negRisk=True)) is None

    def test_non_binary_filtered_out(self) -> None:
        raw = _gamma_raw(outcomes='["A", "B", "C"]', outcomePrices='["0.3","0.3","0.4"]')
        assert polymarket.normalize_market(raw) is None

    def test_unparseable_price_filtered_out(self) -> None:
        assert polymarket.normalize_market(_gamma_raw(outcomePrices='["n/a", "n/a"]')) is None


class TestKalshiParsing:
    def test_bare_offset_datetime_repaired(self) -> None:
        km = kalshi.KalshiMarket.model_validate(
            {"ticker": "T", "close_time": "2026-12-31T00:00:00+00"}
        )
        assert km.close_time is not None

    def test_garbage_datetime_becomes_none(self) -> None:
        km = kalshi.KalshiMarket.model_validate({"ticker": "T", "close_time": "garbage"})
        assert km.close_time is None

    def test_market_prob_prefers_last_trade(self) -> None:
        km = kalshi.KalshiMarket.model_validate(
            {"ticker": "T", "last_price_dollars": 0.65, "yes_bid_dollars": 0.60,
             "yes_ask_dollars": 0.70}
        )
        assert kalshi._market_prob(km) == pytest.approx(0.65)

    def test_market_prob_uses_mid_when_no_trade(self) -> None:
        km = kalshi.KalshiMarket.model_validate(
            {"ticker": "T", "yes_bid_dollars": 0.40, "yes_ask_dollars": 0.60}
        )
        assert kalshi._market_prob(km) == pytest.approx(0.50)

    def test_sentinel_ask_one_ignored_for_mid(self) -> None:
        # ask=1.0 is a no-ask sentinel; mid must not be used, falls back to bid.
        km = kalshi.KalshiMarket.model_validate(
            {"ticker": "T", "yes_bid_dollars": 0.40, "yes_ask_dollars": 1.0}
        )
        assert kalshi._market_prob(km) == pytest.approx(0.40)

    def test_no_price_returns_none(self) -> None:
        km = kalshi.KalshiMarket.model_validate({"ticker": "T"})
        assert kalshi._market_prob(km) is None
