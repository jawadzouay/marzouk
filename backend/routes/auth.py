from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from jose import jwt
from passlib.context import CryptContext
from services.supabase_service import get_client
from dotenv import load_dotenv
from typing import Optional
import os
from datetime import datetime, timedelta

load_dotenv()

router = APIRouter()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
JWT_SECRET = os.getenv("JWT_SECRET")
ADMIN_PIN = (os.getenv("ADMIN_PIN") or "").strip()
ALGORITHM = "HS256"


class LoginRequest(BaseModel):
    name: str
    pin: str


class RegisterRequest(BaseModel):
    requested_name: str
    password: str
    city: Optional[str] = None
    branch_id: Optional[str] = None


def create_token(data: dict, expires_hours: int = 24):
    payload = data.copy()
    payload["exp"] = datetime.utcnow() + timedelta(hours=expires_hours)
    return jwt.encode(payload, JWT_SECRET, algorithm=ALGORITHM)


def _check_admin(name: str, pin: str) -> bool:
    """Check admin credentials — DB settings override env var."""
    try:
        sb = get_client()
        urow = sb.table("settings").select("value").eq("key", "admin_username").execute()
        prow = sb.table("settings").select("value").eq("key", "admin_password_hash").execute()
        admin_username = urow.data[0]["value"] if urow.data else "admin"
        if name.lower() != admin_username.lower():
            return False
        if prow.data:
            return pwd_context.verify(pin, prow.data[0]["value"])
        return pin == ADMIN_PIN
    except Exception:
        return name.lower() == "admin" and pin == ADMIN_PIN


@router.post("/login")
def login(req: LoginRequest):
    # Check if admin
    if _check_admin(req.name, req.pin):
        token = create_token({"sub": "admin", "role": "admin"})
        return {"token": token, "role": "admin", "name": req.name}

    # Check agent
    sb = get_client()
    result = sb.table("agents").select("*").eq("name", req.name).eq("is_active", True).execute()

    if not result.data:
        raise HTTPException(status_code=401, detail="اسم المستخدم غير موجود أو تم إيقافه")

    agent = result.data[0]

    if not pwd_context.verify(req.pin, agent["pin"]):
        raise HTTPException(status_code=401, detail="رمز PIN غير صحيح")

    token = create_token({"sub": agent["id"], "role": "agent", "name": agent["name"]})
    return {"token": token, "role": "agent", "name": agent["name"], "agent_id": agent["id"]}


@router.post("/register-request")
def register_request(req: RegisterRequest):
    if not req.requested_name.strip() or not req.password.strip():
        raise HTTPException(status_code=400, detail="الاسم وكلمة المرور مطلوبان")
    sb = get_client()
    # Check if name already taken by active agent
    existing = sb.table("agents").select("id").eq("name", req.requested_name.strip()).eq("is_active", True).execute()
    if existing.data:
        raise HTTPException(status_code=400, detail="هذا الاسم مستخدم مسبقاً")
    # Check if pending request with same name
    pending = sb.table("agent_requests").select("id").eq("requested_name", req.requested_name.strip()).eq("status", "pending").execute()
    if pending.data:
        raise HTTPException(status_code=400, detail="يوجد طلب بهذا الاسم قيد الانتظار")
    payload = {
        "requested_name": req.requested_name.strip(),
        "password_plain": req.password,
    }
    if req.city:
        payload["requested_city"] = req.city.strip()
    if req.branch_id:
        payload["requested_branch_id"] = req.branch_id
    result = sb.table("agent_requests").insert(payload).execute()
    return {"message": "تم إرسال طلبك. انتظر موافقة الإدارة.", "id": result.data[0]["id"]}


@router.post("/verify")
def verify_token(token: str):
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
        return {"valid": True, "payload": payload}
    except Exception:
        raise HTTPException(status_code=401, detail="Token invalid")
