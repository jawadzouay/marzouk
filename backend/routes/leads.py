from fastapi import APIRouter, HTTPException, Depends, UploadFile, File
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt
from services.supabase_service import get_client
from services.claude_service import extract_leads_from_image
from services.sheets_service import append_leads_to_sheet, append_to_archive, update_lead_in_sheet
from services.swap_service import get_swap_days
from models.lead import LeadSubmit
from dotenv import load_dotenv
from datetime import datetime, timedelta
import os

load_dotenv()

router = APIRouter()
security = HTTPBearer()
JWT_SECRET = os.getenv("JWT_SECRET")
ALGORITHM = "HS256"


def get_current_agent(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[ALGORITHM])
        if payload.get("role") not in ("agent", "admin"):
            raise HTTPException(status_code=403, detail="Forbidden")
        return payload
    except Exception:
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.post("/extract")
async def extract_leads(file: UploadFile = File(...), agent=Depends(get_current_agent)):
    allowed = ["image/jpeg", "image/png", "image/webp", "image/gif"]
    if file.content_type not in allowed:
        raise HTTPException(status_code=400, detail="نوع الملف غير مدعوم")

    image_bytes = await file.read()
    leads = await extract_leads_from_image(image_bytes, file.content_type)
    return {"leads": leads}


@router.post("/submit")
async def submit_leads(submission: LeadSubmit, agent=Depends(get_current_agent)):
    sb = get_client()
    agent_id = agent["sub"]

    if agent_id == "admin":
        raise HTTPException(status_code=403, detail="Admin cannot submit leads")

    today = datetime.utcnow().date().isoformat()
    # Use agent-selected date if provided (for submitting yesterday's list today)
    submission_date = submission.source_date.isoformat() if submission.source_date else today

    # Allow up to 3 submissions per selected date
    existing_sub = sb.table("submissions").select("id").eq("agent_id", agent_id).eq("submission_date", submission_date).execute()
    submissions_done = len(existing_sub.data)
    if submissions_done >= 3:
        raise HTTPException(status_code=400, detail=f"وصلت للحد الأقصى ليوم {submission_date} — 3 إرسالات فقط مسموح بها")

    # Get agent name and branch
    agent_row = sb.table("agents").select("name, branches(name)").eq("id", agent_id).execute()
    agent_name = agent_row.data[0]["name"] if agent_row.data else "Unknown"
    branch_info = agent_row.data[0].get("branches") if agent_row.data else None
    branch_name = branch_info.get("name", "") if branch_info else ""

    # Get blacklist
    blacklist = sb.table("blacklist").select("phone").execute()
    blacklisted_phones = {b["phone"] for b in blacklist.data}

    saved_leads = []
    skipped = []
    swap_days = get_swap_days()
    swap_eligible_at = (datetime.utcnow() + timedelta(days=swap_days)).isoformat()

    for lead in submission.leads:
        phone = lead.phone

        # Block blacklisted
        if phone in blacklisted_phones:
            skipped.append({"phone": phone, "reason": "blacklisted"})
            continue

        # Check duplicate
        dup = sb.table("leads").select("id").eq("phone", phone).execute()
        if dup.data:
            skipped.append({"phone": phone, "reason": "duplicate"})
            continue

        # Determine swap eligibility:
        # B.V, N.R, P.I → swap pool after 4 days
        # Autre ville → stays with original agent, never swapped
        # RDV → no swap
        status = lead.status
        not_swappable = status in ("Autre ville", "RDV")

        lead_data = {
            "phone": phone,
            "name": lead.name,
            "level": lead.level,
            "city": lead.city,
            "status": status,
            "original_agent": agent_id,
            "current_agent": agent_id,
            "swap_count": 0,
            "locked": True,
            "is_blacklisted": False,
            "source_date": submission_date,
            "swap_eligible_at": None if not_swappable else swap_eligible_at
        }

        result = sb.table("leads").insert(lead_data).execute()
        new_lead = result.data[0]
        saved_leads.append(new_lead)

        # Log history
        sb.table("lead_history").insert({
            "lead_id": new_lead["id"],
            "agent_id": agent_id,
            "action": "submitted",
            "status_before": None,
            "status_after": status,
            "note": "Initial submission"
        }).execute()

    # Log submission
    sb.table("submissions").insert({
        "agent_id": agent_id,
        "submission_date": submission_date,
        "leads_count": len(saved_leads)
    }).execute()

    # Sync ALL leads to Google Sheets (agent tab + All Leads tab)
    sheets_error = None
    if saved_leads:
        try:
            append_leads_to_sheet(agent_name, saved_leads, submission_date, branch_name=branch_name)
        except Exception as e:
            sheets_error = str(e)
            print(f"[SHEETS ERROR] {e}")

    new_count = submissions_done + 1
    return {
        "saved": len(saved_leads),
        "skipped": skipped,
        "leads": saved_leads,
        "sheets_error": sheets_error,
        "submissions_today": new_count,
        "submissions_remaining": max(0, 3 - new_count)
    }


@router.patch("/{lead_id}/status")
def update_lead_status(lead_id: str, body: dict, agent=Depends(get_current_agent)):
    sb = get_client()
    agent_id = agent["sub"]
    new_status = body.get("status")

    if not new_status or len(new_status.strip()) == 0:
        raise HTTPException(status_code=400, detail="الحالة فارغة")

    lead = sb.table("leads").select("*").eq("id", lead_id).execute()
    if not lead.data:
        raise HTTPException(status_code=404, detail="Lead not found")

    lead_row = lead.data[0]

    # Ownership check — only current_agent or admin can update
    if agent_id != "admin" and lead_row.get("current_agent") != agent_id:
        raise HTTPException(status_code=403, detail="ليس لديك صلاحية تعديل هذا العميل")

    old_status = lead_row["status"]

    swap_eligible_at = None
    if new_status in ("B.V", "N.R"):
        swap_eligible_at = (datetime.utcnow() + timedelta(days=get_swap_days())).isoformat()

    sb.table("leads").update({
        "status": new_status,
        "swap_eligible_at": swap_eligible_at
    }).eq("id", lead_id).execute()

    sb.table("lead_history").insert({
        "lead_id": lead_id,
        "agent_id": agent_id,
        "action": "status_updated",
        "status_before": old_status,
        "status_after": new_status,
        "note": f"Agent updated status"
    }).execute()

    # Sync status update to Google Sheets (fire-and-forget)
    try:
        agent_row = sb.table("agents").select("name, branches(name)").eq("id", lead_row["original_agent"]).execute()
        agent_name = agent_row.data[0]["name"] if agent_row.data else "Unknown"
        b_info = agent_row.data[0].get("branches") if agent_row.data else None
        b_name = b_info.get("name", "") if b_info else ""
        update_lead_in_sheet(agent_name, lead_id, new_status=new_status, branch_name=b_name)
    except Exception as e:
        print(f"[SHEETS STATUS SYNC] {e}")

    return {"message": "تم تحديث الحالة"}


@router.patch("/{lead_id}/register")
def register_lead(lead_id: str, body: dict, agent=Depends(get_current_agent)):
    sb = get_client()
    agent_id = agent["sub"]
    reg_type = body.get("registration_type")

    if reg_type not in ("logha", "maharat", "takwin"):
        raise HTTPException(status_code=400, detail="نوع التسجيل غير صحيح")

    lead = sb.table("leads").select("*").eq("id", lead_id).execute()
    if not lead.data:
        raise HTTPException(status_code=404, detail="Lead not found")

    lead_row = lead.data[0]

    # Ownership check
    if agent_id != "admin" and lead_row.get("current_agent") != agent_id:
        raise HTTPException(status_code=403, detail="ليس لديك صلاحية تسجيل هذا العميل")

    if lead_row["status"] in ("registered_logha", "registered_maharat", "registered_takwin"):
        raise HTTPException(status_code=400, detail="هذا الطالب مسجل مسبقاً")

    old_status = lead_row["status"]
    if reg_type == "logha":
        new_status = "registered_logha"
        points = 1
    elif reg_type == "maharat":
        new_status = "registered_maharat"
        points = 1
    else:
        new_status = "registered_takwin"
        points = 2

    sb.table("leads").update({
        "status": new_status,
        "swap_eligible_at": None
    }).eq("id", lead_id).execute()

    sb.table("lead_history").insert({
        "lead_id": lead_id,
        "agent_id": agent_id,
        "action": "registered",
        "status_before": old_status,
        "status_after": new_status,
        "note": f"reg_type:{reg_type} points:{points}"
    }).execute()

    return {"message": "تم التسجيل بنجاح", "points": points}


@router.patch("/{lead_id}/visited-center")
def mark_visited_center(lead_id: str, agent=Depends(get_current_agent)):
    sb = get_client()
    agent_id = agent["sub"]
    lead = sb.table("leads").select("id,status,current_agent").eq("id", lead_id).execute()
    if not lead.data:
        raise HTTPException(status_code=404, detail="Lead not found")
    if agent_id != "admin" and lead.data[0].get("current_agent") != agent_id:
        raise HTTPException(status_code=403, detail="ليس لديك صلاحية تعديل هذا العميل")
    sb.table("lead_history").insert({
        "lead_id": lead_id,
        "agent_id": agent_id,
        "action": "visited_center",
        "status_before": lead.data[0]["status"],
        "status_after": lead.data[0]["status"],
        "note": "زار المركز بدون موعد RDV — Visited center (no RDV)"
    }).execute()
    return {"message": "تم تسجيل الزيارة"}


@router.patch("/{lead_id}/note")
def update_lead_note(lead_id: str, body: dict, agent=Depends(get_current_agent)):
    sb = get_client()
    note = body.get("note", "").strip()
    lead = sb.table("leads").select("id").eq("id", lead_id).execute()
    if not lead.data:
        raise HTTPException(status_code=404, detail="Lead not found")
    lead_full = sb.table("leads").select("original_agent, current_agent").eq("id", lead_id).execute()
    if not lead_full.data:
        raise HTTPException(status_code=404, detail="Lead not found")
    agent_id = agent["sub"]
    if agent_id != "admin" and lead_full.data[0].get("current_agent") != agent_id:
        raise HTTPException(status_code=403, detail="ليس لديك صلاحية تعديل هذا العميل")
    sb.table("leads").update({"note": note}).eq("id", lead_id).execute()

    # Sync note to Google Sheets
    try:
        if lead_full.data:
            agent_row = sb.table("agents").select("name, branches(name)").eq("id", lead_full.data[0]["original_agent"]).execute()
            agent_name = agent_row.data[0]["name"] if agent_row.data else "Unknown"
            b_info = agent_row.data[0].get("branches") if agent_row.data else None
            b_name = b_info.get("name", "") if b_info else ""
            update_lead_in_sheet(agent_name, lead_id, note=note, branch_name=b_name)
    except Exception as e:
        print(f"[SHEETS NOTE SYNC] {e}")

    return {"message": "تم حفظ الملاحظة"}


@router.get("/submissions-today")
def get_submissions_today(agent=Depends(get_current_agent), date: str = None):
    sb = get_client()
    agent_id = agent["sub"]
    check_date = date if date else datetime.utcnow().date().isoformat()
    result = sb.table("submissions").select("id,leads_count,submitted_at").eq("agent_id", agent_id).eq("submission_date", check_date).execute()
    count = len(result.data)
    return {"count": count, "remaining": max(0, 3 - count), "date": check_date}


@router.get("/my")
def get_my_leads(agent=Depends(get_current_agent)):
    sb = get_client()
    agent_id = agent["sub"]
    now = datetime.utcnow().isoformat()

    # Show leads where swap window hasn't expired yet OR it's null (permanent: RDV, registered, Autre ville)
    # Two queries: no swap timer at all, OR timer is still in the future
    no_timer = sb.table("leads").select("*").eq("current_agent", agent_id).is_("swap_eligible_at", "null").order("submitted_at", desc=True).execute()
    still_active = sb.table("leads").select("*").eq("current_agent", agent_id).gt("swap_eligible_at", now).order("submitted_at", desc=True).execute()

    # Combine and deduplicate by id, preserving order (no_timer first so registered leads show at top)
    seen = set()
    combined = []
    for lead in (no_timer.data or []) + (still_active.data or []):
        if lead["id"] not in seen:
            seen.add(lead["id"])
            combined.append(lead)

    # Re-sort combined list by submitted_at desc
    combined.sort(key=lambda x: x.get("submitted_at") or "", reverse=True)
    return combined


@router.get("/")
def get_all_leads(agent=Depends(get_current_agent)):
    if agent.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    sb = get_client()
    result = sb.table("leads").select("*").order("submitted_at", desc=True).execute()
    return result.data
