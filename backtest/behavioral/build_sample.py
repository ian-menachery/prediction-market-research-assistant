# ============================================================================
# ARCHIVED — reproduces the behavioral-study SAMPLE (see ../../BACKTEST_NOTES.md, "Test d").
# READ-ONLY, $0: only free Kalshi public reads. Enumerates SETTLED events and builds a
# correlation-controlled sample of resolved binary markets:
#   - <= EVENT_CAP legs per event (limits correlated legs of one underlying event)
#   - <= CAT_CAP markets per category (so no single category dominates)
#   - volume_fp >= 100 (tradeable)
# Writes settled_sample.json. Then run fetch_prices.py to attach entry-time prices.
# Note: the settled universe drifts over time, so re-running yields a different snapshot;
# the frozen dataset actually analyzed is priced_rows.jsonl in this folder.
# ============================================================================
import httpx, time, json
from collections import Counter
from datetime import datetime

BASE = "https://api.elections.kalshi.com/trade-api/v2"
CAT_CAP = 600
EVENT_CAP = 3
OUT = "settled_sample.json"


def dt(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def main():
    cl = httpx.Client(base_url=BASE, timeout=30.0)
    percat, perev, sample, seen_cat = Counter(), Counter(), [], Counter()
    cursor, pages = None, 0
    while pages < 250 and len(sample) < 5000:
        p = {"limit": 200, "status": "settled", "with_nested_markets": True}
        if cursor:
            p["cursor"] = cursor
        b = cl.get("/events", params=p).json()
        for e in b.get("events", []):
            c = e.get("category") or "?"
            ev, series = e.get("event_ticker"), e.get("series_ticker")
            for m in (e.get("markets") or []):
                if m.get("mve_collection_ticker") or m.get("ticker", "").startswith("KXMVE"):
                    continue
                r = str(m.get("result") or "").lower()
                if r not in ("yes", "no"):
                    continue
                seen_cat[c] += 1
                if float(m.get("volume_fp") or 0) < 100:
                    continue
                if percat[c] >= CAT_CAP or perev[ev] >= EVENT_CAP:
                    continue
                o, cl_t = dt(m.get("open_time")), dt(m.get("close_time"))
                if not (o and cl_t) or cl_t <= o:
                    continue
                sample.append(dict(cat=c, ev=ev, series=series, tk=m.get("ticker"),
                                   y=1 if r == "yes" else 0, open=o.timestamp(),
                                   close=cl_t.timestamp(), vol=float(m.get("volume_fp") or 0)))
                percat[c] += 1
                perev[ev] += 1
        cursor = b.get("cursor") or ""
        pages += 1
        if not cursor:
            break
        time.sleep(0.04)
    cl.close()
    json.dump(sample, open(OUT, "w"))
    print(f"pages={pages} sample_n={len(sample)} events={len(set(s['ev'] for s in sample))}")
    print("by category:", dict(percat.most_common()))


if __name__ == "__main__":
    main()
