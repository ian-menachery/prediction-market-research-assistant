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
from dataclasses import dataclass
from typing import Any, get_args

import anthropic
import openai
from dotenv import load_dotenv

from research.models import Analysis, Confidence, Edge, Market, Refutation

_log = logging.getLogger(__name__)

ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-4-6"
OPENAI_DEFAULT_MODEL = "gpt-5.5"
# Curated allow-list of OpenAI models known to work with the Responses API web_search
# tool. Best-effort and editable — extend as new models ship. Used for a startup warning
# only (never blocks), so an omission costs at most a spurious warning.
KNOWN_OPENAI_MODELS = frozenset({"gpt-5.5", "gpt-5", "gpt-5-mini", "gpt-4.1", "gpt-4o"})
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "2000"))
OPENAI_MAX_OUTPUT_TOKENS = 3000
FAIR_BAND = float(os.getenv("FAIR_BAND", "0.03"))  # within this (0-1 space) counts as "fair"
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
    _ensure_env()  # load .env before reading, so dispatch respects LLM_PROVIDER
    return os.getenv("LLM_PROVIDER", "anthropic").strip().lower()


def _model_for_provider(provider: str) -> str:
    """The configured model name for a provider (mirrors the per-provider env defaults)."""
    if provider == "openai":
        return os.getenv("OPENAI_MODEL", OPENAI_DEFAULT_MODEL)
    return os.getenv("ANALYSIS_MODEL", ANTHROPIC_DEFAULT_MODEL)


def current_model() -> str:
    _ensure_env()
    return _model_for_provider(current_provider())


def openai_exhausted() -> bool:
    return _openai_exhausted


def reset_openai_exhausted() -> None:
    """Clear the OpenAI quota-exhaustion latch (e.g. after credits are topped up).

    The latch (set in ``_provider_error``) blocks silent fallback and normally clears only
    on restart; this lets the running process retry OpenAI without one.
    """
    global _openai_exhausted
    if _openai_exhausted:
        _log.info("OpenAI exhaustion latch cleared; next OpenAI call will retry the provider.")
    _openai_exhausted = False


def validate_openai_model() -> None:
    """Warn (once, at startup) if OPENAI_MODEL is unrecognized — only when it'd be used.

    Provider-gated: silent unless LLM_PROVIDER=openai. Never raises; the goal is to
    surface a likely typo before it becomes an opaque API error mid-scan.
    """
    if current_provider() != "openai":
        return
    model = _model_for_provider("openai")
    if model not in KNOWN_OPENAI_MODELS:
        _log.warning(
            "OPENAI_MODEL %r is not a recognized model; known: %s. "
            "The OpenAI Responses API may reject it.",
            model, sorted(KNOWN_OPENAI_MODELS),
        )


def _cross_model_enabled() -> bool:
    """Cross-model adversarial refutation: the skeptic uses the opposite provider."""
    _ensure_env()
    return os.getenv("CROSS_MODEL_ADVERSARIAL", "").strip().lower() == "true"


def _provider_for_model(model: str | None) -> str:
    """Infer the provider that produced a model. The analysis's stored model is
    authoritative (even for DB-cached analyses from a past provider); ``None`` ->
    the currently-configured provider."""
    if model and model.lower().startswith("claude"):
        return "anthropic"
    if model:
        return "openai"
    return current_provider()


def _opposite_provider(provider: str) -> str:
    return "anthropic" if provider == "openai" else "openai"


def _provider_key_configured(provider: str) -> bool:
    _ensure_env()
    env_var = "OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY"
    return bool(os.getenv(env_var))


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
    # The model is told to return an integer 0-100 (so 1 == 1%). Only treat values
    # strictly below 1 as a 0-1 fraction fallback (e.g. 0.04 -> 4%); otherwise the
    # integer 1 (= 1%) gets wrongly scaled to 100%. Values in [1, 100] are percents.
    pct = raw * 100 if raw < 1 else raw
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
        market_prob_at_analysis=market.market_prob,
        confidence=confidence,
        edge=edge,
        edge_magnitude=magnitude,
        factors=factors,
        summary=str(result.get("summary") or ""),
    )


@dataclass(frozen=True)
class Completion:
    """One LLM completion: the text plus the usage that produced it (for cost accounting).

    ``cache_*`` fields track Anthropic prompt-cache usage (0 on OpenAI, which has no caching). They
    are typically NON-zero on real web-search calls: the ``pause_turn`` loop re-sends the prior
    turn's large ``web_search_tool_result`` blocks, so that context is cached (write @1.25x) and
    read back (@0.1x) across continuations. ``web_search_requests`` counts server-side searches,
    which are billed a per-search fee on top of tokens (see ``pricing.cost_usd``).
    """

    text: str
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    web_search_requests: int = 0


def _usage_tokens(response: Any) -> tuple[int, int, int, int]:
    """(input, output, cache_creation, cache_read) tokens from a response; zeros if usage is absent.

    Anthropic ``Message.usage`` exposes all four (``input_tokens`` is the **uncached** remainder;
    ``cache_creation_input_tokens``/``cache_read_input_tokens`` cover prompt caching). OpenAI's usage
    has only input/output, so the cache fields read as 0. Read defensively so a missing field never
    breaks a scan.
    """
    u = getattr(response, "usage", None)
    if u is None:
        return 0, 0, 0, 0
    return (
        int(getattr(u, "input_tokens", 0) or 0),
        int(getattr(u, "output_tokens", 0) or 0),
        int(getattr(u, "cache_creation_input_tokens", 0) or 0),
        int(getattr(u, "cache_read_input_tokens", 0) or 0),
    )


def _web_search_count(response: Any) -> int:
    """Number of server-side web_search calls in a response (0 if absent).

    Anthropic reports these under ``usage.server_tool_use.web_search_requests``; each is billed a
    per-search fee on top of tokens (priced in ``pricing.cost_usd``), so we capture it for honest
    cost accounting. Read defensively — OpenAI / older responses lack the field.
    """
    u = getattr(response, "usage", None)
    st = getattr(u, "server_tool_use", None) if u is not None else None
    return int(getattr(st, "web_search_requests", 0) or 0) if st is not None else 0


def _last_text(response: Any) -> str:
    """The final answer text block — web_search emits server_tool_use / web_search_tool_result
    blocks (and the final text may carry citations) before it; we want the last ``text`` block."""
    texts = [b.text for b in response.content if b.type == "text"]
    if not texts:
        # Web search ran but the turn ended without a final text answer (e.g. exhausted pause
        # continuations). Caught by analyze_market -> error Analysis (graceful, not a crash).
        raise ValueError("No final text block in Claude response (web search returned no answer)")
    return texts[-1]


def _anthropic_message_params(system: str, messages: list[dict]) -> dict:
    """Shared Anthropic Messages params for an analysis.

    Used by BOTH the synchronous ``_anthropic_complete`` and the batch builder, so a batched
    analysis is byte-identical to a live one (the model behaves the same — important for keeping the
    new model's calibration baseline consistent). The static ``system`` prompt is a cache_control
    block; with tools + system rendering before messages, the breakpoint can cache that prefix. In
    practice caching engages mostly *within* a call: the web-search pause_turn loop re-sends large
    web_search_tool_result blocks that get cached and read back across continuations (see
    _anthropic_complete — cache_read is routinely tens of thousands of tokens on real searches).
    """
    return {
        "model": os.getenv("ANALYSIS_MODEL", ANTHROPIC_DEFAULT_MODEL),
        "max_tokens": MAX_TOKENS,
        "tools": [WEB_SEARCH_TOOL],
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": messages,
    }


def _anthropic_complete(system: str, user: str) -> Completion:
    """Anthropic messages.create with web search, 429 backoff, and pause_turn resume.

    cache_* and web_search usage are summed across pause_turn continuations and logged. cache_read
    is typically >0 on real searches: each continuation re-sends the prior turn's large
    web_search_tool_result blocks, which are read from cache (@0.1x) rather than re-billed at full.
    """
    def create(messages: list[dict]) -> Any:
        for attempt in range(_MAX_RETRIES):
            try:
                return _get_client().messages.create(
                    **_anthropic_message_params(system, messages),  # type: ignore[arg-type]
                    timeout=120.0,
                )
            except (anthropic.RateLimitError, anthropic.APITimeoutError,
                    anthropic.APIConnectionError):
                if attempt == _MAX_RETRIES - 1:
                    raise
                time.sleep(2**attempt * 5)
        raise RuntimeError("unreachable")  # pragma: no cover

    messages: list[dict] = [{"role": "user", "content": user}]
    response = create(messages)
    in_tok, out_tok, cc_tok, cr_tok = _usage_tokens(response)  # summed across pause_turn continuations
    ws_tok = _web_search_count(response)
    continuations = 0
    while response.stop_reason == "pause_turn" and continuations < _MAX_PAUSE_CONTINUATIONS:
        messages = [
            {"role": "user", "content": user},
            {"role": "assistant", "content": response.content},
        ]
        response = create(messages)
        i, o, cc, cr = _usage_tokens(response)
        in_tok += i
        out_tok += o
        cc_tok += cc
        cr_tok += cr
        ws_tok += _web_search_count(response)
        continuations += 1
    # Log the counts so cache + search behavior is observed, not assumed (cache_read>0 => cache hit).
    _log.info(
        "anthropic call: input=%d output=%d cache_read=%d cache_creation=%d web_searches=%d",
        in_tok, out_tok, cr_tok, cc_tok, ws_tok,
    )
    return Completion(_last_text(response), in_tok, out_tok, cc_tok, cr_tok, ws_tok)


def _openai_complete(system: str, user: str) -> Completion:
    """OpenAI Responses API with the web_search tool; returns text + token usage.

    Retries transient rate-limit/timeout/connection errors with the same backoff as the
    Anthropic path. A persistent rate limit (incl. insufficient_quota) is re-raised on the
    last attempt; the caller maps it via _provider_error (which latches openai_exhausted).
    """
    for attempt in range(_MAX_RETRIES):
        try:
            response = _get_openai_client().responses.create(
                model=os.getenv("OPENAI_MODEL", OPENAI_DEFAULT_MODEL),
                tools=[{"type": "web_search"}],  # type: ignore[typeddict-item]  # valid web_search tool at runtime
                instructions=system,
                input=user,
                max_output_tokens=OPENAI_MAX_OUTPUT_TOKENS,
                timeout=120.0,
            )
            in_tok, out_tok, _, _ = _usage_tokens(response)  # OpenAI has no prompt-cache fields
            return Completion(response.output_text, in_tok, out_tok)
        except (openai.RateLimitError, openai.APITimeoutError, openai.APIConnectionError) as e:
            # Quota exhaustion is permanent — don't burn retries (the caller latches it).
            if attempt == _MAX_RETRIES - 1 or _is_quota_error(e):
                raise
            time.sleep(2**attempt * 5)
    raise RuntimeError("unreachable")  # pragma: no cover


def _complete(system: str, user: str, provider: str | None = None) -> Completion:
    """One LLM completion with web search (text + token usage). ``provider`` defaults to the
    configured one; pass it explicitly to target a specific provider (cross-model refutation).
    May raise. The sub-functions pick their own model from env per provider."""
    if provider is None:
        provider = current_provider()
    if provider == "openai":
        return _openai_complete(system, user)
    return _anthropic_complete(system, user)


def _is_quota_error(e: Exception) -> bool:
    return getattr(e, "code", None) == "insufficient_quota" or "insufficient_quota" in str(e)


def _provider_error(e: Exception) -> str:
    """Map an exception to an error string; latch + explicit message on OpenAI quota exhaustion."""
    if _is_quota_error(e):
        global _openai_exhausted
        _openai_exhausted = True
        _log.warning("OpenAI credits exhausted (insufficient_quota); set LLM_PROVIDER=anthropic.")
        return "OPENAI_QUOTA_EXHAUSTED: OpenAI credits are out — set LLM_PROVIDER=anthropic and restart."
    return f"{type(e).__name__}: {e}"


def analyze_market(market: Market) -> Analysis:
    """Analyze one market with the configured provider + web search. Never raises."""
    model = current_model()
    try:
        comp = _complete(SYSTEM_PROMPT, _user_prompt(market))
        analysis = _parse_analysis(comp.text, market, model)
        analysis.input_tokens = comp.input_tokens
        analysis.output_tokens = comp.output_tokens
        analysis.cache_creation_input_tokens = comp.cache_creation_input_tokens
        analysis.cache_read_input_tokens = comp.cache_read_input_tokens
        analysis.web_search_requests = comp.web_search_requests
        return analysis
    except Exception as e:  # noqa: BLE001 — scanner needs graceful degradation
        return Analysis(market_id=market.id, model=model, error=_provider_error(e))


REFUTE_SYSTEM_PROMPT = (
    "You are a skeptical adversary reviewing a prediction-market bet. Assume the analyst may be "
    "overconfident or wrong. Use web search to find evidence the analyst missed, then respond ONLY "
    "with valid JSON — no markdown, no backticks:\n"
    '{"probability":NUMBER,"counterpoints":["...","..."],"resolution_risk":true|false,'
    '"summary":"2-3 sentences"}\n\n'
    "probability = your own integer 0-100 YES estimate AFTER trying to break the edge. "
    "resolution_risk = true if the resolution criteria are ambiguous or could resolve on a "
    "technicality. counterpoints = 2-4 reasons the market price may be right."
)


def _refute_prompt(market: Market, claimed_prob: float) -> str:
    mp = "unknown" if market.market_prob is None else f"{round(market.market_prob * 100)}%"
    closes = market.end_date.date().isoformat() if market.end_date else "unknown"
    context = f"\nContext: {market.description[:400]}" if market.description else ""
    return (
        f'Market: "{market.question}"\n'
        f"Current market YES probability: {mp}\n"
        f"Our analyst estimates {round(claimed_prob * 100)}% for YES.\n"
        f"Closes: {closes}{context}\n\n"
        "Argue why the MARKET price is more likely correct than our analyst, search for "
        "disconfirming evidence, and check the resolution criteria. Then give your own calibrated "
        "YES probability."
    )


def _refuter_target(original_model: str | None) -> tuple[str | None, str]:
    """Pick the (provider, model) for the skeptic.

    Default: the configured provider (same-model refutation). When
    ``CROSS_MODEL_ADVERSARIAL=true``, use the OPPOSITE provider from the analysis so the
    skeptic doesn't share its blind spots — but only if that provider's key is configured;
    otherwise fall back to same-model with a warning. Returns ``provider`` as ``None`` to
    mean "use the configured provider" (so default behavior is byte-for-byte unchanged)."""
    if not _cross_model_enabled():
        return None, current_model()
    orig = _provider_for_model(original_model)
    opp = _opposite_provider(orig)
    if _provider_key_configured(opp):
        return opp, _model_for_provider(opp)
    _log.warning(
        "cross-model adversarial: %s key not configured; falling back to same-model %s",
        opp, orig,
    )
    return orig, _model_for_provider(orig)


def refute_edge(market: Market, claimed_prob: float, original_model: str | None = None) -> Refutation:
    """Skeptical second pass that tries to break an edge. Never raises.

    Returns the refuter's own probability + counterpoints + resolution-risk flag; the
    holds/refuted verdict is derived by the caller (scanner) from refuter_prob vs the market.
    With CROSS_MODEL_ADVERSARIAL=true the skeptic runs on the provider opposite to
    ``original_model`` (the analysis's model). ``refuter_model`` records which model ran.
    """
    provider, model = _refuter_target(original_model)
    try:
        comp = _complete(REFUTE_SYSTEM_PROMPT, _refute_prompt(market, claimed_prob), provider=provider)
        result = _extract_json(comp.text)
        cps = [str(c) for c in (result.get("counterpoints") or [])][:4]
        return Refutation(
            refuter_prob=_normalize_prob(result),
            resolution_risk=bool(result.get("resolution_risk")),
            counterpoints=cps,
            summary=str(result.get("summary") or ""),
            refuter_model=model,
            input_tokens=comp.input_tokens,
            output_tokens=comp.output_tokens,
            cache_creation_input_tokens=comp.cache_creation_input_tokens,
            cache_read_input_tokens=comp.cache_read_input_tokens,
            web_search_requests=comp.web_search_requests,
        )
    except Exception as e:  # noqa: BLE001 — a failed refutation shouldn't kill the scan
        return Refutation(error=_provider_error(e), refuter_model=model)


# --- Message Batches (Anthropic) -----------------------------------------------
# The overnight scan submits one batch instead of N synchronous calls (50% cheaper). web_search and
# prompt caching work in batches unchanged, so the analyze request batches as-is. Synchronous on-demand
# analysis (analyze_market) is untouched. See scanner.build_batch_requests / ingest_batch.


def batch_request_params(market: Market) -> dict:
    """The Anthropic Messages params for analyzing one market — identical to the synchronous path."""
    return _anthropic_message_params(SYSTEM_PROMPT, [{"role": "user", "content": _user_prompt(market)}])


def submit_batch(requests: list[dict]) -> str:
    """Submit a Message Batch (``requests`` are ``{custom_id, params}`` dicts). Returns the batch id."""
    batch = _get_client().messages.batches.create(requests=requests)  # type: ignore[arg-type]
    return batch.id


def batch_status(batch_id: str) -> str:
    """``processing_status`` of a batch — ``"ended"`` when results are ready."""
    return str(_get_client().messages.batches.retrieve(batch_id).processing_status)


def batch_results(batch_id: str) -> Any:
    """Iterable of batch result entries (each has ``.custom_id`` + ``.result``), in any order."""
    return _get_client().messages.batches.results(batch_id)


def parse_batch_result(message: Any, market: Market, model: str) -> Analysis:
    """Turn a succeeded batch result's message into a stamped Analysis (reuses the live parser)."""
    in_tok, out_tok, cc_tok, cr_tok = _usage_tokens(message)
    analysis = _parse_analysis(_last_text(message), market, model)
    analysis.input_tokens = in_tok
    analysis.output_tokens = out_tok
    analysis.cache_creation_input_tokens = cc_tok
    analysis.cache_read_input_tokens = cr_tok
    analysis.web_search_requests = _web_search_count(message)
    return analysis
