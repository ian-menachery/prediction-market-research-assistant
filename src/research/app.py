"""Flask app + all routes. The HTTP layer — routes call modules, no logic here.

Serves the single-file frontend at ``/`` and a JSON API under ``/api/``. Run with
``PYTHONPATH=src python -m research.app`` (the Makefile's ``run`` target does this).
"""

from __future__ import annotations

import csv
import io
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_from_directory
from pydantic import ValidationError

from research import analyzer, calibration, db, polymarket, scanner
from research.models import Analysis, CalibrationReport, Market, MarketWithAnalysis, ScanRequest

_FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
_ALLOWED_ORIGINS = {"http://localhost:5173", "http://localhost:3000"}

load_dotenv()
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


def _sweep_resolutions() -> int:
    """Record resolutions for analyzed-but-unresolved markets. One Gamma call per
    market — fine at MVP scale. Resolutions are lost once a market drops out of the
    active fetch, so this runs on every refresh to capture them while they're live.
    """
    resolved = 0
    for market_id in db.get_unresolved_analyzed_market_ids():
        try:
            outcome = polymarket.fetch_resolution(market_id)
        except Exception:  # noqa: BLE001 — transient; next refresh retries
            continue
        if outcome is not None:
            db.mark_resolution(market_id, outcome)
            resolved += 1
    return resolved


@app.post("/api/markets/refresh")
def refresh() -> Any:
    max_markets = int(os.getenv("MAX_SCAN_MARKETS", "100"))
    try:
        markets = polymarket.fetch_all_active(max_markets=max_markets)
    except Exception as e:  # noqa: BLE001 — surface upstream failures as JSON, not a 500 page
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 502
    db.upsert_markets(markets)
    resolved = _sweep_resolutions()
    return jsonify({"count": len(markets), "resolved": resolved})


@app.post("/api/scan")
def scan() -> Any:
    body = request.get_json(silent=True) or {}
    try:
        req = ScanRequest(**body)
    except ValidationError as e:
        return jsonify({"error": e.errors()}), 400
    results = scanner.scan(req)
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


@app.get("/api/provider")
def provider() -> Any:
    return jsonify({
        "provider": analyzer.current_provider(),
        "model": analyzer.current_model(),
        "openai_exhausted": analyzer.openai_exhausted(),
    })


@app.get("/api/calibration/export.csv")
def calibration_export() -> Any:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["market_id", "created_at", "model", "claude_prob", "resolution"])
    for a in db.get_all_resolved_analyses():
        res = "" if a.resolution is None else (1 if a.resolution else 0)
        w.writerow([a.market_id, a.created_at.isoformat(), a.model or "", a.claude_prob, res])
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
    app.run(host="127.0.0.1", port=5000)
