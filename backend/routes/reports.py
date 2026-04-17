from fastapi import APIRouter, HTTPException, Depends, Query, Form, UploadFile, File
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt
from services.supabase_service import get_client
from dotenv import load_dotenv
from datetime import datetime, timedelta, date
from typing import List, Optional
import os
import logging

load_dotenv()

router = APIRouter()
security = HTTPBearer()
JWT_SECRET = os.getenv("JWT_SECRET")
ALGORITHM = "HS256"


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[ALGORITHM])
        if payload.get("role") not in ("agent", "admin"):
            raise HTTPException(status_code=403, detail="Forbidden")
        return payload
    except Exception:
        raise HTTPException(status_code=401, detail="Unauthorized")


def require_admin(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[ALGORITHM])
        if payload.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Admin only")
        return payload
    except Exception:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── Daily Reports ──────────────────────────────────────

@router.post("/submit")
async def submit_daily_report(
    report_date: str = Form(...),
    messages: int = Form(0),
    rdv: int = Form(0),
    autre_ville: int = Form(0),
    pi: int = Form(0),
    bv: int = Form(0),
    pe: int = Form(0),
    over_40: int = Form(0),
    visits: int = Form(0),
    registered: int = Form(0),
    photos: List[UploadFile] = File(None),
    user=Depends(get_current_user)
):
    sb = get_client()
    agent_id = user["sub"]

    # Check existing submission count for this date
    existing = sb.table("daily_reports").select("id, submit_count") \
        .eq("agent_id", agent_id).eq("report_date", report_date).execute()

    if existing.data:
        row = existing.data[0]
        if row["submit_count"] >= 2:
            raise HTTPException(status_code=400, detail="لقد استنفدت محاولتي الإرسال لهذا اليوم")
        new_count = row["submit_count"] + 1
    else:
        new_count = 1

    # Read photos to consume the request body, but skip Drive upload for now
    photo_count = 0
    if photos:
        for ph in photos:
            if ph and ph.filename:
                try:
                    await ph.read()
                    photo_count += 1
                except Exception:
                    pass

    report_data = {
        "agent_id": agent_id,
        "report_date": report_date,
        "messages": messages,
        "rdv": rdv,
        "autre_ville": autre_ville,
        "pi": pi,
        "bv": bv,
        "pe": pe,
        "over_40": over_40,
        "visits": visits,
        "registered": registered,
        "submitted_at": datetime.utcnow().isoformat(),
        "submit_count": new_count,
    }

    # Upsert (insert or update on conflict)
    result = sb.table("daily_reports").upsert(report_data, on_conflict="agent_id,report_date").execute()

    is_first = new_count == 1
    return {
        "message": "تم إرسال التقرير بنجاح",
        "submit_count": new_count,
        "remaining": 2 - new_count,
        "is_first_submit": is_first,
        "photo_count": photo_count,
        "report": result.data[0] if result.data else report_data
    }


@router.get("/my")
def get_my_reports(
    date_from: str = Query(None),
    date_to: str = Query(None),
    user=Depends(get_current_user)
):
    sb = get_client()
    agent_id = user["sub"]
    q = sb.table("daily_reports").select("*").eq("agent_id", agent_id)
    if date_from:
        q = q.gte("report_date", date_from)
    if date_to:
        q = q.lte("report_date", date_to)
    result = q.order("report_date", desc=True).execute()
    return result.data


@router.get("/all")
def get_all_reports(
    date_from: str = Query(None),
    date_to: str = Query(None),
    agent_id: str = Query(None),
    admin=Depends(require_admin)
):
    sb = get_client()
    q = sb.table("daily_reports").select("*, agents(name)")
    if agent_id:
        q = q.eq("agent_id", agent_id)
    if date_from:
        q = q.gte("report_date", date_from)
    if date_to:
        q = q.lte("report_date", date_to)
    result = q.order("report_date", desc=True).execute()
    return result.data


@router.get("/submission-status")
def submission_status(admin=Depends(require_admin)):
    sb = get_client()
    today = date.today()
    yesterday = today - timedelta(days=1)

    agents = sb.table("agents").select("id, name, day_off").eq("is_active", True).execute()

    today_subs = sb.table("daily_reports").select("agent_id").eq("report_date", today.isoformat()).execute()
    yesterday_subs = sb.table("daily_reports").select("agent_id").eq("report_date", yesterday.isoformat()).execute()

    today_ids = {s["agent_id"] for s in today_subs.data}
    yesterday_ids = {s["agent_id"] for s in yesterday_subs.data}

    today_dow = today.weekday()  # 0=Mon ... 6=Sun
    # Convert to JS-style (0=Sun) for day_off comparison
    js_today = (today_dow + 1) % 7
    js_yesterday = (yesterday.weekday() + 1) % 7

    not_today = []
    not_yesterday = []
    submitted_today = []

    for a in agents.data:
        aid, name, day_off = a["id"], a["name"], a.get("day_off")
        if aid not in today_ids and day_off != js_today:
            not_today.append({"id": aid, "name": name})
        if aid in today_ids:
            submitted_today.append({"id": aid, "name": name})
        if aid not in yesterday_ids and day_off != js_yesterday:
            not_yesterday.append({"id": aid, "name": name})

    return {
        "not_submitted_today": not_today,
        "submitted_today": submitted_today,
        "not_submitted_yesterday": not_yesterday,
        "today": today.isoformat(),
        "yesterday": yesterday.isoformat()
    }


# ── Goals ──────────────────────────────────────────────

@router.post("/goals")
def create_goal(body: dict, user=Depends(get_current_user)):
    sb = get_client()
    is_admin = user.get("role") == "admin"

    agent_id = body.get("agent_id") if is_admin else user["sub"]
    if not agent_id:
        raise HTTPException(400, "agent_id required")

    target = body.get("target_registered")
    start = body.get("start_date")
    end = body.get("end_date")

    if not target or not start or not end:
        raise HTTPException(400, "target_registered, start_date, end_date required")
    if int(target) <= 0:
        raise HTTPException(400, "الهدف يجب أن يكون أكبر من صفر")

    result = sb.table("agent_goals").insert({
        "agent_id": agent_id,
        "target_registered": int(target),
        "start_date": start,
        "end_date": end,
        "is_admin_goal": is_admin
    }).execute()

    return result.data[0] if result.data else {"message": "تم إنشاء الهدف"}


@router.get("/goals")
def get_goals(
    agent_id: str = Query(None),
    user=Depends(get_current_user)
):
    sb = get_client()
    is_admin = user.get("role") == "admin"

    if is_admin:
        q = sb.table("agent_goals").select("*, agents(name)")
        if agent_id:
            q = q.eq("agent_id", agent_id)
    else:
        q = sb.table("agent_goals").select("*").eq("agent_id", user["sub"])

    result = q.order("created_at", desc=True).execute()
    return result.data


@router.patch("/goals/{goal_id}")
def update_goal(goal_id: str, body: dict, user=Depends(get_current_user)):
    sb = get_client()
    is_admin = user.get("role") == "admin"

    goal = sb.table("agent_goals").select("*").eq("id", goal_id).execute()
    if not goal.data:
        raise HTTPException(404, "Goal not found")

    g = goal.data[0]
    if not is_admin and g.get("is_admin_goal"):
        raise HTTPException(403, "لا يمكنك تعديل هدف المدير")
    if not is_admin and g["agent_id"] != user["sub"]:
        raise HTTPException(403, "Forbidden")

    updates = {}
    if "target_registered" in body:
        t = int(body["target_registered"])
        if t <= 0:
            raise HTTPException(400, "الهدف يجب أن يكون أكبر من صفر")
        updates["target_registered"] = t
    if "start_date" in body:
        updates["start_date"] = body["start_date"]
    if "end_date" in body:
        updates["end_date"] = body["end_date"]

    if not updates:
        raise HTTPException(400, "لا يوجد حقول للتحديث")

    result = sb.table("agent_goals").update(updates).eq("id", goal_id).execute()
    return result.data[0] if result.data else {"message": "تم التحديث"}


@router.delete("/goals/{goal_id}")
def delete_goal(goal_id: str, user=Depends(get_current_user)):
    sb = get_client()
    is_admin = user.get("role") == "admin"

    goal = sb.table("agent_goals").select("*").eq("id", goal_id).execute()
    if not goal.data:
        raise HTTPException(404, "Goal not found")

    g = goal.data[0]
    if not is_admin and g.get("is_admin_goal"):
        raise HTTPException(403, "لا يمكنك حذف هدف المدير")
    if not is_admin and g["agent_id"] != user["sub"]:
        raise HTTPException(403, "Forbidden")

    sb.table("agent_goals").delete().eq("id", goal_id).execute()
    return {"message": "تم حذف الهدف"}


@router.get("/goals/{goal_id}/progress")
def goal_progress(goal_id: str, user=Depends(get_current_user)):
    sb = get_client()

    goal = sb.table("agent_goals").select("*").eq("id", goal_id).execute()
    if not goal.data:
        raise HTTPException(404, "Goal not found")

    g = goal.data[0]
    agent_id = g["agent_id"]

    # Fetch daily reports in the goal period
    reports = sb.table("daily_reports").select("report_date, registered") \
        .eq("agent_id", agent_id) \
        .gte("report_date", g["start_date"]) \
        .lte("report_date", g["end_date"]) \
        .order("report_date").execute()

    # Build cumulative data
    cumulative = []
    total = 0
    for r in reports.data:
        total += r["registered"]
        cumulative.append({"date": r["report_date"], "value": total})

    # Calculate where they should be today on the target line
    start = datetime.strptime(g["start_date"], "%Y-%m-%d").date()
    end = datetime.strptime(g["end_date"], "%Y-%m-%d").date()
    today = date.today()
    current_day = min(today, end)

    total_days = (end - start).days or 1
    elapsed_days = (current_day - start).days
    expected_today = round((elapsed_days / total_days) * g["target_registered"], 1)
    delta = total - expected_today

    return {
        "goal": g,
        "cumulative": cumulative,
        "actual_total": total,
        "expected_today": expected_today,
        "delta": delta,
        "target_line": [
            {"date": g["start_date"], "value": 0},
            {"date": g["end_date"], "value": g["target_registered"]}
        ]
    }
