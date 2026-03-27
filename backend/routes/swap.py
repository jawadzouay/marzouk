from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt
from services.supabase_service import get_client
from services.swap_service import get_eligible_leads_for_swap, assign_swap, manual_assign_swap, get_swap_level, set_swap_level
from dotenv import load_dotenv
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


@router.get("/settings")
def get_settings(admin=Depends(require_admin)):
    return {"swap_level": get_swap_level()}


@router.post("/settings")
def update_settings(body: dict, admin=Depends(require_admin)):
    level = int(body.get("swap_level", 1))
    if level not in (1, 2, 3, 4):
        raise HTTPException(400, "المستوى يجب أن يكون بين 1 و 4")
    set_swap_level(level)
    return {"swap_level": level}


@router.get("/eligible")
def get_eligible(admin=Depends(require_admin)):
    leads = get_eligible_leads_for_swap()
    return {"count": len(leads), "leads": leads}


@router.post("/run")
def run_swap(admin=Depends(require_admin)):
    level = get_swap_level()
    if level == 4:
        raise HTTPException(400, "وضع التبادل يدوي — استخدم التعيين اليدوي")
    leads = get_eligible_leads_for_swap()
    results = []
    for lead in leads:
        new_agent = assign_swap(lead, level)
        results.append({"lead_id": lead["id"], "new_agent": new_agent})
    return {"swapped": len(results), "results": results}


@router.post("/manual")
def manual_swap(body: dict, admin=Depends(require_admin)):
    lead_id = body.get("lead_id")
    agent_id = body.get("agent_id")
    if not lead_id or not agent_id:
        raise HTTPException(400, "lead_id و agent_id مطلوبان")
    success = manual_assign_swap(lead_id, agent_id)
    if not success:
        raise HTTPException(404, "العميل غير موجود")
    return {"message": "تم التعيين بنجاح"}


@router.get("/pool")
def get_swap_pool(admin=Depends(require_admin)):
    sb = get_client()
    result = sb.table("leads").select("*, agents!leads_current_agent_fkey(name)").in_("status", ["B.V", "N.R"]).lt("swap_count", 3).execute()
    leads = result.data

    # Attach history (actions + notes, no agent names)
    for lead in leads:
        history = sb.table("lead_history").select("action,status_before,status_after,note,created_at").eq("lead_id", lead["id"]).order("created_at").execute()
        lead["history"] = history.data

    return leads


@router.get("/agents")
def get_agents_for_swap(admin=Depends(require_admin)):
    """Returns active agents with branch/city info for manual assignment."""
    sb = get_client()
    agents = sb.table("agents").select("id,name,branch_id").eq("is_active", True).execute().data
    branches = sb.table("branches").select("id,name,city").execute().data
    branch_map = {b["id"]: b for b in branches}
    for a in agents:
        b = branch_map.get(a.get("branch_id"), {})
        a["branch_name"] = b.get("name", "—")
        a["city"] = b.get("city", "—")
    return agents
