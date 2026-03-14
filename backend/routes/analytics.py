from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt
from services.supabase_service import get_client
from services.analytics_service import compute_metrics, compute_warnings, ai_analyze_team, ai_analyze_agent
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
    admin=Depends(require_admin)
):
    sb = get_client()
    agents, leads, rdvs, spend = fetch_all(sb, date_from, date_to)
    if agent_id:
        agents = [a for a in agents if a["id"] == agent_id]
    metrics = compute_metrics(agents, leads, rdvs, spend)
    return metrics


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
