"""Message Batches: db state round-trip + analyzer batch helpers/parsing (mocked, no network)."""

from __future__ import annotations

import pytest
from conftest import make_market

from research import analyzer, scanner
from research.models import Analysis, ScanRequest


# --- db batch-state round-trip -------------------------------------------------


def test_batch_state_roundtrip(temp_db) -> None:
    db = temp_db
    assert db.get_inflight_batch() is None
    db.save_batch("batch_abc", request_count=7)
    inflight = db.get_inflight_batch()
    assert inflight is not None
    assert inflight["id"] == "batch_abc"
    assert inflight["status"] == "submitted"
    assert inflight["request_count"] == 7

    db.mark_batch_ingested("batch_abc")
    assert db.get_inflight_batch() is None  # no longer 'submitted'


# --- analyzer batch helpers (mocked client) ------------------------------------


class _Usage:
    def __init__(self, i: int, o: int, cc: int = 0, cr: int = 0) -> None:
        self.input_tokens = i
        self.output_tokens = o
        self.cache_creation_input_tokens = cc
        self.cache_read_input_tokens = cr


class _TextBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _Msg:
    """A batch result's `.result.message` — same content-block shape as a live response."""

    def __init__(self, text: str, i: int, o: int) -> None:
        self.content = [_TextBlock(text)]
        self.usage = _Usage(i, o)


class _FakeBatches:
    def __init__(self) -> None:
        self.submitted: list = []

    def create(self, requests):  # noqa: ANN001
        self.submitted = list(requests)
        return type("B", (), {"id": "batch_xyz"})()

    def retrieve(self, _id):  # noqa: ANN001
        return type("B", (), {"processing_status": "ended"})()

    def results(self, _id):  # noqa: ANN001
        return iter([])


class _FakeClient:
    def __init__(self) -> None:
        self.messages = type("M", (), {"batches": _FakeBatches()})()


_JSON = '{"probability": 55, "confidence": "medium", "summary": "s", "factors": ["a"]}'


def test_batch_request_params_match_sync_shape() -> None:
    params = analyzer.batch_request_params(make_market(market_prob=0.5))
    assert params["model"]  # ANALYSIS_MODEL or default
    assert params["tools"] == [analyzer.WEB_SEARCH_TOOL]
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert params["messages"][0]["role"] == "user"


def test_submit_and_status(monkeypatch) -> None:
    client = _FakeClient()
    monkeypatch.setattr(analyzer, "_get_client", lambda: client)
    bid = analyzer.submit_batch([{"custom_id": "m1", "params": {"x": 1}}])
    assert bid == "batch_xyz"
    assert client.messages.batches.submitted == [{"custom_id": "m1", "params": {"x": 1}}]
    assert analyzer.batch_status(bid) == "ended"


def test_parse_batch_result_stamps_tokens() -> None:
    a = analyzer.parse_batch_result(_Msg(_JSON, 800, 200), make_market(market_prob=0.4), "claude-sonnet-4-6")
    assert a.error is None
    assert a.claude_prob == 0.55  # parsed from the message's final text block
    assert (a.input_tokens, a.output_tokens) == (800, 200)
    assert a.model == "claude-sonnet-4-6"


# --- scanner batch helpers (offline) -------------------------------------------


class _Result:
    """A batch result entry: `.custom_id` + `.result.type`/`.result.message`."""

    def __init__(self, custom_id: str, message: object, rtype: str = "succeeded") -> None:
        self.custom_id = custom_id
        self.result = type("R", (), {"type": rtype, "message": message})()


def test_build_batch_requests_skips_cache_hits_and_caps(temp_db, monkeypatch) -> None:
    markets = [make_market(id=f"m{i}", market_prob=0.5) for i in range(5)]
    monkeypatch.setattr(scanner.exchanges, "fetch_active", lambda max_markets: markets)
    temp_db.upsert_markets([markets[0]])
    temp_db.save_analysis(Analysis(market_id="m0", model="x", claude_prob=0.7))  # fresh cache hit

    reqs = scanner.build_batch_requests(ScanRequest(max_markets=5, max_age_hours=24, max_llm_calls=2))
    ids = [r["custom_id"] for r in reqs]
    assert "m0" not in ids          # cache hit excluded (no re-spend)
    assert len(reqs) == 2           # capped at max_llm_calls
    assert all("params" in r for r in reqs)


def test_ingest_batch_joins_saves_and_discounts_cost(temp_db, monkeypatch) -> None:
    db = temp_db
    db.upsert_markets([make_market(id="m1", market_prob=0.4), make_market(id="m2", market_prob=0.4)])
    db.save_batch("batch_1", 2)

    monkeypatch.setattr(scanner.analyzer, "batch_status", lambda bid: "ended")
    # Results come back UNORDERED — keyed by custom_id.
    monkeypatch.setattr(scanner.analyzer, "batch_results",
                        lambda bid: iter([_Result("m2", object()), _Result("m1", object())]))
    monkeypatch.setattr(scanner.analyzer, "current_model", lambda: "claude-sonnet-4-6")
    monkeypatch.setattr(scanner.analyzer, "parse_batch_result", lambda msg, m, model: Analysis(
        market_id=m.id, model=model, claude_prob=0.9, market_prob_at_analysis=0.4,
        input_tokens=1_000_000, output_tokens=0,
    ))
    monkeypatch.setattr(scanner.exchanges, "fetch_book", lambda m: None)
    monkeypatch.setattr(scanner, "persist_signals", lambda r: 0)
    monkeypatch.setattr(scanner, "emit_alerts", lambda r: 0)

    stats = scanner.ingest_batch("batch_1")
    assert stats is not None
    assert stats["llm_calls"] == 2
    assert stats["edges"] == 2  # 0.9 vs 0.4 clears the divergence gate for both
    # 2 * 1M input @ $3/1M (sonnet) = $6.00, halved by the batch discount = $3.00
    assert stats["cost_usd"] == pytest.approx(3.0)
    assert db.get_inflight_batch() is None  # marked ingested
    assert db.get_latest_analysis("m1") is not None
    assert db.get_latest_analysis("m2") is not None


def test_ingest_batch_none_while_processing(temp_db, monkeypatch) -> None:
    monkeypatch.setattr(scanner.analyzer, "batch_status", lambda bid: "in_progress")
    assert scanner.ingest_batch("batch_1") is None
