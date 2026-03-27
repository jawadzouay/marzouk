from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from passlib.context import CryptContext
from jose import jwt
from services.supabase_service import get_client
from services.swap_service import redistribute_agent_leads
from dotenv import load_dotenv
from typing import Optional
import os

load_dotenv()

router = APIRouter()
security = HTTPBearer()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
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


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[ALGORITHM])
        return payload
    except Exception:
        raise HTTPException(status_code=401, detail="Unauthorized")


class AgentCreate(BaseModel):
    name: str
    pin: str
    branch_id: Optional[str] = None


class BonusCreate(BaseModel):
    amount: float
    note: Optional[str] = None


@router.get("/")
def list_agents(admin=Depends(require_admin)):
    sb = get_client()
    result = sb.table("agents").select("id, name, is_active, created_at, fired_at, branch_id").order("created_at").execute()
    return result.data


@router.post("/")
def create_agent(agent: AgentCreate, admin=Depends(require_admin)):
    sb = get_client()

    # Check name uniqueness
    existing = sb.table("agents").select("id").eq("name", agent.name).execute()
    if existing.data:
        raise HTTPException(status_code=400, detail="اسم المستخدم موجود مسبقاً")

    hashed_pin = pwd_context.hash(agent.pin)
    data = {"name": agent.name, "pin": hashed_pin, "is_active": True}
    if agent.branch_id:
        data["branch_id"] = agent.branch_id
    result = sb.table("agents").insert(data).execute()

    return result.data[0]


@router.delete("/{agent_id}")
def fire_agent(agent_id: str, admin=Depends(require_admin)):
    sb = get_client()

    from datetime import datetime
    sb.table("agents").update({
        "is_active": False,
        "fired_at": datetime.utcnow().isoformat()
    }).eq("id", agent_id).execute()

    redistribute_agent_leads(agent_id)

    return {"message": "تم إيقاف الوكيل وإعادة توزيع العملاء المحتملين"}


@router.delete("/{agent_id}/wipe")
def fire_and_wipe_agent(agent_id: str, admin=Depends(require_admin)):
    sb = get_client()

    from datetime import datetime
    # Free up the name by renaming to a non-conflicting placeholder
    placeholder = f"محذوف_{agent_id[:8]}"
    sb.table("agents").update({
        "is_active": False,
        "fired_at": datetime.utcnow().isoformat(),
        "name": placeholder
    }).eq("id", agent_id).execute()

    redistribute_agent_leads(agent_id)

    return {"message": "تم إيقاف الوكيل ومسح اسمه وإعادة توزيع العملاء المحتملين"}


@router.get("/my-rank")
def my_rank(user=Depends(get_current_user)):
    """Returns agent rank position — NO other agents' names or sensitive data."""
    sb = get_client()
    agent_id = user["sub"]
    if agent_id == "admin":
        raise HTTPException(400, "Admin has no rank")

    agents = sb.table("agents").select("id").eq("is_active", True).execute().data

    board = []
    for a in agents:
        # registered students (primary sort)
        reg = sb.table("leads").select("status").eq("current_agent", a["id"]).execute().data
        registered = sum(1 for r in reg if r["status"] in ("registered_logha", "registered_maharat", "registered_takwin"))
        # rdv booked (secondary)
        rdvs = sb.table("rdv").select("id", count="exact").eq("agent_id", a["id"]).execute()
        board.append({"id": a["id"], "registered": registered, "rdv": rdvs.count or 0})

    board.sort(key=lambda x: (x["registered"], x["rdv"]), reverse=True)

    idx = next((i for i, b in enumerate(board) if b["id"] == agent_id), None)
    if idx is None:
        return {"rank": None, "total": len(board), "gap_message": ""}

    rank = idx + 1
    my = board[idx]

    gap_msg = ""
    if rank == 1:
        gap_msg = "أنت في المرتبة الأولى 🏆"
    else:
        above = board[idx - 1]
        reg_gap = above["registered"] - my["registered"]
        rdv_gap = above["rdv"] - my["rdv"]
        if reg_gap > 0:
            gap_msg = f"تحتاج {reg_gap} تسجيل للوصول إلى المرتبة #{rank-1}"
        elif rdv_gap > 0:
            gap_msg = f"تحتاج {rdv_gap} RDV للوصول إلى المرتبة #{rank-1}"
        else:
            gap_msg = f"تعادل مع المرتبة #{rank-1} — استمر!"

    return {"rank": rank, "total": len(board), "gap_message": gap_msg}


@router.get("/me")
def get_my_profile(user=Depends(get_current_user)):
    sb = get_client()
    agent_id = user["sub"]
    if agent_id == "admin":
        raise HTTPException(400, "Admin has no profile")
    res = sb.table("agents").select("id, name, avatar_url, goals").eq("id", agent_id).execute()
    if not res.data:
        raise HTTPException(404, "Not found")
    return res.data[0]


@router.patch("/me")
def update_my_profile(body: dict, user=Depends(get_current_user)):
    sb = get_client()
    agent_id = user["sub"]
    if agent_id == "admin":
        raise HTTPException(400, "Admin has no profile")
    updates = {}
    if "avatar_url" in body:
        updates["avatar_url"] = body["avatar_url"]
    if "goals" in body:
        updates["goals"] = body["goals"]
    if not updates:
        raise HTTPException(400, "Nothing to update")
    sb.table("agents").update(updates).eq("id", agent_id).execute()
    return {"message": "تم التحديث"}


@router.get("/requests")
def list_requests(admin=Depends(require_admin)):
    sb = get_client()
    result = sb.table("agent_requests").select("*").eq("status", "pending").order("created_at").execute()
    return result.data


class ApproveRequest(BaseModel):
    final_name: Optional[str] = None
    branch_id: Optional[str] = None


@router.post("/requests/{request_id}/approve")
def approve_request(request_id: str, body: ApproveRequest, admin=Depends(require_admin)):
    sb = get_client()
    req = sb.table("agent_requests").select("*").eq("id", request_id).execute()
    if not req.data:
        raise HTTPException(404, "الطلب غير موجود")
    req = req.data[0]
    name = (body.final_name or req["requested_name"]).strip()
    # Check name not taken
    existing = sb.table("agents").select("id").eq("name", name).execute()
    if existing.data:
        raise HTTPException(400, "هذا الاسم مستخدم مسبقاً")
    hashed = pwd_context.hash(req["password_plain"])
    data = {"name": name, "pin": hashed, "is_active": True}
    if body.branch_id:
        data["branch_id"] = body.branch_id
    sb.table("agents").insert(data).execute()
    sb.table("agent_requests").update({"status": "approved", "final_name": name}).eq("id", request_id).execute()
    return {"message": f"تم قبول الوكيل {name}"}


@router.delete("/requests/{request_id}")
def reject_request(request_id: str, admin=Depends(require_admin)):
    sb = get_client()
    sb.table("agent_requests").update({"status": "rejected"}).eq("id", request_id).execute()
    return {"message": "تم رفض الطلب"}


@router.get("/me/bonuses")
def get_my_bonuses(user=Depends(get_current_user)):
    sb = get_client()
    agent_id = user["sub"]
    if agent_id == "admin":
        raise HTTPException(400, "Admin has no bonuses")
    result = sb.table("bonuses").select("*").eq("agent_id", agent_id).order("created_at", desc=True).execute()
    return result.data


@router.post("/me/bonuses")
def add_bonus(bonus: BonusCreate, user=Depends(get_current_user)):
    sb = get_client()
    agent_id = user["sub"]
    if agent_id == "admin":
        raise HTTPException(400, "Admin has no bonuses")
    result = sb.table("bonuses").insert({
        "agent_id": agent_id,
        "amount": bonus.amount,
        "note": bonus.note
    }).execute()
    return result.data[0]


@router.delete("/me/bonuses/{bonus_id}")
def delete_bonus(bonus_id: str, user=Depends(get_current_user)):
    sb = get_client()
    agent_id = user["sub"]
    existing = sb.table("bonuses").select("id").eq("id", bonus_id).eq("agent_id", agent_id).execute()
    if not existing.data:
        raise HTTPException(404, "Bonus not found")
    sb.table("bonuses").delete().eq("id", bonus_id).execute()
    return {"message": "تم حذف المكافأة"}


@router.patch("/{agent_id}/branch")
def transfer_agent_branch(agent_id: str, body: dict, admin=Depends(require_admin)):
    branch_id = body.get("branch_id") or None
    sb = get_client()
    result = sb.table("agents").update({"branch_id": branch_id}).eq("id", agent_id).execute()
    if not result.data:
        raise HTTPException(404, "الوكيل غير موجود")
    return result.data[0]


@router.get("/{agent_id}/stats")
def agent_stats(agent_id: str, user=Depends(get_current_user)):
    sb = get_client()

    leads = sb.table("leads").select("status, swap_count, submitted_at").eq("original_agent", agent_id).execute()
    total = len(leads.data)

    stats = {"total": total, "RDV": 0, "B.V": 0, "N.R": 0, "P.I": 0, "Autre ville": 0}
    for lead in leads.data:
        status = lead.get("status")
        if status in stats:
            stats[status] += 1

    rdv_result = sb.table("rdv").select("status").eq("agent_id", agent_id).execute()
    showed_up = sum(1 for r in rdv_result.data if r["status"] == "showed_up")
    no_show = sum(1 for r in rdv_result.data if r["status"] == "no_show")

    stats["showed_up"] = showed_up
    stats["no_show"] = no_show
    stats["total_rdv_booked"] = len(rdv_result.data)

    # Registered leads (by current_agent — includes swapped leads they registered)
    reg_result = sb.table("leads").select("status").eq("current_agent", agent_id).execute()
    reg_logha   = sum(1 for r in reg_result.data if r["status"] == "registered_logha")
    reg_maharat = sum(1 for r in reg_result.data if r["status"] == "registered_maharat")
    reg_takwin  = sum(1 for r in reg_result.data if r["status"] == "registered_takwin")
    stats["registered_logha"]   = reg_logha
    stats["registered_maharat"] = reg_maharat
    stats["registered_takwin"]  = reg_takwin
    stats["registered_inscre"]  = reg_logha * 1 + reg_maharat * 1 + reg_takwin * 2
    stats["registered_students"] = reg_logha + reg_maharat + reg_takwin

    return stats
