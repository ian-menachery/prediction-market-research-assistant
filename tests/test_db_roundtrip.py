"""DB round-trip + resolution/signal/idempotency tests against a temp SQLite file."""

from __future__ import annotations

from conftest import make_market
from research.models import Analysis, Signal


def test_market_roundtrip(temp_db) -> None:
    db = temp_db
    m = make_market(market_prob=0.62)
    db.upsert_markets([m])
    got = db.get_market(m.id)
    assert got is not None
    assert got.market_prob == 0.62
    assert got.exchange == "polymarket"
    assert got.question == m.question


def test_analysis_roundtrip_and_resolution(temp_db) -> None:
    db = temp_db
    m = make_market(market_prob=0.5)
    db.upsert_markets([m])
    a = Analysis(
        market_id=m.id, model="test-model", claude_prob=0.7,
        market_prob_at_analysis=0.5, confidence="high", edge="underpriced",
        edge_magnitude=0.2, factors=["x", "y"], summary="s",
    )
    rid = db.save_analysis(a)
    assert isinstance(rid, int)

    latest = db.get_latest_analysis(m.id)
    assert latest is not None
    assert latest.claude_prob == 0.7
    assert latest.model == "test-model"
    assert latest.factors == ["x", "y"]
    assert latest.resolved in (None, False)

    db.mark_resolution(m.id, True)
    settled = db.get_latest_analysis(m.id)
    assert settled.resolved is True
    assert settled.resolution is True


def test_analysis_token_usage_roundtrip(temp_db) -> None:
    db = temp_db
    m = make_market(market_prob=0.5)
    db.upsert_markets([m])
    db.save_analysis(Analysis(
        market_id=m.id, model="test-model", claude_prob=0.6,
        input_tokens=1234, output_tokens=567,
        cache_creation_input_tokens=200, cache_read_input_tokens=1800,
        web_search_requests=5,
    ))
    latest = db.get_latest_analysis(m.id)
    assert latest is not None
    assert latest.input_tokens == 1234
    assert latest.output_tokens == 567
    assert latest.cache_creation_input_tokens == 200
    assert latest.cache_read_input_tokens == 1800
    assert latest.web_search_requests == 5


def test_analysis_history_newest_first(temp_db) -> None:
    db = temp_db
    m = make_market()
    db.upsert_markets([m])
    db.save_analysis(Analysis(market_id=m.id, model="a", claude_prob=0.4))
    db.save_analysis(Analysis(market_id=m.id, model="b", claude_prob=0.6))
    hist = db.get_analysis_history(m.id)
    assert [h.claude_prob for h in hist] == [0.6, 0.4]


def test_signal_roundtrip_and_settlement(temp_db) -> None:
    db = temp_db
    m = make_market()
    db.upsert_markets([m])
    sig = Signal(
        market_id=m.id, question="Q?", side="YES", calibrated_prob=0.7,
        market_prob=0.5, price_paid=0.55, fill_shares=90.0, target_position_usd=50.0,
    )
    sid = db.save_signal(sig)
    assert len(db.get_open_signals_for_market(m.id)) == 1

    db.resolve_signal(sid, True, 40.5)
    assert db.get_open_signals_for_market(m.id) == []  # no longer open
    resolved = [s for s in db.get_signals() if s.id == sid][0]
    assert resolved.resolved is True
    assert resolved.pnl == 40.5
    assert isinstance(db.signal_summary(), dict)


def test_stale_flag_computed(temp_db) -> None:
    db = temp_db
    m = make_market(market_prob=0.80)  # current price
    db.upsert_markets([m])
    # snapshot at 0.50 -> moved 30pp -> stale
    db.save_analysis(Analysis(
        market_id=m.id, model="t", claude_prob=0.6, market_prob_at_analysis=0.50,
    ))
    row = next(r for r in db.get_markets_with_latest_analysis() if r.market.id == m.id)
    assert row.stale is True
    assert row.analysis_count == 1


def test_init_db_idempotent(temp_db) -> None:
    # temp_db already ran init_db once; calling again must not raise (idempotent migrations).
    temp_db.init_db()
    temp_db.init_db()
