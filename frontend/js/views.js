// React components and view tabs. Depends on globals from api.js; used by app.js.
function MarketCard({ item, onAnalyzed }) {
  const { market: m, latest_analysis: a, analysis_count, stale } = item;
  const stalePP = stale && a && a.market_prob_at_analysis != null
    ? Math.round((m.market_prob - a.market_prob_at_analysis) * 100) : null;
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [histOpen, setHistOpen] = useState(false);
  const [hist, setHist] = useState(null);

  async function runAnalyze() {
    setBusy(true);
    setErr(null);
    try {
      onAnalyzed(m.id, await API.analyze(m.id));
      setHist(null);  // invalidate cached history; a new pass was just added
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function toggleHistory() {
    const next = !histOpen;
    setHistOpen(next);
    if (next && hist == null) {
      try {
        const d = await API.getMarket(m.id);
        setHist(d.history || []);
      } catch (e) {
        setErr(e.message);
      }
    }
  }

  const div = divergence(a);
  return (
    <div className="card">
      <div className="tags">
        {m.exchange && <span className={"tag exch " + m.exchange}>{m.exchange}</span>}
        {m.tags.map((t) => <span className="tag" key={t}>{t}</span>)}
      </div>
      <div className="q">{m.question}</div>
      {stalePP != null && (
        <div className="stale-badge">
          ⚠ stale — market moved {stalePP >= 0 ? "+" : ""}{stalePP}pp since analysis
          ({pct(a.market_prob_at_analysis)} → {pct(m.market_prob)}); re-analyze
        </div>
      )}
      <div className="stats">
        <div className="prob">{pct(m.market_prob)} <small>YES</small></div>
        <div className="kv">24h vol <b>{money(m.volume_24h)}</b></div>
        <div className="kv">liquidity <b>{money(m.liquidity)}</b></div>
        <div className="kv">closes <b>{timeToClose(m.end_date)}</b></div>
      </div>
      {a && !a.error && (
        <div className="divergence">
          <span className="kv">Market <b>{pct(m.market_prob)}</b></span>
          <span className="arrow">→</span>
          <span className="kv">Claude <b>{pct(a.claude_prob)}</b></span>
          {div && <span className={"badge " + div.cls}>{div.text}</span>}
          {a.edge && <span className="edge-label">{a.edge}</span>}
        </div>
      )}
      {a && a.factors && a.factors.length > 0 && (
        <div className="factors">{a.factors.map((f, i) => <span className="factor" key={i}>{f}</span>)}</div>
      )}
      {a && a.summary && <div className="summary">{a.summary}</div>}
      {a && a.confidence && (
        <div className="conf">confidence: <b>{a.confidence}</b>
          {analysis_count > 1 && (
            <span className="hist-toggle" onClick={toggleHistory}> · analyzed {analysis_count}× {histOpen ? "▴" : "▾"}</span>
          )}
        </div>
      )}
      {histOpen && (
        <div className="history">
          {hist == null
            ? <span className="dim">loading…</span>
            : hist.map((h, i) => (
                <div className="hist-row" key={i}>
                  <span className="hist-date">{new Date(h.created_at).toLocaleDateString()}</span>
                  <span>Claude <b>{pct(h.claude_prob)}</b></span>
                  {h.market_prob_at_analysis != null && <span className="dim">mkt {pct(h.market_prob_at_analysis)}</span>}
                  {h.confidence && <span className="dim">{h.confidence}</span>}
                  {h.error && <span className="dim">error</span>}
                </div>
              ))}
        </div>
      )}
      {a && <div className="disclaimer">Claude's estimate — a research aid, not financial advice.</div>}
      {err && <div className="err">{err}</div>}
      <div className="row-end">
        <a href={tradeUrl(m.slug, m.exchange)} target="_blank" rel="noopener noreferrer">Trade ↗</a>
        <button onClick={runAnalyze} disabled={busy} className={a ? "" : "primary"}>
          {busy ? <span className="spinner"></span> : a ? "Re-analyze" : "Analyze with Claude ↗"}
        </button>
      </div>
    </div>
  );
}

function MarketsView({ markets, onAnalyzed, refreshing, onRefresh, error }) {
  const [tag, setTag] = useState(null);
  const [analyzedOnly, setAnalyzedOnly] = useState(false);
  const [minDiv, setMinDiv] = useState(0);
  const [sort, setSort] = useState("divergence");

  const allTags = useMemo(() => {
    const s = new Set();
    markets.forEach((it) => it.market.tags.forEach((t) => s.add(t)));
    return [...s].sort();
  }, [markets]);

  const shown = useMemo(() => {
    let list = markets.slice();
    if (tag) list = list.filter((it) => it.market.tags.includes(tag));
    if (analyzedOnly) list = list.filter((it) => it.latest_analysis);
    if (minDiv > 0)
      list = list.filter((it) => it.latest_analysis && it.latest_analysis.edge_magnitude != null
        && it.latest_analysis.edge_magnitude * 100 >= minDiv);
    const dv = (it) => (it.latest_analysis && it.latest_analysis.edge_magnitude) || -1;
    if (sort === "divergence") list.sort((a, b) => dv(b) - dv(a));
    else if (sort === "volume") list.sort((a, b) => (b.market.volume_24h || 0) - (a.market.volume_24h || 0));
    else if (sort === "close") list.sort((a, b) => new Date(a.market.end_date || 8.64e15) - new Date(b.market.end_date || 8.64e15));
    return list;
  }, [markets, tag, analyzedOnly, minDiv, sort]);

  return (
    <div>
      <div className="controls">
        <label className="ctl">sort
          <select value={sort} onChange={(e) => setSort(e.target.value)}>
            <option value="divergence">divergence</option>
            <option value="volume">24h volume</option>
            <option value="close">time to close</option>
          </select>
        </label>
        <label className="ctl">min ±{minDiv}pp
          <input type="range" min="0" max="50" value={minDiv} onChange={(e) => setMinDiv(Number(e.target.value))} />
        </label>
        <label className="ctl">
          <input type="checkbox" checked={analyzedOnly} onChange={(e) => setAnalyzedOnly(e.target.checked)} />
          analyzed only
        </label>
        <button className="primary" onClick={onRefresh} disabled={refreshing}>
          {refreshing ? <span className="spinner"></span> : "Refresh markets"}
        </button>
      </div>
      {allTags.length > 0 && (
        <div className="chips">
          <span className={"chip" + (tag === null ? " active" : "")} onClick={() => setTag(null)}>all</span>
          {allTags.map((t) => (
            <span key={t} className={"chip" + (tag === t ? " active" : "")} onClick={() => setTag(t)}>{t}</span>
          ))}
        </div>
      )}
      {error && <div className="meta-line" style={{ color: "var(--red)" }}>{error}</div>}
      <div className="meta-line">{shown.length} market{shown.length === 1 ? "" : "s"}</div>
      <main>
        {markets.length === 0 ? (
          <div className="empty">No markets yet — hit <b>Refresh markets</b> to load from Polymarket.</div>
        ) : shown.length === 0 ? (
          <div className="empty">No markets match the current filters.</div>
        ) : (
          <div className="grid">
            {shown.map((it) => <MarketCard key={it.market.id} item={it} onAnalyzed={onAnalyzed} />)}
          </div>
        )}
      </main>
    </div>
  );
}

function ScannerView() {
  const [req, setReq] = useState({
    min_volume_24h: 10000, min_liquidity: 0, min_days_to_close: 7,
    min_divergence: 0.05, max_age_hours: 24, max_markets: 25, refute_top: 0,
    max_llm_calls: 0, category: "",
  });
  const [results, setResults] = useState(null);
  const [scanning, setScanning] = useState(false);
  const [err, setErr] = useState(null);
  const [cal, setCal] = useState(null);  // the active model's calibration report

  useEffect(() => {
    Promise.all([API.getProvider(), API.getCalibration()])
      .then(([prov, reports]) =>
        setCal(reports.find((r) => r.model === prov.model)
          || { calibrated: false, n: 0, min_n: 50, model: prov.model }))
      .catch(() => {});
  }, []);

  const num = (k) => (e) => setReq((r) => ({ ...r, [k]: Number(e.target.value) }));

  async function run() {
    setScanning(true);
    setErr(null);
    try {
      const payload = { ...req };
      if (!payload.category) delete payload.category;
      setResults(await API.scan(payload));
    } catch (e) {
      setErr(e.message);
    } finally {
      setScanning(false);
    }
  }

  const fields = [
    ["min_volume_24h", "min 24h volume ($)"],
    ["min_liquidity", "min liquidity ($)"],
    ["min_days_to_close", "min days to close"],
    ["min_divergence", "min divergence (0–1)"],
    ["max_age_hours", "reuse if analyzed within (h)"],
    ["max_markets", "max markets to fetch"],
    ["refute_top", "refute top N (0 = off)"],
    ["max_llm_calls", "max LLM calls (0 = no cap)"],
  ];

  return (
    <main>
      <div className="scan-form">
        {fields.map(([k, label]) => (
          <label key={k}>{label}
            <input type="number" value={req[k]} step={k === "min_divergence" ? "0.01" : "1"} onChange={num(k)} />
          </label>
        ))}
        <label>category (tag, optional)
          <input type="text" value={req.category} placeholder="e.g. Politics"
            onChange={(e) => setReq((r) => ({ ...r, category: e.target.value }))} />
        </label>
        <button className="primary" onClick={run} disabled={scanning}>
          {scanning ? <span className="spinner"></span> : "Run scan"}
        </button>
      </div>

      <div className="warn">
        ⚠ Each scanned market is a live Claude web-search call (takes seconds + costs API spend).
        Bound it with high min-volume and a small max-markets.{" "}
        {cal && cal.calibrated
          ? <span>EV uses <b>calibrated</b> probabilities (T={cal.temperature.toFixed(2)}, N={cal.n}) but the
              market <b>mid</b> price — not executable bid/ask (Phase 3.5).</span>
          : <span>EV uses the market <b>mid</b> price and an <b>uncalibrated</b> estimate
              {cal ? ` (need ${cal.min_n} resolved, have ${cal.n})` : ""} — <b>directional only</b>, not a guaranteed edge.</span>}
      </div>

      {err && <div className="err">{err}</div>}
      {scanning && (
        <div className="empty"><span className="spinner" style={{ borderColor: "#888", borderTopColor: "transparent" }}></span>
          {" "}Scanning… this can take a few minutes. Don't close the tab.</div>
      )}

      {results && !scanning && (
        results.length === 0 ? (
          <div className="empty">No markets cleared the gates. Loosen the filters and rescan.</div>
        ) : (
          <div>
            <div className="meta-line">{results.length} ranked by annualized EV {cal && cal.calibrated ? "(calibrated)" : "(uncalibrated)"} · basis: <b>exec</b> = VWAP fill over the book for the target position (<b>exec*</b> = book too thin to fill it), <b>mid</b> = mid-price fallback · Bid/Ask show top-of-book price ·fillable shares
              {req.refute_top > 0 ? " · verdict = skeptical refutation of the top " + req.refute_top + " (model shown under each verdict)" : ""}</div>
            <div className="table-wrap">
              <table className="scan">
                <thead>
                  <tr>
                    <th>Side</th><th>Exch</th><th>Mkt</th><th>Bid</th><th>Ask</th><th>Claude</th><th>Calib.</th><th>Div</th>
                    <th>EV%</th><th>Kelly</th><th>Ann. EV</th><th>Basis</th><th>Verdict</th><th>Liq</th><th>Closes</th><th>Conf</th>
                    <th className="q-cell">Question</th><th></th>
                  </tr>
                </thead>
                <tbody>
                  {results.map((r) => {
                    const m = r.market, a = r.analysis;
                    return (
                      <tr key={m.id}>
                        <td>{r.side && <span className={"side " + r.side.toLowerCase()}>{r.side}</span>}</td>
                        <td>{m.exchange && <span className={"tag exch " + m.exchange}>{m.exchange}</span>}</td>
                        <td>{pct(m.market_prob)}</td>
                        <td>{pct(r.best_bid)}{r.bid_depth != null && r.best_bid != null &&
                          <span className="dim" title={`${Math.round(r.bid_depth).toLocaleString()} shares · $${Math.round(r.bid_depth * r.best_bid).toLocaleString()}`}> ·{shares(r.bid_depth)}</span>}</td>
                        <td>{pct(r.best_ask)}{r.ask_depth != null && r.best_ask != null &&
                          <span className="dim" title={`${Math.round(r.ask_depth).toLocaleString()} shares · $${Math.round(r.ask_depth * r.best_ask).toLocaleString()}`}> ·{shares(r.ask_depth)}</span>}</td>
                        <td>{pct(a.claude_prob)}</td>
                        <td>{pct(r.calibrated_prob)}</td>
                        <td>{r.ev != null ? Math.round(r.ev * 100) + "pp" : "—"}</td>
                        <td>{pct1(r.ev_pct)}</td>
                        <td>{pct1(r.kelly)}</td>
                        <td className="ann">{pct1(r.annualized_ev)}</td>
                        <td>{(() => {
                          if (!r.executable) return <span className="basis-mid" title="mid-price only — no live order book (e.g. Kalshi, or no two-sided Polymarket book)">mid</span>;
                          const partial = r.fully_filled === false;
                          const tip = `VWAP fill for $${r.target_position_usd} (${shares(r.fill_shares)} shares)`
                            + (partial ? " · partial: book too thin to fill the full target" : "");
                          return <span className="basis-exec" title={tip}>exec{partial ? "*" : ""}</span>;
                        })()}</td>
                        <td>{(() => {
                          const rf = r.refutation;
                          if (!rf) return "—";
                          const model = rf.refuter_model || "";
                          if (!rf.verdict) return <span className="basis-mid" title={(model ? model + ": " : "") + (rf.error || "")}>?</span>;
                          const tip = `refuter ${model ? model + " " : ""}${pct(rf.refuter_prob)}` + (rf.counterpoints && rf.counterpoints[0] ? " · " + rf.counterpoints[0] : "");
                          return <span title={tip}>
                            <span className={rf.verdict === "holds" ? "v-holds" : "v-refuted"}>
                              {rf.verdict}{rf.resolution_risk ? " ⚠" : ""}</span>
                            {model ? <span className="v-model">{model}</span> : null}</span>;
                        })()}</td>
                        <td>{money(m.liquidity)}</td>
                        <td>{r.days_to_close != null ? Math.round(r.days_to_close) + "d" : "—"}</td>
                        <td>{a.confidence || "—"}</td>
                        <td className="q-cell">{m.question}</td>
                        <td><a href={tradeUrl(m.slug, m.exchange)} target="_blank" rel="noopener noreferrer">Trade ↗</a></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )
      )}

      {!results && !scanning && !err && (
        <div className="empty">Set your gates and hit <b>Run scan</b> to rank markets by expected value.</div>
      )}
    </main>
  );
}

function CalibrationCard({ cal }) {
  const S = 240, PAD = 28;
  const xx = (p) => PAD + p * (S - 2 * PAD);
  const yy = (p) => (S - PAD) - p * (S - 2 * PAD);
  const maxCount = Math.max(1, ...cal.curve.map((b) => b.count));
  const pts = cal.curve.filter((b) => b.count > 0 && b.predicted_mean != null);
  const Tnote = !cal.calibrated
    ? `Insufficient resolved markets — using raw estimates (have ${cal.n}, need ${cal.min_n}). Calibration accrues as markets resolve.`
    : cal.temperature > 1.05 ? "T > 1 → overconfident; estimates softened toward 50%."
    : cal.temperature < 0.95 ? "T < 1 → underconfident; estimates sharpened."
    : "T ≈ 1 → already well-calibrated; little correction applied.";

  return (
    <div className="cal-section">
      <div className="cal-head">
        <span className="cal-model">{cal.model}</span>
        <span className={"badge " + (cal.calibrated ? "up" : "fair")}>{cal.calibrated ? "calibrated" : "uncalibrated"}</span>
        <span className="kv">N <b>{cal.n}</b> / need {cal.min_n}</span>
        <span className="kv">T <b>{cal.temperature.toFixed(2)}</b></span>
        <span className="kv">Brier <b>{cal.brier.toFixed(3)}</b></span>
        <span className="kv">log-loss <b>{cal.log_loss.toFixed(3)}</b></span>
      </div>
      <div className="cal-note">{Tnote}</div>
      <div className="cal-grid">
        <svg width={S} height={S} className="reliability">
          <rect x={PAD} y={PAD} width={S - 2 * PAD} height={S - 2 * PAD} fill="none" stroke="var(--line)" />
          <line x1={xx(0)} y1={yy(0)} x2={xx(1)} y2={yy(1)} stroke="#cbd5e1" strokeDasharray="4 4" />
          {pts.map((b, i) => (
            <circle key={i} cx={xx(b.predicted_mean)} cy={yy(b.empirical_rate)}
              r={3 + 7 * (b.count / maxCount)} fill="rgba(37,99,235,.45)" stroke="var(--accent)" />
          ))}
          <text x={S / 2} y={S - 4} textAnchor="middle" className="axis">predicted</text>
          <text x={12} y={S / 2} textAnchor="middle" transform={`rotate(-90 12 ${S / 2})`} className="axis">actual</text>
        </svg>
        <div className="table-wrap" style={{ flex: 1 }}>
          <table className="scan">
            <thead><tr><th>Bin</th><th>Predicted</th><th>Actual</th><th>N</th></tr></thead>
            <tbody>
              {cal.curve.map((b, i) => (
                <tr key={i}>
                  <td>{Math.round(b.bin_lo * 100)}–{Math.round(b.bin_hi * 100)}%</td>
                  <td>{b.predicted_mean == null ? "—" : pct(b.predicted_mean)}</td>
                  <td>{b.empirical_rate == null ? "—" : pct(b.empirical_rate)}</td>
                  <td>{b.count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function runTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return "—";
  const date = d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  const time = d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false });
  return `${date} ${time}`;
}

function ScanHistoryBlock({ hist }) {
  if (!hist) return null;
  return (
    <div className="cal-section">
      <div className="cal-head">
        <span className="cal-model">Auto-scan history</span>
        <span className="kv">runs <b>{hist.total_runs}</b></span>
        <span className="kv">avg edges/run <b>{hist.avg_edges_per_run}</b></span>
        <span className="kv">avg markets scanned <b>{hist.avg_markets_scanned}</b></span>
        <span className="kv">resolutions captured <b>{hist.total_resolutions_captured}</b></span>
      </div>
      {hist.total_runs === 0 ? (
        <div className="cal-note">No auto-scan runs yet — set <code>SCAN_INTERVAL_HOURS</code> to enable.</div>
      ) : (
        <div className="table-wrap">
          <table className="scan">
            <thead><tr><th>Time</th><th>Scanned</th><th>Edges</th><th>Resolutions</th><th>Errors</th></tr></thead>
            <tbody>
              {hist.last_runs.map((r, i) => (
                <tr key={i}>
                  <td>{runTime(r.timestamp)}</td>
                  <td>{r.markets_scanned ?? 0}</td>
                  <td>{r.edges_found ?? 0}</td>
                  <td>{r.resolutions_captured ?? 0}</td>
                  <td>{r.errors && r.errors.length ? `⚠ ${r.errors.length}` : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function CalibrationView() {
  const [reports, setReports] = useState(null);
  const [hist, setHist] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => { API.getCalibration().then(setReports).catch((e) => setErr(e.message)); }, []);
  // History is best-effort — a failure here must never break the calibration view.
  useEffect(() => { API.getScanHistory().then(setHist).catch(() => {}); }, []);

  if (err) return <main><div className="err">{err}</div></main>;
  if (!reports) return <main><div className="empty">Loading…</div></main>;

  return (
    <main>
      <ScanHistoryBlock hist={hist} />
      <div className="cal-note">
        Each model is calibrated separately — mixing models would corrupt the curve.{" "}
        <a href="/api/calibration/export.csv">Export resolved pairs (CSV) ↗</a>
      </div>
      {reports.length === 0
        ? <div className="empty">No resolved markets yet — calibration appears here as markets resolve.</div>
        : reports.map((cal) => <CalibrationCard key={cal.model} cal={cal} />)}
    </main>
  );
}

function SignalsView() {
  const [data, setData] = useState(null);
  const [alerts, setAlerts] = useState([]);
  const [err, setErr] = useState(null);
  useEffect(() => { API.getSignals().then(setData).catch((e) => setErr(e.message)); }, []);
  // Alerts are best-effort — a failure here must never break the signals view.
  useEffect(() => { API.getAlerts().then(setAlerts).catch(() => {}); }, []);

  if (err) return <main><div className="err">{err}</div></main>;
  if (!data) return <main><div className="empty">Loading…</div></main>;

  const s = data.summary, sigs = data.signals;
  const winPct = s.resolved > 0 ? Math.round((s.wins / s.resolved) * 100) + "%" : "—";

  return (
    <main>
      <div className="cal-section">
        <div className="cal-head">
          <span className="cal-model">Forward signals</span>
          <span className="kv">open <b>{s.open}</b></span>
          <span className="kv">resolved <b>{s.resolved}</b></span>
          <span className="kv">win rate <b>{winPct}</b></span>
          <span className="kv">realized P&L <b>{money(s.realized_pnl)}</b></span>
          <span className="kv">avg EV <b>{pct1(s.avg_ev)}</b></span>
        </div>
        <div className="cal-note">
          Each signal is a forward, lookahead-free record of an actionable edge, sized at the
          modeled VWAP fill. P&L is realized when the market resolves.
        </div>
        {sigs.length === 0 ? (
          <div className="empty">No signals yet — enable the auto-scan (<code>SCAN_INTERVAL_HOURS</code>) or run a scan with logging.</div>
        ) : (
          <div className="table-wrap">
            <table className="scan">
              <thead>
                <tr>
                  <th>Time</th><th className="q-cell">Market</th><th>Side</th>
                  <th>Our prob</th><th>Mid</th><th>Price</th><th>EV%</th>
                  <th>Verdict</th><th>Status</th><th>P&L</th>
                </tr>
              </thead>
              <tbody>
                {sigs.map((g) => (
                  <tr key={g.id}>
                    <td>{runTime(g.created_at)}</td>
                    <td className="q-cell">{g.question}</td>
                    <td>{g.side && <span className={"side " + g.side.toLowerCase()}>{g.side}</span>}</td>
                    <td>{pct(g.calibrated_prob)}</td>
                    <td>{pct(g.market_prob)}</td>
                    <td>{pct(g.price_paid)}</td>
                    <td>{pct1(g.ev_pct)}</td>
                    <td>{g.adversarial_verdict
                      ? <span className={g.adversarial_verdict === "holds" ? "v-holds" : "v-refuted"}
                          title={g.refuter_model ? "refuter: " + g.refuter_model : ""}>{g.adversarial_verdict}</span>
                      : "—"}</td>
                    <td>{g.resolved
                      ? <span className={"side " + ((g.pnl ?? 0) > 0 ? "yes" : "no")}>{(g.pnl ?? 0) > 0 ? "won" : "lost"}</span>
                      : <span className="dim">open</span>}</td>
                    <td>{g.resolved ? money(g.pnl) : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="cal-section">
        <div className="cal-head"><span className="cal-model">Recent alerts</span></div>
        {alerts.length === 0 ? (
          <div className="cal-note">No alerts yet — high-divergence edges are flagged here when the auto-scan runs.</div>
        ) : (
          <div className="table-wrap">
            <table className="scan">
              <thead>
                <tr><th>Time</th><th>Divergence</th><th>Side</th><th className="q-cell">Question</th><th>EV%</th><th></th></tr>
              </thead>
              <tbody>
                {alerts.map((al, i) => {
                  const d = Math.round(al.divergence * 100);
                  return (
                    <tr key={i}>
                      <td>{runTime(al.timestamp)}</td>
                      <td className={d >= 0 ? "up" : "down"}>{(d >= 0 ? "+" : "−") + Math.abs(d) + "pp"}</td>
                      <td>{al.side && <span className={"side " + al.side.toLowerCase()}>{al.side}</span>}</td>
                      <td className="q-cell">{al.question}</td>
                      <td>{al.ev != null ? Math.round(al.ev * 100) + "pp" : "—"}</td>
                      <td>{al.trade_url && <a href={al.trade_url} target="_blank" rel="noopener noreferrer">Trade ↗</a>}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </main>
  );
}

function LeaderboardView() {
  const [rows, setRows] = useState(null);
  const [err, setErr] = useState(null);
  const [sort, setSort] = useState("brier");
  useEffect(() => { API.getLeaderboard().then(setRows).catch((e) => setErr(e.message)); }, []);

  if (err) return <main><div className="err">{err}</div></main>;
  if (!rows) return <main><div className="empty">Loading…</div></main>;
  if (rows.length === 0)
    return <main><div className="empty">No resolved markets yet — the model leaderboard appears as forecasts resolve.</div></main>;

  // Lower is better for brier/log_loss; higher is better for accuracy/brier_skill/n.
  const lowerBetter = sort === "brier" || sort === "log_loss";
  const sorted = [...rows].sort((a, b) => {
    const av = a[sort], bv = b[sort];
    const x = av == null ? -Infinity : av, y = bv == null ? -Infinity : bv;
    return lowerBetter ? x - y : y - x;
  });
  const Th = ({ k, children }) => (
    <th onClick={() => setSort(k)} style={{ cursor: "pointer" }} title="Sort by this column">
      {children}{sort === k ? " ▾" : ""}
    </th>
  );
  const skill = (x) => (x == null ? "—" : (x >= 0 ? "+" : "") + (x * 100).toFixed(0) + "%");

  return (
    <main>
      <div className="cal-note">
        Every LLM scored on its own resolved forecasts — an apples-to-apples eval. Lower{" "}
        <b>Brier</b> / <b>log-loss</b> is better; <b>Brier skill</b> &gt; 0 beats the naive
        base-rate forecast; calibration <b>temp</b> shows over- (T&gt;1) / under- (T&lt;1)
        confidence. Click a column to sort.
      </div>
      <div className="table-wrap">
        <table className="scan">
          <thead>
            <tr>
              <th className="q-cell">Model</th>
              <Th k="n">N</Th>
              <Th k="brier">Brier</Th>
              <Th k="log_loss">Log-loss</Th>
              <Th k="accuracy">Accuracy</Th>
              <Th k="brier_skill">Brier skill</Th>
              <Th k="temperature">Temp</Th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((r, i) => (
              <tr key={r.model}>
                <td className="q-cell">
                  {sort === "brier" && i === 0 ? "🏆 " : ""}{r.model}
                  {!r.calibrated && <span className="dim" title="below CALIBRATION_MIN_N resolved markets — metrics shown, temperature not yet applied"> (provisional)</span>}
                </td>
                <td>{r.n}</td>
                <td>{r.brier.toFixed(3)}</td>
                <td>{r.log_loss.toFixed(3)}</td>
                <td>{pct(r.accuracy)}</td>
                <td style={{ color: r.brier_skill == null ? "var(--muted)" : r.brier_skill >= 0 ? "var(--green)" : "var(--red)" }}>{skill(r.brier_skill)}</td>
                <td>{r.temperature.toFixed(2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </main>
  );
}

function EquityCurve({ curve }) {
  if (!curve || curve.length === 0) return null;
  const W = 680, H = 220, PAD = 34;
  const ys = curve.map((p) => p.cum_pnl);
  const maxY = Math.max(0, ...ys), minY = Math.min(0, ...ys);
  const spanY = (maxY - minY) || 1;
  const n = curve.length;
  const xx = (i) => PAD + (n <= 1 ? 0 : (i / (n - 1)) * (W - 2 * PAD));
  const yy = (v) => (H - PAD) - ((v - minY) / spanY) * (H - 2 * PAD);
  const path = curve.map((p, i) => (i === 0 ? "M" : "L") + xx(i).toFixed(1) + " " + yy(p.cum_pnl).toFixed(1)).join(" ");
  const last = ys[ys.length - 1];
  const stroke = last >= 0 ? "var(--green)" : "var(--red)";
  return (
    <svg className="reliability" viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", maxWidth: W }}>
      <line x1={PAD} y1={yy(0)} x2={W - PAD} y2={yy(0)} stroke="var(--line)" strokeDasharray="4 4" />
      <path d={path} fill="none" stroke={stroke} strokeWidth="2" />
      <circle cx={xx(n - 1)} cy={yy(last)} r="3.5" fill={stroke} />
      <text x={PAD} y={18} className="axis">cumulative realized P&amp;L ($)</text>
      <text x={W - PAD} y={yy(0) - 4} textAnchor="end" className="axis">$0</text>
    </svg>
  );
}

function BreakdownTable({ title, rows }) {
  if (!rows || rows.length === 0) return null;
  return (
    <div className="table-wrap" style={{ flex: 1, minWidth: 240 }}>
      <table className="scan">
        <thead><tr><th className="q-cell">{title}</th><th>N</th><th>P&amp;L</th><th>Win rate</th></tr></thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              <td className="q-cell" style={{ textTransform: "capitalize" }}>{r.key}</td>
              <td>{r.n}</td>
              <td style={{ color: r.pnl >= 0 ? "var(--green)" : "var(--red)" }}>{money(r.pnl)}</td>
              <td>{r.win_rate == null ? "—" : pct(r.win_rate)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PerformanceView() {
  const [p, setP] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => { API.getPerformance().then(setP).catch((e) => setErr(e.message)); }, []);

  if (err) return <main><div className="err">{err}</div></main>;
  if (!p) return <main><div className="empty">Loading…</div></main>;
  if (p.settled === 0)
    return <main><div className="empty">No settled signals yet — the track record builds as logged signals resolve ({p.open} open).</div></main>;

  const ret = (x) => (x == null ? "—" : (x >= 0 ? "+" : "") + (x * 100).toFixed(1) + "%");
  return (
    <main>
      <div className="cal-section">
        <div className="cal-head">
          <span className="cal-model">Track record</span>
          <span className="kv">settled <b>{p.settled}</b></span>
          <span className="kv">open <b>{p.open}</b></span>
          <span className="kv">realized P&amp;L <b>{money(p.total_pnl)}</b></span>
          <span className="kv">return on cost <b>{ret(p.total_return)}</b></span>
          <span className="kv">win rate <b>{p.win_rate == null ? "—" : pct(p.win_rate)}</b></span>
          <span className="kv">profit factor <b>{p.profit_factor == null ? "—" : p.profit_factor.toFixed(2)}</b></span>
          <span className="kv">Sharpe/trade <b>{p.sharpe == null ? "—" : p.sharpe.toFixed(2)}</b></span>
          <span className="kv">max drawdown <b>{money(p.max_drawdown)}</b></span>
        </div>
        <div className="cal-note">
          Cumulative realized P&amp;L over logged signals (entry order). Cost basis is the modeled
          VWAP fill; return, Sharpe and drawdown come from settled signals only — open positions
          aren't counted, so the curve stays lookahead-free.
        </div>
        <EquityCurve curve={p.equity_curve} />
      </div>

      <div className="cal-section">
        <div className="cal-head"><span className="cal-model">By exchange / side</span></div>
        <div className="cal-grid">
          <BreakdownTable title="Exchange" rows={p.by_exchange} />
          <BreakdownTable title="Side" rows={p.by_side} />
        </div>
      </div>
    </main>
  );
}

