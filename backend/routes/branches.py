from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from jose import jwt
from services.supabase_service import get_client
from dotenv import load_dotenv
from typing import Optional
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


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[ALGORITHM])
        return payload
    except Exception:
        raise HTTPException(status_code=401, detail="Unauthorized")


class BranchCreate(BaseModel):
    name: str
    city: Optional[str] = None


@router.get("/")
def list_branches(user=Depends(get_current_user)):
    sb = get_client()
    result = sb.table("branches").select("*").order("city").order("name").execute()
    return result.data


@router.get("/public")
def list_branches_public():
    """Unauthenticated list used by the agent signup page (city + branch dropdowns)."""
    sb = get_client()
    result = sb.table("branches").select("id, name, city").order("city").order("name").execute()
    return result.data


@router.get("/cities")
def list_cities(user=Depends(get_current_user)):
    sb = get_client()
    result = sb.table("branches").select("city").execute()
    cities = sorted({r["city"] for r in result.data if r.get("city")})
    return cities


@router.post("/")
def create_branch(branch: BranchCreate, admin=Depends(require_admin)):
    sb = get_client()
    existing = sb.table("branches").select("id").eq("name", branch.name).execute()
    if existing.data:
        raise HTTPException(status_code=400, detail="اسم الفرع موجود مسبقاً")
    result = sb.table("branches").insert({"name": branch.name, "city": branch.city}).execute()
    return result.data[0]


@router.put("/{branch_id}")
def rename_branch(branch_id: str, body: dict, admin=Depends(require_admin)):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "اسم الفرع فارغ")
    sb = get_client()
    result = sb.table("branches").update({"name": name}).eq("id", branch_id).execute()
    if not result.data:
        raise HTTPException(404, "الفرع غير موجود")
    return result.data[0]


@router.delete("/{branch_id}")
def delete_branch(branch_id: str, admin=Depends(require_admin)):
    sb = get_client()
    agents = sb.table("agents").select("id").eq("branch_id", branch_id).eq("is_active", True).execute()
    if agents.data:
        raise HTTPException(status_code=400, detail="لا يمكن حذف فرع يحتوي على وكلاء نشطين")
    sb.table("branches").delete().eq("id", branch_id).execute()
    return {"message": "تم حذف الفرع"}
