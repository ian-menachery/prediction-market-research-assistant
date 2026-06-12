"""Background auto-scan scheduler (stdlib threading.Timer — no APScheduler).

When ``SCAN_INTERVAL_HOURS`` is set (> 0), runs a full ``scanner.scan()`` plus a
``scanner.sweep_resolutions()`` on that interval, appending one JSON line per run to
``data/scan_log.jsonl``. This drives the calibration flywheel (accumulate analyses,
capture resolutions) without manual refreshes.

Started only from ``app.py``'s ``__main__`` block — never on import — so importing the
app for tests/scripts/test_client never spawns live scans. Each run spends LLM budget
(bounded by ``MAX_SCAN_MARKETS``) and uses the configured ``LLM_PROVIDER``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from research import db, scanner
from research.models import ScanRequest

_log = logging.getLogger(__name__)
_timer: threading.Timer | None = None
_resolution_timer: threading.Timer | None = None
# Serializes DB-writing runs so the scan timer's end-of-run sweep and the resolution
# timer's sweep never write sqlite concurrently ("database is locked"). A long scan just
# makes a due sweep wait — intended.
_run_lock = threading.Lock()


def _scan_log_path() -> Path:
    override = os.getenv("SCAN_LOG_PATH")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "data" / "scan_log.jsonl"


def _build_request() -> ScanRequest:
    # Defaults for the gates; no refutation on autoscan (refute_top=0) to bound cost.
    return ScanRequest(max_markets=int(os.getenv("MAX_SCAN_MARKETS", "100")))


def run_once() -> dict:
    """Run one scan + resolution sweep, append a JSON log line, return the record.

    Never raises: scan and sweep are independently guarded so one failing neither
    skips the other nor kills the scheduler.
    """
    errors: list[str] = []

    with _run_lock:  # serialize DB-writing runs (vs. the resolution-sweep timer)
        before = db.count_analyses()
        edges_found = 0
        try:
            edges_found = len(scanner.scan(_build_request()))
        except Exception as e:  # noqa: BLE001
            errors.append(f"scan: {type(e).__name__}: {e}")
        markets_scanned = max(0, db.count_analyses() - before)

        resolutions_captured = 0
        try:
            resolutions_captured = scanner.sweep_resolutions()
        except Exception as e:  # noqa: BLE001
            errors.append(f"sweep: {type(e).__name__}: {e}")

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "markets_scanned": markets_scanned,
        "edges_found": edges_found,
        "resolutions_captured": resolutions_captured,
        "errors": errors,
    }
    try:
        path = _scan_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:  # noqa: BLE001 — logging failure must not kill the run
        _log.warning("scan_log write failed: %s", e)

    _log.info(
        "auto-scan: scanned=%d edges=%d resolutions=%d errors=%d",
        markets_scanned, edges_found, resolutions_captured, len(errors),
    )
    return record


def run_sweep_once() -> int:
    """Run one resolution sweep (no scan), return the count. Never raises.

    Cheap (Gamma only, no LLM spend) so it can run far more often than full scans.
    Logs to the app logger only — it writes NO line to scan_log.jsonl, so the
    scan-history aggregate keeps meaning "full scan runs."
    """
    resolutions_captured = 0
    with _run_lock:  # don't write the DB while a scan/sweep is in flight
        try:
            resolutions_captured = scanner.sweep_resolutions()
        except Exception as e:  # noqa: BLE001 — a failed sweep must not kill the scheduler
            _log.warning("auto-resolution sweep failed: %s: %s", type(e).__name__, e)
            return 0
    _log.info("auto-resolution sweep: resolutions=%d", resolutions_captured)
    return resolutions_captured


def history(last_n: int = 10) -> dict:
    """Aggregate data/scan_log.jsonl. Missing file → empty aggregate (graceful)."""
    path = _scan_log_path()
    records: list[dict] = []
    if path.exists():
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except (json.JSONDecodeError, ValueError):
                    continue  # skip a malformed / partial line
    n = len(records)
    if n == 0:
        return {
            "total_runs": 0,
            "avg_edges_per_run": 0.0,
            "avg_markets_scanned": 0.0,
            "total_resolutions_captured": 0,
            "last_runs": [],
        }
    edges = sum(r.get("edges_found", 0) for r in records)
    scanned = sum(r.get("markets_scanned", 0) for r in records)
    return {
        "total_runs": n,
        "avg_edges_per_run": round(edges / n, 2),
        "avg_markets_scanned": round(scanned / n, 2),
        "total_resolutions_captured": sum(r.get("resolutions_captured", 0) for r in records),
        "last_runs": records[-last_n:][::-1],  # newest first
    }


def _hours_env_seconds(var: str) -> float:
    try:
        return float(os.getenv(var) or 0) * 3600.0
    except ValueError:
        return 0.0


def _interval_seconds() -> float:
    return _hours_env_seconds("SCAN_INTERVAL_HOURS")


def _resolution_interval_seconds() -> float:
    return _hours_env_seconds("AUTO_RESOLUTION_INTERVAL_HOURS")


def _tick() -> None:
    try:
        run_once()
    finally:
        # Re-arm AFTER the run so cycles never overlap (period ~= interval + run time).
        _arm(_interval_seconds())


def _resolution_tick() -> None:
    try:
        run_sweep_once()
    finally:
        _arm_resolution(_resolution_interval_seconds())


def _arm(interval_s: float) -> None:
    global _timer
    _timer = threading.Timer(interval_s, _tick)
    _timer.daemon = True
    _timer.start()


def _arm_resolution(interval_s: float) -> None:
    global _resolution_timer
    _resolution_timer = threading.Timer(interval_s, _resolution_tick)
    _resolution_timer.daemon = True
    _resolution_timer.start()


def start() -> None:
    """Arm the recurring timers. Call once, from __main__.

    Two independent cadences: a full scan + sweep on SCAN_INTERVAL_HOURS, and a cheaper
    resolution-only sweep on AUTO_RESOLUTION_INTERVAL_HOURS (so resolutions are captured
    more often than the expensive scans). Either is off when its var is blank/0.
    """
    scan_s = _interval_seconds()
    if scan_s > 0:
        _arm(scan_s)
        _log.info("auto-scan armed (every %.2fh)", scan_s / 3600.0)
    else:
        _log.info("auto-scan disabled (set SCAN_INTERVAL_HOURS to enable)")

    res_s = _resolution_interval_seconds()
    if res_s > 0:
        _arm_resolution(res_s)
        _log.info("auto-resolution sweep armed (every %.2fh)", res_s / 3600.0)
    else:
        _log.info("auto-resolution sweep disabled (set AUTO_RESOLUTION_INTERVAL_HOURS to enable)")
