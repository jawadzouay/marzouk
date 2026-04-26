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

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

load_dotenv()

router = APIRouter()
security = HTTPBearer()
JWT_SECRET = os.getenv("JWT_SECRET")
ALGORITHM = "HS256"
SYNC_TOKEN = os.getenv("SYNC_CRON_TOKEN", "")

log = logging.getLogger("ad_leads")

# ---------------------------------------------------------------------------
# Moroccan time — the whole app lives in Africa/Casablanca (UTC+1, no DST).
# Timestamps are still stored in UTC, but "today" / "yesterday" / the
# boundaries of a calendar day are computed in Morocco local so the inbox
# doesn't flip at UTC midnight (01:00 Morocco).
# ---------------------------------------------------------------------------

MOROCCO_TZ = ZoneInfo("Africa/Casablanca")


def today_morocco() -> date:
    return datetime.now(MOROCCO_TZ).date()


def morocco_day_bounds_utc(df: str, dt: str) -> tuple[str, str]:
    """Given Morocco-local calendar dates df..dt (YYYY-MM-DD inclusive),
    return UTC ISO strings for the [00:00 Morocco, 23:59:59 Morocco] window."""
    start_local = datetime.fromisoformat(f"{df}T00:00:00").replace(tzinfo=MOROCCO_TZ)
    end_local = datetime.fromisoformat(f"{dt}T23:59:59").replace(tzinfo=MOROCCO_TZ)
    return (start_local.astimezone(timezone.utc).isoformat(),
            end_local.astimezone(timezone.utc).isoformat())


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
    """All named ranges are anchored to Morocco calendar days so the app
    doesn't roll over at UTC midnight (01:00 Casablanca).

    Returns (None, None) for range_key='all' — callers should treat that as
    "skip the date filter entirely" so the admin can see the full history of
    ad performance without windowing."""
    today = today_morocco()
    if range_key == "all":
        return None, None
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


def apply_date_filter(q, df: Optional[str], dt: Optional[str], field: str = "created_time"):
    """Filter a timestamptz column against a Morocco-local [df..dt] range.
    Converts the day boundaries to UTC so the DB query matches the calendar
    day that agents actually see on their phones. Returns the query unchanged
    when df/dt are None — that's how callers express "no date limit" (e.g.
    range='all' on the admin ad-quality view)."""
    if not df or not dt:
        return q
    start_utc, end_utc = morocco_day_bounds_utc(df, dt)
    return q.gte(field, start_utc).lte(field, end_utc)


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


_LEAD_FIELDS_BASE = (
    "id, created_time, full_name, phone_primary, phones, ad_name, "
    "platform, status, assigned_at, contacted_at, last_note, data"
)


def _lead_select_fields() -> str:
    """Compose the SELECT field list based on which optional columns exist.
    Keeps older deployments working before their migration runs."""
    extras = []
    if _has_custom_status_col():
        extras.append("custom_status")
    if _has_rdv_date_col():
        extras.append("rdv_date")
    if _has_rdv_time_col():
        extras.append("rdv_time")
    if _has_status_changed_at_col():
        extras.append("status_changed_at")
    return _LEAD_FIELDS_BASE + ("".join(f", {e}" for e in extras) if extras else "")


@router.get("/my")
def my_leads(
    range: str = Query("today"),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    include_all_new: bool = Query(False),
    agent=Depends(require_agent),
):
    """Leads assigned to me in a date range.

    When `include_all_new=true` the response also includes every ACTIVE lead
    (any non-terminal status) assigned to the agent, regardless of when it
    was assigned. Agents used to lose leads from view the moment they moved
    a backlog lead out of `new` — since it was no longer `new` AND outside
    the date window, the next refresh would hide it. Widening the merge to
    every active-pipeline status keeps a lead visible until the agent hits
    a terminal outcome (registered / pi / pe / autre_ville / over_40 /
    contra), which intentionally retire the lead from the default inbox.
    """
    sb = get_client()
    df, dt = resolve_range(range, date_from, date_to)

    fields = _lead_select_fields()
    q = sb.table("ad_leads").select(fields).eq("assigned_agent_id", agent["sub"])
    # Filter on assigned_at so leads with no source-side date still show up;
    # this also matches the agent's mental model of "leads I got today".
    q = apply_date_filter(q, df, dt, field="assigned_at")
    if status:
        q = q.eq("status", status)
    res = q.order("assigned_at", desc=True).execute()
    rows = res.data or []

    # Active-pipeline merge: pull every in-progress lead that fell outside
    # the window, so status updates never make a lead silently disappear.
    # Skipped when the caller is explicitly filtering by status — that
    # means they want a single bucket, not the full inbox view.
    ACTIVE_STATUSES = [
        "new", "contacted", "rdv", "bv",
        "waiting", "no_answer", "custom", "visits",
    ]
    if include_all_new and not status:
        seen = {r["id"] for r in rows}
        active = sb.table("ad_leads").select(fields) \
            .eq("assigned_agent_id", agent["sub"]) \
            .in_("status", ACTIVE_STATUSES) \
            .order("assigned_at", desc=True).execute()
        for b in (active.data or []):
            if b["id"] not in seen:
                rows.append(b)
                seen.add(b["id"])

    # Include visible column defs so the UI can render the dynamic table
    configs = _configs_for_agent(sb, agent["sub"])
    columns = _columns_for_configs(sb, [c["id"] for c in configs])

    return {
        "leads": rows,
        "columns": _merge_visible_columns(columns),
        "date_from": df,
        "date_to": dt,
        "features": {
            "custom_status": _has_custom_status_col(),
            "rdv_date": _has_rdv_date_col(),
        },
    }


# ---------------------------------------------------------------------------
# My scheduled RDVs — grouped today / tomorrow / this-week / later.
# Powers the RDV section on the agent leads page and the "today's RDVs"
# block on the agent dashboard.
# ---------------------------------------------------------------------------

@router.get("/my/rdvs")
def my_rdvs(bucket: Optional[str] = Query(None), agent=Depends(require_agent)):
    """bucket ∈ {today, tomorrow, week, later, all}. Default all -> grouped dict."""
    sb = get_client()
    if not _has_rdv_date_col():
        # Feature gated on the migration. Return empty buckets so the UI
        # can render its empty state without a noisy error.
        return {
            "enabled": False,
            "today": [], "tomorrow": [], "week": [], "later": [],
        }

    today = today_morocco()
    tomorrow = today + timedelta(days=1)
    week_end = today + timedelta(days=6)  # inclusive → "next 7 days"

    q = sb.table("ad_leads").select(_lead_select_fields()) \
        .eq("assigned_agent_id", agent["sub"]) \
        .eq("status", "rdv") \
        .order("rdv_date")

    if bucket == "today":
        q = q.eq("rdv_date", today.isoformat())
    elif bucket == "tomorrow":
        q = q.eq("rdv_date", tomorrow.isoformat())
    elif bucket == "week":
        q = q.gte("rdv_date", today.isoformat()).lte("rdv_date", week_end.isoformat())
    elif bucket == "later":
        q = q.gt("rdv_date", week_end.isoformat())
    elif bucket == "missed":
        # No-shows: status is still rdv (agent hasn't moved them to visits or
        # registered) AND the appointment date is in the past. Drives the
        # follow-up flow on the agent leads page.
        q = q.lt("rdv_date", today.isoformat())

    res = q.execute()
    # Drop leads with no rdv_date set — simpler than chaining a nullable filter
    # through postgrest-py, and the volume is tiny (per-agent scheduled RDVs).
    rows = [r for r in (res.data or []) if r.get("rdv_date")]

    if bucket:
        return {"enabled": True, "leads": rows, "bucket": bucket}

    # Group mode — split into buckets client-side to avoid five round trips.
    today_s = today.isoformat()
    tomorrow_s = tomorrow.isoformat()
    week_end_s = week_end.isoformat()
    buckets = {"today": [], "tomorrow": [], "week": [], "later": [], "missed": []}
    for r in rows:
        d = r.get("rdv_date")
        if not d:
            continue
        if d < today_s:
            buckets["missed"].append(r)
        elif d == today_s:
            buckets["today"].append(r)
        elif d == tomorrow_s:
            buckets["tomorrow"].append(r)
        elif today_s < d <= week_end_s:
            buckets["week"].append(r)
        elif d > week_end_s:
            buckets["later"].append(r)
    return {"enabled": True, **buckets}


@router.get("/my/streak")
def my_streak(agent=Depends(require_agent)):
    """Consecutive-day streak of booking at least one new RDV (or moving a
    lead past the RDV stage). Computed in Morocco-local days. The agent's
    fixed weekly day_off is treated as a free pass — it doesn't break the
    streak, so reps don't feel punished for taking their planned day off.

    Returns:
      current_streak: how many consecutive days of RDV activity ending today
                      (or yesterday if today has no activity yet)
      best_streak:    longest streak in the last 90 days
      today_has_rdv:  true if today already counts toward the streak
    """
    sb = get_client()
    aid = agent["sub"]

    # Without status_changed_at the streak is meaningless — fall back to
    # zero so the UI can hide the card silently.
    if not _has_status_changed_at_col():
        return {"current_streak": 0, "best_streak": 0, "today_has_rdv": False, "enabled": False}

    today = today_morocco()
    horizon_days = 90
    horizon_start = today - timedelta(days=horizon_days)
    horizon_start_utc, _ = morocco_day_bounds_utc(horizon_start.isoformat(), horizon_start.isoformat())

    # Pull every lead the agent moved to rdv-or-beyond in the last 90 days.
    # status_changed_at is stamped on every status change; same-day moves
    # past RDV still register the day as an "RDV day" (one bucket per day).
    res = sb.table("ad_leads").select("status_changed_at") \
        .eq("assigned_agent_id", aid) \
        .in_("status", ["rdv", "visits", "registered"]) \
        .gte("status_changed_at", horizon_start_utc) \
        .execute()

    days = set()
    for r in (res.data or []):
        ts = r.get("status_changed_at")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            days.add(dt.astimezone(MOROCCO_TZ).date())
        except Exception:
            continue

    # Day-off lookup for free-pass logic
    agent_row = sb.table("agents").select("day_off").eq("id", aid).execute()
    day_off = agent_row.data[0].get("day_off") if agent_row.data else None

    def is_off(d: date) -> bool:
        if day_off is None:
            return False
        # JS-style weekday: 0=Sun..6=Sat (matches the day_off field).
        return ((d.weekday() + 1) % 7) == day_off

    # Current streak — walk back from today. If today has no activity AND
    # is not an off-day, start from yesterday so the streak doesn't drop
    # to zero just because the agent hasn't booked an RDV yet today.
    today_has = today in days
    cursor = today
    if not today_has and not is_off(today):
        cursor = today - timedelta(days=1)
    current = 0
    safety = 0
    while safety < horizon_days + 5:
        safety += 1
        if cursor in days:
            current += 1
            cursor -= timedelta(days=1)
        elif is_off(cursor):
            cursor -= timedelta(days=1)  # skip day-off without breaking
        else:
            break

    # Best streak across the horizon — walk sorted activity days, allowing
    # gaps that consist entirely of the agent's day_off.
    best = 0
    if days:
        sorted_days = sorted(days)
        cur = 1
        best = 1
        for i in range(1, len(sorted_days)):
            gap = (sorted_days[i] - sorted_days[i - 1]).days
            if gap == 1:
                cur += 1
            elif gap > 1 and all(is_off(sorted_days[i - 1] + timedelta(days=k)) for k in range(1, gap)):
                cur += 1  # gap was only off-days
            else:
                cur = 1
            best = max(best, cur)
    best = max(best, current)

    return {
        "current_streak": current,
        "best_streak": best,
        "today_has_rdv": today_has,
        "enabled": True,
    }


@router.get("/my/count")
def my_counts(agent=Depends(require_agent)):
    sb = get_client()
    today_s = today_morocco().isoformat()
    start_utc, _ = morocco_day_bounds_utc(today_s, today_s)
    r = sb.table("ad_leads").select("status") \
        .eq("assigned_agent_id", agent["sub"]) \
        .gte("assigned_at", start_utc).execute()
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
    # New optional fields — both require a one-time ALTER TABLE to enable.
    # Without the migration the app stays functional, just without these
    # features (see _has_custom_status_col / _has_rdv_date_col probes).
    custom_status: Optional[str] = None
    rdv_date: Optional[str] = None  # YYYY-MM-DD
    rdv_time: Optional[str] = None  # HH:MM (24h)


VALID_STATUSES = {
    "new", "contacted", "rdv", "bv", "pi", "pe",
    "autre_ville", "over_40", "contra", "visits", "registered",
    "waiting", "no_answer", "custom",
}


# ---------------------------------------------------------------------------
# Column existence probes — same pattern as agents._safe_write(pin_plain)
# and ad_leads_sync.has_adset_col(). Lets us ship backend ahead of the
# Supabase migration without breaking the app; the affected feature simply
# returns a clear error instead of exploding.
# ---------------------------------------------------------------------------

_HAS_CUSTOM_STATUS_COL: Optional[bool] = None
_HAS_RDV_DATE_COL: Optional[bool] = None
_HAS_RDV_TIME_COL: Optional[bool] = None
_HAS_STATUS_CHANGED_AT_COL: Optional[bool] = None


def _has_custom_status_col() -> bool:
    global _HAS_CUSTOM_STATUS_COL
    if _HAS_CUSTOM_STATUS_COL is None:
        try:
            get_client().table("ad_leads").select("custom_status").limit(1).execute()
            _HAS_CUSTOM_STATUS_COL = True
        except Exception:
            _HAS_CUSTOM_STATUS_COL = False
            log.warning("[ad_leads] custom_status column missing — run migration to enable custom labels")
    return _HAS_CUSTOM_STATUS_COL


def _has_rdv_date_col() -> bool:
    global _HAS_RDV_DATE_COL
    if _HAS_RDV_DATE_COL is None:
        try:
            get_client().table("ad_leads").select("rdv_date").limit(1).execute()
            _HAS_RDV_DATE_COL = True
        except Exception:
            _HAS_RDV_DATE_COL = False
            log.warning("[ad_leads] rdv_date column missing — run migration to enable scheduled RDV dates")
    return _HAS_RDV_DATE_COL


def _has_rdv_time_col() -> bool:
    global _HAS_RDV_TIME_COL
    if _HAS_RDV_TIME_COL is None:
        try:
            get_client().table("ad_leads").select("rdv_time").limit(1).execute()
            _HAS_RDV_TIME_COL = True
        except Exception:
            _HAS_RDV_TIME_COL = False
            log.warning("[ad_leads] rdv_time column missing — run migration to enable scheduled RDV hour-of-day")
    return _HAS_RDV_TIME_COL


def _has_status_changed_at_col() -> bool:
    global _HAS_STATUS_CHANGED_AT_COL
    if _HAS_STATUS_CHANGED_AT_COL is None:
        try:
            get_client().table("ad_leads").select("status_changed_at").limit(1).execute()
            _HAS_STATUS_CHANGED_AT_COL = True
        except Exception:
            _HAS_STATUS_CHANGED_AT_COL = False
            log.warning("[ad_leads] status_changed_at column missing — admin 'today' filter falls back to assigned_at")
    return _HAS_STATUS_CHANGED_AT_COL


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{2}:\d{2}$")


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
    # status_changed_at gives admin "today's activity" view — every status
    # change stamps this column so a lead the agent marked RDV today lands
    # in today's admin bucket even if the lead itself was assigned yesterday.
    if _has_status_changed_at_col():
        updates["status_changed_at"] = now_iso

    # Custom status — requires the free-text label.
    if body.status == "custom":
        label = (body.custom_status or "").strip()
        if not label:
            raise HTTPException(400, "يرجى كتابة الحالة المخصصة")
        if len(label) > 100:
            raise HTTPException(400, "الحالة المخصصة طويلة جداً (الحد الأقصى 100 حرف)")
        if not _has_custom_status_col():
            raise HTTPException(400, "يرجى تنفيذ تحديث قاعدة البيانات لتفعيل الحالة المخصصة")
        updates["custom_status"] = label
    elif _has_custom_status_col():
        # Clear stale custom label when moving off custom status.
        updates["custom_status"] = None

    # RDV scheduled date — optional; only meaningful when status == rdv.
    if body.status == "rdv":
        if body.rdv_date:
            if not _DATE_RE.match(body.rdv_date):
                raise HTTPException(400, "صيغة التاريخ غير صحيحة (YYYY-MM-DD)")
            if not _has_rdv_date_col():
                raise HTTPException(400, "يرجى تنفيذ تحديث قاعدة البيانات لحفظ تاريخ الموعد")
            updates["rdv_date"] = body.rdv_date
        # If agent didn't provide rdv_date, leave whatever was there alone.
        # Same for rdv_time — gets stored as HH:MM:00 for the TIME column.
        if body.rdv_time:
            if not _TIME_RE.match(body.rdv_time):
                raise HTTPException(400, "صيغة الوقت غير صحيحة (HH:MM)")
            if not _has_rdv_time_col():
                raise HTTPException(400, "يرجى تنفيذ تحديث قاعدة البيانات لحفظ وقت الموعد")
            updates["rdv_time"] = body.rdv_time + ":00"
    elif _has_rdv_date_col():
        # Clear RDV date when moving off rdv — avoids stale dates haunting lists.
        updates["rdv_date"] = None
        if _has_rdv_time_col():
            updates["rdv_time"] = None

    sb.table("ad_leads").update(updates).eq("id", lead_id).execute()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Comment on a lead — writes last_note without touching status. Separate
# endpoint so the agent can annotate a lead (e.g. "called mom — no answer")
# without needing to flip status at the same time.
# ---------------------------------------------------------------------------

class NoteUpdate(BaseModel):
    note: str


@router.patch("/{lead_id}/note")
def update_lead_note(lead_id: str, body: NoteUpdate, agent=Depends(require_agent)):
    sb = get_client()
    note = (body.note or "").strip()
    if len(note) > 2000:
        raise HTTPException(400, "التعليق طويل جداً (الحد الأقصى 2000 حرف)")

    lead = sb.table("ad_leads").select("id, assigned_agent_id").eq("id", lead_id).execute()
    if not lead.data:
        raise HTTPException(404, "Lead not found")
    if lead.data[0]["assigned_agent_id"] != agent["sub"]:
        raise HTTPException(403, "This lead is not assigned to you")

    sb.table("ad_leads").update({
        "last_note": note or None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", lead_id).execute()
    return {"ok": True, "note": note}


class NameUpdate(BaseModel):
    full_name: str


@router.patch("/{lead_id}/name")
def update_lead_name(lead_id: str, body: NameUpdate, agent=Depends(require_agent)):
    sb = get_client()
    name = (body.full_name or "").strip()
    if not name:
        raise HTTPException(400, "الاسم لا يمكن أن يكون فارغاً")
    if len(name) > 200:
        raise HTTPException(400, "الاسم طويل جداً")

    lead = sb.table("ad_leads").select("id, assigned_agent_id").eq("id", lead_id).execute()
    if not lead.data:
        raise HTTPException(404, "Lead not found")
    if lead.data[0]["assigned_agent_id"] != agent["sub"]:
        raise HTTPException(403, "This lead is not assigned to you")

    sb.table("ad_leads").update({
        "full_name": name,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", lead_id).execute()
    return {"ok": True, "full_name": name}


# ---------------------------------------------------------------------------
# Availability (off-dates) — per-agent, unrelated to branch
# ---------------------------------------------------------------------------

@router.get("/availability")
def get_my_off_dates(agent=Depends(require_agent)):
    sb = get_client()
    today_iso = today_morocco().isoformat()
    res = sb.table("agent_off_dates").select("off_date") \
        .eq("agent_id", agent["sub"]).gte("off_date", today_iso) \
        .order("off_date").execute()
    return {"off_dates": [r["off_date"] for r in (res.data or [])]}


class OffDatesUpdate(BaseModel):
    off_dates: List[str]


@router.put("/availability")
def set_my_off_dates(body: OffDatesUpdate, agent=Depends(require_agent)):
    sb = get_client()
    today_iso = today_morocco().isoformat()
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


# ---------------------------------------------------------------------------
# Admin transfer — cross-branch, any status, any agent
# ---------------------------------------------------------------------------

class AdminTransferBody(BaseModel):
    from_agent_id: str
    to_agent_id: str
    lead_ids: Optional[List[str]] = None
    count: Optional[int] = None
    status: Optional[str] = None  # optional filter when using count mode


@router.get("/admin/agent-leads/{agent_id}")
def admin_list_agent_leads(
    agent_id: str,
    status: Optional[str] = Query(None),
    range: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    limit: int = Query(500),
    admin=Depends(require_admin),
):
    """Leads assigned to an agent — used by the admin leads page and the
    transfer modal. Date filter is applied on assigned_at so it matches the
    admin's "leads this agent got today" mental model."""
    sb = get_client()
    base = ("id, created_time, assigned_at, full_name, phone_primary, "
            "ad_name, platform, status, last_note")
    if _has_custom_status_col():
        base += ", custom_status"
    if _has_rdv_date_col():
        base += ", rdv_date"
    if _has_rdv_time_col():
        base += ", rdv_time"
    if _has_status_changed_at_col():
        base += ", status_changed_at"
    q = sb.table("ad_leads").select(base).eq("assigned_agent_id", agent_id)
    if status:
        q = q.eq("status", status)
    if range:
        df, dt = resolve_range(range, date_from, date_to)
        # Admin "today" means "status changed today" — a lead assigned
        # yesterday but marked RDV today should count toward today.
        activity_field = "status_changed_at" if _has_status_changed_at_col() else "assigned_at"
        q = apply_date_filter(q, df, dt, field=activity_field)
    # Sort by the same field we filtered on so the most recent activity is first.
    order_field = "status_changed_at" if _has_status_changed_at_col() else "assigned_at"
    res = q.order(order_field, desc=True).limit(max(1, min(2000, limit))).execute()
    return {"leads": res.data or []}


@router.get("/admin/agent-leaderboard")
def admin_agent_leaderboard(
    range: str = Query("today"),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    branch_id: Optional[str] = Query(None),
    city_id: Optional[str] = Query(None),
    admin=Depends(require_admin),
):
    """Per-agent lead counts + status breakdown, ranked by registered →
    RDV → total. Powers the admin leads page."""
    sb = get_client()
    df, dt = resolve_range(range, date_from, date_to)

    # Resolve agents in scope (all active agents, optionally narrowed by
    # branch or city).
    a_q = sb.table("agents").select(
        "id, name, branch_id, is_active, branches(name, city)"
    ).eq("is_active", True)
    if branch_id:
        a_q = a_q.eq("branch_id", branch_id)
    elif city_id:
        city = sb.table("cities").select("name").eq("id", city_id).execute()
        if city.data:
            brs = sb.table("branches").select("id").eq("city", city.data[0]["name"]).execute()
            ids = [b["id"] for b in (brs.data or [])]
            if not ids:
                return {"agents": [], "date_from": df, "date_to": dt}
            a_q = a_q.in_("branch_id", ids)
    agents_data = a_q.execute().data or []

    agent_ids = [a["id"] for a in agents_data]
    counts: Dict[str, Dict[str, Any]] = {}
    # Admin "today" view = status changed today. A lead assigned yesterday
    # that got marked RDV or registered today counts toward today's activity.
    activity_field = "status_changed_at" if _has_status_changed_at_col() else "assigned_at"
    if agent_ids:
        sel = "assigned_agent_id, status, assigned_at"
        if activity_field != "assigned_at":
            sel += f", {activity_field}"
        l_q = sb.table("ad_leads").select(sel).in_("assigned_agent_id", agent_ids)
        l_q = apply_date_filter(l_q, df, dt, field=activity_field)
        leads = l_q.execute().data or []
        for l in leads:
            aid = l["assigned_agent_id"]
            bucket = counts.setdefault(aid, {"total": 0, "by_status": {}})
            bucket["total"] += 1
            s = l.get("status") or "new"
            bucket["by_status"][s] = bucket["by_status"].get(s, 0) + 1

    # All-time pending pile per agent: any lead currently in `new` status
    # regardless of when it was assigned. The date-filtered new_count above
    # only captures activity in the selected window — admins also need the
    # untouched-backlog total so they can see who's drowning in cold leads.
    pending_new_total: Dict[str, int] = {}
    pending_active_total: Dict[str, int] = {}
    if agent_ids:
        ACTIVE_STATUSES = ["new", "contacted", "rdv", "bv", "waiting", "no_answer", "custom", "visits"]
        p_q = sb.table("ad_leads").select("assigned_agent_id, status") \
            .in_("assigned_agent_id", agent_ids) \
            .in_("status", ACTIVE_STATUSES) \
            .execute()
        for r in (p_q.data or []):
            aid = r["assigned_agent_id"]
            if r.get("status") == "new":
                pending_new_total[aid] = pending_new_total.get(aid, 0) + 1
            pending_active_total[aid] = pending_active_total.get(aid, 0) + 1

    rows = []
    for a in agents_data:
        cd = counts.get(a["id"], {"total": 0, "by_status": {}})
        by = cd["by_status"]
        br = a.get("branches") or {}
        registered = by.get("registered", 0)
        rows.append({
            "id": a["id"],
            "name": a["name"],
            "branch_id": a.get("branch_id"),
            "branch_name": br.get("name"),
            "branch_city": br.get("city"),
            "total": cd["total"],
            "by_status": by,
            "new_count":        by.get("new", 0),
            "contacted_count":  sum(v for k, v in by.items() if k != "new"),
            "rdv_count":        by.get("rdv", 0),
            "bv_count":         by.get("bv", 0),
            "pi_count":         by.get("pi", 0),
            "pe_count":         by.get("pe", 0),
            "autre_ville_count": by.get("autre_ville", 0),
            "over_40_count":    by.get("over_40", 0),
            "contra_count":     by.get("contra", 0),
            "visits_count":     by.get("visits", 0),
            "registered_count": registered,
            "conversion_pct": round(registered / cd["total"], 3) if cd["total"] else 0,
            # All-time pile (ignores date filter) — surfaces accumulated work
            "pending_new_total":    pending_new_total.get(a["id"], 0),
            "pending_active_total": pending_active_total.get(a["id"], 0),
        })

    rows.sort(key=lambda x: (-x["registered_count"], -x["rdv_count"], -x["total"], x["name"]))
    for i, r in enumerate(rows):
        r["rank"] = i + 1

    totals = {
        "total": sum(r["total"] for r in rows),
        "rdv": sum(r["rdv_count"] for r in rows),
        "visits": sum(r["visits_count"] for r in rows),
        "registered": sum(r["registered_count"] for r in rows),
        "new": sum(r["new_count"] for r in rows),
    }
    return {"agents": rows, "totals": totals, "date_from": df, "date_to": dt}


@router.post("/admin/transfer")
def admin_transfer(body: AdminTransferBody, admin=Depends(require_admin)):
    sb = get_client()
    if body.from_agent_id == body.to_agent_id:
        raise HTTPException(400, "لا يمكن النقل إلى نفس الوكيل")

    # Verify both agents exist (target should also be active; source can be
    # inactive so admin can drain a fired agent's pool).
    ids = [body.from_agent_id, body.to_agent_id]
    agents = sb.table("agents").select("id, is_active").in_("id", ids).execute()
    by_id = {a["id"]: a for a in (agents.data or [])}
    if body.from_agent_id not in by_id:
        raise HTTPException(404, "الوكيل المصدر غير موجود")
    target = by_id.get(body.to_agent_id)
    if not target:
        raise HTTPException(404, "الوكيل المستهدف غير موجود")
    if not target.get("is_active"):
        raise HTTPException(400, "الوكيل المستهدف موقوف")

    if body.lead_ids:
        # Selection mode — only move leads actually owned by from_agent.
        eligible = sb.table("ad_leads").select("id") \
            .in_("id", body.lead_ids) \
            .eq("assigned_agent_id", body.from_agent_id).execute()
        eligible_ids = [r["id"] for r in (eligible.data or [])]
    elif body.count and body.count > 0:
        q = sb.table("ad_leads").select("id") \
            .eq("assigned_agent_id", body.from_agent_id)
        if body.status:
            q = q.eq("status", body.status)
        # Oldest first so the admin drains the backlog rather than the fresh
        # leads the agent is actively working on.
        res = q.order("assigned_at").limit(body.count).execute()
        eligible_ids = [r["id"] for r in (res.data or [])]
    else:
        raise HTTPException(400, "يرجى تحديد الرسائل أو العدد")

    if not eligible_ids:
        return {"transferred": 0, "requested": len(body.lead_ids or []) or (body.count or 0)}

    now_iso = datetime.now(timezone.utc).isoformat()
    sb.table("ad_leads").update({
        "assigned_agent_id": body.to_agent_id,
        "assigned_at": now_iso,
        "updated_at": now_iso,
    }).in_("id", eligible_ids).execute()

    try:
        sb.table("lead_transfers").insert([{
            "lead_id": lid,
            "from_agent_id": body.from_agent_id,
            "to_agent_id": body.to_agent_id,
        } for lid in eligible_ids]).execute()
    except Exception as e:
        # Transfer log is best-effort — the lead move has already succeeded.
        log.warning(f"lead_transfers insert failed: {e}")

    return {
        "transferred": len(eligible_ids),
        "requested": len(body.lead_ids or []) or (body.count or 0),
    }


class AdminDistributeBody(BaseModel):
    from_agent_id: str
    to_scope_type: str  # 'branch' or 'city'
    to_scope_id: str
    lead_ids: Optional[List[str]] = None
    count: Optional[int] = None
    status: Optional[str] = None


@router.post("/admin/distribute")
def admin_distribute(body: AdminDistributeBody, admin=Depends(require_admin)):
    """Round-robin split leads from one agent across every active agent in
    a branch or city. Source agent is excluded from the target pool."""
    sb = get_client()

    if body.to_scope_type not in ("branch", "city"):
        raise HTTPException(400, "to_scope_type must be 'branch' or 'city'")

    # Resolve target agents within scope
    if body.to_scope_type == "branch":
        ta = sb.table("agents").select("id, name") \
            .eq("branch_id", body.to_scope_id).eq("is_active", True).execute()
        target_agents = ta.data or []
    else:
        city = sb.table("cities").select("name").eq("id", body.to_scope_id).execute()
        if not city.data:
            raise HTTPException(404, "المدينة غير موجودة")
        brs = sb.table("branches").select("id") \
            .eq("city", city.data[0]["name"]).execute()
        branch_ids = [b["id"] for b in (brs.data or [])]
        if not branch_ids:
            return {"transferred": 0, "distribution": [], "target_count": 0}
        ta = sb.table("agents").select("id, name") \
            .in_("branch_id", branch_ids).eq("is_active", True).execute()
        target_agents = ta.data or []

    target_agents = [a for a in target_agents if a["id"] != body.from_agent_id]
    if not target_agents:
        raise HTTPException(400, "لا يوجد وكلاء مستهدفون في هذا النطاق")

    # Resolve eligible leads
    if body.lead_ids:
        eligible = sb.table("ad_leads").select("id") \
            .in_("id", body.lead_ids) \
            .eq("assigned_agent_id", body.from_agent_id).execute()
        eligible_ids = [r["id"] for r in (eligible.data or [])]
    elif body.count and body.count > 0:
        q = sb.table("ad_leads").select("id") \
            .eq("assigned_agent_id", body.from_agent_id)
        if body.status:
            q = q.eq("status", body.status)
        res = q.order("assigned_at").limit(body.count).execute()
        eligible_ids = [r["id"] for r in (res.data or [])]
    else:
        raise HTTPException(400, "يرجى تحديد الرسائل أو العدد")

    if not eligible_ids:
        return {
            "transferred": 0,
            "distribution": [],
            "target_count": len(target_agents),
        }

    # Round-robin assignment
    now_iso = datetime.now(timezone.utc).isoformat()
    assignments: Dict[str, List[str]] = {a["id"]: [] for a in target_agents}
    for i, lid in enumerate(eligible_ids):
        aid = target_agents[i % len(target_agents)]["id"]
        assignments[aid].append(lid)

    transferred = 0
    for aid, ids in assignments.items():
        if not ids:
            continue
        sb.table("ad_leads").update({
            "assigned_agent_id": aid,
            "assigned_at": now_iso,
            "updated_at": now_iso,
        }).in_("id", ids).execute()
        transferred += len(ids)

    # Best-effort audit log — don't fail the whole call if the log insert breaks
    try:
        rows = []
        for aid, ids in assignments.items():
            for lid in ids:
                rows.append({
                    "lead_id": lid,
                    "from_agent_id": body.from_agent_id,
                    "to_agent_id": aid,
                })
        if rows:
            sb.table("lead_transfers").insert(rows).execute()
    except Exception as e:
        log.warning(f"lead_transfers insert failed: {e}")

    name_by_id = {a["id"]: a["name"] for a in target_agents}
    distribution = [
        {"agent_id": aid, "agent_name": name_by_id.get(aid, "—"), "count": len(ids)}
        for aid, ids in assignments.items() if ids
    ]
    distribution.sort(key=lambda x: -x["count"])

    return {
        "transferred": transferred,
        "distribution": distribution,
        "target_count": len(target_agents),
    }


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
    today = today_morocco().isoformat()

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

    today_start_utc, _ = morocco_day_bounds_utc(today, today)
    leads = sb.table("ad_leads").select("assigned_agent_id") \
        .in_("assigned_agent_id", ids).gte("assigned_at", today_start_utc).execute()
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
    date_basis: str = Query("activity"),
    group_by: str = Query("ad"),
    admin=Depends(require_admin),
):
    """Aggregate leads by ad_name. Counts are cumulative through the funnel —
    a lead that progressed new→rdv→visits→registered is counted at every
    stage it passed (so rdv_count includes it), not just its current state.
    This matches the admin's mental model of "how many RDVs did this ad
    bring in" and prevents successful ads from looking weak just because
    their best leads kept advancing.

    date_basis ∈ {activity, arrival}:
      - activity  (default): filter by status_changed_at — "ads that had
        live activity in this window". Picks up a lead the agent marked
        RDV today even if FB delivered it weeks ago. Matches the admin's
        real-time expectation. Falls back to created_time when the column
        is missing or the lead has never been touched.
      - arrival: filter by created_time — classical "ads that delivered
        leads in this window". Kept for ad-performance audits.

    Sort: rdv_count desc, then registered_count desc — best-performing ads
    on top. Color: rdv_rate (green ≥20%, orange 8-20%, red <8%); mature ads
    with zero registered get downgraded one level since registered is the
    secondary long-term metric (1-10 days to materialize)."""
    sb = get_client()
    df, dt = resolve_range(range, date_from, date_to)

    # Pick the date field to filter on. "activity" = status_changed_at so
    # the ad quality board reflects live status updates; "arrival" keeps the
    # classical created_time semantics for audits. Gracefully degrades to
    # created_time if the migration hasn't been applied yet.
    if date_basis == "arrival" or not _has_status_changed_at_col():
        filter_field = "created_time"
    else:
        filter_field = "status_changed_at"

    # adset_name + data are needed for group_by=adset and group_by=campaign
    # (campaign_name lives in the data JSONB; we don't have a promoted column
    # for it yet). data also lets the cards expose a clickable variation
    # count when grouping above the ad level.
    select_cols = "ad_name, adset_name, platform, status, created_time, scope_type, scope_id, data"
    if filter_field == "status_changed_at":
        select_cols += ", status_changed_at"

    q = sb.table("ad_leads").select(select_cols)
    # range='all' ⇒ df/dt come back as None; skip the date filter entirely
    # so the admin sees the full historical ad quality (every lead every ad
    # ever delivered), which is what "see all previous leads" requires.
    if df and dt:
        q = apply_date_filter(q, df, dt, field=filter_field)
    if scope_type and scope_id:
        q = q.eq("scope_type", scope_type).eq("scope_id", scope_id)
    elif city_id:
        q = q.eq("scope_type", "city").eq("scope_id", city_id)
    elif branch_id:
        q = q.eq("scope_type", "branch").eq("scope_id", branch_id)
    res = q.execute()

    # Statuses that have "passed through" RDV / visits / registered. Used to
    # build cumulative funnel counts.
    PAST_RDV     = {"rdv", "visits", "registered"}
    PAST_VISITS  = {"visits", "registered"}
    PAST_REG     = {"registered"}

    # Bucket key extractor + label. Determines the unit of comparison so
    # the same endpoint can serve creative-level, adset-level, campaign-level
    # and platform-level analytics — what an FB ads operator needs for
    # split-testing reads.
    if group_by not in ("ad", "adset", "campaign", "platform"):
        group_by = "ad"
    EMPTY_BUCKET = {
        "ad":       "(بدون اسم)",
        "adset":    "(بدون مجموعة)",
        "campaign": "(بدون حملة)",
        "platform": "(غير محدد)",
    }[group_by]

    def _bucket_key(r: dict) -> str:
        if group_by == "ad":
            return r.get("ad_name") or EMPTY_BUCKET
        if group_by == "adset":
            return r.get("adset_name") or EMPTY_BUCKET
        if group_by == "campaign":
            d = r.get("data") or {}
            # case-insensitive lookup, common variants of the key
            for k in ("campaign_name", "Campaign Name", "campaign", "اسم الحملة"):
                v = d.get(k) if isinstance(d, dict) else None
                if v:
                    return v
            return EMPTY_BUCKET
        if group_by == "platform":
            return (r.get("platform") or EMPTY_BUCKET)
        return EMPTY_BUCKET

    buckets: Dict[str, dict] = {}
    for r in (res.data or []):
        key = _bucket_key(r)
        b = buckets.setdefault(key, {
            "key": key,
            # Backward-compatible alias — the frontend cards still read
            # `ad_name` for the title; we mirror the bucket key into it
            # regardless of which level we grouped at.
            "ad_name": key,
            "group_by": group_by,
            "platform": r.get("platform"),
            # Funnel buckets — rdv_count / visits_count / registered_count
            # are CUMULATIVE; the per-status fields are the raw current-state
            # counts kept for diagnostics.
            "leads": 0,
            "rdv_count": 0, "visits_count": 0, "registered_count": 0,
            "new": 0, "contacted": 0, "rdv": 0, "visits": 0, "registered": 0,
            "bv": 0, "pi": 0, "pe": 0, "waiting": 0, "no_answer": 0,
            "custom": 0, "autre_ville": 0, "over_40": 0, "contra": 0,
            "first_lead_at": None, "last_lead_at": None,
            # Variation counts — for split-test reads at adset/campaign level.
            # _ad_set / _adset_set / _campaign_set are accumulators stripped
            # before serialization.
            "_ad_set": set(),
            "_adset_set": set(),
            "_campaign_set": set(),
        })
        b["leads"] += 1
        s = r.get("status") or "new"
        if s in b:
            b[s] += 1
        # Cumulative funnel counts — every lead that ever became RDV / visit /
        # registered is counted, regardless of where it sits now.
        if s in PAST_RDV:
            b["rdv_count"] += 1
        if s in PAST_VISITS:
            b["visits_count"] += 1
        if s in PAST_REG:
            b["registered_count"] += 1
        # Track variations inside the bucket so split testing reads obvious.
        if r.get("ad_name"):
            b["_ad_set"].add(r["ad_name"])
        if r.get("adset_name"):
            b["_adset_set"].add(r["adset_name"])
        d = r.get("data") or {}
        if isinstance(d, dict):
            for k in ("campaign_name", "Campaign Name", "campaign", "اسم الحملة"):
                v = d.get(k)
                if v:
                    b["_campaign_set"].add(v)
                    break
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
        rdv = b["rdv_count"]
        visits = b["visits_count"]
        registered = b["registered_count"]
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

        # PRIMARY signal: rdv_rate. Single threshold across all maturity
        # levels — fewer surprises for the admin. If the ad is mature
        # (≥7 days, enough time to register) AND hasn't registered anyone,
        # downgrade one level since the secondary metric is also failing.
        if b["rdv_rate"] >= 0.20:
            color = "green"
        elif b["rdv_rate"] >= 0.08:
            color = "orange"
        else:
            color = "red"
        if age_days >= 7 and registered == 0 and color != "red":
            color = "orange" if color == "green" else "red"
        b["color"] = color

        # Convert variation accumulators to int counts before serializing.
        # ad_count = how many distinct creatives in this bucket; same for
        # adset/campaign. Surfaces split-test fan-out at a glance.
        b["ad_count"] = len(b.pop("_ad_set"))
        b["adset_count"] = len(b.pop("_adset_set"))
        b["campaign_count"] = len(b.pop("_campaign_set"))

        ads.append(b)

    # Sort: highest RDV percentage first (the user's chosen primary metric),
    # then total RDVs (volume tiebreaker so an ad with more RDVs at the same
    # rate still wins), then registered, then leads. A bucket with zero
    # leads can't have a rate, so leads desc handles it.
    ads.sort(key=lambda x: (
        -x["rdv_rate"], -x["rdv_count"], -x["registered_count"], -x["leads"]
    ))

    totals = {
        "ads": len(ads),
        "leads": sum(a["leads"] for a in ads),
        "rdv": sum(a["rdv_count"] for a in ads),
        "visits": sum(a["visits_count"] for a in ads),
        "registered": sum(a["registered_count"] for a in ads),
    }
    return {
        "ads": ads,
        "totals": totals,
        "group_by": group_by,
        "date_from": df,
        "date_to": dt,
    }
