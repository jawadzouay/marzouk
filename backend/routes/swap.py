from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt
from services.supabase_service import get_client
from services.swap_service import get_eligible_leads_for_swap, assign_swap
from dotenv import load_dotenv
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


@router.get("/eligible")
def get_eligible(admin=Depends(require_admin)):
    leads = get_eligible_leads_for_swap()
    return {"count": len(leads), "leads": leads}


@router.post("/run")
def run_swap(admin=Depends(require_admin)):
    leads = get_eligible_leads_for_swap()
    results = []
    for lead in leads:
        new_agent = assign_swap(lead)
        results.append({"lead_id": lead["id"], "new_agent": new_agent})
    return {"swapped": len(results), "results": results}


@router.get("/pool")
def get_swap_pool(admin=Depends(require_admin)):
    sb = get_client()
    result = sb.table("leads").select("*, agents!leads_current_agent_fkey(name)").in_("status", ["B.V", "N.R"]).lt("swap_count", 3).execute()
    return result.data
