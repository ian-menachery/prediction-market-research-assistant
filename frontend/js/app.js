// Root App component + mount. Loaded last; depends on api.js + views.js globals.
function App() {
  const [view, setView] = useState("markets");
  const [markets, setMarkets] = useState([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState(null);
  const [provider, setProvider] = useState(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      setMarkets(await API.getMarkets());
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => { load(); }, []);
  // Re-checks on tab change, so a quota-exhaustion that happened during a scan surfaces.
  useEffect(() => { API.getProvider().then(setProvider).catch(() => {}); }, [view]);

  async function handleRefresh() {
    setRefreshing(true);
    setError(null);
    try {
      await API.refresh();
      await load();
    } catch (e) {
      setError(e.message);
    } finally {
      setRefreshing(false);
    }
  }

  function handleAnalyzed(id, analysis) {
    setMarkets((prev) => prev.map((it) =>
      it.market.id === id
        ? { ...it, latest_analysis: analysis, analysis_count: (it.analysis_count || 0) + 1 }
        : it));
  }

  return (
    <div>
      <header>
        <div className="header-row">
          <h1>PMRA <small>Prediction Market Research Assistant</small></h1>
          <div className="tabs">
            <span className={"tab" + (view === "markets" ? " active" : "")} onClick={() => setView("markets")}>Markets</span>
            <span className={"tab" + (view === "scan" ? " active" : "")} onClick={() => setView("scan")}>Scan (EV)</span>
            <span className={"tab" + (view === "signals" ? " active" : "")} onClick={() => setView("signals")}>Signals</span>
            <span className={"tab" + (view === "performance" ? " active" : "")} onClick={() => setView("performance")}>Performance</span>
            <span className={"tab" + (view === "calibration" ? " active" : "")} onClick={() => setView("calibration")}>Calibration</span>
            <span className={"tab" + (view === "leaderboard" ? " active" : "")} onClick={() => setView("leaderboard")}>Leaderboard</span>
          </div>
          {provider && <span className="provider-chip">{provider.provider} · {provider.model}</span>}
        </div>
        {provider && provider.openai_exhausted && (
          <div className="banner-exhausted">
            ⚠ OpenAI credits exhausted — set <code>LLM_PROVIDER=anthropic</code> in <code>.env</code> and restart the app to continue with Claude.
            {" "}Topped up your OpenAI credits?{" "}
            <button onClick={async () => { try { await API.resetProvider(); setProvider(await API.getProvider()); } catch (e) {} }}>
              Retry OpenAI
            </button>
          </div>
        )}
      </header>
      {view === "markets"
        ? (loading
            ? <div className="empty">Loading…</div>
            : <MarketsView markets={markets} onAnalyzed={handleAnalyzed}
                refreshing={refreshing} onRefresh={handleRefresh} error={error} />)
        : view === "scan" ? <ScannerView />
        : view === "signals" ? <SignalsView />
        : view === "performance" ? <PerformanceView />
        : view === "leaderboard" ? <LeaderboardView />
        : <CalibrationView />}
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
