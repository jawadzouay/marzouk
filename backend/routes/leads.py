from fastapi import APIRouter, HTTPException, Depends, UploadFile, File
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt
from services.supabase_service import get_client
from services.claude_service import extract_leads_from_image
from services.sheets_service import append_leads_to_sheet, append_to_archive
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

    # Check if agent already submitted today
    existing_sub = sb.table("submissions").select("id").eq("agent_id", agent_id).eq("submission_date", today).execute()
    if existing_sub.data:
        raise HTTPException(status_code=400, detail="لقد قمت بالإرسال مسبقاً اليوم")

    # Get agent name
    agent_row = sb.table("agents").select("name").eq("id", agent_id).execute()
    agent_name = agent_row.data[0]["name"] if agent_row.data else "Unknown"

    # Get blacklist
    blacklist = sb.table("blacklist").select("phone").execute()
    blacklisted_phones = {b["phone"] for b in blacklist.data}

    saved_leads = []
    skipped = []
    swap_eligible_at = (datetime.utcnow() + timedelta(days=4)).isoformat()

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

        # Determine archive vs swap pool
        status = lead.status
        locked = True
        is_archived = status in ("P.I", "Autre ville")

        lead_data = {
            "phone": phone,
            "name": lead.name,
            "level": lead.level,
            "city": lead.city,
            "status": status,
            "original_agent": agent_id,
            "current_agent": agent_id,
            "swap_count": 0,
            "locked": locked,
            "is_blacklisted": False,
            "source_date": submission.source_date.isoformat() if submission.source_date else today,
            "swap_eligible_at": None if is_archived or status == "RDV" else swap_eligible_at
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

        # Archive immediately if P.I or Autre ville
        if is_archived:
            append_to_archive(agent_name, [new_lead], today)

    # Log submission
    sb.table("submissions").insert({
        "agent_id": agent_id,
        "submission_date": today,
        "leads_count": len(saved_leads)
    }).execute()

    # Sync to Google Sheets
    sheets_error = None
    non_archived = [l for l in saved_leads if l["status"] not in ("P.I", "Autre ville")]
    if non_archived:
        try:
            append_leads_to_sheet(agent_name, non_archived, today)
        except Exception as e:
            sheets_error = str(e)
            print(f"[SHEETS ERROR] {e}")

    return {
        "saved": len(saved_leads),
        "skipped": skipped,
        "leads": saved_leads,
        "sheets_error": sheets_error
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
    old_status = lead_row["status"]

    from datetime import datetime, timedelta
    swap_eligible_at = None
    if new_status in ("B.V", "N.R"):
        swap_eligible_at = (datetime.utcnow() + timedelta(days=4)).isoformat()

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
    lead = sb.table("leads").select("id,status").eq("id", lead_id).execute()
    if not lead.data:
        raise HTTPException(status_code=404, detail="Lead not found")
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
    sb.table("leads").update({"note": note}).eq("id", lead_id).execute()
    return {"message": "تم حفظ الملاحظة"}


@router.get("/my")
def get_my_leads(agent=Depends(get_current_agent)):
    sb = get_client()
    agent_id = agent["sub"]
    result = sb.table("leads").select("*").eq("current_agent", agent_id).order("submitted_at", desc=True).execute()
    return result.data


@router.get("/")
def get_all_leads(agent=Depends(get_current_agent)):
    if agent.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    sb = get_client()
    result = sb.table("leads").select("*").order("submitted_at", desc=True).execute()
    return result.data
