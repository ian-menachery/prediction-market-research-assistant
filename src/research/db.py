"""SQLite storage layer. The ONLY module that runs SQL (per CLAUDE.md).

Reads and writes only — no business logic. Datetimes are stored as ISO-8601
strings (UTC) to dodge sqlite3's deprecated datetime adapters in 3.12+; on read
the ISO string is handed to pydantic, which coerces it back to a tz-aware
``datetime``. ``tags``/``factors`` are stored as JSON arrays.

Each public function opens its own short-lived connection (``_conn``): Flask's
dev server is threaded and sqlite3 connections aren't shareable across threads,
so a per-call connection is the safe choice for a local single-user tool.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from research.models import Analysis, Market, MarketWithAnalysis

_SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    id           TEXT PRIMARY KEY,
    slug         TEXT,
    question     TEXT    NOT NULL,
    market_prob  REAL,
    volume_24h   REAL,
    volume_total REAL,
    liquidity    REAL,
    yes_token_id TEXT,
    end_date     TEXT,
    tags         TEXT,                -- JSON array of label strings
    description  TEXT,
    fetched_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS analyses (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id      TEXT    NOT NULL,
    created_at     TEXT    NOT NULL,
    model          TEXT,                -- which LLM produced the estimate (per-model calibration)
    claude_prob    REAL,
    market_prob_at_analysis REAL,       -- market YES mid when the analysis ran (staleness)
    confidence     TEXT,
    edge           TEXT,
    edge_magnitude REAL,
    factors        TEXT,                -- JSON array of strings
    summary        TEXT,
    resolved       INTEGER DEFAULT NULL,
    resolution     INTEGER DEFAULT NULL,
    error          TEXT    DEFAULT NULL,
    FOREIGN KEY (market_id) REFERENCES markets(id)
);

CREATE INDEX IF NOT EXISTS idx_analyses_market_id  ON analyses(market_id);
CREATE INDEX IF NOT EXISTS idx_analyses_created_at ON analyses(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_analyses_edge_mag   ON analyses(edge_magnitude DESC);
"""

_MARKET_COLUMNS = (
    "id, slug, question, market_prob, volume_24h, volume_total, liquidity, "
    "yes_token_id, end_date, tags, description, fetched_at"
)
_ANALYSIS_COLUMNS = (
    "market_id, created_at, model, claude_prob, market_prob_at_analysis, "
    "confidence, edge, edge_magnitude, factors, summary, resolved, resolution, error"
)


def _db_path() -> Path:
    """Resolve the DB path at call time so RESEARCH_DB_PATH overrides are honored."""
    override = os.getenv("RESEARCH_DB_PATH")
    if override:
        return Path(override)
    # src/research/db.py -> parents[2] == project root
    return Path(__file__).resolve().parents[2] / "data" / "polymarket.db"


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    """Open a connection, commit on success, always close."""
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# --- row <-> model mapping (kept in one place) ---------------------------------


def _market_to_row(m: Market) -> tuple:
    return (
        m.id,
        m.slug,
        m.question,
        m.market_prob,
        m.volume_24h,
        m.volume_total,
        m.liquidity,
        m.yes_token_id,
        m.end_date.isoformat() if m.end_date else None,
        json.dumps(m.tags),
        m.description,
        m.fetched_at.isoformat(),
    )


def _row_to_market(row: sqlite3.Row) -> Market:
    data = dict(row)
    data["tags"] = json.loads(data["tags"]) if data["tags"] else []
    return Market(**data)


def _analysis_to_row(a: Analysis) -> tuple:
    return (
        a.market_id,
        a.created_at.isoformat(),
        a.model,
        a.claude_prob,
        a.market_prob_at_analysis,
        a.confidence,
        a.edge,
        a.edge_magnitude,
        json.dumps(a.factors),
        a.summary,
        int(a.resolved) if a.resolved is not None else None,
        int(a.resolution) if a.resolution is not None else None,
        a.error,
    )


def _row_to_analysis(row: sqlite3.Row) -> Analysis:
    data = dict(row)
    data["factors"] = json.loads(data["factors"]) if data["factors"] else []
    return Analysis(**data)


# --- schema --------------------------------------------------------------------


def init_db() -> None:
    """Create tables + indexes if absent, and run idempotent column migrations."""
    with _conn() as conn:
        conn.executescript(_SCHEMA)
        # Ad-hoc migration: add columns to pre-existing tables (CREATE IF NOT EXISTS
        # won't alter them). Pre-existing analysis rows get model = NULL (legacy).
        cols = {row[1] for row in conn.execute("PRAGMA table_info(analyses)").fetchall()}
        if "model" not in cols:
            conn.execute("ALTER TABLE analyses ADD COLUMN model TEXT")
        if "market_prob_at_analysis" not in cols:
            conn.execute("ALTER TABLE analyses ADD COLUMN market_prob_at_analysis REAL")


# --- markets -------------------------------------------------------------------


def upsert_markets(markets: list[Market]) -> None:
    """INSERT OR REPLACE markets so re-fetching doesn't duplicate rows."""
    rows = [_market_to_row(m) for m in markets]
    placeholders = ", ".join(["?"] * 12)
    with _conn() as conn:
        conn.executemany(
            f"INSERT OR REPLACE INTO markets ({_MARKET_COLUMNS}) VALUES ({placeholders})",
            rows,
        )


def get_market(market_id: str) -> Market | None:
    with _conn() as conn:
        row = conn.execute(
            f"SELECT {_MARKET_COLUMNS} FROM markets WHERE id = ?", (market_id,)
        ).fetchone()
    return _row_to_market(row) if row is not None else None


# --- analyses ------------------------------------------------------------------


def save_analysis(analysis: Analysis) -> int:
    """Append an analysis (always INSERT, never UPDATE). Returns the new row id."""
    placeholders = ", ".join(["?"] * 13)
    with _conn() as conn:
        cur = conn.execute(
            f"INSERT INTO analyses ({_ANALYSIS_COLUMNS}) VALUES ({placeholders})",
            _analysis_to_row(analysis),
        )
        return int(cur.lastrowid)


def get_latest_analysis(market_id: str) -> Analysis | None:
    """Most recent analysis for a market (newest = highest autoincrement id)."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM analyses WHERE market_id = ? ORDER BY id DESC LIMIT 1",
            (market_id,),
        ).fetchone()
    return _row_to_analysis(row) if row is not None else None


def get_analysis_history(market_id: str) -> list[Analysis]:
    """All analyses for a market, newest first."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM analyses WHERE market_id = ? ORDER BY id DESC",
            (market_id,),
        ).fetchall()
    return [_row_to_analysis(r) for r in rows]


def get_markets_with_latest_analysis() -> list[MarketWithAnalysis]:
    """All markets, each paired with its latest analysis and total analysis count.

    Three queries (no N+1): markets, latest-per-market, counts-per-market.
    """
    with _conn() as conn:
        market_rows = conn.execute(
            f"SELECT {_MARKET_COLUMNS} FROM markets ORDER BY volume_24h DESC"
        ).fetchall()
        latest_rows = conn.execute(
            "SELECT * FROM analyses WHERE id IN "
            "(SELECT MAX(id) FROM analyses GROUP BY market_id)"
        ).fetchall()
        count_rows = conn.execute(
            "SELECT market_id, COUNT(*) AS n FROM analyses GROUP BY market_id"
        ).fetchall()

    latest = {r["market_id"]: _row_to_analysis(r) for r in latest_rows}
    counts = {r["market_id"]: r["n"] for r in count_rows}
    return [
        MarketWithAnalysis(
            market=_row_to_market(mr),
            latest_analysis=latest.get(mr["id"]),
            analysis_count=counts.get(mr["id"], 0),
        )
        for mr in market_rows
    ]


def get_analysis_age_hours(market_id: str) -> float | None:
    """Hours since the latest analysis for a market; None if never analyzed."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT created_at FROM analyses WHERE market_id = ? ORDER BY id DESC LIMIT 1",
            (market_id,),
        ).fetchone()
    if row is None:
        return None
    created = datetime.fromisoformat(row["created_at"])
    return (datetime.now(timezone.utc) - created).total_seconds() / 3600.0


# --- resolution (calibration) --------------------------------------------------


def mark_resolution(market_id: str, outcome: bool) -> int:
    """Mark every analysis for a market resolved with the given outcome.

    The one sanctioned UPDATE to existing analysis rows — it sets resolution
    metadata only, never the ``claude_prob`` estimate (which stays immutable).
    Returns the number of rows updated.
    """
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE analyses SET resolved = 1, resolution = ? WHERE market_id = ?",
            (1 if outcome else 0, market_id),
        )
        return cur.rowcount


def get_unresolved_analyzed_market_ids() -> list[str]:
    """Market ids with at least one analysis not yet marked resolved.

    Drives the refresh-time resolution sweep — we only need to chase resolutions
    for markets we actually analyzed (those carry the calibration data points).
    """
    with _conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT market_id FROM analyses WHERE resolved IS NULL"
        ).fetchall()
    return [r["market_id"] for r in rows]


def get_resolved_pairs_by_model() -> dict[str, list[tuple[float, bool]]]:
    """Resolved (claude_prob, outcome) pairs grouped by model — calibration dataset.

    Rows with a NULL model (legacy, pre-tagging) are grouped under "unknown".
    """
    with _conn() as conn:
        rows = conn.execute(
            "SELECT model, claude_prob, resolution FROM analyses "
            "WHERE resolved = 1 AND resolution IS NOT NULL AND claude_prob IS NOT NULL"
        ).fetchall()
    out: dict[str, list[tuple[float, bool]]] = {}
    for r in rows:
        out.setdefault(r["model"] or "unknown", []).append(
            (float(r["claude_prob"]), bool(r["resolution"]))
        )
    return out


def get_all_resolved_analyses() -> list[Analysis]:
    """All resolved analyses (for CSV export to the calibration tracker)."""
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM analyses WHERE resolved = 1 ORDER BY id").fetchall()
    return [_row_to_analysis(r) for r in rows]
