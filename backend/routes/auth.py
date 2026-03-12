from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from jose import jwt
from passlib.context import CryptContext
from services.supabase_service import get_client
from dotenv import load_dotenv
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


def create_token(data: dict, expires_hours: int = 24):
    payload = data.copy()
    payload["exp"] = datetime.utcnow() + timedelta(hours=expires_hours)
    return jwt.encode(payload, JWT_SECRET, algorithm=ALGORITHM)


@router.post("/login")
def login(req: LoginRequest):
    # Check if admin
    if req.name.lower() == "admin" and req.pin == ADMIN_PIN:
        token = create_token({"sub": "admin", "role": "admin"})
        return {"token": token, "role": "admin", "name": "admin"}

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


@router.post("/verify")
def verify_token(token: str):
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
        return {"valid": True, "payload": payload}
    except Exception:
        raise HTTPException(status_code=401, detail="Token invalid")
