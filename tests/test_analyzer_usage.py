"""Token-usage capture in the completion functions + analyze_market/refute_edge (no network)."""

from __future__ import annotations

from conftest import make_market

from research import analyzer


class _Usage:
    def __init__(self, i: int, o: int, cc: int = 0, cr: int = 0) -> None:
        self.input_tokens = i
        self.output_tokens = o
        self.cache_creation_input_tokens = cc
        self.cache_read_input_tokens = cr


class _Block:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _NonTextBlock:
    """A non-text content block (server_tool_use / web_search_tool_result) — must be skipped."""

    def __init__(self, block_type: str) -> None:
        self.type = block_type


class _AnthResp:
    def __init__(self, text: str, i: int, o: int, stop: str = "end_turn") -> None:
        self.content = [_Block(text)]
        self.stop_reason = stop
        self.usage = _Usage(i, o)


class _FakeAnthClient:
    def __init__(self, responses: list[_AnthResp]) -> None:
        self._it = iter(responses)
        self.messages = self

    def create(self, **_kw):  # noqa: ANN003 - test stub
        return next(self._it)


class _OAIResp:
    def __init__(self, text: str, i: int, o: int) -> None:
        self.output_text = text
        self.usage = _Usage(i, o)


class _FakeOAIClient:
    def __init__(self, resp: _OAIResp) -> None:
        self._resp = resp
        self.responses = self

    def create(self, **_kw):  # noqa: ANN003 - test stub
        return self._resp


class _ServerToolUse:
    def __init__(self, n: int) -> None:
        self.web_search_requests = n


_JSON = '{"probability": 60, "confidence": "high", "summary": "s", "factors": ["a"]}'


def test_anthropic_usage_sums_across_pause_turn(monkeypatch) -> None:
    r1 = _AnthResp("", 10, 5, stop="pause_turn")  # web-search pause, no final text yet
    r2 = _AnthResp(_JSON, 20, 7, stop="end_turn")
    client = _FakeAnthClient([r1, r2])  # single instance so the response iterator persists
    monkeypatch.setattr(analyzer, "_get_client", lambda: client)
    comp = analyzer._anthropic_complete("sys", "user")
    assert (comp.input_tokens, comp.output_tokens) == (30, 12)  # summed across both calls
    assert "probability" in comp.text  # final text block


def test_openai_usage_captured(monkeypatch) -> None:
    monkeypatch.setattr(analyzer, "_get_openai_client", lambda: _FakeOAIClient(_OAIResp("hi", 11, 22)))
    comp = analyzer._openai_complete("sys", "user")
    assert (comp.text, comp.input_tokens, comp.output_tokens) == ("hi", 11, 22)


def test_usage_tokens_missing_is_zero() -> None:
    assert analyzer._usage_tokens(object()) == (0, 0, 0, 0)


def test_anthropic_captures_cache_tokens(monkeypatch) -> None:
    resp = _AnthResp(_JSON, 50, 20)
    resp.usage = _Usage(50, 20, cc=200, cr=1800)  # a cache write + read on this call
    monkeypatch.setattr(analyzer, "_get_client", lambda: _FakeAnthClient([resp]))
    comp = analyzer._anthropic_complete("sys", "user")
    assert comp.cache_creation_input_tokens == 200
    assert comp.cache_read_input_tokens == 1800


def test_analyze_market_stamps_tokens(monkeypatch) -> None:
    monkeypatch.setattr(analyzer, "_complete", lambda *a, **k: analyzer.Completion(_JSON, 123, 45))
    a = analyzer.analyze_market(make_market(market_prob=0.4))
    assert a.error is None
    assert (a.input_tokens, a.output_tokens) == (123, 45)


def test_web_search_count_extracted() -> None:
    r = _AnthResp(_JSON, 10, 5)
    r.usage.server_tool_use = _ServerToolUse(3)
    assert analyzer._web_search_count(r) == 3


def test_web_search_count_absent_is_zero() -> None:
    assert analyzer._web_search_count(_AnthResp(_JSON, 10, 5)) == 0  # no server_tool_use attr
    assert analyzer._web_search_count(object()) == 0  # no usage at all


def test_anthropic_sums_web_searches_across_pause_turn(monkeypatch) -> None:
    r1 = _AnthResp("", 10, 5, stop="pause_turn")
    r1.usage.server_tool_use = _ServerToolUse(2)
    r2 = _AnthResp(_JSON, 20, 7)
    r2.usage.server_tool_use = _ServerToolUse(1)
    client = _FakeAnthClient([r1, r2])  # single instance so the response iterator persists
    monkeypatch.setattr(analyzer, "_get_client", lambda: client)
    comp = analyzer._anthropic_complete("sys", "user")
    assert comp.web_search_requests == 3  # summed across both rounds


def test_analyze_market_stamps_web_searches(monkeypatch) -> None:
    # Completion(text, input, output, cache_creation, cache_read, web_search_requests)
    monkeypatch.setattr(analyzer, "_complete", lambda *a, **k: analyzer.Completion(_JSON, 1, 1, 0, 0, 4))
    a = analyzer.analyze_market(make_market(market_prob=0.4))
    assert a.web_search_requests == 4


def test_refute_edge_stamps_tokens(monkeypatch) -> None:
    refute_json = '{"probability": 40, "counterpoints": ["c"], "resolution_risk": false, "summary": "s"}'
    monkeypatch.setattr(analyzer, "_complete", lambda *a, **k: analyzer.Completion(refute_json, 9, 3))
    ref = analyzer.refute_edge(make_market(market_prob=0.5), claimed_prob=0.7)
    assert ref.error is None
    assert (ref.input_tokens, ref.output_tokens) == (9, 3)


def test_web_search_shaped_response_parses(monkeypatch) -> None:
    """End-to-end: a realistic Anthropic web-search response (search-activity blocks, then a final
    text block carrying citations + the JSON) must parse to a probability via the existing path."""
    text_block = _Block(_JSON)
    text_block.citations = [{"type": "web_search_result_location", "url": "https://example.com"}]
    resp = _AnthResp(_JSON, 100, 40)
    resp.content = [
        _NonTextBlock("server_tool_use"),       # the web_search invocation
        _NonTextBlock("web_search_tool_result"),  # the returned results (skipped by _last_text)
        text_block,                              # the final answer
    ]
    monkeypatch.setattr(analyzer, "current_provider", lambda: "anthropic")
    monkeypatch.setattr(analyzer, "_get_client", lambda: _FakeAnthClient([resp]))

    a = analyzer.analyze_market(make_market(market_prob=0.4))
    assert a.error is None
    assert a.claude_prob == 0.6  # extracted from the JSON in the final text block
    assert a.model  # records which model produced it (per-model calibration)
