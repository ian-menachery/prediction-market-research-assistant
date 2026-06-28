"""Flask route smoke + validation tests (test_client, temp DB, no network)."""

from __future__ import annotations

from conftest import make_market


def test_health(client, monkeypatch) -> None:
    # Patch the live Kalshi check so this stays network-free.
    from research import kalshi
    monkeypatch.setattr(kalshi, "health_check", lambda: {"discovery_ok": True, "book_ok": True})
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.get_json()["status"] == "ok"
    assert r.get_json()["kalshi"]["book_ok"] is True


def test_markets_empty_list(client) -> None:
    r = client.get("/api/markets")
    assert r.status_code == 200
    assert r.get_json() == []


def test_market_detail_404(client) -> None:
    assert client.get("/api/markets/does-not-exist").status_code == 404


def test_scan_rejects_oversized_request(client) -> None:
    r = client.post("/api/scan", json={"max_markets": 1_000_000})
    assert r.status_code == 400  # ScanRequest bound; never reaches a real scan


def test_resolution_validation(client, temp_db) -> None:
    m = make_market()
    temp_db.upsert_markets([m])
    assert client.put(f"/api/markets/{m.id}/resolution", json={}).status_code == 400
    assert client.put(f"/api/markets/{m.id}/resolution", json={"outcome": "true"}).status_code == 400
    assert client.put(f"/api/markets/{m.id}/resolution", json={"outcome": True}).status_code == 200


def test_export_csv_has_horizon_column(client) -> None:
    r = client.get("/api/calibration/export.csv")
    assert r.status_code == 200
    assert "days_before_close" in r.get_data(as_text=True).splitlines()[0]


def test_provider_reset(client) -> None:
    r = client.post("/api/provider/reset")
    assert r.status_code == 200
    assert r.get_json()["openai_exhausted"] is False


def test_dashboard_reads_degrade_gracefully(client) -> None:
    # Empty DB / missing JSONL files must still 200 (defensive routes), not 500.
    assert client.get("/api/alerts").status_code == 200
    assert client.get("/api/scan-history").status_code == 200
    assert client.get("/api/signals").status_code == 200
