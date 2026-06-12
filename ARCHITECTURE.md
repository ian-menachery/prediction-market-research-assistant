# Architecture

## Data flow
```
Polymarket Gamma API
        ↓  httpx (sync, paginated)
  polymarket.py
  (normalize, type, paginate)
        ↓
  SQLite: markets table
        ↓  on demand or batch
   analyzer.py
   (Claude claude-sonnet-4-20250514 + web_search)
        ↓
  SQLite: analyses table
        ↓
  Flask routes (/api/*, JSON)
        ↓
  Single-file React frontend (served by Flask, React via CDN — no build step)
  (MarketList, MarketCard, ScannerView)
```

---

## Backend components

### `polymarket.py` — Market fetcher
Fetches and normalizes Polymarket markets.

Uses a synchronous `httpx.Client()` — no `asyncio`.

```python
def fetch_markets(limit: int = 50, offset: int = 0, tag: str | None = None) -> list[Market]
def fetch_all_active(max_markets: int = 500) -> list[Market]
def get_market(market_id: str) -> Market | None
```

Key normalization behavior:
- Parse `outcomePrices` and `outcomes` from JSON strings to Python lists
- Convert prices from string to float
- `market_prob` = `float(outcomePrices[0])` for binary Yes/No markets
- Tag list = `[t["label"] for t in tags]`
- If prices are missing or malformed, set `market_prob = None`

---

### `analyzer.py` — Claude analysis engine
Calls Claude with web_search and extracts structured analysis.

Uses a synchronous `anthropic.Anthropic()` client.

```python
def analyze_market(market: Market) -> Analysis
```

Model: `claude-sonnet-4-20250514`
Tool: `{"type": "web_search_20250305", "name": "web_search"}`

**System prompt:**
```
You are a calibrated prediction market analyst. Your job is to estimate
the probability of the YES outcome for a Polymarket question using current
web information.

Respond ONLY with valid JSON — no markdown, no backticks, no explanation:
{"probability":NUMBER,"confidence":"low"|"medium"|"high","edge":"underpriced"|"overpriced"|"fair","factors":["...","...","..."],"summary":"2-3 sentences"}

probability = integer 0-100 (your YES estimate).
confidence = how strong your information base is.
edge = whether market price is underpriced (you estimate higher), overpriced
(you estimate lower), or fair (within 3pp of your estimate).
factors = 2-4 short strings naming key drivers.
```

**User prompt template:**
```
Market: "{market.question}"
Current market YES probability: {round(market.market_prob * 100)}%
Closes: {formatted end date}
{Context: description if available}

Search for current information and give your calibrated probability estimate.
```

**Response parsing:**
```python
text_blocks = [b.text for b in response.content if b.type == "text"]
raw = text_blocks[-1] if text_blocks else ""
match = re.search(r'\{.*\}', raw, re.DOTALL)  # greedy, handles nested arrays
result = json.loads(match.group(0))
# Normalize probability: if 0-1, multiply by 100
prob = float(result["probability"])
if 0 <= prob <= 1:
    prob *= 100
prob = max(0, min(100, prob))
```

Error handling: return `Analysis` with `error` field if API call or parse fails. Never raise — scanner depends on graceful degradation.

---

### `db.py` — Storage layer
All database access. Stdlib `sqlite3` (synchronous) throughout — no `aiosqlite`.

```python
def init_db() -> None                                              # called on app startup
def upsert_markets(markets: list[Market]) -> None                  # INSERT OR REPLACE
def save_analysis(analysis: Analysis) -> int                       # INSERT, returns row id
def get_latest_analysis(market_id: str) -> Analysis | None
def get_analysis_history(market_id: str) -> list[Analysis]         # newest first
def get_markets_with_latest_analysis() -> list[MarketWithAnalysis]
def get_all_resolved_analyses() -> list[Analysis]                  # for calibration
def mark_resolution(market_id: str, outcome: bool) -> None         # update all matching rows
def get_analysis_age_hours(market_id: str) -> float | None         # hours since last analysis
```

Database path: `data/polymarket.db` (created if not exists).

---

### `scanner.py` — Batch divergence scanner
Fetches markets, filters, runs analysis, returns sorted divergences.

```python
def scan(
    min_volume_24h: float = 10_000,
    max_age_hours: float = 24.0,       # skip if analyzed within this window
    min_divergence: float = 0.05,      # only return if |claude - market| >= this
    category: str | None = None,       # filter by tag label
    max_markets: int = 100,
) -> list[ScanResult]
```

Implementation: sequential, no `asyncio`. Rate-limit by sleeping between calls.
```python
for market in markets:
    result = analyzer.analyze_market(market)
    time.sleep(float(os.getenv("ANALYSIS_DELAY_SECONDS", "1.5")))
    ...
```

Return: list of `ScanResult` sorted by annualized EV descending. EV is computed from
the **calibrated** probability (see `calibration.py`) and the market mid price; the
`min_divergence` gate applies to the calibrated divergence.

---

### `calibration.py` — Recalibration + metrics (math only)
Pure math, no SQL/HTTP. Reads resolved `(claude_prob, resolution)` pairs via `db`,
writes nothing. Recalibration is **temperature scaling** (one parameter `T`):
`p_cal = sigmoid(logit(p) / T)`, with `T` fit by minimizing log-loss. Applied only
once `CALIBRATION_MIN_N` (default 50) markets have resolved; below that, identity.

```python
def fit_temperature(pairs: list[tuple[float, bool]]) -> float
def brier_score(pairs) -> float
def log_loss(pairs) -> float
def calibration_curve(pairs, bins=10) -> list[dict]
def build_recalibrator() -> Recalibrator   # .apply(p), .calibrated, .n, .temperature, metrics
```

The scanner builds one `Recalibrator` per scan and applies `.apply()` to each
market's `claude_prob` before the EV calc. Raw `claude_prob` stays immutable (it is
the calibration source).

---

### `app.py` — Flask app

Synchronous Flask. All API routes are under `/api/` and return JSON; `/` serves
the single-file frontend.

```
GET  /                             → serve frontend/index.html
GET  /api/health                   → {"status": "ok"}
GET  /api/markets                  → list[MarketWithAnalysis]
     ?tag=Politics                 filter by tag
     ?analyzed_only=true           only markets with at least one analysis
     ?min_divergence=0.05          only markets where latest divergence >= threshold
GET  /api/markets/<id>             → MarketWithAnalysis + full analysis_history
POST /api/markets/<id>/analyze     → Analysis  (runs fresh analysis, saves to DB)
GET  /api/analyses                 → list[Analysis]  (paginated, newest first)
     ?market_id=xxx                filter by market
     ?limit=50&offset=0
POST /api/scan                     → list[ScanResult]
     body: ScanRequest (min_volume_24h, max_age_hours, min_divergence, category, max_markets)
POST /api/markets/refresh          → {"count": N}  (re-fetch markets from Polymarket API)
PUT  /api/markets/<id>/resolution  → Analysis  (mark outcome, body: {"outcome": true|false})
```

CORS: allow `http://localhost:5173` and `http://localhost:3000` in development.
The frontend is same-origin (served by Flask), so CORS only matters for dev tooling.
Startup: call `db.init_db()` once when the app boots.

---

## Frontend components

### `api.ts`
Central file for all backend calls. Base URL from `VITE_API_URL` env var (default `http://localhost:8000`). All fetch calls live here — no inline fetches in components.

### `MarketList`
- Fetches `/markets` on mount and after refresh
- Renders filterable list of `MarketCard`
- Filters: category tag chips, "analyzed only", divergence threshold slider
- Sort: by volume, divergence, time to close
- Refresh button

### `MarketCard`
- Shows: category tag, question, market probability (large), 24h volume, time to close
- If analyzed: Market X% → Claude Y%, divergence badge (±Npp, colored green/red/gray), edge label, factors chips, summary, confidence + disclaimer
- If not analyzed: "Analyze with Claude ↗" button (disabled + spinner while analyzing)
- "Re-analyze" link when analysis exists
- History badge: "analyzed N times" → click to see history modal

### `ScannerView`
- ScanRequest form: min volume, max age, min divergence, category filter, max markets
- "Run scan" button → POST /scan
- Progress indicator during scan (poll or SSE)
- Results table: question (truncated), market%, claude%, divergence, edge, confidence
- Sortable columns
- Click row → expand to show full analysis inline

---

## Database schema

```sql
CREATE TABLE IF NOT EXISTS markets (
    id           TEXT PRIMARY KEY,
    slug         TEXT,               -- builds the polymarket.com trade URL
    question     TEXT    NOT NULL,
    market_prob  REAL,
    volume_24h   REAL,
    volume_total REAL,               -- lifetime volume (Gamma `volume`)
    liquidity    REAL,               -- order-book depth (Gamma `liquidity`)
    yes_token_id TEXT,               -- CLOB token for YES outcome (clobTokenIds[0])
    end_date     TEXT,
    tags         TEXT,               -- JSON array of label strings
    description  TEXT,
    fetched_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS analyses (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id      TEXT    NOT NULL,
    created_at     TEXT    NOT NULL,
    claude_prob    REAL    NOT NULL,    -- 0.0–1.0
    confidence     TEXT,                -- low|medium|high
    edge           TEXT,                -- underpriced|overpriced|fair
    edge_magnitude REAL,                -- abs(claude_prob - market_prob)
    factors        TEXT,                -- JSON array of strings
    summary        TEXT,
    resolved       INTEGER DEFAULT NULL, -- NULL until resolved; 0=no, 1=yes
    resolution     INTEGER DEFAULT NULL, -- NULL until resolved; 0=NO won, 1=YES won
    error          TEXT    DEFAULT NULL, -- non-null if analysis failed
    FOREIGN KEY (market_id) REFERENCES markets(id)
);

CREATE INDEX IF NOT EXISTS idx_analyses_market_id  ON analyses(market_id);
CREATE INDEX IF NOT EXISTS idx_analyses_created_at ON analyses(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_analyses_edge_mag   ON analyses(edge_magnitude DESC);
```
