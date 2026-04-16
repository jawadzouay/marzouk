from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt
from services.supabase_service import get_client
from dotenv import load_dotenv
from datetime import datetime, date, timedelta
import os

load_dotenv()

router = APIRouter()
security = HTTPBearer()
JWT_SECRET = os.getenv("JWT_SECRET")
ALGORITHM = "HS256"

# Default thresholds (configurable from settings table)
DEFAULT_GREEN_MAX = 0.30   # < 30% bad leads = GREEN
DEFAULT_ORANGE_MAX = 0.50  # 30-50% = ORANGE, > 50% = RED
DEFAULT_MIN_RDV_RATE = 0.20
DEFAULT_MIN_SHOW_RATE = 0.40
DEFAULT_MIN_CLOSE_RATE = 0.30


def require_admin(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[ALGORITHM])
        if payload.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Admin only")
        return payload
    except Exception:
        raise HTTPException(status_code=401, detail="Unauthorized")


def load_thresholds():
    """Load quality thresholds from settings table, with defaults."""
    sb = get_client()
    keys = ["quality_green_max", "quality_orange_max",
            "quality_min_rdv_rate", "quality_min_show_rate", "quality_min_close_rate"]
    try:
        rows = sb.table("settings").select("key, value").in_("key", keys).execute()
        cfg = {r["key"]: float(r["value"]) for r in rows.data}
    except Exception:
        cfg = {}

    return {
        "green_max": cfg.get("quality_green_max", DEFAULT_GREEN_MAX),
        "orange_max": cfg.get("quality_orange_max", DEFAULT_ORANGE_MAX),
        "min_rdv_rate": cfg.get("quality_min_rdv_rate", DEFAULT_MIN_RDV_RATE),
        "min_show_rate": cfg.get("quality_min_show_rate", DEFAULT_MIN_SHOW_RATE),
        "min_close_rate": cfg.get("quality_min_close_rate", DEFAULT_MIN_CLOSE_RATE),
    }


def compute_agent_score(totals: dict, thresholds: dict) -> dict:
    """Compute quality metrics and classify an agent."""
    messages = totals.get("messages", 0)
    rdv = totals.get("rdv", 0)
    autre_ville = totals.get("autre_ville", 0)
    pi_count = totals.get("pi", 0)
    bv = totals.get("bv", 0)
    pe = totals.get("pe", 0)
    over_40 = totals.get("over_40", 0)
    visits = totals.get("visits", 0)
    registered = totals.get("registered", 0)

    bad_leads = autre_ville + pi_count + pe + over_40
    actionable = messages - autre_ville - pe - over_40  # PI and BV are still "real" leads in terms of ad targeting

    bad_lead_pct = (bad_leads / messages) if messages > 0 else 0
    autre_ville_pct = (autre_ville / messages) if messages > 0 else 0
    pi_pct = (pi_count / messages) if messages > 0 else 0
    bv_pct = (bv / messages) if messages > 0 else 0
    pe_pct = (pe / messages) if messages > 0 else 0
    over_40_pct = (over_40 / messages) if messages > 0 else 0

    rdv_rate = (rdv / actionable) if actionable > 0 else 0
    show_rate = (visits / rdv) if rdv > 0 else 0
    close_rate = (registered / visits) if visits > 0 else 0

    # Classify: GREEN / ORANGE / RED
    if bad_lead_pct <= thresholds["green_max"]:
        color = "green"
    elif bad_lead_pct <= thresholds["orange_max"]:
        color = "orange"
    else:
        color = "red"

    # Determine bottleneck
    bottleneck = None
    if bad_lead_pct > thresholds["orange_max"]:
        bottleneck = "ad_quality"  # Ad is bringing bad leads
    elif messages > 0 and rdv_rate < thresholds["min_rdv_rate"]:
        bottleneck = "agent_conversion"  # Agent not converting leads to RDV
    elif rdv > 0 and show_rate < thresholds["min_show_rate"]:
        bottleneck = "agent_followup"  # Agent not getting people to show up
    elif visits > 0 and close_rate < thresholds["min_close_rate"]:
        bottleneck = "agent_closing"  # Agent not closing deals

    return {
        "totals": totals,
        "messages": messages,
        "bad_leads": bad_leads,
        "bad_lead_pct": round(bad_lead_pct, 3),
        "autre_ville_pct": round(autre_ville_pct, 3),
        "pi_pct": round(pi_pct, 3),
        "bv_pct": round(bv_pct, 3),
        "pe_pct": round(pe_pct, 3),
        "over_40_pct": round(over_40_pct, 3),
        "actionable": actionable,
        "rdv_rate": round(rdv_rate, 3),
        "show_rate": round(show_rate, 3),
        "close_rate": round(close_rate, 3),
        "color": color,
        "bottleneck": bottleneck,
    }


@router.get("/scores")
def quality_scores(
    date_from: str = Query(None),
    date_to: str = Query(None),
    branch_id: str = Query(None),
    admin=Depends(require_admin)
):
    sb = get_client()
    thresholds = load_thresholds()

    # Get active agents
    agents_q = sb.table("agents").select("id, name, branch_id, branches(name)").eq("is_active", True)
    if branch_id:
        agents_q = agents_q.eq("branch_id", branch_id)
    agents = agents_q.execute()

    # Default date range: today
    if not date_from:
        date_from = date.today().isoformat()
    if not date_to:
        date_to = date.today().isoformat()

    results = []
    for agent in agents.data:
        aid = agent["id"]
        # Fetch and aggregate reports for this agent in date range
        reports = sb.table("daily_reports").select("*") \
            .eq("agent_id", aid) \
            .gte("report_date", date_from) \
            .lte("report_date", date_to) \
            .execute()

        totals = {
            "messages": 0, "rdv": 0, "autre_ville": 0, "pi": 0,
            "bv": 0, "pe": 0, "over_40": 0, "visits": 0, "registered": 0
        }
        for r in reports.data:
            for key in totals:
                totals[key] += r.get(key, 0)

        score = compute_agent_score(totals, thresholds)
        branch_info = agent.get("branches")
        score["agent_id"] = aid
        score["agent_name"] = agent["name"]
        score["branch_name"] = branch_info.get("name", "") if branch_info else ""
        score["report_count"] = len(reports.data)
        results.append(score)

    # Sort: red first, then orange, then green (worst to best for attention)
    color_order = {"red": 0, "orange": 1, "green": 2}
    results.sort(key=lambda x: color_order.get(x["color"], 3))

    return {
        "scores": results,
        "thresholds": thresholds,
        "date_from": date_from,
        "date_to": date_to
    }


@router.get("/thresholds")
def get_thresholds(admin=Depends(require_admin)):
    return load_thresholds()


@router.put("/thresholds")
def set_thresholds(body: dict, admin=Depends(require_admin)):
    sb = get_client()
    valid_keys = ["quality_green_max", "quality_orange_max",
                  "quality_min_rdv_rate", "quality_min_show_rate", "quality_min_close_rate"]

    for key in valid_keys:
        if key in body:
            val = str(body[key])
            sb.table("settings").upsert(
                {"key": key, "value": val, "updated_at": datetime.utcnow().isoformat()},
                on_conflict="key"
            ).execute()

    return {"message": "تم تحديث الحدود", "thresholds": load_thresholds()}
