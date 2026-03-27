from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt
from services.supabase_service import get_client
from services.analytics_service import compute_metrics, compute_warnings, ai_analyze_team, ai_analyze_agent, ai_script_analyzer
from dotenv import load_dotenv
from datetime import datetime, timedelta
import os

load_dotenv()
router   = APIRouter()
security = HTTPBearer()
JWT_SECRET = os.getenv("JWT_SECRET")
ALGORITHM  = "HS256"


def require_admin(creds: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(creds.credentials, JWT_SECRET, algorithms=[ALGORITHM])
        if payload.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Admin only")
        return payload
    except Exception:
        raise HTTPException(status_code=401, detail="Unauthorized")


def get_current_user(creds: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(creds.credentials, JWT_SECRET, algorithms=[ALGORITHM])
        return payload
    except Exception:
        raise HTTPException(status_code=401, detail="Unauthorized")


def fetch_all(sb, date_from, date_to):
    agents = sb.table("agents").select("id,name").eq("is_active", True).execute().data

    leads_q = sb.table("leads").select("id,status,original_agent,current_agent,submitted_at")
    if date_from: leads_q = leads_q.gte("submitted_at", date_from)
    if date_to:   leads_q = leads_q.lte("submitted_at", date_to + "T23:59:59")
    leads = leads_q.execute().data

    rdvs_q = sb.table("rdv").select("id,status,agent_id,created_at")
    if date_from: rdvs_q = rdvs_q.gte("created_at", date_from)
    if date_to:   rdvs_q = rdvs_q.lte("created_at", date_to + "T23:59:59")
    rdvs = rdvs_q.execute().data

    spend_q = sb.table("ad_spend").select("*")
    if date_from: spend_q = spend_q.gte("period_start", date_from)
    if date_to:   spend_q = spend_q.lte("period_end", date_to)
    spend = spend_q.execute().data

    return agents, leads, rdvs, spend


@router.get("/metrics")
def analytics_metrics(
    date_from: str = Query(None),
    date_to:   str = Query(None),
    agent_id:  str = Query(None),
    branch_id: str = Query(None),
    city:      str = Query(None),
    admin=Depends(require_admin)
):
    sb = get_client()
    agents, leads, rdvs, spend = fetch_all(sb, date_from, date_to)

    if branch_id:
        branch_agents = sb.table("agents").select("id").eq("branch_id", branch_id).execute().data
        ids = {a["id"] for a in branch_agents}
        agents = [a for a in agents if a["id"] in ids]
    elif city:
        city_branches = sb.table("branches").select("id").eq("city", city).execute().data
        city_branch_ids = [b["id"] for b in city_branches]
        if city_branch_ids:
            city_agents = sb.table("agents").select("id").in_("branch_id", city_branch_ids).execute().data
            ids = {a["id"] for a in city_agents}
            agents = [a for a in agents if a["id"] in ids]
        else:
            agents = []

    if agent_id:
        agents = [a for a in agents if a["id"] == agent_id]

    metrics = compute_metrics(agents, leads, rdvs, spend)
    return metrics


@router.get("/branches-summary")
def branches_summary(
    date_from: str = Query(None),
    date_to:   str = Query(None),
    admin=Depends(require_admin)
):
    """Returns cost metrics grouped by branch and city for comparison."""
    sb = get_client()
    branches = sb.table("branches").select("*").execute().data
    agents_all = sb.table("agents").select("id,name,branch_id").eq("is_active", True).execute().data
    _, leads, rdvs, spend = fetch_all(sb, date_from, date_to)

    result = []
    for branch in branches:
        branch_agent_ids = {a["id"] for a in agents_all if a.get("branch_id") == branch["id"]}
        b_agents = [a for a in agents_all if a["id"] in branch_agent_ids]
        metrics = compute_metrics(b_agents, leads, rdvs, spend)
        total_spend = sum(m.get("spend", 0) for m in metrics)
        total_leads = sum(m.get("total", 0) for m in metrics)
        total_rdv   = sum(m.get("rdv_booked", 0) for m in metrics)
        total_show  = sum(m.get("showed_up", 0) for m in metrics)
        total_reg   = sum(m.get("registered", 0) for m in metrics)
        result.append({
            "branch_id":   branch["id"],
            "branch_name": branch["name"],
            "city":        branch.get("city", "—"),
            "agents":      len(b_agents),
            "spend":       total_spend,
            "leads":       total_leads,
            "rdv":         total_rdv,
            "visits":      total_show,
            "registered":  total_reg,
            "cpl":  round(total_spend / total_leads, 0)      if total_leads and total_spend else 0,
            "cp_rdv": round(total_spend / total_rdv, 0)      if total_rdv and total_spend else 0,
            "cp_visit": round(total_spend / total_show, 0)   if total_show and total_spend else 0,
            "cp_reg": round(total_spend / total_reg, 0)      if total_reg and total_spend else 0,
        })
    return result


@router.get("/warnings")
def analytics_warnings(
    date_from: str = Query(None),
    date_to:   str = Query(None),
    admin=Depends(require_admin)
):
    sb = get_client()
    # Default: last 7 days
    if not date_from:
        date_from = (datetime.utcnow() - timedelta(days=7)).date().isoformat()
    if not date_to:
        date_to = datetime.utcnow().date().isoformat()

    agents, leads, rdvs, spend = fetch_all(sb, date_from, date_to)
    metrics  = compute_metrics(agents, leads, rdvs, spend)
    warnings = compute_warnings(metrics)
    return {"warnings": warnings, "period": {"from": date_from, "to": date_to}}


@router.get("/ads-quality")
def ads_quality(
    date_from: str = Query(None),
    date_to:   str = Query(None),
    agent_id:  str = Query(None),
    admin=Depends(require_admin)
):
    """Daily lead-quality breakdown to diagnose ad targeting problems."""
    sb = get_client()
    if not date_from:
        date_from = (datetime.utcnow() - timedelta(days=7)).date().isoformat()
    if not date_to:
        date_to = datetime.utcnow().date().isoformat()

    leads_q = sb.table("leads").select("status, submitted_at, original_agent") \
        .gte("submitted_at", date_from).lte("submitted_at", date_to + "T23:59:59")
    if agent_id:
        leads_q = leads_q.eq("original_agent", agent_id)
    leads = leads_q.execute().data

    from collections import defaultdict
    daily = defaultdict(lambda: {"total": 0, "rdv": 0, "bv": 0, "nr": 0, "pi": 0, "av": 0, "other": 0})
    for lead in leads:
        date = (lead.get("submitted_at") or "")[:10]
        if not date:
            continue
        daily[date]["total"] += 1
        s = lead.get("status", "")
        if   s == "RDV":          daily[date]["rdv"] += 1
        elif s == "B.V":          daily[date]["bv"]  += 1
        elif s == "N.R":          daily[date]["nr"]  += 1
        elif s == "P.I":          daily[date]["pi"]  += 1
        elif s == "Autre ville":  daily[date]["av"]  += 1
        else:                     daily[date]["other"] += 1

    result = []
    for date in sorted(daily.keys()):
        d     = daily[date]
        total = d["total"] or 1
        result.append({
            "date":    date,
            "total":   d["total"],
            "rdv":     d["rdv"],  "rdv_pct": round(d["rdv"] / total * 100, 1),
            "bv":      d["bv"],   "bv_pct":  round(d["bv"]  / total * 100, 1),
            "nr":      d["nr"],   "nr_pct":  round(d["nr"]  / total * 100, 1),
            "pi":      d["pi"],   "pi_pct":  round(d["pi"]  / total * 100, 1),
            "av":      d["av"],   "av_pct":  round(d["av"]  / total * 100, 1),
            # quality score 0-100: penalise av/pi/nr, reward rdv
            "quality_score": max(0, min(100, round(
                100
                - (d["av"]  / total * 50)
                - (d["pi"]  / total * 30)
                - (d["nr"]  / total * 10)
                + (d["rdv"] / total * 20)
            ))),
        })

    # Agent-level summary for same period (only when no agent filter)
    agents_summary = []
    if not agent_id:
        agents = sb.table("agents").select("id, name").eq("is_active", True).execute().data
        for ag in agents:
            ag_leads = [l for l in leads if l.get("original_agent") == ag["id"]]
            t = len(ag_leads) or 1
            av_c = sum(1 for l in ag_leads if l.get("status") == "Autre ville")
            pi_c = sum(1 for l in ag_leads if l.get("status") == "P.I")
            nr_c = sum(1 for l in ag_leads if l.get("status") == "N.R")
            rdv_c = sum(1 for l in ag_leads if l.get("status") == "RDV")
            if not ag_leads:
                continue
            agents_summary.append({
                "agent_id":   ag["id"],
                "agent_name": ag["name"],
                "total":      len(ag_leads),
                "av_pct":     round(av_c / t * 100, 1),
                "pi_pct":     round(pi_c / t * 100, 1),
                "nr_pct":     round(nr_c / t * 100, 1),
                "rdv_pct":    round(rdv_c / t * 100, 1),
                "quality_score": max(0, min(100, round(
                    100 - (av_c/t*50) - (pi_c/t*30) - (nr_c/t*10) + (rdv_c/t*20)
                ))),
            })
        agents_summary.sort(key=lambda x: x["quality_score"])

    return {
        "days":    result,
        "agents":  agents_summary,
        "period":  {"from": date_from, "to": date_to},
    }


@router.post("/my-coach")
async def my_coach(user=Depends(get_current_user)):
    """AI coaching for agent — accessible to agents and admin."""
    sb = get_client()
    agent_id = user["sub"]
    if agent_id == "admin":
        raise HTTPException(400, "Use /ai-analysis for admin")

    agent_row = sb.table("agents").select("id,name").eq("id", agent_id).execute().data
    if not agent_row:
        raise HTTPException(404, "Agent not found")

    leads  = sb.table("leads").select("status,submitted_at").eq("original_agent", agent_id).execute().data
    rdvs   = sb.table("rdv").select("status").eq("agent_id", agent_id).execute().data
    reg_l  = sb.table("leads").select("status").eq("current_agent", agent_id).execute().data

    total      = len(leads)
    rdv_c      = sum(1 for l in leads if l["status"] == "RDV")
    bv_c       = sum(1 for l in leads if l["status"] == "B.V")
    nr_c       = sum(1 for l in leads if l["status"] == "N.R")
    pi_c       = sum(1 for l in leads if l["status"] == "P.I")
    av_c       = sum(1 for l in leads if l["status"] == "Autre ville")
    showed     = sum(1 for r in rdvs if r["status"] == "showed_up")
    rdv_booked = len(rdvs)
    registered = sum(1 for r in reg_l if r["status"] in ("registered_logha", "registered_maharat", "registered_takwin"))

    rdv_rate  = round(rdv_c / total * 100, 1) if total else 0
    show_rate = round(showed / rdv_booked * 100, 1) if rdv_booked else 0
    reg_rate  = round(registered / showed * 100, 1) if showed else 0

    # Team averages (lightweight — just rates)
    all_agents = sb.table("agents").select("id").eq("is_active", True).execute().data
    t_rdv, t_show, t_reg = [], [], []
    for a in all_agents:
        al = sb.table("leads").select("status").eq("original_agent", a["id"]).execute().data
        at = len(al)
        ar = sum(1 for l in al if l["status"] == "RDV")
        av = sb.table("rdv").select("status").eq("agent_id", a["id"]).execute().data
        ash = sum(1 for r in av if r["status"] == "showed_up")
        arb = len(av)
        arl = sb.table("leads").select("status").eq("current_agent", a["id"]).execute().data
        areg = sum(1 for r in arl if r["status"] in ("registered_logha", "registered_maharat", "registered_takwin"))
        if at: t_rdv.append(ar / at * 100)
        if arb: t_show.append(ash / arb * 100)
        if ash: t_reg.append(areg / ash * 100)

    team_avg = {
        "rdv_rate":  sum(t_rdv) / len(t_rdv)   if t_rdv  else 0,
        "show_rate": sum(t_show) / len(t_show) if t_show else 0,
        "reg_rate":  sum(t_reg) / len(t_reg)   if t_reg  else 0,
    }

    agent_metrics = {
        "agent_name": agent_row[0]["name"],
        "total_leads": total, "rdv_count": rdv_c,
        "bv_count": bv_c, "nr_count": nr_c, "pi_count": pi_c, "av_count": av_c,
        "rdv_booked": rdv_booked, "showed_up": showed, "registered": registered,
        "rdv_rate": rdv_rate, "show_rate": show_rate, "reg_rate": reg_rate,
        "cpl": 0, "cost_per_registration": 0,
    }

    text = await ai_analyze_agent(agent_metrics, team_avg)
    return {"analysis": text, "agent_name": agent_row[0]["name"]}


@router.post("/ai-analysis")
async def ai_analysis(
    body: dict,
    date_from: str = Query(None),
    date_to:   str = Query(None),
    admin=Depends(require_admin)
):
    """body: { agent_id: optional — if omitted, analyze whole team }"""
    sb = get_client()
    agents, leads, rdvs, spend = fetch_all(sb, date_from, date_to)
    metrics = compute_metrics(agents, leads, rdvs, spend)

    agent_id = body.get("agent_id")
    if agent_id:
        agent = next((m for m in metrics if m["agent_id"] == agent_id), None)
        if not agent:
            raise HTTPException(404, "Agent not found")
        active = [m for m in metrics if m["total_leads"] > 0]
        team_avg = {
            "rdv_rate":  sum(a["rdv_rate"] for a in active) / len(active)  if active else 0,
            "show_rate": sum(a["show_rate"] for a in active) / len(active) if active else 0,
            "reg_rate":  sum(a["reg_rate"] for a in active) / len(active)  if active else 0,
        }
        text = await ai_analyze_agent(agent, team_avg)
        return {"type": "agent", "agent_name": agent["agent_name"], "analysis": text}
    else:
        text = await ai_analyze_team(metrics)
        return {"type": "team", "analysis": text}


@router.post("/script-analyzer")
async def script_analyzer(admin=Depends(require_admin)):
    """Analyze lead notes + objection patterns and generate sales/video scripts."""
    sb = get_client()

    leads = sb.table("leads").select("note,status").execute()
    notes = [l["note"].strip() for l in leads.data if l.get("note") and l["note"].strip()]

    status_counts = {
        "bv":  sum(1 for l in leads.data if l["status"] == "B.V"),
        "nr":  sum(1 for l in leads.data if l["status"] == "N.R"),
        "pi":  sum(1 for l in leads.data if l["status"] == "P.I"),
        "rdv": sum(1 for l in leads.data if l["status"] == "RDV"),
    }

    analysis = await ai_script_analyzer(notes, status_counts)
    return {"analysis": analysis, "notes_count": len(notes), "status_counts": status_counts}
