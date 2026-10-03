from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import httpx, csv, io, math, random, asyncio
from collections import defaultdict
from datetime import datetime, timezone

app = FastAPI(title="EDGE90", version="1.0")

LEAGUES={"E0":"Premier League","SP1":"LaLiga","D1":"Bundesliga","I1":"Serie A","F1":"Ligue 1","N1":"Eredivisie","P1":"Primeira Liga","E1":"Championship","SP2":"LaLiga 2"}
SEASONS=["2425","2526","2627"]
BASE="https://www.football-data.co.uk/mmz4281/{season}/{div}.csv"
FIXTURES="https://www.football-data.co.uk/matches/resources/fixtures.csv"
STATE={"models":{},"fixtures":[],"recommendations":[],"status":"initializing","updated":None,"errors":[]}

def fnum(x,d=None):
    try: return float(str(x).strip()) if x not in (None,"") else d
    except: return d

def parse_csv(text): return list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))
def pmf(k,l): return math.exp(-l)*l**k/math.factorial(k)
def fair(p): return round(1/max(.01,min(.99,p)),2)

def probs(lh,la):
    h=d=a=o15=o25=btts=0.0
    for i in range(9):
        for j in range(9):
            p=pmf(i,lh)*pmf(j,la)
            if i>j:h+=p
            elif i==j:d+=p
            else:a+=p
            if i+j>=2:o15+=p
            if i+j>=3:o25+=p
            if i>0 and j>0:btts+=p
    s=h+d+a
    return {"HOME":h/s,"DRAW":d/s,"AWAY":a/s,"OVER15":o15,"OVER25":o25,"BTTS":btts,"1X":(h+d)/s,"X2":(d+a)/s}

def remove_margin(odds):
    inv=[1/o for o in odds if o and o>1]; s=sum(inv)
    return [x/s for x in inv] if s else []

class LeagueModel:
    def __init__(self,div,rows,do_backtest=True):
        self.div=div; self.name=LEAGUES.get(div,div)
        self.rows=[(r,int(float(r["FTHG"])),int(float(r["FTAG"]))) for r in rows if fnum(r.get("FTHG")) is not None and fnum(r.get("FTAG")) is not None]
        self.n=len(self.rows); self.fit()
        self.metrics=self.backtest() if do_backtest else {"n":0,"brier":None,"log_loss":None}
    def fit(self):
        if not self.rows:
            self.avg_home=1.45;self.avg_away=1.15;self.attack={};self.defence={};return
        hgf=defaultdict(float);hga=defaultdict(float);hc=defaultdict(float)
        agf=defaultdict(float);aga=defaultdict(float);ac=defaultdict(float)
        th=ta=tw=0.0;n=len(self.rows)
        teams=set()
        for k,(r,hg,ag) in enumerate(self.rows):
            w=.35+.65*((k+1)/n)**2; ht=r["HomeTeam"];at=r["AwayTeam"];teams|={ht,at}
            hgf[ht]+=hg*w;hga[ht]+=ag*w;hc[ht]+=w;agf[at]+=ag*w;aga[at]+=hg*w;ac[at]+=w
            th+=hg*w;ta+=ag*w;tw+=w
        self.avg_home=max(.5,th/tw);self.avg_away=max(.4,ta/tw);self.attack={};self.defence={}
        for t in teams:
            hf=(hgf[t]+self.avg_home*3)/(hc[t]+3); af=(agf[t]+self.avg_away*3)/(ac[t]+3)
            ha=(hga[t]+self.avg_away*3)/(hc[t]+3); aa=(aga[t]+self.avg_home*3)/(ac[t]+3)
            self.attack[t]=(hf/self.avg_home+af/self.avg_away)/2
            self.defence[t]=(ha/self.avg_away+aa/self.avg_home)/2
    def predict(self,home,away):
        lh=max(.18,min(3.8,self.avg_home*self.attack.get(home,1)*self.defence.get(away,1)))
        la=max(.15,min(3.4,self.avg_away*self.attack.get(away,1)*self.defence.get(home,1)))
        p=probs(lh,la); base=probs(self.avg_home,self.avg_away)
        return lh,la,{k:.9*v+.1*base[k] for k,v in p.items()}
    def backtest(self):
        if self.n<90:return {"n":self.n,"brier":None,"log_loss":None}
        start=max(70,self.n-140); vals=[]
        for i in range(start,self.n):
            if i%10==start%10:
                temp=LeagueModel(self.div,[r for r,_,__ in self.rows[:i]],do_backtest=False)
            r,hg,ag=self.rows[i]; _,_,p=temp.predict(r["HomeTeam"],r["AwayTeam"])
            q=[p["HOME"],p["DRAW"],p["AWAY"]]; y=[hg>ag,hg==ag,hg<ag]
            vals.append((sum((q[z]-float(y[z]))**2 for z in range(3))/3,-math.log(max(q[y.index(True)],1e-8))))
        return {"n":len(vals),"brier":round(sum(x[0] for x in vals)/len(vals),4),"log_loss":round(sum(x[1] for x in vals)/len(vals),4)}

def odds_ref(row,key):
    if key not in ("HOME","DRAW","AWAY","OVER25"):return None,None
    if key in ("HOME","DRAW","AWAY"):
        hs=[fnum(row.get(x)) for x in ("AvgH","B365H","PSH","MaxH")];ds=[fnum(row.get(x)) for x in ("AvgD","B365D","PSD","MaxD")];as_=[fnum(row.get(x)) for x in ("AvgA","B365A","PSA","MaxA")]
        oh=next((x for x in hs if x and x>1),None);od=next((x for x in ds if x and x>1),None);oa=next((x for x in as_ if x and x>1),None)
        if oh and od and oa:
            mp=remove_margin([oh,od,oa]); data={"HOME":(oh,mp[0]),"DRAW":(od,mp[1]),"AWAY":(oa,mp[2])};return data[key]
    if key=="OVER25":
        oo=next((fnum(row.get(x)) for x in ("Avg>2.5","B365>2.5","P>2.5","Max>2.5") if fnum(row.get(x))),None)
        ou=next((fnum(row.get(x)) for x in ("Avg<2.5","B365<2.5","P<2.5","Max<2.5") if fnum(row.get(x))),None)
        if oo and ou:
            mp=remove_margin([oo,ou]);return oo,mp[0]
    return None,None

def recommendation(model,row):
    home,away=row.get("HomeTeam",""),row.get("AwayTeam","");lh,la,p=model.predict(home,away)
    labels={"HOME":f"{home} gana","DRAW":"Empate","AWAY":f"{away} gana","OVER15":"Más de 1.5 goles","OVER25":"Más de 2.5 goles","BTTS":"Ambos marcan","1X":f"{home} o empate","X2":f"{away} o empate"}
    cand=[]
    for key in labels:
        pr=p[key];od,mp=odds_ref(row,key);edge=pr-mp if mp is not None else None;ev=pr*od-1 if od else None
        eff=max(25,min(220,model.n/4));se=math.sqrt(pr*(1-pr)/eff);lo=max(.01,pr-1.64*se);hi=min(.99,pr+1.64*se)
        cand.append({"selection":key,"market":labels[key],"probability":pr,"lower":lo,"upper":hi,"fair_odds":fair(pr),"odds":od,"market_prob":mp,"edge":edge,"ev":ev})
    with_edge=[x for x in cand if x["edge"] is not None and x["edge"]>=.02 and x["ev"] is not None and x["ev"]>0]
    best=max(with_edge,key=lambda x:(x["edge"],x["probability"]),default=max(cand,key=lambda x:x["probability"]))
    return {**best,"league":model.name,"div":model.div,"home":home,"away":away,"date":row.get("Date",""),"kickoff":row.get("Time",""),"lambda_home":lh,"lambda_away":la,"sample_size":model.n,"quality":"Alta" if model.n>=250 else "Media","uncertainty":"Baja" if model.n>=300 else "Media","model_metrics":model.metrics}

async def fetch(url):
    async with httpx.AsyncClient(timeout=20,follow_redirects=True,headers={"User-Agent":"EDGE90 research client"}) as c:
        r=await c.get(url);r.raise_for_status();return r.text

async def rebuild():
    errors=[];models={}
    for div in LEAGUES:
        rows=[]
        for s in SEASONS:
            try: rows+=parse_csv(await fetch(BASE.format(season=s,div=div)))
            except Exception as e: errors.append(f"{div}/{s}:{type(e).__name__}")
        if len(rows)>=40:
            try: models[div]=LeagueModel(div,rows)
            except Exception as e: errors.append(f"model {div}:{type(e).__name__}")
    fixtures=[]
    try: fixtures=parse_csv(await fetch(FIXTURES))
    except Exception as e:errors.append(f"fixtures:{type(e).__name__}")
    recs=[]
    for row in fixtures:
        div=row.get("Div")
        if div in models and row.get("HomeTeam") and row.get("AwayTeam"):
            try: recs.append(recommendation(models[div],row))
            except: pass
    STATE.update({"models":models,"fixtures":fixtures,"recommendations":recs,"status":"live" if recs else ("historical_only" if models else "offline"),"updated":datetime.now(timezone.utc).isoformat(),"errors":errors[-10:]})

@app.on_event("startup")
async def startup(): asyncio.create_task(rebuild())

@app.get("/api/health")
def health(): return {"ok":True,"status":STATE["status"],"models":len(STATE["models"]),"fixtures":len(STATE["fixtures"]),"updated":STATE["updated"],"errors":STATE["errors"]}

@app.get("/api/overview")
def overview():
    return {"status":STATE["status"],"updated":STATE["updated"],"models":[{"league":m.name,"matches":m.n,**m.metrics} for m in STATE["models"].values()],"count":len(STATE["recommendations"]),"source":"Football-Data.co.uk"}

@app.post("/api/refresh")
async def refresh(): await rebuild(); return overview()

@app.get("/api/recommendations")
def recommendations(min_probability:float=Query(.55,ge=.5,le=.95)):
    all_items=[]
    for i,r in enumerate(STATE["recommendations"]):
        x=dict(r);x["id"]=i+1;x["meets_probability"]=x["probability"]>=min_probability;all_items.append(x)
    all_items.sort(key=lambda x:(x.get("date",""),x.get("kickoff",""),x.get("league",""),x.get("home","")))
    return {"items":all_items,"count":len(all_items),"matching":sum(1 for x in all_items if x["meets_probability"]),"status":STATE["status"],"updated":STATE["updated"]}

@app.get("/api/matches")
def matches():
    out=[]
    for i,r in enumerate(STATE["recommendations"]):
        x=dict(r);x["id"]=i+1;out.append(x)
    out.sort(key=lambda x:(x.get("date",""),x.get("kickoff",""),x.get("league",""),x.get("home","")))
    return {"items":out,"count":len(out),"status":STATE["status"],"updated":STATE["updated"]}

class Challenge(BaseModel):
    bankroll:float=100
    target:float=500
    difficulty:str="easy"

@app.post("/api/challenge")
def challenge(x:Challenge):
    cfg={
        "easy":{"min_p":.70,"stake":.01,"label":"Fácil"},
        "medium":{"min_p":.62,"stake":.02,"label":"Medio"},
        "hard":{"min_p":.55,"stake":.035,"label":"Difícil"},
    }.get(x.difficulty,{"min_p":.70,"stake":.01,"label":"Fácil"})
    pool=[r for r in STATE["recommendations"] if r["probability"]>=cfg["min_p"]]
    pool.sort(key=lambda r:(r["probability"],r["edge"] if r["edge"] is not None else -1),reverse=True)
    chosen=pool[:6]
    bankroll=max(1.0,x.bankroll)
    steps=[]
    joint=1.0
    for idx,r in enumerate(chosen,1):
        stake=max(.5,round(bankroll*cfg["stake"],2))
        odds=r["odds"] if r["odds"] and r["odds"]>1 else r["fair_odds"]
        win_bank=bankroll+stake*(odds-1)
        lose_bank=max(0,bankroll-stake)
        joint*=r["probability"]
        steps.append({
            "step":idx,"home":r["home"],"away":r["away"],"league":r["league"],
            "market":r["market"],"probability":r["probability"],"stake":stake,
            "stake_pct":cfg["stake"],"odds":odds,"odds_type":"referencia pública" if r["odds"] else "cuota justa del modelo",
            "bankroll_before":bankroll,"bankroll_if_win":win_bank,"bankroll_if_lose":lose_bank
        })
        bankroll=win_bank
        if bankroll>=x.target: break
    return {
        "difficulty":cfg["label"],"min_probability":cfg["min_p"],"starting_bankroll":x.bankroll,
        "target":x.target,"steps":steps,"sequence_probability":joint if steps else None,
        "projected_if_all_win":bankroll if steps else x.bankroll,
        "note":"La secuencia es una guía de riesgo basada en las oportunidades actuales. No persigue pérdidas ni aumenta stake tras perder."
    }

HTML=r'''<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>EDGE90</title><style>
:root{--bg:#07110e;--p:#10201a;--p2:#142820;--line:#264137;--txt:#f6fbf8;--mut:#91a69e;--a:#42e99a}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 90% 0,#153f2e,transparent 30%),var(--bg);color:var(--txt);font-family:Inter,system-ui,sans-serif}.wrap{max-width:1250px;margin:auto;padding:28px}.top{display:flex;justify-content:space-between;gap:20px;align-items:center}.brand{font-weight:900;letter-spacing:.12em;font-size:22px}.brand b{color:var(--a)}.status,.mut{color:var(--mut)}.hero{margin:30px 0;display:grid;grid-template-columns:1.25fr .75fr;gap:16px}.card{background:linear-gradient(180deg,var(--p2),var(--p));border:1px solid var(--line);border-radius:20px;padding:22px}.ey{color:var(--a);font-size:11px;font-weight:800;letter-spacing:.14em}.big{font-size:44px;font-weight:900;color:var(--a)}input[type=range]{width:100%;accent-color:var(--a)}.row{display:flex;justify-content:space-between;gap:14px;align-items:center}.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.stat{background:#0b1713;border:1px solid var(--line);border-radius:14px;padding:14px}.stat span{display:block;color:var(--mut);font-size:11px}.stat strong{font-size:22px}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.match h3{margin:8px 0}.market{color:var(--a);font-weight:800}.prob{font-size:38px;font-weight:900}.tag{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:6px 9px;margin:4px 5px 0 0;font-size:11px}.tag.ok{border-color:var(--a);color:var(--a)}.tag.no{color:var(--mut)}.btn{border:0;background:var(--a);color:#052016;font-weight:900;padding:12px 16px;border-radius:12px;cursor:pointer}.btn2{border:1px solid var(--line);background:transparent;color:var(--txt);font-weight:800;padding:11px 14px;border-radius:12px;cursor:pointer}.tabs{display:flex;gap:8px;margin:18px 0;flex-wrap:wrap}.tabs button{border:1px solid var(--line);background:var(--p);color:var(--txt);padding:9px 12px;border-radius:999px;cursor:pointer}.hidden{display:none}.section{margin-top:26px}.modelrow{display:flex;justify-content:space-between;border-bottom:1px solid var(--line);padding:10px 0}.info{background:#0b1713;border:1px solid var(--line);border-radius:14px;padding:14px;line-height:1.55}.datebar{display:flex;gap:8px;flex-wrap:wrap;margin:18px 0}.datebar button{background:#0b1713;color:var(--txt);border:1px solid var(--line);border-radius:10px;padding:9px 12px}.datebar button.active{border-color:var(--a);color:var(--a)}.step{border-left:3px solid var(--a);padding-left:14px;margin:18px 0}.stepnum{color:var(--a);font-weight:900}.challengeStart{display:grid;grid-template-columns:1fr 1fr;gap:16px}.challengeStart input,.challengeStart select{width:100%;background:#091612;border:1px solid var(--line);color:var(--txt);padding:12px;border-radius:10px;margin:6px 0 14px}.edgeHelp{font-size:12px;color:var(--mut);margin-top:10px}.edgeHelp strong{color:var(--txt)}@media(max-width:900px){.hero,.challengeStart{grid-template-columns:1fr}.grid{grid-template-columns:1fr 1fr}}@media(max-width:600px){.wrap{padding:18px}.grid{grid-template-columns:1fr}.top{align-items:flex-start}.stats{grid-template-columns:1fr}.big{font-size:36px}}
</style></head><body><div class="wrap"><div class="top"><div><div class="brand"><b>EDGE90</b> FOOTBALL INTELLIGENCE</div><div class="mut">Todos los partidos del día · apuesta sugerida · % estimado</div></div><div id="status" class="status">Preparando datos reales…</div></div>
<div class="tabs"><button onclick="show('home')">Partidos</button><button onclick="show('model')">Modelo</button><button onclick="show('challenge')">Reto</button></div>
<section id="home"><div class="hero"><div class="card"><div class="row"><div><div class="ey">PORCENTAJE MÍNIMO</div><h2>Marca las apuestas que llegan a tu nivel</h2></div><div id="pv" class="big">70%</div></div><input id="ps" type="range" min="55" max="95" value="70"><div class="row mut"><span>55%</span><span>95%</span></div><p class="mut">Siempre verás todos los partidos. Los que alcancen tu porcentaje aparecerán marcados como <b>“Cumple tu filtro”</b>.</p><div class="info"><strong>¿Qué es el edge?</strong><br><span class="mut">Es la diferencia entre la probabilidad de EDGE90 y la probabilidad que refleja una cuota de mercado. Ejemplo: modelo 65% y mercado 60% = edge +5 puntos. Si no tenemos cuota pública, no lo inventamos.</span></div></div><div class="card"><div class="ey">RESUMEN</div><div class="stats"><div class="stat"><strong id="count">—</strong><span>partidos</span></div><div class="stat"><strong id="matching">—</strong><span>cumplen tu %</span></div><div class="stat"><strong id="models">—</strong><span>ligas modeladas</span></div></div><br><button class="btn" onclick="refreshData()">Actualizar datos</button></div></div><div id="datebar" class="datebar"></div><h2 id="dayTitle">Partidos</h2><div id="grid" class="grid"></div></section>
<section id="model" class="hidden section"><div class="card"><div class="ey">TRANSPARENCIA</div><h2>Cómo sale cada porcentaje</h2><p class="mut">EDGE90 entrena fuerza ofensiva y defensiva por equipo, separa local/visitante, pondera más los partidos recientes, genera una distribución Poisson de goles y aplica regularización hacia la media de liga. De ahí obtiene mercados como 1X2, goles, BTTS y doble oportunidad. El backtest mantiene el orden temporal.</p><div id="modelrows"></div></div></section>
<section id="challenge" class="hidden section"><div id="challengeIntro" class="card"><div class="ey">RETO</div><h2>Construye un reto paso a paso</h2><p class="mut">EDGE90 te mostrará qué selección iría primero, cuánto representa del bankroll, el % estimado de acierto y qué vendría después. Nunca usa martingala ni aumenta la apuesta para recuperar pérdidas.</p><button class="btn" onclick="startChallenge()">Empezar reto</button></div><div id="challengeSetup" class="hidden challengeStart section"><div class="card"><div class="stepnum">PASO 1 DE 3</div><h2>¿Con cuánto empiezas?</h2><input id="bank" type="number" min="10" value="100"><button class="btn" onclick="challengeStep(2)">Siguiente</button></div><div class="card"><p class="mut">Primero fijamos tu capital. El stake se calculará como un porcentaje pequeño de ese bankroll.</p></div></div><div id="challengeStep2" class="hidden challengeStart section"><div class="card"><div class="stepnum">PASO 2 DE 3</div><h2>¿Cuál es tu objetivo?</h2><input id="target" type="number" min="20" value="500"><button class="btn2" onclick="challengeStep(1)">Atrás</button> <button class="btn" onclick="challengeStep(3)">Siguiente</button></div><div class="card"><p class="mut">El objetivo sirve para proyectar el recorrido. No implica que exista una ruta segura para alcanzarlo.</p></div></div><div id="challengeStep3" class="hidden challengeStart section"><div class="card"><div class="stepnum">PASO 3 DE 3</div><h2>Elige el nivel de riesgo</h2><select id="diff"><option value="easy">Fácil · mínimo 70% · stake 1%</option><option value="medium">Medio · mínimo 62% · stake 2%</option><option value="hard">Difícil · mínimo 55% · stake 3,5%</option></select><button class="btn2" onclick="challengeStep(2)">Atrás</button> <button class="btn" onclick="buildChallenge()">Crear mi reto</button></div><div class="card"><p class="mut"><b>Fácil</b> significa menor exposición, no “más seguro”. <b>Difícil</b> acepta selecciones con menor probabilidad y más volatilidad.</p></div></div><div id="challengePlan" class="hidden section"></div></section>
<p class="mut" style="margin-top:30px;font-size:12px">18+ · Juega con responsabilidad. EDGE90 es una herramienta de análisis estadístico; no garantiza beneficios. Fuente pública principal: Football-Data.co.uk.</p></div><script>
const $=s=>document.querySelector(s);let all=[],activeDate='all';function pc(x){return x==null?'—':(x*100).toFixed(1)+'%'}function pp(x){return x==null?'—':(x>=0?'+':'')+(x*100).toFixed(1)+' pp'}function show(id){['home','model','challenge'].forEach(x=>$('#'+x).classList.toggle('hidden',x!==id))}
async function ov(){let r=await fetch('/api/overview'),d=await r.json();$('#status').textContent=(d.status==='live'?'Datos reales cargados':d.status==='historical_only'?'Histórico real cargado · sin fixtures actuales':'Fuente no disponible')+(d.updated?' · '+new Date(d.updated).toLocaleString('es-ES'):'');$('#models').textContent=d.models.length;$('#modelrows').innerHTML=d.models.map(m=>'<div class="modelrow"><span>'+m.league+' · '+m.matches+' partidos</span><strong>Brier '+(m.brier??'—')+'</strong></div>').join('')}
function renderDates(){let dates=[...new Set(all.map(x=>x.date).filter(Boolean))];let h='<button class="'+(activeDate==='all'?'active':'')+'" onclick="setDate(\'all\')">Todos</button>';h+=dates.map(d=>'<button class="'+(activeDate===d?'active':'')+'" onclick="setDate(\''+d+'\')">'+d+'</button>').join('');$('#datebar').innerHTML=h}
function setDate(d){activeDate=d;renderDates();renderMatches()}
function renderMatches(){let threshold=+$('#ps').value/100;let items=all.filter(x=>activeDate==='all'||x.date===activeDate);let matching=items.filter(x=>x.probability>=threshold).length;$('#count').textContent=items.length;$('#matching').textContent=matching;$('#dayTitle').textContent=activeDate==='all'?'Todos los partidos disponibles':'Partidos · '+activeDate;$('#grid').innerHTML=items.length?items.map(x=>{let ok=x.probability>=threshold;return '<article class="card match"><div class="row"><div class="ey">'+x.league+(x.kickoff?' · '+x.kickoff:'')+'</div><span class="tag '+(ok?'ok':'no')+'">'+(ok?'✓ Cumple tu filtro':'Por debajo de '+Math.round(threshold*100)+'%')+'</span></div><h3>'+x.home+' <span class="mut">vs</span> '+x.away+'</h3><div class="mut">APUESTA SUGERIDA</div><div class="market">'+x.market+'</div><div class="prob">'+pc(x.probability)+'</div><p class="mut">Rango estimado '+pc(x.lower)+'–'+pc(x.upper)+'</p><div class="stats"><div class="stat"><strong>'+x.fair_odds.toFixed(2)+'</strong><span>cuota justa</span></div><div class="stat"><strong>'+(x.odds?x.odds.toFixed(2):'—')+'</strong><span>cuota pública ref.</span></div><div class="stat"><strong>'+pp(x.edge)+'</strong><span>edge</span></div></div><div class="edgeHelp">'+(x.edge==null?'Sin cuota pública comparable: mostramos la probabilidad y cuota justa del modelo.':'<strong>Edge '+pp(x.edge)+':</strong> ventaja estimada frente a la probabilidad del mercado.')+'</div><div><span class="tag">Datos '+x.quality+'</span><span class="tag">Muestra '+x.sample_size+'</span></div></article>'}).join(''):'<div class="card mut">No hay partidos disponibles en esta fecha desde la fuente pública.</div>'}
async function loadMatches(){let r=await fetch('/api/matches'),d=await r.json();all=d.items||[];renderDates();renderMatches()}
async function refreshData(){await fetch('/api/refresh',{method:'POST'});await ov();await loadMatches()}$('#ps').oninput=()=>{$('#pv').textContent=$('#ps').value+'%';renderMatches()};
function startChallenge(){$('#challengeIntro').classList.add('hidden');challengeStep(1)}
function challengeStep(n){['challengeSetup','challengeStep2','challengeStep3'].forEach(x=>$('#'+x).classList.add('hidden'));if(n===1)$('#challengeSetup').classList.remove('hidden');if(n===2)$('#challengeStep2').classList.remove('hidden');if(n===3)$('#challengeStep3').classList.remove('hidden')}
async function buildChallenge(){let body={bankroll:+$('#bank').value,target:+$('#target').value,difficulty:$('#diff').value};let r=await fetch('/api/challenge',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),d=await r.json();['challengeSetup','challengeStep2','challengeStep3'].forEach(x=>$('#'+x).classList.add('hidden'));let html='<div class="card"><div class="ey">TU RETO · '+d.difficulty.toUpperCase()+'</div><h2>'+d.starting_bankroll.toFixed(2)+' € → '+d.target.toFixed(2)+' €</h2><p class="mut">Selecciones mínimas: '+pc(d.min_probability)+' · Probabilidad conjunta estimada de acertar toda la secuencia: <b>'+pc(d.sequence_probability)+'</b></p></div>';if(!d.steps.length)html+='<div class="card section"><p class="mut">Ahora mismo no hay partidos que cumplan el mínimo de este nivel. No añadimos apuestas ficticias.</p></div>';else html+=d.steps.map(x=>'<div class="card step"><div class="stepnum">PASO '+x.step+'</div><h3>'+x.home+' vs '+x.away+'</h3><div class="market">'+x.market+'</div><div class="prob">'+pc(x.probability)+'</div><div class="stats"><div class="stat"><strong>'+x.stake.toFixed(2)+' €</strong><span>stake · '+pc(x.stake_pct)+'</span></div><div class="stat"><strong>'+x.odds.toFixed(2)+'</strong><span>'+x.odds_type+'</span></div><div class="stat"><strong>'+x.bankroll_if_win.toFixed(2)+' €</strong><span>bankroll si gana</span></div></div><p class="mut">Si pierde: '+x.bankroll_if_lose.toFixed(2)+' €. No se aumenta el siguiente stake para recuperar la pérdida.</p></div>').join('');html+='<div class="card section"><p class="mut">'+d.note+'</p><button class="btn2" onclick="restartChallenge()">Crear otro reto</button></div>';$('#challengePlan').innerHTML=html;$('#challengePlan').classList.remove('hidden')}
function restartChallenge(){$('#challengePlan').classList.add('hidden');$('#challengeIntro').classList.remove('hidden')}
ov();loadMatches();</script></body></html>'''

@app.get("/",response_class=HTMLResponse)
def root(): return HTML
