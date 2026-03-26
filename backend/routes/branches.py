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


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[ALGORITHM])
        return payload
    except Exception:
        raise HTTPException(status_code=401, detail="Unauthorized")


class BranchCreate(BaseModel):
    name: str


@router.get("/")
def list_branches(user=Depends(get_current_user)):
    sb = get_client()
    result = sb.table("branches").select("*").order("name").execute()
    return result.data


@router.post("/")
def create_branch(branch: BranchCreate, admin=Depends(require_admin)):
    sb = get_client()
    existing = sb.table("branches").select("id").eq("name", branch.name).execute()
    if existing.data:
        raise HTTPException(status_code=400, detail="اسم الفرع موجود مسبقاً")
    result = sb.table("branches").insert({"name": branch.name}).execute()
    return result.data[0]


@router.delete("/{branch_id}")
def delete_branch(branch_id: str, admin=Depends(require_admin)):
    sb = get_client()
    agents = sb.table("agents").select("id").eq("branch_id", branch_id).eq("is_active", True).execute()
    if agents.data:
        raise HTTPException(status_code=400, detail="لا يمكن حذف فرع يحتوي على وكلاء نشطين")
    sb.table("branches").delete().eq("id", branch_id).execute()
    return {"message": "تم حذف الفرع"}
