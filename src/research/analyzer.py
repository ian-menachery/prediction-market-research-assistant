"""LLM analysis engine. The only module that calls an LLM provider.

Takes a normalized ``Market`` and asks an LLM — with web search — to estimate the
YES probability, then returns an ``Analysis`` comparing the estimate to the live
market price. ``LLM_PROVIDER`` selects the provider: ``anthropic`` (default, Claude)
or ``openai``. Each ``Analysis`` records which ``model`` produced it so calibration
stays per-model. The ``edge`` label is derived deterministically here (3pp rule),
not taken from the model's self-report.

``analyze_market`` never raises: on any failure it returns an ``Analysis`` carrying
``market_id`` + ``model`` + ``error``, because the batch scanner depends on graceful
degradation. When OpenAI credits run out (``insufficient_quota``) it latches a flag
(surfaced via ``openai_exhausted()``) and returns an explicit error telling the user
to set ``LLM_PROVIDER=anthropic`` — it does NOT silently fall back to Claude.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, get_args

import anthropic
import openai
from dotenv import load_dotenv

from research.models import Analysis, Confidence, Edge, Market

_log = logging.getLogger(__name__)

ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-4-6"
OPENAI_DEFAULT_MODEL = "gpt-5.5"
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}
MAX_TOKENS = 2000
OPENAI_MAX_OUTPUT_TOKENS = 3000
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

_dotenv_loaded = False
_anthropic_client: anthropic.Anthropic | None = None
_openai_client: openai.OpenAI | None = None
_openai_exhausted = False  # latched True once OpenAI returns insufficient_quota


def _ensure_env() -> None:
    global _dotenv_loaded
    if not _dotenv_loaded:
        load_dotenv()
        _dotenv_loaded = True


def current_provider() -> str:
    return os.getenv("LLM_PROVIDER", "anthropic").strip().lower()


def current_model() -> str:
    if current_provider() == "openai":
        return os.getenv("OPENAI_MODEL", OPENAI_DEFAULT_MODEL)
    return os.getenv("ANALYSIS_MODEL", ANTHROPIC_DEFAULT_MODEL)


def openai_exhausted() -> bool:
    return _openai_exhausted


def _get_client() -> anthropic.Anthropic:
    """Lazy Anthropic singleton; SDK reads ANTHROPIC_API_KEY from env."""
    global _anthropic_client
    _ensure_env()
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic()
    return _anthropic_client


def _get_openai_client() -> openai.OpenAI:
    """Lazy OpenAI singleton; SDK reads OPENAI_API_KEY from env."""
    global _openai_client
    _ensure_env()
    if _openai_client is None:
        _openai_client = openai.OpenAI()
    return _openai_client


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


def _parse_analysis(text: str, market: Market, model: str) -> Analysis:
    """Turn a model's text output into an Analysis. Pure + testable.

    ``model`` is stamped on the result so calibration can stay per-model.
    """
    result = _extract_json(text)
    claude_prob = _normalize_prob(result)
    edge, magnitude = _derive_edge(claude_prob, market.market_prob)

    raw_conf = result.get("confidence")
    confidence: Confidence | None = raw_conf if raw_conf in _VALID_CONFIDENCE else None

    factors = result.get("factors") or []
    factors = [str(f) for f in factors][:4]

    return Analysis(
        market_id=market.id,
        model=model,
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
    model = os.getenv("ANALYSIS_MODEL", ANTHROPIC_DEFAULT_MODEL)
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
    """Run the Claude call, resuming through server-tool pause_turns."""
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


def _analyze_anthropic(market: Market) -> Analysis:
    model = os.getenv("ANALYSIS_MODEL", ANTHROPIC_DEFAULT_MODEL)
    return _parse_analysis(_call_claude(market), market, model)


def _is_quota_error(e: Exception) -> bool:
    return getattr(e, "code", None) == "insufficient_quota" or "insufficient_quota" in str(e)


def _analyze_openai(market: Market) -> Analysis:
    """OpenAI Responses API with the web_search tool. Reads response.output_text."""
    model = os.getenv("OPENAI_MODEL", OPENAI_DEFAULT_MODEL)
    try:
        resp = _get_openai_client().responses.create(
            model=model,
            tools=[{"type": "web_search"}],
            instructions=SYSTEM_PROMPT,
            input=_user_prompt(market),
            max_output_tokens=OPENAI_MAX_OUTPUT_TOKENS,
        )
    except openai.RateLimitError as e:
        if _is_quota_error(e):
            global _openai_exhausted
            _openai_exhausted = True
            _log.warning("OpenAI credits exhausted (insufficient_quota); set LLM_PROVIDER=anthropic.")
            return Analysis(
                market_id=market.id,
                model=model,
                error="OPENAI_QUOTA_EXHAUSTED: OpenAI credits are out — set LLM_PROVIDER=anthropic and restart.",
            )
        raise  # transient rate limit → handled by analyze_market's generic catch
    return _parse_analysis(resp.output_text, market, model)


def analyze_market(market: Market) -> Analysis:
    """Analyze one market with the configured provider + web search. Never raises."""
    try:
        if current_provider() == "openai":
            return _analyze_openai(market)
        return _analyze_anthropic(market)
    except Exception as e:  # noqa: BLE001 — scanner needs graceful degradation
        return Analysis(market_id=market.id, model=current_model(), error=f"{type(e).__name__}: {e}")
