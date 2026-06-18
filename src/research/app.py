"""Flask app + all routes. The HTTP layer — routes call modules, no logic here.

Serves the single-file frontend at ``/`` and a JSON API under ``/api/``. Run with
``PYTHONPATH=src python -m research.app`` (the Makefile's ``run`` target does this).
"""

from __future__ import annotations

import csv
import io
import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_from_directory
from pydantic import ValidationError

from research import analyzer, calibration, db, performance, polymarket, scanner, scheduler
from research.models import Analysis, CalibrationReport, Market, MarketWithAnalysis, ScanRequest

_FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_ALLOWED_ORIGINS = {"http://localhost:5173", "http://localhost:3000"}


def _init_logging() -> None:
    """Route root logging to a size-capped rotating file plus the console.

    Replaces the previous unbounded append (see ROADMAP). Tunable via env: ``LOG_LEVEL``
    (default INFO), ``LOG_MAX_BYTES`` (default 10MB), ``LOG_BACKUP_COUNT`` (default 3), and
    ``LOG_FILE`` (default ``data/app.log``). Called once at import so both the web process
    and the scheduler thread log to file. If the log file can't be opened (e.g. locked by
    another process on Windows), we degrade to console-only rather than failing startup.
    """
    root = logging.getLogger()
    if any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        return  # idempotent — don't stack handlers on re-import
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.setLevel(getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO))
    root.addHandler(console)
    log_path = Path(os.getenv("LOG_FILE") or (_DATA_DIR / "app.log"))
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=int(os.getenv("LOG_MAX_BYTES", str(10_000_000))),
            backupCount=int(os.getenv("LOG_BACKUP_COUNT", "3")),
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError as e:
        root.warning("file logging disabled (could not open %s): %s", log_path, e)


load_dotenv()
_init_logging()
_log = logging.getLogger(__name__)
analyzer.validate_openai_model()  # warn early on a typo'd OPENAI_MODEL (provider-gated)
db.init_db()

app = Flask(__name__)


@app.after_request
def _cors(resp: Response) -> Response:
    """Dev CORS — frontend is same-origin, so this only helps local tooling."""
    origin = request.headers.get("Origin")
    if origin in _ALLOWED_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


# --- frontend ------------------------------------------------------------------


@app.get("/")
def index() -> Any:
    if (_FRONTEND_DIR / "index.html").exists():
        return send_from_directory(_FRONTEND_DIR, "index.html")
    return (
        "<h1>Polymarket Research Copilot</h1>"
        "<p>API is up. The frontend (frontend/index.html) is the next build slice.</p>"
        '<p>Try <a href="/api/health">/api/health</a>.</p>'
    )


# --- API -----------------------------------------------------------------------


@app.get("/api/health")
def health() -> Any:
    return jsonify({"status": "ok"})


@app.get("/api/markets")
def list_markets() -> Any:
    items: list[MarketWithAnalysis] = db.get_markets_with_latest_analysis()

    tag = request.args.get("tag")
    if tag:
        items = [m for m in items if tag in m.market.tags]

    if request.args.get("analyzed_only", "").lower() == "true":
        items = [m for m in items if m.latest_analysis is not None]

    min_div = request.args.get("min_divergence", type=float)
    if min_div is not None:
        items = [
            m
            for m in items
            if m.latest_analysis is not None
            and m.latest_analysis.edge_magnitude is not None
            and m.latest_analysis.edge_magnitude >= min_div
        ]

    return jsonify([m.model_dump(mode="json") for m in items])


@app.get("/api/markets/<market_id>")
def get_market(market_id: str) -> Any:
    market: Market | None = db.get_market(market_id)
    if market is None:
        return jsonify({"error": "market not found"}), 404
    history = db.get_analysis_history(market_id)
    return jsonify(
        {
            "market": market.model_dump(mode="json"),
            "latest_analysis": history[0].model_dump(mode="json") if history else None,
            "analysis_count": len(history),
            "stale": db.is_stale(market, history[0] if history else None),
            "history": [a.model_dump(mode="json") for a in history],
        }
    )


@app.post("/api/markets/<market_id>/analyze")
def analyze(market_id: str) -> Any:
    market = db.get_market(market_id)
    if market is None:
        return jsonify({"error": "market not found"}), 404

    result: Analysis = analyzer.analyze_market(market)
    if result.error:
        # Don't persist failures — keep the append-only history clean; surface the error.
        return jsonify(result.model_dump(mode="json")), 502

    db.save_analysis(result)
    saved = db.get_latest_analysis(market_id)  # round-trip to return the persisted row (with id)
    return jsonify(saved.model_dump(mode="json"))


@app.post("/api/markets/refresh")
def refresh() -> Any:
    max_markets = int(os.getenv("MAX_SCAN_MARKETS", "100"))
    try:
        markets = polymarket.fetch_all_active(max_markets=max_markets)
    except Exception as e:  # noqa: BLE001 — surface upstream failures as JSON, not a 500 page
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 502
    db.upsert_markets(markets)
    resolved = scanner.sweep_resolutions()
    return jsonify({"count": len(markets), "resolved": resolved})


@app.post("/api/scan")
def scan() -> Any:
    body = request.get_json(silent=True) or {}
    try:
        req = ScanRequest(**body)
    except ValidationError as e:
        return jsonify({"error": e.errors()}), 400
    results = scanner.scan(req)
    # Manual scans don't log by default (so UI param-tuning doesn't flood the signal
    # log); opt in with {"log_signals": true}.
    if body.get("log_signals"):
        scanner.persist_signals(results)
    return jsonify([r.model_dump(mode="json") for r in results])


@app.get("/api/calibration")
def calibration_report() -> Any:
    recals = calibration.build_recalibrators()
    reports = [
        CalibrationReport(
            model=r.model, n=r.n, calibrated=r.calibrated, temperature=r.temperature,
            min_n=r.min_n, brier=r.brier, log_loss=r.log_loss, curve=r.curve,
        )
        for r in recals.values()
    ]
    reports.sort(key=lambda x: x.n, reverse=True)
    return jsonify([x.model_dump(mode="json") for x in reports])


@app.get("/api/leaderboard")
def leaderboard() -> Any:
    """Per-model forecasting scorecard (Brier / log-loss / accuracy / Brier skill)."""
    return jsonify(calibration.model_leaderboard())


@app.get("/api/performance")
def performance_report() -> Any:
    """Track record derived from settled forward signals (equity curve + risk/return stats)."""
    return jsonify(performance.report())


@app.get("/api/scan-history")
def scan_history() -> Any:
    try:
        return jsonify(scheduler.history())
    except Exception as e:  # noqa: BLE001 — a dashboard read shouldn't 500
        _log.warning("scan-history read failed: %s", e)
        return jsonify({"total_runs": 0, "last_runs": []})


@app.get("/api/signals")
def signals() -> Any:
    return jsonify({
        "summary": db.signal_summary(),
        "signals": [s.model_dump(mode="json") for s in db.get_signals()],
    })


@app.get("/api/alerts")
def alerts() -> Any:
    try:
        return jsonify(scanner.read_alerts())
    except Exception as e:  # noqa: BLE001 — a dashboard read shouldn't 500
        _log.warning("alerts read failed: %s", e)
        return jsonify([])


@app.get("/api/provider")
def provider() -> Any:
    return jsonify({
        "provider": analyzer.current_provider(),
        "model": analyzer.current_model(),
        "openai_exhausted": analyzer.openai_exhausted(),
    })


@app.post("/api/provider/reset")
def provider_reset() -> Any:
    """Clear the OpenAI exhaustion latch without a server restart."""
    analyzer.reset_openai_exhausted()
    return jsonify({"openai_exhausted": analyzer.openai_exhausted()})


def _days_before_close(created_at: str | None, end_date: str | None) -> str:
    """Forecast horizon in whole days, or "" when either timestamp is missing/unparseable."""
    if not created_at or not end_date:
        return ""
    try:
        return str((datetime.fromisoformat(end_date) - datetime.fromisoformat(created_at)).days)
    except (ValueError, TypeError):
        return ""


@app.get("/api/calibration/export.csv")
def calibration_export() -> Any:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["market_id", "created_at", "model", "claude_prob", "resolution", "days_before_close"])
    for r in db.get_resolved_export_rows():
        res = "" if r["resolution"] is None else (1 if r["resolution"] else 0)
        w.writerow([
            r["market_id"], r["created_at"], r["model"] or "", r["claude_prob"], res,
            _days_before_close(r["created_at"], r["end_date"]),
        ])
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=calibration.csv"},
    )


@app.put("/api/markets/<market_id>/resolution")
def set_resolution(market_id: str) -> Any:
    if db.get_market(market_id) is None:
        return jsonify({"error": "market not found"}), 404
    body = request.get_json(silent=True) or {}
    outcome = body.get("outcome")
    if not isinstance(outcome, bool):
        return jsonify({"error": 'body must be {"outcome": true|false}'}), 400
    rows = db.mark_resolution(market_id, outcome)
    return jsonify({"market_id": market_id, "resolution": outcome, "rows_updated": rows})


if __name__ == "__main__":
    # Start the background auto-scan only when run as the server (never on import),
    # so tests/scripts/test_client don't spawn live scans. (scheduler is imported at
    # module top, but start() is called only here.)
    scheduler.start()
    app.run(host="127.0.0.1", port=5000)
