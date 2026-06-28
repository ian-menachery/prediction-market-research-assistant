"""Tests for Kalshi market eligibility (MVE/parlay + two-sided filter) and /events discovery.

No network: these exercise the pure filters and the events-page flattening against a
fake KalshiClient. The fix these guard: Kalshi's raw /markets listing is ~99%
auto-generated multivariate-event (MVE) parlay markets with no live book, so discovery
goes through /events and eligibility drops MVE/one-sided stragglers.
"""

from __future__ import annotations

import pytest

from research import kalshi


def _raw(**over) -> dict:
    """A real, eligible binary Kalshi market row; override fields per test."""
    base = {
        "ticker": "KXFEDDECISION-28JAN-H25",
        "market_type": "binary",
        "title": "Will the Federal Reserve hike rates by 25bps?",
        "status": "active",
        "yes_bid_dollars": "0.30",
        "yes_ask_dollars": "0.34",
        "last_price_dollars": "0.32",
        "volume_fp": "12000",
        "close_time": "2026-07-01T01:38:00Z",
    }
    base.update(over)
    return base


class TestIsMve:
    def test_detects_selected_legs(self) -> None:
        km = kalshi.KalshiMarket.model_validate(
            _raw(mve_selected_legs=[{"market_ticker": "X-Y", "side": "yes"}])
        )
        assert kalshi._is_mve(km) is True

    def test_detects_collection_ticker(self) -> None:
        km = kalshi.KalshiMarket.model_validate(_raw(mve_collection_ticker="KXMVE...-R"))
        assert kalshi._is_mve(km) is True

    def test_detects_ticker_prefix(self) -> None:
        km = kalshi.KalshiMarket.model_validate(_raw(ticker="KXMVESPORTS-S1-ABC"))
        assert kalshi._is_mve(km) is True

    def test_real_market_is_not_mve(self) -> None:
        assert kalshi._is_mve(kalshi.KalshiMarket.model_validate(_raw())) is False


class TestTwoSidedQuote:
    def test_both_inside_band(self) -> None:
        assert kalshi._has_two_sided_quote(kalshi.KalshiMarket.model_validate(_raw())) is True

    @pytest.mark.parametrize(
        "bid,ask",
        [("0", "0.34"), ("0.30", "1"), ("0", "1"), (None, "0.34"), ("0.30", None)],
    )
    def test_sentinels_and_missing_rejected(self, bid, ask) -> None:
        km = kalshi.KalshiMarket.model_validate(_raw(yes_bid_dollars=bid, yes_ask_dollars=ask))
        assert kalshi._has_two_sided_quote(km) is False


class TestEligibilityAndNormalize:
    def test_real_two_sided_market_normalizes(self) -> None:
        m = kalshi.normalize_market(_raw())
        assert m is not None
        assert m.exchange == "kalshi"
        assert m.market_prob == pytest.approx(0.32)  # last trade preferred

    def test_mve_rejected(self) -> None:
        assert kalshi.normalize_market(_raw(mve_selected_legs=[{"side": "yes"}])) is None

    def test_one_sided_rejected(self) -> None:
        # MVE parlays present exactly like this: no live quote (0 bid / 1 ask sentinels).
        assert kalshi.normalize_market(_raw(yes_bid_dollars="0", yes_ask_dollars="1")) is None

    def test_non_binary_rejected(self) -> None:
        assert kalshi.normalize_market(_raw(market_type="scalar")) is None


class _FakeClient:
    """KalshiClient stand-in returning a fixed /events body for any .get(**params)."""

    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, path, **params):
        return self._body


class TestEventsDiscovery:
    def test_fetch_events_page_flattens_nested_markets(self, monkeypatch) -> None:
        body = {
            "events": [
                {"event_ticker": "E1", "markets": [_raw(ticker="A"), _raw(ticker="B")]},
                {"event_ticker": "E2", "markets": [_raw(ticker="C")]},
                {"event_ticker": "E3"},  # no markets key — tolerated
            ],
            "cursor": "next",
        }
        client = _FakeClient(body)
        raw_markets, cursor = kalshi._fetch_events_page(client, limit=200, cursor=None)
        assert [m["ticker"] for m in raw_markets] == ["A", "B", "C"]
        assert cursor == "next"

    def test_events_fallback_filters_mve_and_volume(self, monkeypatch) -> None:
        # KALSHI_SERIES empty -> fall back to /events discovery.
        monkeypatch.setenv("KALSHI_SERIES", "")
        body = {
            "events": [
                {
                    "markets": [
                        _raw(ticker="GOOD", volume_fp="9000"),
                        _raw(ticker="LOWVOL", volume_fp="100"),  # below floor
                        _raw(ticker="KXMVE-PARLAY", mve_collection_ticker="C"),  # MVE
                        _raw(ticker="ONESIDED", yes_bid_dollars="0", yes_ask_dollars="1"),
                    ]
                }
            ],
            "cursor": "",  # single page
        }
        monkeypatch.setattr(kalshi, "KalshiClient", lambda *a, **k: _FakeClient(body))
        out = kalshi.fetch_all_active(max_markets=50, min_volume=5000.0, max_pages=3)
        assert [m.id for m in out] == ["GOOD"]

    def test_series_discovery_filters_and_sorts_by_close(self, monkeypatch) -> None:
        # One series; the fake returns a /markets-shaped body. Near-dated sorts first; MVE/low-vol dropped.
        monkeypatch.setenv("KALSHI_SERIES", "KXTEST")
        body = {
            "markets": [
                _raw(ticker="FAR", volume_fp="9000", close_time="2030-01-01T00:00:00Z"),
                _raw(ticker="NEAR", volume_fp="9000", close_time="2026-07-01T00:00:00Z"),
                _raw(ticker="LOWVOL", volume_fp="100", close_time="2026-07-01T00:00:00Z"),
                _raw(ticker="KXMVE-X", mve_collection_ticker="C", volume_fp="9000"),
            ],
            "cursor": "",
        }
        monkeypatch.setattr(kalshi, "KalshiClient", lambda *a, **k: _FakeClient(body))
        out = kalshi.fetch_all_active(max_markets=50, min_volume=5000.0)
        assert [m.id for m in out] == ["NEAR", "FAR"]  # near-dated first, MVE + low-vol dropped
