from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from passlib.context import CryptContext
from jose import jwt
from services.supabase_service import get_client
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
def list_agents(branch_id: str = None, admin=Depends(require_admin)):
    sb = get_client()
    q = sb.table("agents").select("id, name, is_active, created_at, fired_at, branch_id, drive_folder_id").eq("is_active", True).order("created_at")
    if branch_id:
        q = q.eq("branch_id", branch_id)
    result = q.execute()
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
    new_agent = result.data[0]

    # Create Google Drive folder for the agent
    try:
        from services.drive_service import create_agent_folder
        branch_name = ""
        if agent.branch_id:
            br = sb.table("branches").select("name").eq("id", agent.branch_id).execute()
            branch_name = br.data[0]["name"] if br.data else ""
        folder_id = create_agent_folder(agent.name, branch_name)
        sb.table("agents").update({"drive_folder_id": folder_id}).eq("id", new_agent["id"]).execute()
        new_agent["drive_folder_id"] = folder_id
    except Exception as e:
        import logging
        logging.warning(f"[DRIVE] Failed to create folder for {agent.name}: {e}")

    return new_agent


@router.delete("/{agent_id}")
def fire_agent(agent_id: str, admin=Depends(require_admin)):
    sb = get_client()

    from datetime import datetime
    sb.table("agents").update({
        "is_active": False,
        "fired_at": datetime.utcnow().isoformat()
    }).eq("id", agent_id).execute()

    return {"message": "تم إيقاف الوكيل"}


@router.delete("/{agent_id}/wipe")
def fire_and_wipe_agent(agent_id: str, admin=Depends(require_admin)):
    sb = get_client()

    from datetime import datetime
    placeholder = f"محذوف_{agent_id[:8]}"
    sb.table("agents").update({
        "is_active": False,
        "fired_at": datetime.utcnow().isoformat(),
        "name": placeholder
    }).eq("id", agent_id).execute()

    return {"message": "تم إيقاف الوكيل ومسح اسمه"}


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
    res = sb.table("agents").select("id, name, avatar_url, goals, branch_id, branches(name, city)").eq("id", agent_id).execute()
    if not res.data:
        raise HTTPException(404, "Not found")
    row = res.data[0]
    br = row.pop("branches", None) or {}
    row["branch_name"] = br.get("name")
    row["branch_city"] = br.get("city")
    return row


@router.patch("/me/credentials")
def update_my_credentials(body: dict, user=Depends(get_current_user)):
    sb = get_client()
    agent_id = user["sub"]
    if agent_id == "admin":
        raise HTTPException(400, "استخدم صفحة إعدادات الإدارة")
    current_pin = (body.get("current_pin") or "").strip()
    new_name    = (body.get("new_name") or "").strip()
    new_pin     = (body.get("new_pin") or "").strip()
    if not current_pin:
        raise HTTPException(400, "كلمة المرور الحالية مطلوبة للتحقق")
    if not new_name and not new_pin:
        raise HTTPException(400, "يرجى إدخال اسم أو كلمة مرور جديدة")
    agent_row = sb.table("agents").select("id, name, pin").eq("id", agent_id).execute()
    if not agent_row.data:
        raise HTTPException(404, "الوكيل غير موجود")
    agent = agent_row.data[0]
    if not pwd_context.verify(current_pin, agent["pin"]):
        raise HTTPException(400, "كلمة المرور الحالية غير صحيحة")
    updates = {}
    if new_name and new_name != agent["name"]:
        existing = sb.table("agents").select("id").eq("name", new_name).execute()
        if existing.data:
            raise HTTPException(400, "هذا الاسم مستخدم مسبقاً")
        updates["name"] = new_name
    if new_pin:
        updates["pin"] = pwd_context.hash(new_pin)
    if not updates:
        return {"message": "لا يوجد تغيير"}
    sb.table("agents").update(updates).eq("id", agent_id).execute()
    return {"message": "تم تحديث بيانات الدخول بنجاح", "name_changed": "name" in updates}


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
    rows = result.data or []

    # Resolve branch name for each requested_branch_id so admin sees what the agent picked
    branch_ids = {r.get("requested_branch_id") for r in rows if r.get("requested_branch_id")}
    branch_map = {}
    if branch_ids:
        br = sb.table("branches").select("id, name, city").in_("id", list(branch_ids)).execute()
        branch_map = {b["id"]: b for b in (br.data or [])}
    for r in rows:
        bid = r.get("requested_branch_id")
        r["requested_branch_name"] = branch_map.get(bid, {}).get("name") if bid else None
    return rows


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
    final_branch = body.branch_id or req.get("requested_branch_id")
    if final_branch:
        data["branch_id"] = final_branch
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


@router.patch("/{agent_id}/day-off")
def set_day_off(agent_id: str, body: dict, admin=Depends(require_admin)):
    """Set or clear an agent's weekly day off. day_off: 0=Sun,1=Mon,...,6=Sat, null=none."""
    sb = get_client()
    day_off = body.get("day_off")  # None clears it
    if day_off is not None and day_off not in range(7):
        raise HTTPException(400, "day_off يجب أن يكون بين 0 و 6")
    result = sb.table("agents").update({"day_off": day_off}).eq("id", agent_id).execute()
    if not result.data:
        raise HTTPException(404, "الوكيل غير موجود")
    return result.data[0]


@router.patch("/{agent_id}/rename")
def rename_agent(agent_id: str, body: dict, admin=Depends(require_admin)):
    sb = get_client()
    new_name = (body.get("name") or "").strip()
    if not new_name:
        raise HTTPException(400, "الاسم فارغ")
    existing = sb.table("agents").select("id").eq("name", new_name).execute()
    if existing.data and existing.data[0]["id"] != agent_id:
        raise HTTPException(400, "هذا الاسم مستخدم مسبقاً")
    result = sb.table("agents").update({"name": new_name}).eq("id", agent_id).execute()
    if not result.data:
        raise HTTPException(404, "الوكيل غير موجود")
    return result.data[0]


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
