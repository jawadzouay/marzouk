from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt
from passlib.context import CryptContext
from services.supabase_service import get_client
from dotenv import load_dotenv
from datetime import datetime, timedelta
import os

load_dotenv()

router = APIRouter()
security = HTTPBearer()
JWT_SECRET = os.getenv("JWT_SECRET")
ALGORITHM = "HS256"
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def require_admin(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[ALGORITHM])
        if payload.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Admin only")
        return payload
    except Exception:
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.get("/dashboard")
def admin_dashboard(
    date_from: str = Query(None),
    date_to: str = Query(None),
    admin=Depends(require_admin)
):
    sb = get_client()
    today = datetime.utcnow().date().isoformat()

    agents = sb.table("agents").select("id, name, is_active, day_off").eq("is_active", True).execute()
    submissions_today = sb.table("submissions").select("agent_id").eq("submission_date", today).execute()
    submitted_ids = {s["agent_id"] for s in submissions_today.data}

    agent_status = []
    for agent in agents.data:
        agent_status.append({
            "id": agent["id"],
            "name": agent["name"],
            "submitted_today": agent["id"] in submitted_ids,
            "day_off": agent.get("day_off")  # 0=Sun … 6=Sat, None=no fixed day off
        })

    leads_q = sb.table("leads").select("id", count="exact")
    if date_from:
        leads_q = leads_q.gte("submitted_at", date_from)
    if date_to:
        leads_q = leads_q.lte("submitted_at", date_to + "T23:59:59")
    total_leads = leads_q.execute()

    rdv_q = sb.table("rdv").select("id", count="exact")
    if date_from:
        rdv_q = rdv_q.gte("created_at", date_from)
    if date_to:
        rdv_q = rdv_q.lte("created_at", date_to + "T23:59:59")
    total_rdv = rdv_q.execute()

    swap_pool = sb.table("leads").select("id", count="exact").in_("status", ["B.V", "N.R", "P.I"]).lt("swap_count", 3).execute()

    return {
        "agents": agent_status,
        "total_leads": total_leads.count,
        "total_rdv": total_rdv.count,
        "swap_pool_count": swap_pool.count,
        "date": today
    }


@router.get("/leaderboard")
def leaderboard(
    date_from: str = Query(None),
    date_to: str = Query(None),
    agent_id: str = Query(None),
    admin=Depends(require_admin)
):
    sb = get_client()
    agents_q = sb.table("agents").select("id, name").eq("is_active", True)
    if agent_id:
        agents_q = agents_q.eq("id", agent_id)
    agents = agents_q.execute()

    board = []
    for agent in agents.data:
        leads_q = sb.table("leads").select("status, submitted_at, city, phone").eq("original_agent", agent["id"])
        if date_from:
            leads_q = leads_q.gte("submitted_at", date_from)
        if date_to:
            leads_q = leads_q.lte("submitted_at", date_to + "T23:59:59")
        leads = leads_q.execute()

        total    = len(leads.data)
        rdv_count = sum(1 for l in leads.data if l["status"] == "RDV")
        bv_count  = sum(1 for l in leads.data if l["status"] == "B.V")
        nr_count  = sum(1 for l in leads.data if l["status"] == "N.R")
        pi_count  = sum(1 for l in leads.data if l["status"] == "P.I")
        av_count  = sum(1 for l in leads.data if l["status"] == "Autre ville")
        rdv_pct   = round((rdv_count / total * 100) if total else 0, 1)

        rdvs_q = sb.table("rdv").select("status").eq("agent_id", agent["id"])
        if date_from:
            rdvs_q = rdvs_q.gte("created_at", date_from)
        if date_to:
            rdvs_q = rdvs_q.lte("created_at", date_to + "T23:59:59")
        rdvs = rdvs_q.execute()

        showed_up        = sum(1 for r in rdvs.data if r["status"] == "showed_up")
        total_rdv_booked = len(rdvs.data)

        reg_leads_q = sb.table("leads").select("status").eq("current_agent", agent["id"])
        if date_from:
            reg_leads_q = reg_leads_q.gte("submitted_at", date_from)
        if date_to:
            reg_leads_q = reg_leads_q.lte("submitted_at", date_to + "T23:59:59")
        reg_leads = reg_leads_q.execute()

        reg_logha   = sum(1 for r in reg_leads.data if r["status"] == "registered_logha")
        reg_maharat = sum(1 for r in reg_leads.data if r["status"] == "registered_maharat")
        reg_takwin  = sum(1 for r in reg_leads.data if r["status"] == "registered_takwin")
        registered_students = reg_logha + reg_maharat + reg_takwin
        registered_inscre   = reg_logha + reg_maharat + reg_takwin * 2

        board.append({
            "id":                  agent["id"],
            "name":                agent["name"],
            "total_leads":         total,
            "rdv_count":           rdv_count,
            "bv_count":            bv_count,
            "nr_count":            nr_count,
            "pi_count":            pi_count,
            "av_count":            av_count,
            "rdv_pct":             rdv_pct,
            "total_rdv_booked":    total_rdv_booked,
            "showed_up":           showed_up,
            "registered_students": registered_students,
            "registered_inscre":   registered_inscre,
            # legacy keys
            "total": total,
            "rdv":   rdv_count,
        })

    board.sort(key=lambda x: x["rdv"], reverse=True)
    for i, entry in enumerate(board):
        entry["rank"] = i + 1

    return board


@router.get("/ethics")
def ethics_check(admin=Depends(require_admin)):
    sb = get_client()
    agents = sb.table("agents").select("id, name").eq("is_active", True).execute()
    flags = []

    for agent in agents.data:
        agent_flags = []
        leads = sb.table("leads").select("phone, status, submitted_at, city, swap_count").eq("original_agent", agent["id"]).execute()
        total = len(leads.data)
        rdv_count = sum(1 for l in leads.data if l["status"] == "RDV")

        # Check 1: Many RDVs booked but zero show-ups
        rdvs = sb.table("rdv").select("status").eq("agent_id", agent["id"]).execute()
        rdv_total = len(rdvs.data)
        showed_up = sum(1 for r in rdvs.data if r["status"] == "showed_up")
        if rdv_total >= 5 and showed_up == 0:
            agent_flags.append({
                "type": "fake_rdv",
                "severity": "high",
                "message": f"لديه {rdv_total} موعد RDV بدون أي حضور"
            })

        # Check 2: Suspiciously high single-day submission
        subs = sb.table("submissions").select("leads_count, submission_date") \
            .eq("agent_id", agent["id"]).order("leads_count", desc=True).limit(1).execute()
        if subs.data and subs.data[0]["leads_count"] > 50:
            agent_flags.append({
                "type": "suspicious_volume",
                "severity": "medium",
                "message": f"إرسال {subs.data[0]['leads_count']} عميل في يوم واحد ({subs.data[0]['submission_date']})"
            })

        # Check 3: REMOVED — agents are assigned one city only, same-city is normal

        # Check 4: Sequential phone numbers (possible fabricated list)
        phones = [l["phone"] for l in leads.data if l.get("phone") and l["phone"].isdigit() and len(l["phone"]) == 10]
        phones_sorted = sorted(set(phones))
        sequential = sum(
            1 for i in range(len(phones_sorted) - 1)
            if abs(int(phones_sorted[i + 1]) - int(phones_sorted[i])) <= 2
        )
        if sequential >= 5:
            agent_flags.append({
                "type": "sequential_phones",
                "severity": "high",
                "message": f"{sequential} أرقام متتالية أو متقاربة — قائمة مشبوهة"
            })

        # Check 5: 0% RDV rate with 25+ leads
        if total >= 25 and rdv_count == 0:
            agent_flags.append({
                "type": "zero_rdv",
                "severity": "medium",
                "message": f"لا يوجد أي RDV رغم {total} عميل — أداء أو بيانات مشكوك فيها"
            })

        # Check 6: Wrong number / Blacklist — leads originally submitted by this agent
        # that were later marked as wrong number by another agent after swap
        all_agent_leads = sb.table("leads").select("status, swap_count") \
            .eq("original_agent", agent["id"]).execute()
        wrong_numbers = sum(
            1 for l in all_agent_leads.data
            if l.get("status") == "Blacklist" and (l.get("swap_count") or 0) > 0
        )
        if wrong_numbers >= 3:
            agent_flags.append({
                "type": "wrong_numbers",
                "severity": "high",
                "message": f"{wrong_numbers} رقم خاطئ — عملاء قدّمهم ثم صنّفهم الوكيل الجديد كـ 'رقم خاطئ'"
            })
        elif wrong_numbers >= 1:
            agent_flags.append({
                "type": "wrong_numbers",
                "severity": "medium",
                "message": f"{wrong_numbers} رقم مشكوك فيه — صُنِّف كـ 'رقم خاطئ' بعد التبادل"
            })

        if agent_flags:
            flags.append({
                "agent_id":   agent["id"],
                "agent_name": agent["name"],
                "total_leads": total,
                "total_rdv":  rdv_total,
                "flags":      agent_flags
            })

    return {"flagged_agents": flags, "total_flagged": len(flags)}


@router.get("/submissions/calendar")
def submission_calendar(admin=Depends(require_admin)):
    sb = get_client()
    result = sb.table("submissions").select("agent_id, submission_date, leads_count, agents(name)") \
        .order("submission_date", desc=True).execute()
    return result.data


@router.get("/sheets-config")
def get_sheets_config(admin=Depends(require_admin)):
    sb = get_client()
    env_id = os.getenv("GOOGLE_SHEET_ID", "")
    try:
        row = sb.table("settings").select("value").eq("key", "google_sheet_id").execute()
        db_id = row.data[0]["value"] if row.data else None
    except Exception:
        db_id = None
    sheet_id = db_id or env_id
    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}" if sheet_id else None
    return {
        "sheet_id":   sheet_id,
        "sheet_url":  sheet_url,
        "source":     "db" if db_id else ("env" if env_id else "none"),
        "env_id":     env_id,
    }


@router.put("/sheets-config")
def set_sheets_config(body: dict, admin=Depends(require_admin)):
    raw = (body.get("sheet_id") or "").strip()
    if not raw:
        raise HTTPException(400, "يرجى إدخال معرّف الجدول أو الرابط")
    # Accept full URL or bare ID
    if "spreadsheets/d/" in raw:
        parts = raw.split("spreadsheets/d/")
        raw = parts[1].split("/")[0].split("?")[0]
    if not raw:
        raise HTTPException(400, "لم يتم التعرف على معرّف الجدول")
    sb = get_client()
    sb.table("settings").upsert({"key": "google_sheet_id", "value": raw, "updated_at": datetime.utcnow().isoformat()}).execute()
    return {"sheet_id": raw, "sheet_url": f"https://docs.google.com/spreadsheets/d/{raw}"}


def _service_account_email() -> str:
    import os as _os, json as _json
    raw = _os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
    if not raw:
        return ""
    if not raw.startswith("{"):
        brace = raw.find("{")
        if brace != -1:
            raw = raw[brace:]
    try:
        return _json.loads(raw).get("client_email", "") if raw.startswith("{") else ""
    except Exception:
        return ""


@router.get("/service-account")
def service_account_info(admin=Depends(require_admin)):
    """Returns the service account email that admins must share each sheet with."""
    return {"client_email": _service_account_email()}


@router.get("/sheets-test")
def test_sheets_connection(admin=Depends(require_admin)):
    """Test Google Sheets connectivity and return a clear diagnosis."""
    import os as _os
    from services.sheets_service import get_sheet_id, get_sheets_service
    creds_raw = _os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
    sheet_id  = get_sheet_id()
    diag = {
        "has_credentials": bool(creds_raw),
        "credentials_looks_like_json": creds_raw.startswith("{") if creds_raw else False,
        "credentials_preview": creds_raw[:60] + "..." if len(creds_raw) > 60 else creds_raw,
        "client_email": _service_account_email(),
        "sheet_id": sheet_id,
    }
    try:
        svc = get_sheets_service()
        result = svc.spreadsheets().get(spreadsheetId=sheet_id, fields="spreadsheetId,properties/title").execute()
        diag["connection"] = "ok"
        diag["sheet_title"] = result.get("properties", {}).get("title", "")
    except Exception as e:
        diag["connection"] = "error"
        diag["error"] = str(e)
    return diag


@router.delete("/sheets-config")
def delete_sheets_config(admin=Depends(require_admin)):
    sb = get_client()
    sb.table("settings").delete().eq("key", "google_sheet_id").execute()
    env_id = os.getenv("GOOGLE_SHEET_ID", "")
    return {"message": "تمت إزالة الجدول من قاعدة البيانات", "fallback_env": bool(env_id)}


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


@router.get("/swap-enabled")
def get_swap_enabled_setting(admin=Depends(require_admin)):
    from services.swap_service import get_swap_enabled
    return {"swap_enabled": get_swap_enabled()}


@router.patch("/swap-enabled")
def set_swap_enabled_setting(body: dict, admin=Depends(require_admin)):
    enabled = body.get("swap_enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(400, "swap_enabled يجب أن يكون true أو false")
    from services.swap_service import set_swap_enabled
    set_swap_enabled(enabled)
    return {"swap_enabled": enabled, "message": "تم تفعيل التبادل" if enabled else "تم إيقاف التبادل"}


@router.get("/swap-days")
def get_swap_days_setting(admin=Depends(require_admin)):
    from services.swap_service import get_swap_days
    return {"swap_days": get_swap_days()}


@router.patch("/swap-days")
def set_swap_days_setting(body: dict, admin=Depends(require_admin)):
    days = body.get("swap_days")
    if not isinstance(days, int) or days < 1 or days > 30:
        raise HTTPException(400, "يجب أن يكون عدد أيام التبادل بين 1 و 30")
    from services.swap_service import set_swap_days
    set_swap_days(days)
    return {"swap_days": days, "message": "تم تحديث مدة التبادل"}


@router.get("/credentials")
def get_admin_credentials(admin=Depends(require_admin)):
    sb = get_client()
    urow = sb.table("settings").select("value").eq("key", "admin_username").execute()
    username = urow.data[0]["value"] if urow.data else "admin"
    has_custom_pass = bool(
        sb.table("settings").select("value").eq("key", "admin_password_hash").execute().data
    )
    return {"username": username, "has_custom_password": has_custom_pass}


@router.patch("/credentials")
def update_admin_credentials(body: dict, admin=Depends(require_admin)):
    sb = get_client()
    new_username = (body.get("username") or "").strip()
    new_password = (body.get("password") or "").strip()
    if not new_username and not new_password:
        raise HTTPException(400, "يرجى إدخال اسم المستخدم أو كلمة المرور الجديدة")
    now = datetime.utcnow().isoformat()
    try:
        if new_username:
            sb.table("settings").upsert({"key": "admin_username", "value": new_username, "updated_at": now}).execute()
        if new_password:
            hashed = pwd_context.hash(new_password)
            sb.table("settings").upsert({"key": "admin_password_hash", "value": hashed, "updated_at": now}).execute()
    except Exception as e:
        raise HTTPException(500, f"خطأ في قاعدة البيانات — تأكد من وجود جدول settings: {str(e)}")
    return {"message": "تم تحديث بيانات الدخول"}
