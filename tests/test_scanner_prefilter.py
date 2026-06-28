"""Tests for the scanner pre-filter, focused on the max_days_to_close gate (resolve-fast bias)."""

from __future__ import annotations

from conftest import make_market

from research.models import ScanRequest
from research.scanner import _passes_pre

KALSHI_MIN = 5000.0


def _req(**over) -> ScanRequest:
    return ScanRequest(**over)


def test_no_max_cap_by_default() -> None:
    # max_days_to_close defaults to 0 (no cap): a far-dated market still passes.
    m = make_market(days_to_close=120.0, volume_24h=10_000.0)
    assert _passes_pre(m, _req(), KALSHI_MIN) is True


def test_within_max_cap_passes() -> None:
    m = make_market(days_to_close=10.0, volume_24h=10_000.0)
    assert _passes_pre(m, _req(max_days_to_close=30.0), KALSHI_MIN) is True


def test_beyond_max_cap_rejected() -> None:
    m = make_market(days_to_close=60.0, volume_24h=10_000.0)
    assert _passes_pre(m, _req(max_days_to_close=30.0), KALSHI_MIN) is False


def test_min_days_floor_still_applies() -> None:
    m = make_market(days_to_close=0.5, volume_24h=10_000.0)
    assert _passes_pre(m, _req(min_days_to_close=1.0, max_days_to_close=30.0), KALSHI_MIN) is False
