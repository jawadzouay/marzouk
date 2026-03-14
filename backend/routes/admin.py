from fastapi import APIRouter, HTTPException, Depends, Query
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
def admin_dashboard(
    date_from: str = Query(None),
    date_to: str = Query(None),
    admin=Depends(require_admin)
):
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

    swap_pool = sb.table("leads").select("id", count="exact").in_("status", ["B.V", "N.R"]).lt("swap_count", 3).execute()

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
        reg_takwin  = sum(1 for r in reg_leads.data if r["status"] == "registered_takwin")
        registered_students = reg_logha + reg_takwin
        registered_inscre   = reg_logha + reg_takwin * 2

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
        leads = sb.table("leads").select("phone, status, submitted_at, city").eq("original_agent", agent["id"]).execute()
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

        # Check 3: All leads from the same city
        if total >= 10:
            cities = [l["city"] for l in leads.data if l.get("city")]
            if cities and len(set(cities)) == 1:
                agent_flags.append({
                    "type": "same_city",
                    "severity": "medium",
                    "message": f"كل العملاء من نفس المدينة: {cities[0]}"
                })

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
