from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from jose import jwt
from services.supabase_service import get_client
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


class BlacklistAdd(BaseModel):
    phone: str
    reason: str = ""


@router.get("/")
def get_blacklist(admin=Depends(require_admin)):
    sb = get_client()
    result = sb.table("blacklist").select("*").order("created_at", desc=True).execute()
    return result.data


@router.post("/")
def add_to_blacklist(item: BlacklistAdd, admin=Depends(require_admin)):
    sb = get_client()

    existing = sb.table("blacklist").select("id").eq("phone", item.phone).execute()
    if existing.data:
        raise HTTPException(status_code=400, detail="الرقم موجود مسبقاً في القائمة السوداء")

    result = sb.table("blacklist").insert({
        "phone": item.phone,
        "reason": item.reason,
        "added_by": "admin"
    }).execute()

    # Also mark any existing leads with this phone
    sb.table("leads").update({"is_blacklisted": True}).eq("phone", item.phone).execute()

    return result.data[0]


@router.delete("/{blacklist_id}")
def remove_from_blacklist(blacklist_id: str, admin=Depends(require_admin)):
    sb = get_client()

    item = sb.table("blacklist").select("phone").eq("id", blacklist_id).execute()
    if not item.data:
        raise HTTPException(status_code=404, detail="Not found")

    phone = item.data[0]["phone"]
    sb.table("blacklist").delete().eq("id", blacklist_id).execute()
    sb.table("leads").update({"is_blacklisted": False}).eq("phone", phone).execute()

    return {"message": "تم الحذف من القائمة السوداء"}
