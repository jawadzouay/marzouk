from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt
from services.supabase_service import get_client
from models.rdv import RDVCreate, RDVUpdate
from dotenv import load_dotenv
from datetime import datetime, timedelta
import os

load_dotenv()

router = APIRouter()
security = HTTPBearer()
JWT_SECRET = os.getenv("JWT_SECRET")
ALGORITHM = "HS256"


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[ALGORITHM])
        return payload
    except Exception:
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.post("/")
def book_rdv(rdv: RDVCreate, user=Depends(get_current_user)):
    sb = get_client()
    agent_id = user["sub"]

    # Update lead status to RDV
    sb.table("leads").update({
        "status": "RDV",
        "swap_eligible_at": None
    }).eq("id", rdv.lead_id).execute()

    result = sb.table("rdv").insert({
        "lead_id": rdv.lead_id,
        "agent_id": agent_id,
        "rdv_date": rdv.rdv_date.isoformat(),
        "status": "scheduled"
    }).execute()

    # Log history
    sb.table("lead_history").insert({
        "lead_id": rdv.lead_id,
        "agent_id": agent_id,
        "action": "rdv_booked",
        "status_before": None,
        "status_after": "RDV",
        "note": f"RDV scheduled for {rdv.rdv_date}"
    }).execute()

    return result.data[0]


@router.patch("/{rdv_id}")
def update_rdv(rdv_id: str, update: RDVUpdate, user=Depends(get_current_user)):
    sb = get_client()
    agent_id = user["sub"]

    rdv_result = sb.table("rdv").select("*").eq("id", rdv_id).execute()
    if not rdv_result.data:
        raise HTTPException(status_code=404, detail="RDV not found")

    rdv_row = rdv_result.data[0]

    if update.status == "no_show":
        # Lead goes back to pool after 10 days from RDV date
        rdv_date = datetime.fromisoformat(rdv_row["rdv_date"])
        repool_at = (rdv_date + timedelta(days=10)).isoformat()

        sb.table("rdv").update({
            "status": "no_show",
            "no_show_repool_at": repool_at
        }).eq("id", rdv_id).execute()

        sb.table("leads").update({
            "status": "N.R",
            "swap_eligible_at": repool_at
        }).eq("id", rdv_row["lead_id"]).execute()

        sb.table("lead_history").insert({
            "lead_id": rdv_row["lead_id"],
            "agent_id": agent_id,
            "action": "no_show",
            "status_before": "RDV",
            "status_after": "N.R",
            "note": f"No show — repool at {repool_at}"
        }).execute()

    elif update.status == "showed_up":
        sb.table("rdv").update({"status": "showed_up", "confirmed_at": datetime.utcnow().isoformat()}).eq("id", rdv_id).execute()
        sb.table("lead_history").insert({
            "lead_id": rdv_row["lead_id"],
            "agent_id": agent_id,
            "action": "showed_up",
            "status_before": "RDV",
            "status_after": "RDV",
            "note": "Showed up"
        }).execute()

    elif update.status == "registered":
        sb.table("rdv").update({"status": "registered", "confirmed_at": datetime.utcnow().isoformat()}).eq("id", rdv_id).execute()
        sb.table("leads").update({"status": "registered"}).eq("id", rdv_row["lead_id"]).execute()
        sb.table("lead_history").insert({
            "lead_id": rdv_row["lead_id"],
            "agent_id": agent_id,
            "action": "registered",
            "status_before": "RDV",
            "status_after": "registered",
            "note": "تسجل — Registered"
        }).execute()

    elif update.status == "visited_center":
        sb.table("rdv").update({"status": "visited_center", "confirmed_at": datetime.utcnow().isoformat()}).eq("id", rdv_id).execute()
        sb.table("lead_history").insert({
            "lead_id": rdv_row["lead_id"],
            "agent_id": agent_id,
            "action": "visited_center",
            "status_before": rdv_row["status"],
            "status_after": "RDV",
            "note": "زار المركز — Visited center"
        }).execute()

    return {"message": "تم التحديث"}


@router.get("/my")
def get_my_rdvs(user=Depends(get_current_user)):
    sb = get_client()
    agent_id = user["sub"]
    result = sb.table("rdv").select("*, leads(phone, name, city)").eq("agent_id", agent_id).order("rdv_date").execute()
    return result.data


@router.get("/all")
def get_all_rdvs(user=Depends(get_current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    sb = get_client()
    result = sb.table("rdv").select("*, leads(phone, name, city), agents(name)").order("rdv_date").execute()
    return result.data
