import json, urllib.request, statistics
LID="1312862141639839744"
def get(u): return json.load(urllib.request.urlopen(u, timeout=30))
BASE=r"C:/Users/jjfro/Documents/keep-the-change-tribune/"
rk=json.load(open(BASE+"rankings.json"))["rankings"]
info={t["roster_id"]:t for t in rk}
WEEKS=list(range(1,11))
S={t:dict(apw=0.0, medw=0, lucky=0, unlucky=0, pts=[], pa=0.0, w=0, l=0, tie=0) for t in info}
for w in WEEKS:
    rows=[r for r in get(f"https://api.sleeper.app/v1/league/{LID}/matchups/{w}") if r.get("points") is not None]
    pts={r["roster_id"]:r["points"] for r in rows}
    n=len(pts); vals=sorted(pts.values())
    med=(vals[n//2-1]+vals[n//2])/2
    for t in pts:
        p=pts[t]; S[t]["pts"].append(p)
        aw=len([o for o in pts if o!=t and p>pts[o]]); tie=len([o for o in pts if o!=t and p==pts[o]])
        S[t]["apw"]+=(aw+0.5*tie)/(n-1)
        if p>med: S[t]["medw"]+=1
    mids={}
    for r in rows: mids.setdefault(r.get("matchup_id"),[]).append(r)
    for m,rs in mids.items():
        if len(rs)!=2 or m is None: continue
        a,b=rs; pa,pb=a["points"],b["points"]
        S[a["roster_id"]]["pa"]+=pb; S[b["roster_id"]]["pa"]+=pa
        if pa>pb:
            S[a["roster_id"]]["w"]+=1; S[b["roster_id"]]["l"]+=1
            if pa<med: S[a["roster_id"]]["lucky"]+=1
            if pb>med: S[b["roster_id"]]["unlucky"]+=1
        elif pb>pa:
            S[b["roster_id"]]["w"]+=1; S[a["roster_id"]]["l"]+=1
            if pb<med: S[b["roster_id"]]["lucky"]+=1
            if pa>med: S[a["roster_id"]]["unlucky"]+=1
        else:
            S[a["roster_id"]]["tie"]+=1; S[b["roster_id"]]["tie"]+=1
rows=[]
for t,s in S.items():
    r0=info[t]; pf=sum(s["pts"]); actual=s["w"]+0.5*s["tie"]
    luck=actual-s["apw"]
    rows.append(dict(team=r0["owner"]["team_name"], owner=r0["owner"]["display_name"], rid=t,
        w=s["w"], l=s["l"], tie=s["tie"], pf=round(pf,1), pa=round(s["pa"],1),
        exp=round(s["apw"],2), luck=round(luck,2), medw=s["medw"], luckmed=round(actual-s["medw"],2),
        lucky=s["lucky"], unlucky=s["unlucky"], avg=round(statistics.mean(s["pts"]),1),
        sd=round(statistics.pstdev(s["pts"]),1), power=r0["rank"]))
for k in ("pf","pa"):
    for i,r in enumerate(sorted(rows,key=lambda r:-r[k]),1): r[k+"_rank"]=i
rows.sort(key=lambda r:-r["luck"])
tot=0
for i,r in enumerate(rows,1):
    r["luck_rank"]=i; tot+=r["luck"]
json.dump(rows,open(r"C:/Users/jjfro/AppData/Local/hermes/cache/scratch/ktc_luck3.json","w"),indent=1)
print("sum luck",round(tot,2),"mean exp",round(statistics.mean(r['exp'] for r in rows),2))
for r in rows:
    print(f"{r['luck_rank']:>2} {r['team'][:26]:26} {r['w']}-{r['l']}{('-'+str(r['tie'])) if r['tie'] else '':2} PF{r['pf']:7.1f}(#{r['pf_rank']:>2}) PA{r['pa']:7.1f}(#{r['pa_rank']:>2}) exp{r['exp']:5.2f} LUCK{r['luck']:+6.2f} medW{r['medw']:>2} luckyW{r['lucky']:>2} unluckyL{r['unlucky']:>2} avg{r['avg']:6.1f}sd{r['sd']:4.1f} pwr#{r['power']}")
