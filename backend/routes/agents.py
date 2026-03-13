from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from passlib.context import CryptContext
from jose import jwt
from services.supabase_service import get_client
from services.swap_service import redistribute_agent_leads
from dotenv import load_dotenv
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


class AgentCreate(BaseModel):
    name: str
    pin: str


@router.get("/")
def list_agents(admin=Depends(require_admin)):
    sb = get_client()
    result = sb.table("agents").select("id, name, is_active, created_at, fired_at").order("created_at").execute()
    return result.data


@router.post("/")
def create_agent(agent: AgentCreate, admin=Depends(require_admin)):
    sb = get_client()

    # Check name uniqueness
    existing = sb.table("agents").select("id").eq("name", agent.name).execute()
    if existing.data:
        raise HTTPException(status_code=400, detail="اسم المستخدم موجود مسبقاً")

    hashed_pin = pwd_context.hash(agent.pin)
    result = sb.table("agents").insert({
        "name": agent.name,
        "pin": hashed_pin,
        "is_active": True
    }).execute()

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


@router.get("/{agent_id}/stats")
def agent_stats(agent_id: str):
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
    reg_logha = sum(1 for r in reg_result.data if r["status"] == "registered_logha")
    reg_takwin = sum(1 for r in reg_result.data if r["status"] == "registered_takwin")
    stats["registered_logha"] = reg_logha
    stats["registered_takwin"] = reg_takwin
    stats["registered_inscre"] = reg_logha * 1 + reg_takwin * 2
    stats["registered_students"] = reg_logha + reg_takwin

    return stats
