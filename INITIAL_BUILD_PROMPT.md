# Initial Build Prompt — Phase 1 MVP

Paste the section below the divider into a new Claude Code session from the project root (after all MD files are in place).

---

## Build: Polymarket Claude Research Copilot — Phase 1

### Context
I'm building a local research tool that fetches live Polymarket markets, analyzes them with Claude (web search enabled), and flags markets where Claude's probability estimate diverges from the current price. This sits alongside my existing calibration tracker (separate project).

Read these docs before writing any code:
- `ARCHITECTURE.md` — full system design, DB schema, component specs
- `API_REFERENCE.md` — Polymarket API fields and Claude API usage
- `CALIBRATION_NOTES.md` — integration points with calibration tracker
- `CLAUDE.md` — code conventions and rules for this project

### Rules (from CLAUDE.md)
1. **State the plan in 3–5 sentences and wait for my confirmation before writing any code.** No exceptions.
2. One module at a time.
3. No new dependencies without asking. Approved stack: `flask`, `httpx`, `pydantic`, `anthropic`, `sqlite3` (stdlib), `python-dotenv`.

### Tech stack
- **Backend**: Python 3.11+, Flask (sync), stdlib `sqlite3`, `httpx` (sync client), `anthropic` SDK
- **Frontend**: single HTML file served by Flask; use the React artifact code from the session as reference for layout/logic, but compile it down to plain HTML + vanilla JS or keep React via CDN — no build pipeline
- **Storage**: SQLite at `data/polymarket.db`
- **No async**: use `httpx.Client()` (sync), `time.sleep()` for rate limiting, stdlib `sqlite3` — no `asyncio`, no `aiosqlite`

### What to borrow from calibration tracker
The calibration tracker at `../calibration-tracker/` has battle-tested code. Before writing any of the following, check if the calibration tracker already has it:
- **Market normalization** (`polymarket/discovery.py`) — the `outcomePrices`/`outcomes`/`clobTokenIds` JSON string parsing logic. Copy the normalization pattern directly.
- **SQLite connection patterns** (`storage/`) — connection context managers, upsert patterns.
- **Pydantic Market model** — adapt the existing model rather than inventing a new one.
- **Tag fetching** (`polymarket/tags.py`) — the category tag logic feeds directly into the new tool's filtering.

### Critical API gotchas (already learned — do not rediscover)
- `outcomePrices`, `outcomes`, `clobTokenIds` from Gamma API are **JSON-encoded strings** inside the JSON response. Must call `json.loads()` on them.
- CLOB `prices-history` endpoint takes a **token ID**, not a slug. Lookup path: slug → Gamma API → `clobTokenIds` → first element = YES token.
- For binary markets: `outcomePrices[0]` = YES probability (as a string like `"0.73"`).

### Project structure
```
polymarket-research/
├── CLAUDE.md
├── ARCHITECTURE.md
├── ROADMAP.md
├── API_REFERENCE.md
├── CALIBRATION_NOTES.md
├── src/
│   └── research/
│       ├── polymarket.py     ← Polymarket API client (borrow from calibration tracker)
│       ├── analyzer.py       ← Claude analysis engine
│       ├── db.py             ← sqlite3 access layer
│       ├── scanner.py        ← batch divergence scanner
│       └── app.py            ← Flask app + all routes
├── frontend/
│   └── index.html            ← single file, served by Flask at /
├── data/                     ← SQLite DB here, gitignored
├── requirements.txt
├── .env.example
└── Makefile
```

### Step 1: Scaffold
Create the directory structure. Initialize:

**`requirements.txt`:**
```
flask>=3.0
httpx>=0.27
anthropic>=0.28
pydantic>=2.0
python-dotenv>=1.0
```

**`.env.example`:**
```
ANTHROPIC_API_KEY=
POLYMARKET_API_BASE=https://gamma-api.polymarket.com
ANALYSIS_DELAY_SECONDS=1.5
MAX_SCAN_MARKETS=100
```

**`Makefile`:**
```makefile
.PHONY: install run

install:
	pip install -r requirements.txt

run:
	python -m research.app
```

### Step 2: Data models (`src/research/models.py`)
Adapt the calibration tracker's existing market model:

```python
from pydantic import BaseModel, Field
from datetime import datetime

class Market(BaseModel):
    id: str
    question: str
    market_prob: float | None       # YES probability 0–1; None if unavailable
    volume_24h: float
    end_date: datetime | None
    tags: list[str]
    description: str
    fetched_at: datetime = Field(default_factory=datetime.utcnow)

class Analysis(BaseModel):
    id: int | None = None
    market_id: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    claude_prob: float              # 0–1
    confidence: str                 # low|medium|high
    edge: str                       # underpriced|overpriced|fair
    edge_magnitude: float           # abs(claude_prob - market_prob)
    factors: list[str]
    summary: str
    resolved: bool | None = None
    resolution: bool | None = None  # True=YES won, False=NO won
    error: str | None = None

class MarketWithAnalysis(BaseModel):
    market: Market
    latest_analysis: Analysis | None
    analysis_count: int

class ScanResult(BaseModel):
    market: Market
    analysis: Analysis

class ScanRequest(BaseModel):
    min_volume_24h: float = 10_000
    max_age_hours: float = 24.0
    min_divergence: float = 0.05
    category: str | None = None
    max_markets: int = 100
```

### Step 3: Database (`src/research/db.py`)
Sync `sqlite3`. Schema from ARCHITECTURE.md. Key rules:
- `upsert_markets`: `INSERT OR REPLACE` — don't duplicate markets
- `save_analysis`: always `INSERT`, never `UPDATE` — preserve history
- `get_analysis_age_hours`: return `None` if never analyzed

Use context managers for connections. No raw SQL outside this module.

### Step 4: Polymarket client (`src/research/polymarket.py`)
**First: check if `../calibration-tracker/src/calibration/polymarket/discovery.py` has usable normalization code.** If so, adapt it — don't rewrite.

Otherwise implement with `httpx.Client()` (sync). Key normalization (from API_REFERENCE.md):
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
        end_date=datetime.fromisoformat(raw["endDate"].replace("Z", "+00:00"))
                 if raw.get("endDate") else None,
        tags=tags,
        description=raw.get("description") or "",
    )
```

Functions needed:
```python
def fetch_markets(limit=50, offset=0, tag=None) -> list[Market]
def fetch_all_active(max_markets=500) -> list[Market]   # paginated
```

### Step 5: Claude analyzer (`src/research/analyzer.py`)
Sync `anthropic.Anthropic()` client.

**System prompt:**
```
You are a calibrated prediction market analyst. Use web search to research
the question, then respond ONLY with valid JSON — no markdown, no backticks:
{"probability":NUMBER,"confidence":"low"|"medium"|"high","edge":"underpriced"|"overpriced"|"fair","factors":["...","...","..."],"summary":"2-3 sentences"}

probability = integer 0-100 for YES. edge = whether the current market price
is underpriced (your estimate is higher), overpriced (your estimate is lower),
or fair (within 3pp). confidence = quality of information you found.
```

**User prompt:**
```
Market: "{market.question}"
Current market YES probability: {round(market.market_prob * 100)}%
Closes: {formatted date or "unknown"}
{f"Context: {market.description[:400]}" if market.description else ""}

Search for current information and give your calibrated probability estimate.
```

**Response parsing** (robust — Claude wraps JSON in markdown sometimes even when told not to):
```python
text_blocks = [b.text for b in response.content if b.type == "text"]
raw = text_blocks[-1] if text_blocks else ""
match = re.search(r'\{.*\}', raw, re.DOTALL)  # greedy
result = json.loads(match.group(0))
prob = float(result["probability"])
if 0 <= prob <= 1:      # normalize if Claude returned 0-1 range
    prob *= 100
prob = max(0, min(100, prob))
```

Never raise — return `Analysis` with `error` field on failure.

### Step 6: Scanner (`src/research/scanner.py`)
Sequential with `time.sleep()` between calls (no async). Skip markets analyzed within `max_age_hours`. Rate limit: `time.sleep(float(os.getenv("ANALYSIS_DELAY_SECONDS", "1.5")))` after each analysis.

```python
def scan(
    min_volume_24h: float = 10_000,
    max_age_hours: float = 24.0,
    min_divergence: float = 0.05,
    category: str | None = None,
    max_markets: int = 100,
) -> list[ScanResult]
```

Return sorted by `edge_magnitude` descending.

### Step 7: Flask app (`src/research/app.py`)
Routes:
```
GET  /                          → serve frontend/index.html
GET  /api/markets               → list[MarketWithAnalysis]  (?tag=, ?min_divergence=)
GET  /api/markets/<id>          → MarketWithAnalysis with full history
POST /api/markets/<id>/analyze  → Analysis  (runs fresh analysis + saves)
POST /api/scan                  → list[ScanResult]  (body: ScanRequest as JSON)
POST /api/markets/refresh       → {"count": N}  (re-fetch from Polymarket API)
GET  /api/health                → {"status": "ok"}
```

CORS: allow localhost:5173 and localhost:3000 for development. Return JSON from all `/api/` routes.

### Step 8: Frontend (`frontend/index.html`)
Single HTML file. Use React via CDN (no build pipeline):
```html
<script src="https://unpkg.com/react@18/umd/react.development.js"></script>
<script src="https://unpkg.com/react-dom@18/umd/react-dom.development.js"></script>
<script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
```

All API calls go to `/api/` (same origin, served by Flask). Match the layout/components from the artifact built in the prior Claude session: MarketCard with market%, claude%, divergence badge (±Npp green/red), factors chips, summary. Tag filter chips. ScannerView with form and results table.

### Step 9: Verify the pipeline
1. `make install`
2. Create `.env` from `.env.example`, add `ANTHROPIC_API_KEY`
3. `make run` — confirm Flask starts at http://localhost:5000
4. `POST /api/markets/refresh` — confirm markets load into DB
5. `POST /api/markets/<id>/analyze` on one market — show me the raw JSON response
6. Confirm: if `market_prob=0.45` and `claude_prob=0.62`, then `edge_magnitude=0.17`, `edge="underpriced"`

### Constraints
- No hardcoded API keys
- All probabilities stored as float 0–1 in DB
- All Polymarket API calls only through `polymarket.py`
- All DB operations only through `db.py`
- No `asyncio` anywhere
- Never overwrite or delete analysis history — only append
- Type hints on every function
