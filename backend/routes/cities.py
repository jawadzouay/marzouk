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
    try:
        return sb.table("cities").select("*").order("name").execute().data or []
    except Exception:
        return []


@router.post("/")
def add_city(body: dict, admin=Depends(require_admin)):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "اسم المدينة فارغ")
    sb = get_client()
    try:
        existing = sb.table("cities").select("id").eq("name", name).execute()
        if existing.data:
            raise HTTPException(400, "المدينة موجودة مسبقاً — احذفها أولاً ثم أعد الإضافة")
        result = sb.table("cities").insert({"name": name}).execute()
        if not result.data:
            raise HTTPException(500, "فشل الحفظ — تأكد من تنفيذ SQL لإنشاء جدول cities في Supabase")
        return result.data[0]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, "خطأ في قاعدة البيانات — يجب تنفيذ SQL: CREATE TABLE cities في Supabase")


@router.put("/{city_id}")
def rename_city(city_id: str, body: dict, admin=Depends(require_admin)):
    new_name = (body.get("name") or "").strip()
    if not new_name:
        raise HTTPException(400, "اسم المدينة فارغ")
    sb = get_client()
    try:
        old = sb.table("cities").select("name").eq("id", city_id).execute()
        if not old.data:
            raise HTTPException(404, "المدينة غير موجودة")
        old_name = old.data[0]["name"]
        result = sb.table("cities").update({"name": new_name}).eq("id", city_id).execute()
        sb.table("branches").update({"city": new_name}).eq("city", old_name).execute()
        return result.data[0]
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(500, "خطأ في قاعدة البيانات")


@router.delete("/{city_id}")
def delete_city(city_id: str, admin=Depends(require_admin)):
    sb = get_client()
    try:
        city = sb.table("cities").select("id,name").eq("id", city_id).execute()
        if not city.data:
            return {"message": "تم الحذف"}
        city_name = city.data[0]["name"]
        sb.table("branches").update({"city": None}).eq("city", city_name).execute()
        sb.table("cities").delete().eq("id", city_id).execute()
        sb.table("cities").delete().eq("name", city_name).execute()
        return {"message": "تم حذف المدينة"}
    except Exception:
        raise HTTPException(500, "خطأ في الحذف")
