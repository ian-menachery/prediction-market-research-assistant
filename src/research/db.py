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

from research.models import Analysis, Market, MarketWithAnalysis, Signal

_SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    id           TEXT PRIMARY KEY,
    exchange     TEXT    NOT NULL DEFAULT 'polymarket',  -- 'polymarket' | 'kalshi'
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
-- latest-per-market (get_markets_with_latest_analysis) and resolution sweeps / calibration reads
CREATE INDEX IF NOT EXISTS idx_analyses_market_latest ON analyses(market_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_analyses_resolved      ON analyses(resolved);

CREATE TABLE IF NOT EXISTS signals (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id           TEXT    NOT NULL,
    exchange            TEXT    NOT NULL DEFAULT 'polymarket',  -- 'polymarket' | 'kalshi'
    question            TEXT    NOT NULL,
    created_at          TEXT    NOT NULL,
    model               TEXT,
    side                TEXT    NOT NULL,    -- 'YES' or 'NO'
    calibrated_prob     REAL    NOT NULL,    -- our estimate on the chosen side at log time
    market_prob         REAL    NOT NULL,    -- market YES mid at log time
    price_paid          REAL    NOT NULL,    -- VWAP fill cost/share on the chosen side
    ev                  REAL,
    ev_pct              REAL,
    kelly               REAL,
    annualized_ev       REAL,
    fill_shares         REAL    NOT NULL,    -- shares the VWAP walk filled toward the target
    target_position_usd REAL    NOT NULL,
    days_to_close       REAL,
    adversarial_verdict TEXT    DEFAULT NULL,  -- 'holds'/'refuted' from refutation, NULL if not run
    refuter_model       TEXT    DEFAULT NULL,
    resolved            INTEGER DEFAULT NULL,
    resolution          INTEGER DEFAULT NULL,  -- 1=YES won, 0=NO won
    pnl                 REAL    DEFAULT NULL,   -- realized $ on resolution (modeled VWAP fill)
    FOREIGN KEY (market_id) REFERENCES markets(id)
);

CREATE INDEX IF NOT EXISTS idx_signals_market_id  ON signals(market_id);
CREATE INDEX IF NOT EXISTS idx_signals_created_at ON signals(created_at DESC);
"""

STALE_THRESHOLD = float(os.getenv("STALE_THRESHOLD", "0.04"))  # price move (0-1) since analysis = stale

_MARKET_COLUMNS = (
    "id, exchange, slug, question, market_prob, volume_24h, volume_total, liquidity, "
    "yes_token_id, end_date, tags, description, fetched_at"
)
_ANALYSIS_COLUMNS = (
    "market_id, created_at, model, claude_prob, market_prob_at_analysis, "
    "confidence, edge, edge_magnitude, factors, summary, resolved, resolution, error"
)
_SIGNAL_COLUMNS = (
    "market_id, exchange, question, created_at, model, side, calibrated_prob, market_prob, "
    "price_paid, ev, ev_pct, kelly, annualized_ev, fill_shares, target_position_usd, "
    "days_to_close, adversarial_verdict, refuter_model, resolved, resolution, pnl"
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
    """Open a connection, commit on success, always close.

    WAL + a 30s busy timeout make the threaded Flask server and the background scheduler
    safe to write concurrently: under WAL readers never block the writer, and busy_timeout
    turns transient write contention into a short wait instead of an immediate
    ``database is locked`` crash. WAL is a persistent property of the DB file; re-setting it
    per connection is cheap and idempotent.
    """
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
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
        m.exchange,
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


def _signal_to_row(s: Signal) -> tuple:
    return (
        s.market_id,
        s.exchange,
        s.question,
        s.created_at.isoformat(),
        s.model,
        s.side,
        s.calibrated_prob,
        s.market_prob,
        s.price_paid,
        s.ev,
        s.ev_pct,
        s.kelly,
        s.annualized_ev,
        s.fill_shares,
        s.target_position_usd,
        s.days_to_close,
        s.adversarial_verdict,
        s.refuter_model,
        int(s.resolved) if s.resolved is not None else None,
        int(s.resolution) if s.resolution is not None else None,
        s.pnl,
    )


def _row_to_signal(row: sqlite3.Row) -> Signal:
    return Signal(**dict(row))


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
        # Dual-exchange: tag pre-existing markets/signals as 'polymarket' (the only
        # source before Kalshi support). New rows set it explicitly via the models.
        market_cols = {row[1] for row in conn.execute("PRAGMA table_info(markets)").fetchall()}
        if "exchange" not in market_cols:
            conn.execute("ALTER TABLE markets ADD COLUMN exchange TEXT NOT NULL DEFAULT 'polymarket'")
        signal_cols = {row[1] for row in conn.execute("PRAGMA table_info(signals)").fetchall()}
        if "exchange" not in signal_cols:
            conn.execute("ALTER TABLE signals ADD COLUMN exchange TEXT NOT NULL DEFAULT 'polymarket'")


# --- markets -------------------------------------------------------------------


def upsert_markets(markets: list[Market]) -> None:
    """INSERT OR REPLACE markets so re-fetching doesn't duplicate rows."""
    rows = [_market_to_row(m) for m in markets]
    placeholders = ", ".join(["?"] * 13)
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


def is_stale(market: Market, latest: Analysis | None) -> bool:
    """True if the current market price moved > STALE_THRESHOLD since ``latest`` ran.

    False when there's no analysis, no at-analysis snapshot (legacy rows), or no
    current price.
    """
    if latest is None or market.market_prob is None or latest.market_prob_at_analysis is None:
        return False
    return abs(market.market_prob - latest.market_prob_at_analysis) > STALE_THRESHOLD


def get_markets_with_latest_analysis() -> list[MarketWithAnalysis]:
    """All markets, each paired with its latest analysis, count, and a stale flag.

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
    out: list[MarketWithAnalysis] = []
    for mr in market_rows:
        mkt = _row_to_market(mr)
        la = latest.get(mkt.id)
        out.append(
            MarketWithAnalysis(
                market=mkt,
                latest_analysis=la,
                analysis_count=counts.get(mkt.id, 0),
                stale=is_stale(mkt, la),
            )
        )
    return out


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


# Parser-corrupted calibration rows: an integer "1" answer (= 1%) that the old
# normalization scaled to claude_prob = 1.0 (100%). Identified by prob == 1.0 with a
# summary that mentions "1%". Excluded from calibration reads so they don't poison the
# metrics — the rows themselves are kept (append-only). See ROADMAP.md. The ESCAPE makes
# the trailing % in "1%" a literal; COALESCE keeps a NULL summary from NULL-ing the whole
# predicate (which would wrongly drop a legitimate claude_prob = 1.0 row).
_NOT_CORRUPTED = "NOT (claude_prob = 1.0 AND COALESCE(summary, '') LIKE '%1\\%%' ESCAPE '\\')"


def get_resolved_pairs_by_model() -> dict[str, list[tuple[float, bool]]]:
    """Resolved (claude_prob, outcome) pairs grouped by model — calibration dataset.

    Rows with a NULL model (legacy, pre-tagging) are grouped under "unknown".
    Parser-corrupted rows (see ``_NOT_CORRUPTED``) are excluded.
    """
    with _conn() as conn:
        rows = conn.execute(
            "SELECT model, claude_prob, resolution FROM analyses "
            "WHERE resolved = 1 AND resolution IS NOT NULL AND claude_prob IS NOT NULL "
            f"AND {_NOT_CORRUPTED}"
        ).fetchall()
    out: dict[str, list[tuple[float, bool]]] = {}
    for r in rows:
        out.setdefault(r["model"] or "unknown", []).append(
            (float(r["claude_prob"]), bool(r["resolution"]))
        )
    return out


def count_analyses() -> int:
    """Total analysis rows — used to measure fresh analyses created during a scan."""
    with _conn() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM analyses").fetchone()[0])


def get_all_resolved_analyses() -> list[Analysis]:
    """All resolved analyses (for CSV export to the calibration tracker)."""
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM analyses WHERE resolved = 1 ORDER BY id").fetchall()
    return [_row_to_analysis(r) for r in rows]


def get_resolved_export_rows() -> list[dict]:
    """Resolved analyses joined to their market's ``end_date`` — drives the calibration CSV.

    Returns plain dicts with the stored ISO ``created_at``/``end_date`` strings so the route
    can derive ``days_before_close`` (forecast horizon) without another lookup. Unfiltered:
    the export is the source of record (the parser-corruption filter is calibration-only).
    """
    with _conn() as conn:
        rows = conn.execute(
            "SELECT a.market_id, a.created_at, a.model, a.claude_prob, a.resolution, "
            "m.end_date AS end_date "
            "FROM analyses a LEFT JOIN markets m ON a.market_id = m.id "
            "WHERE a.resolved = 1 ORDER BY a.id"
        ).fetchall()
    return [dict(r) for r in rows]


# --- signals (forward edge log) ------------------------------------------------


def save_signal(sig: Signal) -> int:
    """Append a forward signal (always INSERT, never UPDATE). Returns the new row id."""
    placeholders = ", ".join(["?"] * 21)
    with _conn() as conn:
        cur = conn.execute(
            f"INSERT INTO signals ({_SIGNAL_COLUMNS}) VALUES ({placeholders})",
            _signal_to_row(sig),
        )
        return int(cur.lastrowid)


def get_open_signal_keys() -> set[tuple[str, str]]:
    """``(market_id, side)`` pairs with an open (unresolved) signal — write-time dedup."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT market_id, side FROM signals WHERE resolved IS NULL"
        ).fetchall()
    return {(r["market_id"], r["side"]) for r in rows}


def get_open_signals_for_market(market_id: str) -> list[Signal]:
    """Open (unresolved) signals for a market — the rows the resolution sweep settles."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE market_id = ? AND resolved IS NULL ORDER BY id",
            (market_id,),
        ).fetchall()
    return [_row_to_signal(r) for r in rows]


def resolve_signal(signal_id: int, outcome: bool, pnl: float) -> None:
    """Settle one open signal: set resolved/resolution/pnl. The one sanctioned UPDATE.

    Never touches the recorded prices/EV — those stay frozen at log time.
    """
    with _conn() as conn:
        conn.execute(
            "UPDATE signals SET resolved = 1, resolution = ?, pnl = ? WHERE id = ?",
            (1 if outcome else 0, pnl, signal_id),
        )


def get_signals(limit: int = 200) -> list[Signal]:
    """Signals newest first, capped at ``limit``."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_signal(r) for r in rows]


def get_resolved_signals() -> list[Signal]:
    """Settled signals (resolved, P&L set) in entry order (id ASC) — the track-record dataset.

    Ascending order so the performance module can walk them into a cumulative-P&L equity curve.
    """
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE resolved = 1 AND pnl IS NOT NULL ORDER BY id"
        ).fetchall()
    return [_row_to_signal(r) for r in rows]


def signal_summary() -> dict:
    """Aggregate counts and realized P&L over all signals (reads only)."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT "
            "COUNT(*) AS total, "
            "SUM(CASE WHEN resolved IS NULL THEN 1 ELSE 0 END) AS open, "
            "SUM(CASE WHEN resolved = 1 THEN 1 ELSE 0 END) AS resolved, "
            "SUM(CASE WHEN resolved = 1 AND pnl > 0 THEN 1 ELSE 0 END) AS wins, "
            "SUM(CASE WHEN resolved = 1 THEN pnl ELSE 0 END) AS realized_pnl, "
            "AVG(ev) AS avg_ev "
            "FROM signals"
        ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "open": int(row["open"] or 0),
        "resolved": int(row["resolved"] or 0),
        "wins": int(row["wins"] or 0),
        "realized_pnl": float(row["realized_pnl"]) if row["realized_pnl"] is not None else 0.0,
        "avg_ev": float(row["avg_ev"]) if row["avg_ev"] is not None else None,
    }
