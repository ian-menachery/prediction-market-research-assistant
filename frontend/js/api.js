// API client + formatting/display helpers. Loaded first; everything below is a
// browser global shared with views.js / app.js (classic scripts, no ES modules).
const { useState, useEffect, useMemo } = React;

// --- API helpers (all fetch calls live here) ---
const API = {
  async getMarkets() {
    const r = await fetch("/api/markets");
    if (!r.ok) throw new Error("Failed to load markets");
    return r.json();
  },
  async refresh() {
    const r = await fetch("/api/markets/refresh", { method: "POST" });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "Refresh failed");
    return d;
  },
  async analyze(id) {
    const r = await fetch(`/api/markets/${id}/analyze`, { method: "POST" });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "Analysis failed");
    return d;
  },
  async scan(params) {
    const r = await fetch("/api/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(typeof d.error === "string" ? d.error : "Scan failed");
    return d;
  },
  async estimateScan(params) {
    const r = await fetch("/api/scan/estimate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(typeof d.error === "string" ? d.error : "Estimate failed");
    return d;
  },
  async getCalibration() {
    const r = await fetch("/api/calibration");
    if (!r.ok) throw new Error("Failed to load calibration");
    return r.json();
  },
  async getProvider() {
    const r = await fetch("/api/provider");
    if (!r.ok) throw new Error("Failed to load provider");
    return r.json();
  },
  async getMarket(id) {
    const r = await fetch(`/api/markets/${id}`);
    if (!r.ok) throw new Error("Failed to load market");
    return r.json();
  },
  async resetProvider() {
    const r = await fetch("/api/provider/reset", { method: "POST" });
    if (!r.ok) throw new Error("Failed to reset provider");
    return r.json();
  },
  async getScanHistory() {
    const r = await fetch("/api/scan-history");
    if (!r.ok) throw new Error("Failed to load scan history");
    return r.json();
  },
  async getSignals() {
    const r = await fetch("/api/signals");
    if (!r.ok) throw new Error("Failed to load signals");
    return r.json();
  },
  async getAlerts() {
    const r = await fetch("/api/alerts");
    if (!r.ok) throw new Error("Failed to load alerts");
    return r.json();
  },
  async getLeaderboard() {
    const r = await fetch("/api/leaderboard");
    if (!r.ok) throw new Error("Failed to load leaderboard");
    return r.json();
  },
  async getPerformance() {
    const r = await fetch("/api/performance");
    if (!r.ok) throw new Error("Failed to load performance");
    return r.json();
  },
};

// --- formatting helpers ---
const pct = (x) => (x == null ? "—" : Math.round(x * 100) + "%");
const pct1 = (x) => (x == null ? "—" : (x * 100).toFixed(0) + "%");
const money = (n) => (n == null ? "—" : "$" + Math.round(n).toLocaleString("en-US"));
const shares = (n) => (n == null ? "" : n >= 1000 ? Math.round(n / 1000) + "k" : String(Math.round(n)));
const tradeUrl = (slug, exchange) =>
  exchange === "kalshi" ? `https://kalshi.com/markets/${slug}` : `https://polymarket.com/event/${slug}`;

function timeToClose(iso) {
  if (!iso) return "—";
  const ms = new Date(iso) - new Date();
  if (ms <= 0) return "closed";
  const days = Math.floor(ms / 86400000);
  if (days >= 1) return `${days}d`;
  return `${Math.floor(ms / 3600000)}h`;
}

function divergence(a) {
  if (!a || a.edge_magnitude == null) return null;
  const pp = Math.round(a.edge_magnitude * 100);
  if (a.edge === "underpriced") return { cls: "up", text: `+${pp}pp` };
  if (a.edge === "overpriced") return { cls: "down", text: `−${pp}pp` };
  return { cls: "fair", text: "fair" };
}

