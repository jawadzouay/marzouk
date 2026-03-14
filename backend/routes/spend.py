from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt
from services.supabase_service import get_client
from services.analytics_service import extract_ad_spend_from_image
from dotenv import load_dotenv
from datetime import datetime
import os

load_dotenv()
router   = APIRouter()
security = HTTPBearer()
JWT_SECRET = os.getenv("JWT_SECRET")
ALGORITHM  = "HS256"


def require_admin(creds: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(creds.credentials, JWT_SECRET, algorithms=[ALGORITHM])
        if payload.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Admin only")
        return payload
    except Exception:
        raise HTTPException(status_code=401, detail="Unauthorized")


def match_agent(name: str, agents, aliases):
    """Returns (agent_id, agent_name) or (None, None)."""
    name_l = name.lower().strip()
    # alias map: alias_text → agent_id
    alias_map = {r["ad_name"].lower(): r["agent_id"] for r in aliases}
    # exact alias
    if name_l in alias_map:
        aid = alias_map[name_l]
        a = next((x for x in agents if x["id"] == aid), None)
        return (aid, a["name"]) if a else (None, None)
    # partial alias
    for alias, aid in alias_map.items():
        if alias in name_l or name_l in alias:
            a = next((x for x in agents if x["id"] == aid), None)
            return (aid, a["name"]) if a else (None, None)
    # agent name fallback
    for a in agents:
        an = a["name"].lower()
        if an in name_l or name_l in an:
            return (a["id"], a["name"])
    return (None, None)


@router.post("/extract")
async def extract_spend(file: UploadFile = File(...), admin=Depends(require_admin)):
    if file.content_type not in ["image/jpeg","image/png","image/webp"]:
        raise HTTPException(400, "نوع الملف غير مدعوم")
    image_bytes = await file.read()
    rows = await extract_ad_spend_from_image(image_bytes, file.content_type)

    sb = get_client()
    agents  = sb.table("agents").select("id,name").eq("is_active", True).execute().data
    aliases = sb.table("agent_ad_names").select("agent_id,ad_name").execute().data

    for row in rows:
        aid, aname = match_agent(row["name"], agents, aliases)
        row["matched_agent_id"]   = aid
        row["matched_agent_name"] = aname

    return {"rows": rows, "agents": agents}


@router.post("/confirm")
async def confirm_spend(body: dict, admin=Depends(require_admin)):
    """body: { period_start, period_end, rows: [{agent_id, spend, ad_results, cost_per_result, raw_name}] }"""
    sb = get_client()
    rows         = body.get("rows", [])
    period_start = body.get("period_start")
    period_end   = body.get("period_end")
    if not period_start or not period_end:
        raise HTTPException(400, "حدد الفترة الزمنية")

    saved = 0
    for row in rows:
        if not row.get("agent_id"):
            continue
        sb.table("ad_spend").insert({
            "agent_id":        row["agent_id"],
            "spend":           float(row.get("spend", 0)),
            "ad_results":      int(row.get("ad_results", 0)),
            "cost_per_result": float(row.get("cost_per_result", 0)),
            "period_start":    period_start,
            "period_end":      period_end,
            "raw_name":        row.get("raw_name", ""),
        }).execute()
        saved += 1

    return {"saved": saved}


@router.get("/history")
def spend_history(admin=Depends(require_admin)):
    sb = get_client()
    res = sb.table("ad_spend").select("*, agents(name)").order("created_at", desc=True).limit(200).execute()
    return res.data


@router.delete("/{spend_id}")
def delete_spend(spend_id: str, admin=Depends(require_admin)):
    sb = get_client()
    sb.table("ad_spend").delete().eq("id", spend_id).execute()
    return {"message": "تم الحذف"}


@router.get("/agent-names")
def get_agent_names(admin=Depends(require_admin)):
    sb = get_client()
    agents  = sb.table("agents").select("id,name").eq("is_active", True).execute().data
    aliases = sb.table("agent_ad_names").select("*").execute().data
    # Group aliases by agent_id
    alias_map = {}
    for a in aliases:
        alias_map.setdefault(a["agent_id"], []).append({"id": a["id"], "name": a["ad_name"]})
    for agent in agents:
        agent["aliases"] = alias_map.get(agent["id"], [])
    return agents


@router.post("/agent-names/{agent_id}")
def add_agent_alias(agent_id: str, body: dict, admin=Depends(require_admin)):
    name = (body.get("ad_name") or "").strip()
    if not name:
        raise HTTPException(400, "اسم الإعلان فارغ")
    sb = get_client()
    try:
        sb.table("agent_ad_names").insert({"agent_id": agent_id, "ad_name": name}).execute()
    except Exception:
        raise HTTPException(400, "الاسم موجود مسبقاً")
    return {"message": "تم الإضافة"}


@router.delete("/agent-names/alias/{alias_id}")
def delete_alias(alias_id: str, admin=Depends(require_admin)):
    sb = get_client()
    sb.table("agent_ad_names").delete().eq("id", alias_id).execute()
    return {"message": "تم الحذف"}
