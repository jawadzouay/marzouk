from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt
from services.supabase_service import get_client
from dotenv import load_dotenv
from datetime import datetime, timedelta
import os

load_dotenv()

router = APIRouter()
security = HTTPBearer()
JWT_SECRET = os.getenv("JWT_SECRET")
ALGORITHM = "HS256"


def require_admin(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[ALGORITHM])
        if payload.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Admin only")
        return payload
    except Exception:
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.get("/dashboard")
def admin_dashboard(admin=Depends(require_admin)):
    sb = get_client()
    today = datetime.utcnow().date().isoformat()

    agents = sb.table("agents").select("id, name, is_active").eq("is_active", True).execute()
    submissions_today = sb.table("submissions").select("agent_id").eq("submission_date", today).execute()
    submitted_ids = {s["agent_id"] for s in submissions_today.data}

    agent_status = []
    for agent in agents.data:
        agent_status.append({
            "id": agent["id"],
            "name": agent["name"],
            "submitted_today": agent["id"] in submitted_ids
        })

    total_leads = sb.table("leads").select("id", count="exact").execute()
    total_rdv = sb.table("rdv").select("id", count="exact").execute()
    swap_pool = sb.table("leads").select("id", count="exact").in_("status", ["B.V", "N.R"]).lt("swap_count", 3).execute()

    return {
        "agents": agent_status,
        "total_leads": total_leads.count,
        "total_rdv": total_rdv.count,
        "swap_pool_count": swap_pool.count,
        "date": today
    }


@router.get("/leaderboard")
def leaderboard(admin=Depends(require_admin)):
    sb = get_client()
    agents = sb.table("agents").select("id, name").eq("is_active", True).execute()

    board = []
    for agent in agents.data:
        leads = sb.table("leads").select("status").eq("original_agent", agent["id"]).execute()
        total = len(leads.data)
        rdv_count = sum(1 for l in leads.data if l["status"] == "RDV")
        rdv_pct = round((rdv_count / total * 100) if total else 0, 1)

        board.append({
            "name": agent["name"],
            "total": total,
            "rdv": rdv_count,
            "rdv_pct": rdv_pct
        })

    board.sort(key=lambda x: x["rdv"], reverse=True)
    for i, entry in enumerate(board):
        entry["rank"] = i + 1

    return board


@router.get("/submissions/calendar")
def submission_calendar(admin=Depends(require_admin)):
    sb = get_client()
    result = sb.table("submissions").select("agent_id, submission_date, leads_count, agents(name)").order("submission_date", desc=True).execute()
    return result.data


@router.get("/stats/overview")
def stats_overview(admin=Depends(require_admin)):
    sb = get_client()

    leads = sb.table("leads").select("status").execute()
    total = len(leads.data)
    breakdown = {}
    for lead in leads.data:
        s = lead["status"]
        breakdown[s] = breakdown.get(s, 0) + 1

    return {"total": total, "breakdown": breakdown}
