"""
Ad leads API — generic per-branch / per-city Google Sheet feeds.

Admin endpoints manage lead_sheet_configs (scope, sheet, column mapping).
Agent endpoints are scope-agnostic: they operate on whatever leads were
assigned to the caller regardless of which config produced them.
"""
from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from jose import jwt
from services.supabase_service import get_client
from services.ad_leads_sync import (
    sync_all_enabled,
    sync_config,
    fetch_sheet_headers,
    distribute_unassigned_for_scope,
    distribute_all_unassigned,
    normalize_morocco_phone,
)
from dotenv import load_dotenv
from datetime import datetime, date, timedelta, timezone
from typing import Optional, List, Dict, Any
import os
import logging
import re

load_dotenv()

router = APIRouter()
security = HTTPBearer()
JWT_SECRET = os.getenv("JWT_SECRET")
ALGORITHM = "HS256"
SYNC_TOKEN = os.getenv("SYNC_CRON_TOKEN", "")

log = logging.getLogger("ad_leads")


def require_agent(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[ALGORITHM])
        if payload.get("role") != "agent":
            raise HTTPException(status_code=403, detail="Agent only")
        return payload
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Unauthorized")


def require_admin(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[ALGORITHM])
        if payload.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Admin only")
        return payload
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ---------------------------------------------------------------------------
# Date range helper
# ---------------------------------------------------------------------------

def resolve_range(range_key: str, date_from: Optional[str], date_to: Optional[str]):
    today = date.today()
    if range_key == "today":
        return today.isoformat(), today.isoformat()
    if range_key == "yesterday":
        y = today - timedelta(days=1)
        return y.isoformat(), y.isoformat()
    if range_key == "last3":
        return (today - timedelta(days=2)).isoformat(), today.isoformat()
    if range_key == "week":
        start = today - timedelta(days=today.weekday())
        return start.isoformat(), today.isoformat()
    if range_key == "month":
        return today.replace(day=1).isoformat(), today.isoformat()
    if range_key == "custom" and date_from and date_to:
        return date_from, date_to
    return today.isoformat(), today.isoformat()


def apply_date_filter(q, df: str, dt: str, field: str = "created_time"):
    return q.gte(field, f"{df}T00:00:00+00:00").lte(field, f"{dt}T23:59:59+00:00")


def _agent_scopes(sb, agent_id: str):
    """Returns list of (scope_type, scope_id) the agent belongs to, based on
    their branch and that branch's city. Used to resolve which configs/columns
    apply to them."""
    me = sb.table("agents").select("branch_id, branches(id, city)").eq("id", agent_id).execute()
    if not me.data:
        return []
    row = me.data[0]
    branch_id = row.get("branch_id")
    city_name = (row.get("branches") or {}).get("city") if row.get("branches") else None
    scopes = []
    if branch_id:
        scopes.append(("branch", branch_id))
    if city_name:
        city = sb.table("cities").select("id").eq("name", city_name).execute()
        if city.data:
            scopes.append(("city", city.data[0]["id"]))
    return scopes


def _configs_for_agent(sb, agent_id: str) -> List[dict]:
    scopes = _agent_scopes(sb, agent_id)
    if not scopes:
        return []
    # Fetch all enabled configs, then filter client-side (tiny list)
    res = sb.table("lead_sheet_configs").select("*").eq("enabled", True).execute()
    configs = res.data or []
    out = []
    for c in configs:
        for st, sid in scopes:
            if c["scope_type"] == st and c["scope_id"] == sid:
                out.append(c)
                break
    return out


def _columns_for_configs(sb, config_ids: List[str]) -> List[dict]:
    if not config_ids:
        return []
    res = sb.table("lead_sheet_columns").select("*") \
        .in_("config_id", config_ids).order("display_order").execute()
    return res.data or []


def _merge_visible_columns(columns: List[dict]) -> List[dict]:
    """Merge visible columns across configs. Same display_name = one column."""
    seen: Dict[str, dict] = {}
    for c in columns:
        if not c.get("visible"):
            continue
        key = c.get("display_name") or c.get("source_header")
        if key and key not in seen:
            seen[key] = {
                "display_name": c["display_name"],
                "column_type": c["column_type"],
                "display_order": c.get("display_order", 0),
            }
    return sorted(seen.values(), key=lambda x: x["display_order"])


# ===========================================================================
# AGENT ENDPOINTS
# ===========================================================================

@router.get("/my/columns")
def my_columns(agent=Depends(require_agent)):
    """Visible column definitions for the agent's scope(s)."""
    sb = get_client()
    configs = _configs_for_agent(sb, agent["sub"])
    columns = _columns_for_configs(sb, [c["id"] for c in configs])
    return {"columns": _merge_visible_columns(columns)}


@router.get("/my")
def my_leads(
    range: str = Query("today"),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    agent=Depends(require_agent),
):
    """Leads assigned to me in a date range."""
    sb = get_client()
    df, dt = resolve_range(range, date_from, date_to)

    q = sb.table("ad_leads").select(
        "id, created_time, full_name, phone_primary, phones, ad_name, "
        "platform, status, assigned_at, contacted_at, last_note, data"
    ).eq("assigned_agent_id", agent["sub"])
    # Filter on assigned_at so leads with no source-side date still show up;
    # this also matches the agent's mental model of "leads I got today".
    q = apply_date_filter(q, df, dt, field="assigned_at")
    if status:
        q = q.eq("status", status)
    res = q.order("assigned_at", desc=True).execute()

    # Include visible column defs so the UI can render the dynamic table
    configs = _configs_for_agent(sb, agent["sub"])
    columns = _columns_for_configs(sb, [c["id"] for c in configs])

    return {
        "leads": res.data or [],
        "columns": _merge_visible_columns(columns),
        "date_from": df,
        "date_to": dt,
    }


@router.get("/my/count")
def my_counts(agent=Depends(require_agent)):
    sb = get_client()
    today_start = f"{date.today().isoformat()}T00:00:00+00:00"
    r = sb.table("ad_leads").select("status") \
        .eq("assigned_agent_id", agent["sub"]) \
        .gte("assigned_at", today_start).execute()
    rows = r.data or []
    by_status: Dict[str, int] = {}
    for row in rows:
        s = row.get("status") or "new"
        by_status[s] = by_status.get(s, 0) + 1
    return {
        "today_total": len(rows),
        "today_new": by_status.get("new", 0),
        "today_contacted": sum(v for k, v in by_status.items() if k != "new"),
    }


@router.get("/my/status")
def my_inbox_status(agent=Depends(require_agent)):
    """Used by the agent dashboard to decide whether to show the inbox button."""
    sb = get_client()
    configs = _configs_for_agent(sb, agent["sub"])
    r = sb.table("ad_leads").select("id", count="exact") \
        .eq("assigned_agent_id", agent["sub"]).eq("status", "new").execute()
    return {"enabled": bool(configs), "new_count": r.count or 0}


class StatusUpdate(BaseModel):
    status: str
    note: Optional[str] = None


VALID_STATUSES = {
    "new", "contacted", "rdv", "bv", "pi", "pe",
    "autre_ville", "over_40", "visits", "registered",
}


@router.patch("/{lead_id}/status")
def update_lead_status(lead_id: str, body: StatusUpdate, agent=Depends(require_agent)):
    sb = get_client()
    if body.status not in VALID_STATUSES:
        raise HTTPException(400, f"invalid status: {body.status}")

    lead = sb.table("ad_leads").select("id, assigned_agent_id").eq("id", lead_id).execute()
    if not lead.data:
        raise HTTPException(404, "Lead not found")
    if lead.data[0]["assigned_agent_id"] != agent["sub"]:
        raise HTTPException(403, "This lead is not assigned to you")

    now_iso = datetime.now(timezone.utc).isoformat()
    updates: Dict[str, Any] = {"status": body.status, "updated_at": now_iso}
    if body.note is not None:
        updates["last_note"] = body.note
    if body.status != "new":
        updates["contacted_at"] = now_iso

    sb.table("ad_leads").update(updates).eq("id", lead_id).execute()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Availability (off-dates) — per-agent, unrelated to branch
# ---------------------------------------------------------------------------

@router.get("/availability")
def get_my_off_dates(agent=Depends(require_agent)):
    sb = get_client()
    today_iso = date.today().isoformat()
    res = sb.table("agent_off_dates").select("off_date") \
        .eq("agent_id", agent["sub"]).gte("off_date", today_iso) \
        .order("off_date").execute()
    return {"off_dates": [r["off_date"] for r in (res.data or [])]}


class OffDatesUpdate(BaseModel):
    off_dates: List[str]


@router.put("/availability")
def set_my_off_dates(body: OffDatesUpdate, agent=Depends(require_agent)):
    sb = get_client()
    today_iso = date.today().isoformat()
    clean = sorted({d for d in body.off_dates if d >= today_iso})

    sb.table("agent_off_dates").delete() \
        .eq("agent_id", agent["sub"]).gte("off_date", today_iso).execute()
    if clean:
        sb.table("agent_off_dates").insert(
            [{"agent_id": agent["sub"], "off_date": d} for d in clean]
        ).execute()
    return {"off_dates": clean}


# ---------------------------------------------------------------------------
# Transfer (agent -> agent, same branch)
# ---------------------------------------------------------------------------

class TransferByCount(BaseModel):
    to_agent_id: str
    count: int


class TransferBySelection(BaseModel):
    to_agent_id: str
    lead_ids: List[str]


def _branch_peers(sb, agent_id: str) -> List[str]:
    me = sb.table("agents").select("branch_id").eq("id", agent_id).execute()
    if not me.data or not me.data[0].get("branch_id"):
        return []
    branch_id = me.data[0]["branch_id"]
    peers = sb.table("agents").select("id").eq("branch_id", branch_id).eq("is_active", True).execute()
    return [p["id"] for p in (peers.data or []) if p["id"] != agent_id]


@router.get("/transfer/peers")
def list_transfer_peers(agent=Depends(require_agent)):
    sb = get_client()
    me = sb.table("agents").select("branch_id").eq("id", agent["sub"]).execute()
    if not me.data or not me.data[0].get("branch_id"):
        return []
    branch_id = me.data[0]["branch_id"]
    peers = sb.table("agents").select("id, name") \
        .eq("branch_id", branch_id).eq("is_active", True).execute()
    return [p for p in (peers.data or []) if p["id"] != agent["sub"]]


def _do_transfer(sb, lead_ids: List[str], from_agent: str, to_agent: str) -> int:
    if not lead_ids:
        return 0
    now_iso = datetime.now(timezone.utc).isoformat()

    eligible = sb.table("ad_leads").select("id") \
        .in_("id", lead_ids) \
        .eq("assigned_agent_id", from_agent) \
        .eq("status", "new").execute()
    eligible_ids = [r["id"] for r in (eligible.data or [])]
    if not eligible_ids:
        return 0

    sb.table("ad_leads").update({
        "assigned_agent_id": to_agent,
        "assigned_at": now_iso,
        "updated_at": now_iso,
    }).in_("id", eligible_ids).execute()

    sb.table("lead_transfers").insert([
        {"lead_id": lid, "from_agent_id": from_agent, "to_agent_id": to_agent}
        for lid in eligible_ids
    ]).execute()
    return len(eligible_ids)


@router.post("/transfer/by-count")
def transfer_by_count(body: TransferByCount, agent=Depends(require_agent)):
    sb = get_client()
    if body.count <= 0:
        raise HTTPException(400, "count must be > 0")
    peers = _branch_peers(sb, agent["sub"])
    if body.to_agent_id not in peers:
        raise HTTPException(400, "Target agent is not in your branch")

    picks = sb.table("ad_leads").select("id") \
        .eq("assigned_agent_id", agent["sub"]).eq("status", "new") \
        .order("created_time").limit(body.count).execute()
    ids = [r["id"] for r in (picks.data or [])]

    n = _do_transfer(sb, ids, agent["sub"], body.to_agent_id)
    return {"transferred": n, "requested": body.count}


@router.post("/transfer/by-selection")
def transfer_by_selection(body: TransferBySelection, agent=Depends(require_agent)):
    sb = get_client()
    peers = _branch_peers(sb, agent["sub"])
    if body.to_agent_id not in peers:
        raise HTTPException(400, "Target agent is not in your branch")
    n = _do_transfer(sb, body.lead_ids, agent["sub"], body.to_agent_id)
    return {"transferred": n, "requested": len(body.lead_ids)}


@router.get("/new-count")
def my_new_count(agent=Depends(require_agent)):
    sb = get_client()
    r = sb.table("ad_leads").select("id", count="exact") \
        .eq("assigned_agent_id", agent["sub"]).eq("status", "new").execute()
    return {"count": r.count or 0}


# ===========================================================================
# ADMIN — config CRUD + sync + analytics
# ===========================================================================

class ConfigCreate(BaseModel):
    name: str
    scope_type: str   # 'city' | 'branch'
    scope_id: str
    enabled: Optional[bool] = False
    sheet_id: Optional[str] = None
    sheet_tab: Optional[str] = None


class ConfigUpdate(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    sheet_id: Optional[str] = None
    sheet_tab: Optional[str] = None


class ColumnSpec(BaseModel):
    source_header: str
    display_name: str
    column_type: str
    display_order: int
    visible: bool


class ColumnsUpdate(BaseModel):
    columns: List[ColumnSpec]


_SHEET_ID_RE = re.compile(r"/d/([a-zA-Z0-9-_]+)")


def _parse_sheet_id(raw: str) -> str:
    raw = (raw or "").strip()
    m = _SHEET_ID_RE.search(raw)
    return m.group(1) if m else raw


@router.get("/admin/configs")
def admin_list_configs(admin=Depends(require_admin)):
    sb = get_client()
    configs = sb.table("lead_sheet_configs").select("*").order("created_at").execute()
    cities = {c["id"]: c["name"] for c in (sb.table("cities").select("id,name").execute().data or [])}
    branches = {b["id"]: b for b in (sb.table("branches").select("id,name,city").execute().data or [])}
    out = []
    for c in (configs.data or []):
        if c["scope_type"] == "city":
            c["scope_label"] = cities.get(c["scope_id"], "—")
        else:
            b = branches.get(c["scope_id"]) or {}
            c["scope_label"] = b.get("name") or "—"
            c["scope_city"] = b.get("city")
        out.append(c)
    return {"configs": out}


@router.post("/admin/configs")
def admin_create_config(body: ConfigCreate, admin=Depends(require_admin)):
    if body.scope_type not in ("city", "branch"):
        raise HTTPException(400, "scope_type must be 'city' or 'branch'")
    sb = get_client()
    # Make sure scope exists
    table = "cities" if body.scope_type == "city" else "branches"
    ref = sb.table(table).select("id").eq("id", body.scope_id).execute()
    if not ref.data:
        raise HTTPException(400, f"{body.scope_type} not found")

    sheet_id = _parse_sheet_id(body.sheet_id or "")
    payload = {
        "name": body.name,
        "scope_type": body.scope_type,
        "scope_id": body.scope_id,
        "enabled": bool(body.enabled),
        "sheet_id": sheet_id or None,
        "sheet_tab": body.sheet_tab or None,
    }
    try:
        res = sb.table("lead_sheet_configs").insert(payload).execute()
    except Exception as e:
        raise HTTPException(400, f"config exists for this {body.scope_type}: {e}")
    return res.data[0] if res.data else {}


@router.put("/admin/configs/{config_id}")
def admin_update_config(config_id: str, body: ConfigUpdate, admin=Depends(require_admin)):
    sb = get_client()
    updates: Dict[str, Any] = {}
    if body.name is not None:
        updates["name"] = body.name
    if body.enabled is not None:
        updates["enabled"] = body.enabled
    if body.sheet_id is not None:
        updates["sheet_id"] = _parse_sheet_id(body.sheet_id) or None
    if body.sheet_tab is not None:
        updates["sheet_tab"] = body.sheet_tab or None
    if not updates:
        raise HTTPException(400, "nothing to update")
    res = sb.table("lead_sheet_configs").update(updates).eq("id", config_id).execute()
    if not res.data:
        raise HTTPException(404, "config not found")
    return res.data[0]


@router.delete("/admin/configs/{config_id}")
def admin_delete_config(config_id: str, admin=Depends(require_admin)):
    sb = get_client()
    sb.table("lead_sheet_configs").delete().eq("id", config_id).execute()
    return {"ok": True}


@router.get("/admin/configs/{config_id}/columns")
def admin_get_columns(config_id: str, admin=Depends(require_admin)):
    sb = get_client()
    res = sb.table("lead_sheet_columns").select("*") \
        .eq("config_id", config_id).order("display_order").execute()
    return {"columns": res.data or []}


@router.put("/admin/configs/{config_id}/columns")
def admin_save_columns(config_id: str, body: ColumnsUpdate, admin=Depends(require_admin)):
    sb = get_client()
    valid_types = {"key", "name", "date", "phone", "ad_name", "platform", "number", "text"}
    for c in body.columns:
        if c.column_type not in valid_types:
            raise HTTPException(400, f"invalid column_type: {c.column_type}")
    # Replace the whole mapping atomically
    sb.table("lead_sheet_columns").delete().eq("config_id", config_id).execute()
    if body.columns:
        sb.table("lead_sheet_columns").insert([{
            "config_id": config_id,
            "source_header": c.source_header,
            "display_name": c.display_name,
            "column_type": c.column_type,
            "display_order": c.display_order,
            "visible": c.visible,
        } for c in body.columns]).execute()
    return {"ok": True, "count": len(body.columns)}


@router.post("/admin/configs/{config_id}/fetch-headers")
def admin_fetch_headers(config_id: str, admin=Depends(require_admin)):
    sb = get_client()
    cfg = sb.table("lead_sheet_configs").select("sheet_id, sheet_tab") \
        .eq("id", config_id).execute()
    if not cfg.data:
        raise HTTPException(404, "config not found")
    row = cfg.data[0]
    if not row.get("sheet_id"):
        raise HTTPException(400, "sheet_id not set — save it first")
    try:
        headers = fetch_sheet_headers(row["sheet_id"], row.get("sheet_tab"))
    except Exception as e:
        raise HTTPException(400, f"could not read sheet: {e}")
    return {"headers": headers}


@router.post("/admin/configs/{config_id}/sync")
def admin_sync_config(config_id: str, admin=Depends(require_admin)):
    return sync_config(config_id)


@router.post("/admin/sync-all")
def admin_sync_all(admin=Depends(require_admin)):
    return sync_all_enabled()


@router.post("/redistribute")
def admin_redistribute(admin=Depends(require_admin)):
    return {"assigned": distribute_all_unassigned()}


@router.post("/sync/cron")
def cron_sync(token: str = Query(...)):
    if not SYNC_TOKEN or token != SYNC_TOKEN:
        raise HTTPException(403, "Invalid cron token")
    return sync_all_enabled()


# ---------------------------------------------------------------------------
# Admin analytics
# ---------------------------------------------------------------------------

@router.get("/admin/pool")
def admin_pool(
    scope_type: str = Query(...),
    scope_id: str = Query(...),
    admin=Depends(require_admin),
):
    """Agents in the requested scope with today's lead count and off-dates."""
    sb = get_client()
    today = date.today().isoformat()

    if scope_type == "branch":
        agents = sb.table("agents").select(
            "id, name, is_active, last_distributed_at, branch_id"
        ).eq("is_active", True).eq("branch_id", scope_id).execute()
        matched = agents.data or []
    elif scope_type == "city":
        city = sb.table("cities").select("name").eq("id", scope_id).execute()
        if not city.data:
            return {"agents": [], "today": today}
        city_name = city.data[0]["name"]
        brs = sb.table("branches").select("id").eq("city", city_name).execute()
        branch_ids = [b["id"] for b in (brs.data or [])]
        if not branch_ids:
            return {"agents": [], "today": today}
        agents = sb.table("agents").select(
            "id, name, is_active, last_distributed_at, branch_id"
        ).eq("is_active", True).in_("branch_id", branch_ids).execute()
        matched = agents.data or []
    else:
        raise HTTPException(400, "scope_type must be 'city' or 'branch'")

    if not matched:
        return {"agents": [], "today": today}

    ids = [a["id"] for a in matched]
    off = sb.table("agent_off_dates").select("agent_id, off_date") \
        .in_("agent_id", ids).gte("off_date", today).execute()
    off_map: Dict[str, List[str]] = {}
    for r in (off.data or []):
        off_map.setdefault(r["agent_id"], []).append(r["off_date"])

    today_start = f"{today}T00:00:00+00:00"
    leads = sb.table("ad_leads").select("assigned_agent_id") \
        .in_("assigned_agent_id", ids).gte("assigned_at", today_start).execute()
    count_map: Dict[str, int] = {}
    for r in (leads.data or []):
        aid = r["assigned_agent_id"]
        count_map[aid] = count_map.get(aid, 0) + 1

    return {
        "agents": [{
            "id": a["id"],
            "name": a["name"],
            "last_distributed_at": a.get("last_distributed_at"),
            "off_dates": sorted(off_map.get(a["id"], [])),
            "is_off_today": today in off_map.get(a["id"], []),
            "leads_today": count_map.get(a["id"], 0),
        } for a in matched],
        "today": today,
    }


@router.get("/admin/ad-quality")
def admin_ad_quality(
    range: str = Query("month"),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    scope_type: Optional[str] = Query(None),
    scope_id: Optional[str] = Query(None),
    city_id: Optional[str] = Query(None),
    branch_id: Optional[str] = Query(None),
    admin=Depends(require_admin),
):
    """Aggregate leads by ad_name with RDV-first quality scoring."""
    sb = get_client()
    df, dt = resolve_range(range, date_from, date_to)

    q = sb.table("ad_leads").select(
        "ad_name, platform, status, created_time, scope_type, scope_id"
    )
    q = apply_date_filter(q, df, dt)
    if scope_type and scope_id:
        q = q.eq("scope_type", scope_type).eq("scope_id", scope_id)
    elif city_id:
        q = q.eq("scope_type", "city").eq("scope_id", city_id)
    elif branch_id:
        q = q.eq("scope_type", "branch").eq("scope_id", branch_id)
    res = q.execute()

    buckets: Dict[str, dict] = {}
    for r in (res.data or []):
        key = r.get("ad_name") or "(بدون اسم)"
        b = buckets.setdefault(key, {
            "ad_name": key,
            "platform": r.get("platform"),
            "leads": 0, "new": 0, "contacted": 0, "rdv": 0,
            "visits": 0, "registered": 0, "bv": 0, "pi": 0, "pe": 0,
            "autre_ville": 0, "over_40": 0,
            "first_lead_at": None, "last_lead_at": None,
        })
        b["leads"] += 1
        s = r.get("status") or "new"
        if s in b:
            b[s] += 1
        ct = r.get("created_time")
        if ct:
            if not b["first_lead_at"] or ct < b["first_lead_at"]:
                b["first_lead_at"] = ct
            if not b["last_lead_at"] or ct > b["last_lead_at"]:
                b["last_lead_at"] = ct

    now = datetime.now(timezone.utc)
    ads = []
    for b in buckets.values():
        leads = b["leads"]
        rdv = b["rdv"]
        visits = b["visits"]
        registered = b["registered"]
        contacted_total = leads - b["new"]

        b["rdv_rate"] = round(rdv / leads, 3) if leads else 0
        b["show_rate"] = round(visits / rdv, 3) if rdv else 0
        b["close_rate"] = round(registered / visits, 3) if visits else 0
        b["registered_rate"] = round(registered / leads, 3) if leads else 0
        b["contact_rate"] = round(contacted_total / leads, 3) if leads else 0

        age_days = 0
        if b["first_lead_at"]:
            try:
                first = datetime.fromisoformat(b["first_lead_at"].replace("Z", "+00:00"))
                age_days = (now - first).days
            except Exception:
                age_days = 0
        b["age_days"] = age_days
        if age_days < 3:
            b["maturity"] = "fresh"
        elif age_days < 7:
            b["maturity"] = "evaluating"
        else:
            b["maturity"] = "mature"

        if age_days >= 7:
            if b["registered_rate"] >= 0.10:
                b["color"] = "green"
            elif b["registered_rate"] >= 0.04:
                b["color"] = "orange"
            else:
                b["color"] = "red"
        else:
            if b["rdv_rate"] >= 0.20:
                b["color"] = "green"
            elif b["rdv_rate"] >= 0.08:
                b["color"] = "orange"
            else:
                b["color"] = "red"

        ads.append(b)

    color_order = {"red": 0, "orange": 1, "green": 2}
    ads.sort(key=lambda x: (color_order.get(x["color"], 3), -x["leads"]))
    return {"ads": ads, "date_from": df, "date_to": dt}
