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


@router.post("/manual-bulk")
def manual_bulk_swap(body: dict, admin=Depends(require_admin)):
    """Transfer ALL eligible leads from one agent to another."""
    from_agent_id = body.get("from_agent_id")
    to_agent_id   = body.get("to_agent_id")
    if not from_agent_id or not to_agent_id:
        raise HTTPException(400, "from_agent_id و to_agent_id مطلوبان")
    if from_agent_id == to_agent_id:
        raise HTTPException(400, "لا يمكن النقل إلى نفس الوكيل")

    sb = get_client()
    from datetime import datetime, timedelta

    now = datetime.utcnow().isoformat()
    leads = sb.table("leads").select("*") \
        .eq("current_agent", from_agent_id) \
        .in_("status", ["B.V", "N.R", "P.I"]) \
        .lte("swap_eligible_at", now) \
        .lt("swap_count", 3) \
        .execute().data

    transferred = 0
    for lead in leads:
        new_swap_count = lead["swap_count"] + 1
        updates = {"current_agent": to_agent_id, "swap_count": new_swap_count}
        if new_swap_count < 3:
            updates["swap_eligible_at"] = (datetime.utcnow() + timedelta(days=4)).isoformat()
        else:
            updates["swap_eligible_at"] = None
        sb.table("leads").update(updates).eq("id", lead["id"]).execute()
        sb.table("lead_history").insert({
            "lead_id":      lead["id"],
            "agent_id":     to_agent_id,
            "action":       "swapped",
            "status_before": lead["status"],
            "status_after":  lead["status"],
            "note": f"Swap #{new_swap_count} — Bulk manual by admin"
        }).execute()
        transferred += 1

    return {"transferred": transferred, "message": f"تم نقل {transferred} عميل بنجاح"}


@router.get("/pool")
def get_swap_pool(admin=Depends(require_admin)):
    sb = get_client()
    result = sb.table("leads").select("*, agents!leads_current_agent_fkey(name)").in_("status", ["B.V", "N.R", "P.I"]).lt("swap_count", 3).execute()
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
