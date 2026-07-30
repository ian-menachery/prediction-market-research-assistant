# ============================================================================
# ARCHIVED — reproduces the behavioral-study RESULTS (see ../../BACKTEST_NOTES.md, "Test d").
# READ-ONLY, $0, no network: pure analysis of the frozen priced_rows.jsonl in this folder.
# Tests whether any mechanical (non-forecasting) mispricing survives Kalshi's overround + fees.
#
# P&L convention: held to resolution (no exit spread). GROSS = entry at mid, no fee.
# NET = realistic far-touch fill (BUY at yes_ask, SELL at 1 - yes_bid) + 0.07*p*(1-p) fee.
# All significance is EVENT-CLUSTERED: average within an event first, then across events, so
# correlated legs of one underlying event count once (effective n = distinct events).
# ============================================================================
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROWS = [json.loads(l) for l in open(HERE / "priced_rows.jsonl") if l.strip()]
SAMPLE = {s["tk"]: s for s in json.load(open(HERE / "settled_sample.json"))}


def fee(p):
    return 0.07 * p * (1 - p)


def net_buy(r, h):  # buy YES at ask
    a = h.get("ask")
    if a is None or not (0 < a < 1):
        return None
    return (1 - a - fee(a)) if r["y"] == 1 else (-a - fee(a))


def net_sell(r, h):  # sell YES at bid == buy NO at 1-bid
    b = h.get("bid")
    if b is None or not (0 < b < 1):
        return None
    c = 1 - b
    return (1 - c - fee(c)) if r["y"] == 0 else (-c - fee(c))


def clustered(rows, key, side, lo, hi):
    """Event-clustered mean/SE/t of the net per-contract P&L for `side` in mid band [lo,hi)."""
    per_ev, per_ev_gross = defaultdict(list), defaultdict(list)
    for r in rows:
        h = r.get(key)
        if not h or h.get("mean") is None or not (lo <= h["mean"] < hi):
            continue
        v = net_buy(r, h) if side == "buy" else net_sell(r, h)
        if v is None:
            continue
        per_ev[r["ev"]].append(v)
        g = (r["y"] - h["mean"]) if side == "buy" else (h["mean"] - r["y"])
        per_ev_gross[r["ev"]].append(g)
    ev = [sum(v) / len(v) for v in per_ev.values()]
    evg = [sum(v) / len(v) for v in per_ev_gross.values()]
    nev = len(ev)
    if nev < 2:
        return None
    m = sum(ev) / nev
    var = sum((x - m) ** 2 for x in ev) / (nev - 1)
    se = math.sqrt(var / nev)
    return dict(nc=sum(len(v) for v in per_ev.values()), nev=nev,
                gross=sum(evg) / nev, net=m, se=se, t=(m / se if se > 0 else 0.0))


BUCKETS = [(0.01, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 0.30), (0.30, 0.40),
           (0.40, 0.50), (0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 0.90),
           (0.90, 0.95), (0.95, 0.99)]


def header():
    print(f"priced rows: {len(ROWS)}   distinct events: {len(set(r['ev'] for r in ROWS))}")
    from collections import Counter
    print("by category:", dict(Counter(r["cat"] for r in ROWS).most_common()))
    for key in ["p_open", "p_mid", "p_24h", "p_last"]:
        hs = [r[key] for r in ROWS if r.get(key)]
        va = sum(1 for h in hs if h.get("ask") is not None and 0 < h["ask"] < 1)
        vb = sum(1 for h in hs if h.get("bid") is not None and 0 < h["bid"] < 1)
        print(f"  {key}: rows={len(hs)} valid_ask={va} valid_bid={vb}")


def test1():
    print("\n### TEST 1 — longshot bias / calibration (horizon p_mid) ###")
    print(f"{'band':11s} {'n':>4} {'ev':>4} {'mid':>5} {'freq':>5} {'gBUY':>7} {'gSELL':>7}")
    for lo, hi in BUCKETS:
        sel = [(r, r["p_mid"]) for r in ROWS if r.get("p_mid")
               and r["p_mid"].get("mean") is not None and lo <= r["p_mid"]["mean"] < hi]
        if not sel:
            continue
        n = len(sel)
        freq = sum(r["y"] for r, _ in sel) / n
        mid = sum(h["mean"] for _, h in sel) / n
        gb = sum(r["y"] - h["mean"] for r, h in sel) / n
        print(f"{lo:.2f}-{hi:.2f} {n:>4} {len(set(r['ev'] for r,_ in sel)):>4} "
              f"{mid:>5.3f} {freq:>5.3f} {gb:>+7.3f} {-gb:>+7.3f}")

    print("\n  Strategy net edges (event-clustered), horizon p_mid:")
    for side, lo, hi, lab in [("sell", 0.01, 0.10, "sell deep longshots"),
                              ("sell", 0.01, 0.50, "sell all longshots"),
                              ("buy", 0.50, 0.99, "buy all favorites"),
                              ("buy", 0.90, 0.99, "buy deep favorites")]:
        s = clustered(ROWS, "p_mid", side, lo, hi)
        ok = "EXPLOITABLE" if (s and s["net"] - 2 * s["se"] > 0) else "no (>=0 within 2 SE)"
        print(f"    {lab:20s} nEv={s['nev']:>4} gross/ct={s['gross']:>+.3f} "
              f"net/ct={s['net']:>+.3f} SE={s['se']:.3f} t={s['t']:>+.2f}  {ok}")

    print("\n  Buy-favorites (0.50-0.99) by category:")
    bycat = defaultdict(list)
    for r in ROWS:
        bycat[r["cat"]].append(r)
    for cat, rs in sorted(bycat.items(), key=lambda x: -len(x[1])):
        s = clustered(rs, "p_mid", "buy", 0.50, 0.99)
        if s and s["nev"] >= 10:
            print(f"    {cat:22s} nEv={s['nev']:>4} net/ct={s['net']:>+.3f} SE={s['se']:.3f} t={s['t']:>+.2f}")


def test2_horizons():
    print("\n### TEST 2 — bias vs entry timing / near resolution (robustness) ###")
    print("  buy-favorites (0.50-0.99) across horizons:")
    for key in ["p_open", "p_mid", "p_24h", "p_last"]:
        s = clustered(ROWS, key, "buy", 0.50, 0.99)
        print(f"    {key}: net/ct={s['net']:>+.3f} SE={s['se']:.3f} t={s['t']:>+.2f}")
    print("  near-resolution (p_last):")
    for side, lo, hi, lab in [("sell", 0.01, 0.50, "sell longshots"),
                              ("buy", 0.50, 0.99, "buy favorites"),
                              ("buy", 0.90, 0.99, "buy deep favorites")]:
        s = clustered(ROWS, "p_last", side, lo, hi)
        print(f"    {lab:18s} nEv={s['nev']:>4} net/ct={s['net']:>+.3f} SE={s['se']:.3f} t={s['t']:>+.2f}")


def test3_timing():
    print("\n### TEST 3 — timing (open drift, weekday); granularity-limited ###")
    op = [(r, r["p_open"]) for r in ROWS if r.get("p_open")]
    print(f"  mean(outcome - open_price) = {sum(r['y']-h['mean'] for r,h in op)/len(op):+.4f} "
          "(= the favorite-longshot skew re-expressed, not a $ edge)")
    wd = defaultdict(list)
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for r in ROWS:
        s = SAMPLE.get(r["tk"])
        h = r.get("p_mid")
        if s and h and h.get("mean") is not None:
            wd[datetime.fromtimestamp(s["close"], tz=timezone.utc).weekday()].append(r["y"] - h["mean"])
    for d in range(7):
        if wd.get(d):
            print(f"    {names[d]}: n={len(wd[d]):>4} calib_err(freq-price)={sum(wd[d])/len(wd[d]):>+.3f}")
    print("  NOTE: candlestick granularity hourly(<=3d)/daily(>3d) -> sub-daily timing NOT resolvable.")


def test4_structural():
    print("\n### TEST 4 — other structural regularities ###")
    mr = []
    for r in ROWS:
        o, m, l = r.get("p_open"), r.get("p_mid"), r.get("p_last")
        if o and m and l and None not in (o.get("mean"), m.get("mean"), l.get("mean")):
            mr.append((m["mean"] - o["mean"], l["mean"] - m["mean"]))
    md1 = sum(a for a, _ in mr) / len(mr)
    md2 = sum(b for _, b in mr) / len(mr)
    cov = sum((a - md1) * (b - md2) for a, b in mr) / len(mr)
    s1 = statistics.pstdev([a for a, _ in mr])
    s2 = statistics.pstdev([b for _, b in mr])
    corr = cov / (s1 * s2) if s1 * s2 > 0 else 0
    print(f"  (a) mean-reversion corr(open->mid, mid->last) = {corr:+.3f} (n={len(mr)}) "
          f"{'reversion' if corr < -0.05 else 'none'}")
    vb = defaultdict(list)
    for r in ROWS:
        h = r.get("p_mid")
        if h and h.get("mean") is not None:
            t = "<1k" if r["vol"] < 1000 else ("1k-10k" if r["vol"] < 10000 else ">=10k")
            vb[t].append((h["mean"] - r["y"]) ** 2)
    print("  (b) market Brier by volume tier (composition artifact, not tradeable):")
    for t in ["<1k", "1k-10k", ">=10k"]:
        if vb.get(t):
            print(f"      {t:7s} n={len(vb[t]):>4} Brier={sum(vb[t])/len(vb[t]):.3f}")


def deep_favorite_dissection():
    print("\n### DEEP-FAVORITE (0.90-0.99) fragility — the lone nominal net-positive ###")
    for key in ["p_mid", "p_last"]:
        wins = losses = 0
        pnl = 0.0
        entries = []
        for r in ROWS:
            h = r.get(key)
            if not h or h.get("mean") is None or not (0.90 <= h["mean"] < 0.99):
                continue
            a = h.get("ask")
            if a is None or not (0 < a < 1):
                continue
            entries.append(a)
            if r["y"] == 1:
                wins += 1
                pnl += (1 - a - fee(a))
            else:
                losses += 1
                pnl += (-a - fee(a))
        n = wins + losses
        print(f"  [{key}] n={n} wins={wins} LOSSES={losses} "
              f"mean_entry_ask={sum(entries)/len(entries):.3f} sum_net_pnl/ct={pnl:+.2f} avg={pnl/n:+.4f}")
    print("  Verdict: extreme-negative-skew payoff; the entire positive rests on a handful of loss")
    print("  events (7 mid-life / 2 near-close). A few more losses (inside the Poisson CI) zeroes it,")
    print("  so the Gaussian t-stat is invalid. No ask-SIZE in candlesticks -> $10 fills unverifiable. DEAD.")


if __name__ == "__main__":
    header()
    test1()
    test2_horizons()
    test3_timing()
    test4_structural()
    deep_favorite_dissection()
