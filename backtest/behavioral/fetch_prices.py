import httpx, time, json
BASE="https://api.elections.kalshi.com/trade-api/v2"
SP=r"C:/Users/ianme/AppData/Local/Temp/claude/C--users-ianme-projects-PMRA/4d9d18c9-3a8b-4758-a657-7db90b67c2e0/scratchpad"
cl=httpx.Client(base_url=BASE, timeout=30.0)
def ff(x):
    try:
        v=float(x); return v
    except: return None
def get_cs(series,tk,start,end,interval):
    for attempt in range(4):
        try:
            r=cl.get(f"/series/{series}/markets/{tk}/candlesticks",
                     params={"start_ts":int(start),"end_ts":int(end),"period_interval":interval})
            if r.status_code==429:
                time.sleep(1.0+attempt); continue
            if r.status_code!=200: return None
            return r.json().get("candlesticks",[])
        except Exception:
            time.sleep(0.5+attempt)
    return None
def bar_vals(c):
    pr=c.get("price") or {}; b=c.get("yes_bid") or {}; a=c.get("yes_ask") or {}
    mean=ff(pr.get("mean_dollars")) or ff(pr.get("close_dollars"))
    bid=ff(b.get("close_dollars")); ask=ff(a.get("close_dollars"))
    return mean,bid,ask
def pick(bars, target):
    best=None
    for ts,mean,bid,ask in bars:
        if mean is None or not(0.0<mean<1.0): continue
        d=abs(ts-target)
        if best is None or d<best[0]: best=(d,ts,mean,bid,ask)
    return None if best is None else {"ts":best[1],"mean":best[2],"bid":best[3],"ask":best[4]}
sample=json.load(open(SP+"/settled_sample.json"))
out=open(SP+"/priced_rows.jsonl","w"); done=0; hit=0
for s in sample:
    life=s["close"]-s["open"]; interval=60 if life<=3*86400 else 1440
    start=max(s["open"], s["close"]-30*86400)
    cs=get_cs(s["series"],s["tk"],start,s["close"],interval)
    done+=1
    if cs:
        bars=[]
        for c in cs:
            ts=c.get("end_period_ts",0)
            if ts>s["close"]: continue
            m,b,a=bar_vals(c); bars.append((ts,m,b,a))
        bars.sort()
        valid=[x for x in bars if x[1] is not None and 0<x[1]<1]
        if valid:
            hit+=1
            rec=dict(cat=s["cat"],ev=s["ev"],tk=s["tk"],y=s["y"],vol=s["vol"],
                     life_days=life/86400.0,
                     p_open=pick(bars,s["open"]),
                     p_mid=pick(bars,(s["open"]+s["close"])/2),
                     p_24h=pick(bars,s["close"]-86400),
                     p_last=pick(bars,s["close"]-1))
            out.write(json.dumps(rec)+"\n"); out.flush()
    if done%250==0:
        print(f"progress {done}/{len(sample)} priced_hit={hit}", flush=True)
    time.sleep(0.05)
out.close()
print(f"DONE fetched={done} priced_rows={hit}", flush=True)
