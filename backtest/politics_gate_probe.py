import os, time, json
from datetime import datetime
os.environ.setdefault("LLM_PROVIDER","anthropic")
import httpx
from research import analyzer, pricing
from research.models import Market
OUT="/c/Users/ianme/AppData/Local/Temp/claude/C--users-ianme-projects-PMRA/4d9d18c9-3a8b-4758-a657-7db90b67c2e0/scratchpad/probe_out.jsonl".replace("/c/","C:/")
BASE="https://api.elections.kalshi.com/trade-api/v2"
cl=httpx.Client(base_url=BASE, timeout=30.0)
def dt(s):
    try:return datetime.fromisoformat(s.replace("Z","+00:00"))
    except:return None
def ff(x):
    try:return float(x)
    except:return None
cursor=None;pages=0;cand=[];seen=set()
while pages<40:
    p={"limit":200,"status":"settled","with_nested_markets":True}
    if cursor:p["cursor"]=cursor
    b=cl.get("/events",params=p).json()
    for e in b.get("events",[]):
        if (e.get("category") or "")!="Politics":continue
        s=e.get("series_ticker")
        for m in (e.get("markets") or []):
            if m.get("mve_collection_ticker") or m.get("ticker","").startswith("KXMVE"):continue
            r=str(m.get("result") or "").lower()
            if r not in ("yes","no"):continue
            o,c=dt(m.get("open_time")),dt(m.get("close_time"))
            if not(o and c) or (c-o).days>30 or float(m.get("volume_fp") or 0)<300:continue
            if not m.get("rules_primary") or s in seen:continue
            seen.add(s)
            cand.append(dict(tk=m.get("ticker"),title=(m.get("title") or e.get("title") or "")[:70],
                             y=1 if r=="yes" else 0, close=c, vol=float(m.get("volume_fp") or 0),
                             rules=m.get("rules_primary"), sub=m.get("yes_sub_title") or ""))
    cursor=b.get("cursor") or "";pages+=1
    if not cursor:break
cl.close()
picks=cand[:6]
open(OUT,"w").close()
for m in picks:
    mk=Market(id=m['tk'],exchange="kalshi",slug=m['tk'],question=m['title'],market_prob=0.5,
              volume_24h=0.0,volume_total=m['vol'],liquidity=None,yes_token_id=None,
              end_date=m['close'],tags=["Politics"],description=m['sub'],resolution_rules=m['rules'])
    a=analyzer.analyze_market(mk)
    rec={"tk":m['tk'],"title":m['title'],"actual":m['y']}
    if a.error:
        rec["error"]=a.error
    else:
        rec.update(prob=a.claude_prob,conf=a.confidence,search=a.web_search_requests,
                   cost=pricing.cost_usd(a.model,a.input_tokens,a.output_tokens,a.cache_creation_input_tokens,a.cache_read_input_tokens,web_search_requests=a.web_search_requests),
                   summary=a.summary,factors=a.factors)
    with open(OUT,"a") as f: f.write(json.dumps(rec)+"\n")
    time.sleep(1.0)
print("DONE")
