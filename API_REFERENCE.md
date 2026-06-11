# API Reference

## Polymarket Gamma API

**Base URL:** `https://gamma-api.polymarket.com`  
**Auth:** None (public API)  
**Rate limit:** Be conservative — 50 req/min max

---

### GET /markets

Fetch active markets.

```
GET /markets?active=true&closed=false&limit=50&offset=0&order=volumeNum&ascending=false
```

**Query parameters:**

| Param | Type | Notes |
|-------|------|-------|
| `active` | bool | `true` = only active markets |
| `closed` | bool | `false` = exclude resolved/closed |
| `limit` | int | Max 100 per page; use 50 to be safe |
| `offset` | int | Pagination cursor |
| `order` | string | `volumeNum` = sort by volume (recommended) |
| `ascending` | bool | `false` = highest volume first |
| `tag_slug` | string | Filter by category (e.g. `politics`, `crypto`) |

**Key response fields per market:**

```json
{
  "id": "0x1234abc...",
  "question": "Will X happen before Y date?",
  "outcomePrices": "[\"0.73\", \"0.27\"]",
  "outcomes": "[\"Yes\", \"No\"]",
  "volume": "1250000.00",
  "volume24hr": "85000.00",
  "endDate": "2026-11-04T00:00:00Z",
  "tags": [{"id": "abc", "label": "Politics", "slug": "politics"}],
  "description": "This market resolves YES if...",
  "active": true,
  "closed": false,
  "liquidity": "45000.00"
}
```

**Critical normalization notes:**
- `outcomePrices` is a **JSON string** — must be parsed: `json.loads(market["outcomePrices"])`
- `outcomes` and `clobTokenIds` are also JSON strings — same treatment
- After parsing: `prices[0]` = YES probability (0–1 as string), `prices[1]` = NO probability
- Multi-outcome markets: N prices summing to ~1.0; use `prices[0]` for the first/leading outcome
- All prices come as strings — convert with `float()`
- Volume fields are strings — convert with `float()`
- **Date gotcha:** `endDate` occasionally arrives with a bare timezone offset (`+00` instead of
  `+00:00`), which `datetime.fromisoformat()` rejects. Pad bare offsets before parsing and treat
  unparseable values as `None` rather than letting the whole fetch crash. (See the borrowed
  `_normalize_dt` validator in `polymarket.py`.)

**Normalization function:**
```python
def normalize_market(raw: dict) -> Market:
    prices = json.loads(raw.get("outcomePrices") or "[]")
    market_prob = float(prices[0]) if prices else None
    tags = [t["label"] for t in (raw.get("tags") or []) if "label" in t]
    return Market(
        id=raw["id"],
        question=raw["question"],
        market_prob=market_prob,
        volume_24h=float(raw.get("volume24hr") or 0),
        end_date=datetime.fromisoformat(raw["endDate"].replace("Z", "+00:00")) if raw.get("endDate") else None,
        tags=tags,
        description=raw.get("description") or "",
    )
```

**Pagination pattern** (synchronous — no `asyncio`; rate-limiting handled by the client's
`time.sleep` per request):
```python
def fetch_all_active(max_markets: int = 500) -> list[Market]:
    markets: list[Market] = []
    offset = 0
    limit = 50
    with httpx.Client(base_url=BASE_URL, timeout=15.0) as client:
        while len(markets) < max_markets:
            r = client.get(
                "/markets",
                params={"active": "true", "closed": "false", "limit": limit, "offset": offset, "order": "volumeNum", "ascending": "false"},
            )
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            # normalize_market returns None for ineligible rows (e.g. non-binary); drop those.
            markets.extend(m for m in (normalize_market(raw) for raw in batch) if m is not None)
            offset += len(batch)   # page by RAW count, not filtered count
            if len(batch) < limit:  # last page
                break
    return markets
```

---

### Category tag slugs (common)

| Label | Slug |
|-------|------|
| Politics | `politics` |
| Crypto | `crypto` |
| Science & Tech | `science-and-tech` |
| Sports | `sports` |
| Economy | `economics` |
| AI | `ai` |
| Entertainment | `entertainment` |

---

## Anthropic API

**SDK:** `anthropic` (Python)  
**Model:** `claude-sonnet-4-20250514`  
**Web search tool:** `web_search_20250305`

---

### Analysis call

```python
import anthropic
import re, json

client = anthropic.AsyncAnthropic()

response = await client.messages.create(
    model="claude-sonnet-4-20250514",
    max_tokens=1000,
    tools=[{"type": "web_search_20250305", "name": "web_search"}],
    system=SYSTEM_PROMPT,
    messages=[{"role": "user", "content": user_prompt}]
)
```

---

### Response parsing

The response `content` is a list of blocks. Claude may use the web_search tool (producing `tool_use` and `tool_result` blocks) before the final text block. Always take the **last** text block:

```python
text_blocks = [b.text for b in response.content if b.type == "text"]
if not text_blocks:
    raise ValueError("No text in response")
raw = text_blocks[-1]

# Extract JSON robustly — Claude may wrap in markdown even when told not to
match = re.search(r'\{.*\}', raw, re.DOTALL)
if not match:
    raise ValueError(f"No JSON found in: {raw[:200]}")
result = json.loads(match.group(0))
```

---

### Expected JSON schema

```json
{
  "probability": 62,
  "confidence": "medium",
  "edge": "underpriced",
  "factors": [
    "Fed signals dovish shift",
    "Labor market softening",
    "Recent CPI data elevated"
  ],
  "summary": "Current macro data supports a rate cut. Market may be underpricing this given recent Fed communications."
}
```

**Field notes:**
- `probability`: integer 0–100; Claude occasionally returns 0–1 range — normalize with `if prob <= 1: prob *= 100`
- `edge`: `"underpriced"` = your estimate is higher than market (consider buying YES); `"overpriced"` = your estimate is lower (consider buying NO); `"fair"` = within 3pp
- `factors`: keep to 2–4 items max in the prompt — Claude tends to pad otherwise

---

### Rate limits (approximate — check console.anthropic.com for your tier)

| Tier | RPM | TPM |
|------|-----|-----|
| 1 | 50 | 40K |
| 2 | 1000 | 80K |
| 3 | 2000 | 160K |

For batch analysis (scanner), use:
```python
asyncio.Semaphore(5)     # max 5 concurrent analyses
asyncio.sleep(1.5)       # delay between completions
```

This keeps you well within Tier 1 limits and avoids hammering the API.

**Exponential backoff on 429:**
```python
import asyncio

async def with_retry(coro, max_retries=3):
    for attempt in range(max_retries):
        try:
            return await coro
        except anthropic.RateLimitError:
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(2 ** attempt * 5)
```
