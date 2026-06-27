"""Scheduler run_once + history, fully mocked (no scan, no network, no timers)."""

from __future__ import annotations

from research import scanner, scheduler


def test_run_once_submits_batch_when_idle(temp_db, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SCAN_LOG_PATH", str(tmp_path / "scan_log.jsonl"))
    monkeypatch.setattr(scanner, "submit_batch", lambda req: "batch_1")
    monkeypatch.setattr(scanner, "sweep_resolutions", lambda: 2)

    rec = scheduler.run_once()
    assert rec["batch_state"] == "submitted"
    assert rec["resolutions_captured"] == 2
    assert rec["errors"] == []
    assert scheduler.history()["total_runs"] == 1  # submitted ticks are logged


def test_run_once_ingests_when_batch_ends(temp_db, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SCAN_LOG_PATH", str(tmp_path / "log.jsonl"))
    temp_db.save_batch("batch_1", 5)  # an in-flight batch
    monkeypatch.setattr(scanner, "ingest_batch", lambda bid: {
        "edges": 3, "llm_calls": 5, "cost_usd": 0.42, "signals": 1, "alerts": 0,
        "cache_read_tokens": 0, "cache_creation_tokens": 0,
    })
    monkeypatch.setattr(scanner, "sweep_resolutions", lambda: 0)

    rec = scheduler.run_once()
    assert rec["batch_state"] == "ingested"
    assert rec["edges_found"] == 3
    assert rec["llm_calls"] == 5
    assert rec["cost_usd"] == 0.42
    assert scheduler.history()["total_cost_usd"] == 0.42


def test_run_once_processing_tick_is_quiet(temp_db, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SCAN_LOG_PATH", str(tmp_path / "log.jsonl"))
    temp_db.save_batch("batch_1", 5)
    monkeypatch.setattr(scanner, "ingest_batch", lambda bid: None)  # still processing
    monkeypatch.setattr(scanner, "sweep_resolutions", lambda: 4)

    rec = scheduler.run_once()
    assert rec["batch_state"] == "processing"
    assert rec["resolutions_captured"] == 4  # sweep still runs
    assert scheduler.history()["total_runs"] == 0  # processing tick is NOT logged


def test_run_once_isolates_submit_failure_from_sweep(temp_db, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SCAN_LOG_PATH", str(tmp_path / "log.jsonl"))

    def boom(req):
        raise RuntimeError("anthropic down")

    monkeypatch.setattr(scanner, "submit_batch", boom)
    monkeypatch.setattr(scanner, "sweep_resolutions", lambda: 7)
    rec = scheduler.run_once()
    assert rec["batch_state"] == "error"
    assert any("anthropic down" in e for e in rec["errors"])
    assert rec["resolutions_captured"] == 7  # sweep still runs despite the submit failing


def test_sweep_only_tick_captures_resolutions(temp_db, monkeypatch) -> None:
    # The cheap resolution-sweep timer settles markets (calibration flywheel) with no scan/LLM.
    monkeypatch.setattr(scanner, "sweep_resolutions", lambda: 3)
    assert scheduler.run_sweep_once() == 3


def test_sweep_only_tick_isolates_failure(temp_db, monkeypatch) -> None:
    def boom():
        raise RuntimeError("gamma down")

    monkeypatch.setattr(scanner, "sweep_resolutions", boom)
    assert scheduler.run_sweep_once() == 0  # never raises; next sweep retries


def test_history_missing_file_is_empty(temp_db, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SCAN_LOG_PATH", str(tmp_path / "nope.jsonl"))
    h = scheduler.history()
    assert h["total_runs"] == 0
    assert h["total_cost_usd"] == 0.0
    assert h["last_runs"] == []


def test_history_skips_malformed_lines(temp_db, monkeypatch, tmp_path) -> None:
    p = tmp_path / "log.jsonl"
    p.write_text(
        '{"edges_found": 3, "markets_scanned": 5, "cost_usd": 0.5, "llm_calls": 2}\n'
        "}{ not json\n"
        "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SCAN_LOG_PATH", str(p))
    h = scheduler.history()
    assert h["total_runs"] == 1  # malformed/blank lines skipped
    assert h["total_cost_usd"] == 0.5
    assert h["avg_edges_per_run"] == 3.0
