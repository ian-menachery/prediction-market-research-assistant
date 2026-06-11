# Polymarket Research Copilot — CLAUDE.md

## The single most important rule

**Before writing or modifying code, state the plan in 3–5 sentences and wait for confirmation.** No exceptions, even for "small" changes. If I say "go ahead" or "looks good," then implement. If I push back, revise the plan.

This is non-negotiable. If you find yourself drafting code before stating a plan, stop.

## What this is
A local research tool that fetches live Polymarket markets, analyzes them with Claude (web search enabled), and surfaces markets where Claude's probability estimate diverges significantly from the current price. Companion project to the calibration tracker (separate repo).

## Stack (locked)
- `flask`, `httpx` (sync), `anthropic`, `pydantic`, `sqlite3` (stdlib), `python-dotenv`
- No `asyncio`. No `aiosqlite`. No React build pipeline. No Docker. No Postgres.
- New dependencies require asking first.

## Quick start
```bash
make install
cp .env.example .env    # add ANTHROPIC_API_KEY
make run                # → http://localhost:5000
```

## Project structure
```
polymarket-research/
├── CLAUDE.md                 ← you are here
├── ARCHITECTURE.md           ← data flow, DB schema, component specs
├── ROADMAP.md                ← phased feature plan
├── API_REFERENCE.md          ← Polymarket + Anthropic API reference
├── CALIBRATION_NOTES.md      ← integration with calibration tracker
├── src/
│   └── research/
│       ├── models.py         ← Pydantic models
│       ├── db.py             ← sqlite3 access layer (sync only)
│       ├── polymarket.py     ← Polymarket API client (httpx sync)
│       ├── analyzer.py       ← Claude analysis engine
│       ├── scanner.py        ← batch divergence scanner
│       └── app.py            ← Flask app + all routes
├── frontend/
│   └── index.html            ← single file served by Flask at /
├── data/                     ← SQLite DB, gitignored
├── requirements.txt
├── .env.example
└── Makefile
```

## Module boundaries (enforce these)
- All Polymarket API calls live in `polymarket.py`. No `httpx` imports anywhere else.
- All DB operations live in `db.py`. No raw SQL outside that module.
- No business logic in `db.py`. It only reads and writes.
- Flask routes in `app.py` call the modules — they don't contain logic.

## Code conventions
- Type hints on every function in Python
- All probabilities stored as float 0–1 in DB; displayed as % in frontend
- Market IDs are always strings
- Never delete or overwrite analysis records — only append

## Things you should not do
- Do not add infrastructure speculatively (no FastAPI upgrade, no Redis, no Celery)
- Do not "clean up" code I didn't ask you to clean up — mention it, don't fix it silently
- Do not invent data. If an API call fails, surface the error — never fill with zeros
- Do not use `asyncio` anywhere. Use `time.sleep()` for rate limiting.
- Do not hardcode the Anthropic API key

## Things you should do
- After each substantive change, suggest a commit message (short, imperative)
- When you finish a piece of work, summarize what changed in 2–3 sentences
- If a request seems like Phase 2+ work, say so and ask whether to skip ahead

## Phase awareness
Phases are in ROADMAP.md. Always know which phase we're in.
Current phase: **Phase 1 — MVP** (not started).

## Relationship to calibration tracker
This is a separate project. The `polymarket.py` module here borrows patterns from the calibration tracker's `polymarket/` module (API normalization, gotchas) but does not import from it directly. The shared data contract is documented in CALIBRATION_NOTES.md.
