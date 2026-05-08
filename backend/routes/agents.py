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

from services.manager_scope import (
    require_admin_or_manager, require_admin_only,
    resolve_manager_branch_ids, assert_agent_in_scope,
)


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


# Field tiers walked in order by _select_agents — drops the newest optional
# column on the first error so an un-migrated DB still returns the rest.
# Order matters: each tier removes the LATEST migration column that might
# not be applied yet. role + manager_scope_* are first to drop because
# they're the latest addition (branch-manager feature).
_AGENT_FIELDS_TIERS = [
    "id, name, is_active, created_at, fired_at, branch_id, drive_folder_id, day_off, pin_plain, accepts_leads, role, manager_scope_type, manager_scope_id",
    "id, name, is_active, created_at, fired_at, branch_id, drive_folder_id, day_off, pin_plain, accepts_leads",
    "id, name, is_active, created_at, fired_at, branch_id, drive_folder_id, day_off, pin_plain",
    "id, name, is_active, created_at, fired_at, branch_id, drive_folder_id, day_off",
]
# Back-compat: existing call sites elsewhere reference these names.
_AGENT_FIELDS_FULL    = _AGENT_FIELDS_TIERS[2]
_AGENT_FIELDS_LEGACY  = _AGENT_FIELDS_TIERS[3]


def _safe_write(fn, *, fallback_key="pin_plain"):
    """Run a DB write that may reference an optional column. fn gets a bool
    telling it whether to include the optional field. If the first attempt
    fails because the column is missing, we retry without."""
    try:
        return fn(True)
    except Exception as e:
        if fallback_key in str(e):
            return fn(False)
        raise


def _select_agents(sb, branch_id=None):
    """Query agents with optional columns, falling back through tiers when
    the DB hasn't been migrated yet. Keeps the list working for anyone who
    deployed the new code before running the ALTER TABLE."""
    last_err = None
    OPTIONAL_COLS = ("accepts_leads", "pin_plain", "role", "manager_scope_type", "manager_scope_id")
    for fields in _AGENT_FIELDS_TIERS:
        try:
            q = sb.table("agents").select(fields).eq("is_active", True).order("created_at")
            if branch_id:
                q = q.eq("branch_id", branch_id)
            return q.execute().data
        except Exception as e:
            last_err = e
            msg = str(e)
            # only fall through on missing-column errors
            if any(col in msg for col in OPTIONAL_COLS):
                continue
            raise
    if last_err:
        raise last_err
    return []


@router.get("/")
def list_agents(branch_id: str = None, caller=Depends(require_admin_or_manager)):
    # pin_plain is stored alongside the bcrypt hash so admin/manager can
    # view the actual PIN in the agents list. Manager scope auto-narrows
    # the list to their branch(es); they can't see agents outside scope.
    sb = get_client()
    scope_branch_ids = resolve_manager_branch_ids(sb, caller)
    if scope_branch_ids is not None:
        # Manager scope active. If admin passed a branch_id, only honor it
        # when it's inside the manager's scope; otherwise we ignore it.
        if branch_id and branch_id in scope_branch_ids:
            return _select_agents(sb, branch_id)
        if not scope_branch_ids:
            return []
        # Multi-branch fetch — _select_agents only handles one. Filter post-hoc.
        rows = _select_agents(sb, None) or []
        return [a for a in rows if a.get("branch_id") in scope_branch_ids]
    # Admin or scope='all'
    return _select_agents(sb, branch_id)


@router.post("/")
def create_agent(agent: AgentCreate, caller=Depends(require_admin_or_manager)):
    sb = get_client()
    # Manager can only add agents inside their own scope.
    scope_branch_ids = resolve_manager_branch_ids(sb, caller)
    if scope_branch_ids is not None:
        if not agent.branch_id:
            raise HTTPException(400, "يجب تحديد الفرع")
        if agent.branch_id not in scope_branch_ids:
            raise HTTPException(403, "لا يمكن إضافة وكيل خارج نطاق إدارتك")

    # Check name uniqueness
    existing = sb.table("agents").select("id").eq("name", agent.name).execute()
    if existing.data:
        raise HTTPException(status_code=400, detail="اسم المستخدم موجود مسبقاً")

    hashed_pin = pwd_context.hash(agent.pin)

    def _do_insert(with_plain):
        data = {"name": agent.name, "pin": hashed_pin, "is_active": True}
        if with_plain:
            data["pin_plain"] = agent.pin
        if agent.branch_id:
            data["branch_id"] = agent.branch_id
        return sb.table("agents").insert(data).execute()

    result = _safe_write(_do_insert)
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
def fire_agent(agent_id: str, caller=Depends(require_admin_or_manager)):
    sb = get_client()
    assert_agent_in_scope(sb, caller, agent_id)
    from datetime import datetime
    sb.table("agents").update({
        "is_active": False,
        "fired_at": datetime.utcnow().isoformat()
    }).eq("id", agent_id).execute()

    return {"message": "تم إيقاف الوكيل"}


@router.delete("/{agent_id}/wipe")
def fire_and_wipe_agent(agent_id: str, caller=Depends(require_admin_or_manager)):
    sb = get_client()
    assert_agent_in_scope(sb, caller, agent_id)
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
    # `phone` is optional — column may not exist yet on older deployments.
    sel_with_phone = "id, name, avatar_url, goals, branch_id, phone, branches(name, city)"
    sel_base       = "id, name, avatar_url, goals, branch_id, branches(name, city)"
    try:
        res = sb.table("agents").select(sel_with_phone).eq("id", agent_id).execute()
    except Exception as e:
        if "phone" in str(e):
            res = sb.table("agents").select(sel_base).eq("id", agent_id).execute()
        else:
            raise
    if not res.data:
        raise HTTPException(404, "Not found")
    row = res.data[0]
    br = row.pop("branches", None) or {}
    row["branch_name"] = br.get("name")
    row["branch_city"] = br.get("city")
    return row


@router.patch("/me/phone")
def update_my_phone(body: dict, user=Depends(get_current_user)):
    """Agent self-service: set or update their work phone number.
    Required on first login — the dashboard freezes until this is set."""
    sb = get_client()
    agent_id = user["sub"]
    if agent_id == "admin":
        raise HTTPException(400, "Admin has no profile")
    raw = (body.get("phone") or "").strip()
    if not raw:
        raise HTTPException(400, "رقم الهاتف مطلوب")
    # Normalize to 10-digit Moroccan format (06/07XXXXXXXX). Accepts +212,
    # 00212, with or without leading 0, with spaces / dashes / dots.
    from services.ad_leads_sync import normalize_morocco_phone
    normalized = normalize_morocco_phone(raw)
    if not normalized:
        raise HTTPException(400, "رقم هاتف غير صالح — يجب أن يبدأ بـ 06 أو 07 ويتكوّن من 10 أرقام")
    try:
        sb.table("agents").update({"phone": normalized}).eq("id", agent_id).execute()
    except Exception as e:
        if "phone" in str(e):
            raise HTTPException(500, "ميزة الهاتف غير مفعّلة — اطلب من الإدارة تشغيل تحديث قاعدة البيانات (ALTER TABLE agents ADD COLUMN phone TEXT)")
        raise
    return {"phone": normalized, "message": "تم حفظ رقم الهاتف"}


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
    if not updates and not new_pin:
        return {"message": "لا يوجد تغيير"}

    def _do_update(with_plain):
        payload = dict(updates)
        if new_pin and with_plain:
            payload["pin_plain"] = new_pin
        return sb.table("agents").update(payload).eq("id", agent_id).execute()

    _safe_write(_do_update)
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
def list_requests(caller=Depends(require_admin_or_manager)):
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

    # Manager only sees signup requests targeting their own branches.
    scope_branch_ids = resolve_manager_branch_ids(sb, caller)
    if scope_branch_ids is not None:
        rows = [r for r in rows if r.get("requested_branch_id") in scope_branch_ids]
    return rows


class ApproveRequest(BaseModel):
    final_name: Optional[str] = None
    branch_id: Optional[str] = None


@router.post("/requests/{request_id}/approve")
def approve_request(request_id: str, body: ApproveRequest, caller=Depends(require_admin_or_manager)):
    sb = get_client()
    req = sb.table("agent_requests").select("*").eq("id", request_id).execute()
    if not req.data:
        raise HTTPException(404, "الطلب غير موجود")
    req = req.data[0]
    final_branch = body.branch_id or req.get("requested_branch_id")
    # Manager can only approve into branches inside their scope.
    scope_branch_ids = resolve_manager_branch_ids(sb, caller)
    if scope_branch_ids is not None:
        if not final_branch or final_branch not in scope_branch_ids:
            raise HTTPException(403, "الفرع المختار خارج نطاق إدارتك")
    name = (body.final_name or req["requested_name"]).strip()
    # Check name not taken
    existing = sb.table("agents").select("id").eq("name", name).execute()
    if existing.data:
        raise HTTPException(400, "هذا الاسم مستخدم مسبقاً")
    hashed = pwd_context.hash(req["password_plain"])

    def _do_insert(with_plain):
        data = {"name": name, "pin": hashed, "is_active": True}
        if with_plain:
            data["pin_plain"] = req["password_plain"]
        if final_branch:
            data["branch_id"] = final_branch
        return sb.table("agents").insert(data).execute()

    _safe_write(_do_insert)
    sb.table("agent_requests").update({"status": "approved", "final_name": name}).eq("id", request_id).execute()
    return {"message": f"تم قبول الوكيل {name}"}


@router.delete("/requests/{request_id}")
def reject_request(request_id: str, caller=Depends(require_admin_or_manager)):
    sb = get_client()
    # Manager can only reject requests targeting their own scope.
    scope_branch_ids = resolve_manager_branch_ids(sb, caller)
    if scope_branch_ids is not None:
        req = sb.table("agent_requests").select("requested_branch_id").eq("id", request_id).execute()
        if req.data and req.data[0].get("requested_branch_id") not in scope_branch_ids:
            raise HTTPException(403, "هذا الطلب خارج نطاق إدارتك")
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


@router.patch("/{agent_id}/reset-pin")
def reset_agent_pin(agent_id: str, body: dict, caller=Depends(require_admin_or_manager)):
    """Admin or scoped manager sets a new PIN for an agent. Stores both
    the bcrypt hash and the plaintext so the admin can see it in the
    agents list afterwards."""
    sb = get_client()
    assert_agent_in_scope(sb, caller, agent_id)
    new_pin = (body.get("pin") or "").strip()
    if not new_pin:
        raise HTTPException(400, "كلمة المرور فارغة")
    if len(new_pin) > 50:
        raise HTTPException(400, "كلمة المرور طويلة جداً")
    hashed = pwd_context.hash(new_pin)

    def _do_update(with_plain):
        payload = {"pin": hashed}
        if with_plain:
            payload["pin_plain"] = new_pin
        return sb.table("agents").update(payload).eq("id", agent_id).execute()

    result = _safe_write(_do_update)
    if not result.data:
        raise HTTPException(404, "الوكيل غير موجود")
    return {"message": "تم تحديث كلمة المرور", "pin_plain": new_pin}


@router.patch("/{agent_id}/rename")
def rename_agent(agent_id: str, body: dict, caller=Depends(require_admin_or_manager)):
    sb = get_client()
    assert_agent_in_scope(sb, caller, agent_id)
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
def transfer_agent_branch(agent_id: str, body: dict, caller=Depends(require_admin_or_manager)):
    sb = get_client()
    assert_agent_in_scope(sb, caller, agent_id)
    branch_id = body.get("branch_id") or None
    # Manager can only move agents to branches inside their own scope —
    # otherwise a branch manager could "leak" an agent to a branch they
    # don't oversee.
    scope_branch_ids = resolve_manager_branch_ids(sb, caller)
    if scope_branch_ids is not None and branch_id and branch_id not in scope_branch_ids:
        raise HTTPException(403, "لا يمكن نقل الوكيل خارج نطاق إدارتك")
    result = sb.table("agents").update({"branch_id": branch_id}).eq("id", agent_id).execute()
    if not result.data:
        raise HTTPException(404, "الوكيل غير موجود")
    return result.data[0]


class PromoteManagerBody(BaseModel):
    scope_type: str               # 'all' | 'city' | 'branch'
    scope_id: Optional[str] = None  # required when scope_type != 'all'


@router.patch("/{agent_id}/promote-manager")
def promote_to_manager(agent_id: str, body: PromoteManagerBody, admin=Depends(require_admin)):
    """Admin-only — promote an agent to branch manager with the given
    scope. The agent keeps their agent identity (still receives leads),
    but the JWT they get on next login carries role='manager' so admin
    endpoints scope-narrow data to their branch(es)."""
    sb = get_client()
    if body.scope_type not in ("all", "city", "branch"):
        raise HTTPException(400, "scope_type must be 'all', 'city' or 'branch'")
    if body.scope_type != "all" and not body.scope_id:
        raise HTTPException(400, "scope_id مطلوب لنطاق المدينة أو الفرع")
    payload = {
        "role": "manager",
        "manager_scope_type": body.scope_type,
        "manager_scope_id": body.scope_id if body.scope_type != "all" else None,
    }
    try:
        result = sb.table("agents").update(payload).eq("id", agent_id).execute()
    except Exception as e:
        if "role" in str(e) or "manager_scope" in str(e):
            raise HTTPException(400, "يرجى تنفيذ تحديث قاعدة البيانات لتفعيل أدوار المدراء")
        raise
    if not result.data:
        raise HTTPException(404, "الوكيل غير موجود")
    return {"id": agent_id, "role": "manager", **payload}


@router.patch("/{agent_id}/demote-manager")
def demote_manager(agent_id: str, admin=Depends(require_admin)):
    """Admin-only — revoke the manager role, returning the agent to a
    regular agent identity. They keep all their leads / history."""
    sb = get_client()
    payload = {"role": "agent", "manager_scope_type": None, "manager_scope_id": None}
    try:
        result = sb.table("agents").update(payload).eq("id", agent_id).execute()
    except Exception:
        result = sb.table("agents").update({"role": "agent"}).eq("id", agent_id).execute()
    if not result.data:
        raise HTTPException(404, "الوكيل غير موجود")
    return {"id": agent_id, "role": "agent"}


@router.patch("/{agent_id}/accepts-leads")
def set_accepts_leads(agent_id: str, body: dict, caller=Depends(require_admin_or_manager)):
    """Pause / resume new-lead distribution for a single agent. When false,
    the round-robin in ad_leads_sync skips this agent until the admin/
    manager flips it back on. Replaces the agent-managed off-dates flow."""
    sb = get_client()
    assert_agent_in_scope(sb, caller, agent_id)
    val = body.get("accepts_leads")
    if not isinstance(val, bool):
        raise HTTPException(400, "accepts_leads must be true or false")
    try:
        result = sb.table("agents").update({"accepts_leads": val}).eq("id", agent_id).execute()
    except Exception as e:
        if "accepts_leads" in str(e):
            raise HTTPException(400, "يرجى تنفيذ تحديث قاعدة البيانات لتفعيل هذه الميزة")
        raise
    if not result.data:
        raise HTTPException(404, "الوكيل غير موجود")
    return {"id": agent_id, "accepts_leads": val}


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
