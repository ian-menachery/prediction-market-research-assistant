"""One-call live verification of the OpenAI->Anthropic switch (migration Phase 0/1).

Runs a SINGLE real analysis through the configured provider and prints the parsed probability, the
model, the four token counts, and the priced cost. Use it the moment Anthropic credits land to
confirm, for real, three things the offline tests can't:

  - the web-search response parses to a probability        (Phase 0 parser works)
  - cache_read ~ 0 at the current ~300-token prefix        (Phase 1 prediction — caching won't engage)
  - the per-call cost is roughly half the old gpt-5.5 cost  (the real saver is the model switch)

Spends a little money (one analyze). Reads markets from the DB — refresh first if it's empty.

Run:  LLM_PROVIDER=anthropic RESEARCH_DB_PATH=<db> PYTHONPATH=src python scripts/verify_live.py [market_id]
Prereq: the server-side web_search tool is enabled for the org in the Claude Console.
"""

from __future__ import annotations

import sys

from research import analyzer, db, pricing
from research.models import Market


def _pick_market(market_id: str | None) -> Market:
    if market_id:
        m = db.get_market(market_id)
        if m is None:
            raise SystemExit(f"market {market_id!r} not found — run POST /api/markets/refresh first")
        return m
    markets = db.get_markets_with_latest_analysis()
    if not markets:
        raise SystemExit("no markets in the DB — run POST /api/markets/refresh first")
    return markets[0].market


def main() -> None:
    db.init_db()
    market = _pick_market(sys.argv[1] if len(sys.argv) > 1 else None)
    print(f"provider = {analyzer.current_provider()}   model = {analyzer.current_model()}")
    print(f"analyzing: {market.question[:80]!r}")

    a = analyzer.analyze_market(market)  # ONE real, billable LLM call
    if a.error:
        raise SystemExit(f"FAILED: {a.error}")

    cost = pricing.cost_usd(
        a.model, a.input_tokens, a.output_tokens,
        a.cache_creation_input_tokens, a.cache_read_input_tokens,
        web_search_requests=a.web_search_requests,
    )
    cr = a.cache_read_input_tokens or 0
    print("=" * 60)
    print(f"claude_prob   : {a.claude_prob}")
    print(f"model         : {a.model}")
    print(f"input_tokens  : {a.input_tokens}")
    print(f"output_tokens : {a.output_tokens}")
    print(f"cache_read    : {cr}")
    print(f"cache_create  : {a.cache_creation_input_tokens or 0}")
    print(f"web_searches  : {a.web_search_requests or 0}")
    print(f"cost_usd      : ${cost:.4f}  (tokens + web-search fee)")
    print("=" * 60)
    print("cache ENGAGED" if cr > 0
          else "no cache reads — expected at the current ~300-token prefix (Phase 1 prediction holds)")
    assert a.claude_prob is not None, "no probability parsed from the response"


if __name__ == "__main__":
    main()
