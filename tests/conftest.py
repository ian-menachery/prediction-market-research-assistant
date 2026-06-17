"""Shared test helpers. ``pythonpath = ["src"]`` in pyproject puts ``research`` on path."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from research.models import Market


def make_market(
    *,
    market_prob: float | None = 0.50,
    days_to_close: float | None = 30.0,
    **overrides: object,
) -> Market:
    """A minimal valid Market for pure-logic tests.

    ``days_to_close`` is converted to an absolute ``end_date`` (None to omit).
    """
    end_date = (
        datetime.now(timezone.utc) + timedelta(days=days_to_close)
        if days_to_close is not None
        else None
    )
    fields: dict = {
        "id": "mkt-1",
        "slug": "test-market",
        "question": "Will it happen?",
        "market_prob": market_prob,
        "volume_24h": 10_000.0,
        "end_date": end_date,
        "tags": [],
        "description": "",
    }
    fields.update(overrides)
    return Market(**fields)
