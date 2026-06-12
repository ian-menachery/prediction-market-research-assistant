"""Claude analysis engine. The only module that calls the Anthropic API.

Takes a normalized ``Market`` and asks Claude — with web search — to estimate the
YES probability, then returns an ``Analysis`` comparing Claude's estimate to the
live market price. The ``edge`` label is derived deterministically here from the
two probabilities (3pp rule), not taken from Claude's self-report.

This function never raises: on any failure it returns an ``Analysis`` carrying
only ``market_id`` + ``error``, because the batch scanner depends on graceful
degradation.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, get_args

import anthropic
from dotenv import load_dotenv

from research.models import Analysis, Confidence, Edge, Market

DEFAULT_MODEL = "claude-sonnet-4-6"
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}
MAX_TOKENS = 2000
FAIR_BAND = 0.03  # within 3 percentage points (0-1 space) counts as "fair"
_MAX_RETRIES = 3
_MAX_PAUSE_CONTINUATIONS = 3

_VALID_CONFIDENCE = set(get_args(Confidence))

SYSTEM_PROMPT = (
    "You are a calibrated prediction market analyst. Use web search to research "
    "the question, then respond ONLY with valid JSON — no markdown, no backticks:\n"
    '{"probability":NUMBER,"confidence":"low"|"medium"|"high",'
    '"edge":"underpriced"|"overpriced"|"fair",'
    '"factors":["...","...","..."],"summary":"2-3 sentences"}\n\n'
    "probability = integer 0-100 for YES. edge = whether the current market price "
    "is underpriced (your estimate is higher), overpriced (your estimate is lower), "
    "or fair (within 3pp). confidence = quality of information you found."
)

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    """Lazy singleton. Loads .env once; SDK reads ANTHROPIC_API_KEY from env."""
    global _client
    if _client is None:
        load_dotenv()
        _client = anthropic.Anthropic()
    return _client


def _user_prompt(market: Market) -> str:
    if market.market_prob is not None:
        price_line = f"Current market YES probability: {round(market.market_prob * 100)}%"
    else:
        price_line = "Current market YES probability: unknown"
    closes = market.end_date.date().isoformat() if market.end_date else "unknown"
    context = f"\nContext: {market.description[:400]}" if market.description else ""
    return (
        f'Market: "{market.question}"\n'
        f"{price_line}\n"
        f"Closes: {closes}"
        f"{context}\n\n"
        "Search for current information and give your calibrated probability estimate."
    )


def _extract_json(text: str) -> dict:
    """Pull the JSON object out of a text block. Raises ValueError if absent.

    Greedy match handles Claude wrapping JSON in markdown fences despite the prompt.
    """
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object found in response: {text[:200]!r}")
    return json.loads(match.group(0))


def _normalize_prob(result: dict) -> float:
    """Claude returns 0-100 (occasionally 0-1). Return a clamped 0-1 float."""
    raw = float(result["probability"])
    pct = raw * 100 if raw <= 1 else raw
    pct = max(0.0, min(100.0, pct))
    return pct / 100.0


def _derive_edge(
    claude_prob: float, market_prob: float | None
) -> tuple[Edge | None, float | None]:
    """Authoritative edge + magnitude from the two probabilities (3pp rule).

    underpriced = Claude higher than market (YES looks cheap → buy YES);
    overpriced = Claude lower; fair = within FAIR_BAND. None if no market price.
    """
    if market_prob is None:
        return None, None
    magnitude = abs(claude_prob - market_prob)
    if magnitude <= FAIR_BAND:
        edge: Edge = "fair"
    elif claude_prob > market_prob:
        edge = "underpriced"
    else:
        edge = "overpriced"
    return edge, magnitude


def _parse_analysis(text: str, market: Market) -> Analysis:
    """Turn Claude's final text block into an Analysis. Pure + testable."""
    result = _extract_json(text)
    claude_prob = _normalize_prob(result)
    edge, magnitude = _derive_edge(claude_prob, market.market_prob)

    raw_conf = result.get("confidence")
    confidence: Confidence | None = raw_conf if raw_conf in _VALID_CONFIDENCE else None

    factors = result.get("factors") or []
    factors = [str(f) for f in factors][:4]

    return Analysis(
        market_id=market.id,
        claude_prob=claude_prob,
        confidence=confidence,
        edge=edge,
        edge_magnitude=magnitude,
        factors=factors,
        summary=str(result.get("summary") or ""),
    )


def _last_text(response: Any) -> str:
    """The final text block (web_search emits tool blocks before it)."""
    texts = [b.text for b in response.content if b.type == "text"]
    if not texts:
        raise ValueError("No text block in Claude response")
    return texts[-1]


def _create_message(messages: list[dict]) -> Any:
    """One messages.create call with synchronous 429 backoff."""
    model = os.getenv("ANALYSIS_MODEL", DEFAULT_MODEL)
    for attempt in range(_MAX_RETRIES):
        try:
            return _get_client().messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                tools=[WEB_SEARCH_TOOL],
                system=SYSTEM_PROMPT,
                messages=messages,
            )
        except anthropic.RateLimitError:
            if attempt == _MAX_RETRIES - 1:
                raise
            time.sleep(2**attempt * 5)
    raise RuntimeError("unreachable")  # pragma: no cover


def _call_claude(market: Market) -> str:
    """Run the analysis call, resuming through server-tool pause_turns."""
    messages: list[dict] = [{"role": "user", "content": _user_prompt(market)}]
    response = _create_message(messages)
    continuations = 0
    while response.stop_reason == "pause_turn" and continuations < _MAX_PAUSE_CONTINUATIONS:
        # Re-send user + assistant turns; the server resumes the tool loop.
        messages = [
            {"role": "user", "content": _user_prompt(market)},
            {"role": "assistant", "content": response.content},
        ]
        response = _create_message(messages)
        continuations += 1
    return _last_text(response)


def analyze_market(market: Market) -> Analysis:
    """Analyze one market with Claude + web search. Never raises."""
    try:
        text = _call_claude(market)
        return _parse_analysis(text, market)
    except Exception as e:  # noqa: BLE001 — scanner needs graceful degradation
        return Analysis(market_id=market.id, error=f"{type(e).__name__}: {e}")
