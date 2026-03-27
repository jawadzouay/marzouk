from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt
from services.supabase_service import get_client
from dotenv import load_dotenv
import os

load_dotenv()
router = APIRouter()
security = HTTPBearer()
JWT_SECRET = os.getenv("JWT_SECRET")
ALGORITHM = "HS256"


def require_admin(creds: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(creds.credentials, JWT_SECRET, algorithms=[ALGORITHM])
        if payload.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Admin only")
        return payload
    except Exception:
        raise HTTPException(status_code=401, detail="Unauthorized")


def get_current_user(creds: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(creds.credentials, JWT_SECRET, algorithms=[ALGORITHM])
        return payload
    except Exception:
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.get("/")
def list_cities(user=Depends(get_current_user)):
    sb = get_client()
    return sb.table("cities").select("*").order("name").execute().data


@router.post("/")
def add_city(body: dict, admin=Depends(require_admin)):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "اسم المدينة فارغ")
    sb = get_client()
    try:
        result = sb.table("cities").insert({"name": name}).execute()
        return result.data[0]
    except Exception:
        raise HTTPException(400, "المدينة موجودة مسبقاً")


@router.put("/{city_id}")
def rename_city(city_id: str, body: dict, admin=Depends(require_admin)):
    new_name = (body.get("name") or "").strip()
    if not new_name:
        raise HTTPException(400, "اسم المدينة فارغ")
    sb = get_client()
    old = sb.table("cities").select("name").eq("id", city_id).execute()
    if not old.data:
        raise HTTPException(404, "المدينة غير موجودة")
    old_name = old.data[0]["name"]
    result = sb.table("cities").update({"name": new_name}).eq("id", city_id).execute()
    # Cascade rename in branches
    sb.table("branches").update({"city": new_name}).eq("city", old_name).execute()
    return result.data[0]


@router.delete("/{city_id}")
def delete_city(city_id: str, admin=Depends(require_admin)):
    sb = get_client()
    city = sb.table("cities").select("name").eq("id", city_id).execute()
    if city.data:
        city_name = city.data[0]["name"]
        # Detach branches from this city — keep branches and all their data, just remove the city label
        sb.table("branches").update({"city": None}).eq("city", city_name).execute()
    sb.table("cities").delete().eq("id", city_id).execute()
    return {"message": "تم حذف المدينة"}
