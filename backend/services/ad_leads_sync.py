"""
Generic Google Sheet -> Supabase lead sync.

Admin defines a `lead_sheet_configs` row per city or branch, maps which sheet
columns to surface (with type: key/name/date/phone/ad_name/adset_name/
platform/number/text), and toggles it on. This service iterates every enabled
config on each tick, pulls the sheet, normalises Moroccan phones, dedups by the
row key, inserts new leads, and round-robin assigns them to active agents in
that scope (skipping agents whose off_dates contains today).

Transfers between agents don't update `agents.last_distributed_at`, so
transferred leads stay additive on top of the receiving agent's normal share.
"""
from services.sheets_service import get_sheets_service
from services.supabase_service import get_client
from dotenv import load_dotenv
from datetime import datetime, date, timezone
from typing import Optional, List, Dict, Any, Tuple
import hashlib
import logging
import re

load_dotenv()

log = logging.getLogger("ad_leads_sync")
log.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Moroccan phone normalization
#
# Accepts every variant users type: leading 0 or not, leading 6/7 or not,
# country code (+212, 00212, 212), spaces / dashes / dots / parens.
# Returns a canonical 10-digit string starting with 06 or 07, or None when
# the input is unrecoverable.
# ---------------------------------------------------------------------------

_PHONE_STRIP = re.compile(r"[^\d]")

# Tri-state probes: None=unknown, True/False=confirmed. Strip optional
# columns from payloads when the Supabase migration hasn't been run yet,
# so deploying the backend ahead of the SQL doesn't break the sync loop.
_HAS_ADSET_COL: Optional[bool] = None
_HAS_STATUS_CHANGED_AT_COL: Optional[bool] = None


def has_adset_col() -> bool:
    global _HAS_ADSET_COL
    if _HAS_ADSET_COL is None:
        try:
            get_client().table("ad_leads").select("adset_name").limit(1).execute()
            _HAS_ADSET_COL = True
        except Exception:
            _HAS_ADSET_COL = False
            log.warning("[ad_leads] adset_name column missing — run the migration to enable adset analytics")
    return _HAS_ADSET_COL


def has_status_changed_at_col() -> bool:
    global _HAS_STATUS_CHANGED_AT_COL
    if _HAS_STATUS_CHANGED_AT_COL is None:
        try:
            get_client().table("ad_leads").select("status_changed_at").limit(1).execute()
            _HAS_STATUS_CHANGED_AT_COL = True
        except Exception:
            _HAS_STATUS_CHANGED_AT_COL = False
    return _HAS_STATUS_CHANGED_AT_COL


def _strip_optional_cols(payload: dict) -> dict:
    """Remove columns that the DB doesn't have yet. Idempotent — returns
    the original payload when every optional column exists."""
    out = dict(payload)
    if not has_adset_col():
        out.pop("adset_name", None)
    if not has_status_changed_at_col():
        out.pop("status_changed_at", None)
    return out


def _strip_adset(payload: dict) -> dict:
    # Back-compat shim — existing call sites keep working.
    return _strip_optional_cols(payload)


def normalize_morocco_phone(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # Meta sometimes prefixes with "p:"
    if s.startswith("p:"):
        s = s[2:]
    digits = _PHONE_STRIP.sub("", s)

    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("212"):
        digits = digits[3:]

    if len(digits) == 10 and digits[0] == "0" and digits[1] in ("6", "7"):
        return digits
    if len(digits) == 9 and digits[0] in ("6", "7"):
        return "0" + digits
    return None


# ---------------------------------------------------------------------------
# Sheet IO
# ---------------------------------------------------------------------------

def _fetch_sheet_values(sheet_id: str, tab: Optional[str]) -> List[list]:
    service = get_sheets_service()
    if not tab:
        meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
        tab = meta["sheets"][0]["properties"]["title"]
    res = service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"'{tab}'!A:ZZ"
    ).execute()
    return res.get("values", [])


def fetch_sheet_headers(sheet_id: str, tab: Optional[str] = None) -> List[str]:
    """Public helper for the admin 'fetch headers' UI button."""
    rows = _fetch_sheet_values(sheet_id, tab)
    if not rows:
        return []
    return [(h or "").strip() for h in rows[0]]


# ---------------------------------------------------------------------------
# Row transformation
# ---------------------------------------------------------------------------

def _cell(row: list, idx: Optional[int]) -> Optional[str]:
    if idx is None or idx >= len(row):
        return None
    v = row[idx]
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.startswith("<test lead:"):
        return None
    return s


def _is_test_lead_row(row: list) -> bool:
    """Meta/Facebook emits a placeholder row with cells like
    '<test lead: dummy data for ...>' whenever an admin uses the form
    preview. Real lead rows never contain that marker — skip any row
    that has it anywhere."""
    for v in row:
        if v is None:
            continue
        s = str(v)
        if "<test lead:" in s or s.startswith("p:<test lead:"):
            return True
    return False


def _parse_created_time(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Auto-detect ad / adset / platform / campaign by header name. Admins often
# don't mark these columns as type=ad_name in the lead-sources mapping
# because they don't want them shown to agents — but the admin's ad quality
# page still needs them populated to bucket leads by ad. These regexes
# match the common Facebook / Meta lead-export headers in English and
# Arabic so the values get extracted automatically as a fallback.
# ---------------------------------------------------------------------------

_AD_NAME_HEADER     = re.compile(r'(?:^|[\s_-])ad[\s_-]*name\b|اسم\s*الإعلان', re.IGNORECASE)
_ADSET_NAME_HEADER  = re.compile(r'ad[\s_-]*set[\s_-]*name|adset[\s_-]*name|اسم\s*المجموعة', re.IGNORECASE)
_CAMPAIGN_HEADER    = re.compile(r'campaign[\s_-]*name|اسم\s*الحملة', re.IGNORECASE)
_PLATFORM_HEADER    = re.compile(r'^platform$|المنصة|منصة', re.IGNORECASE)


def _find_by_header(row: list, header_idx: Dict[str, int], pattern: re.Pattern) -> Optional[str]:
    """Return the first non-empty cell whose header matches the regex.
    Used as a fallback when the admin hasn't explicitly mapped the column."""
    for h, i in header_idx.items():
        if pattern.search(h or ""):
            v = _cell(row, i)
            if v:
                return v
    return None


def _row_key(row: list, header_row: List[str], key_idx: Optional[int]) -> str:
    """Unique stable key per sheet row. Uses the admin-marked key column
    when present; otherwise hashes the row contents."""
    if key_idx is not None and key_idx < len(row):
        v = (row[key_idx] or "").strip() if row[key_idx] is not None else ""
        if v:
            return v
    payload = "\u0001".join(
        ("" if idx >= len(row) else str(row[idx] or "")).strip()
        for idx in range(len(header_row))
    )
    return "h:" + hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _build_lead_from_row(
    row: list,
    header_row: List[str],
    columns: List[dict],
    config: dict,
) -> Optional[dict]:
    """Transform a raw sheet row into an ad_leads record using the column map.
    Returns None for rows that should be skipped (empty, no key)."""
    # Index by source_header
    header_idx = {h: i for i, h in enumerate(header_row)}

    key_idx = None
    data: Dict[str, Any] = {}
    phones: List[Dict[str, str]] = []
    phone_primary: Optional[str] = None
    full_name: Optional[str] = None
    ad_name: Optional[str] = None
    adset_name: Optional[str] = None
    platform: Optional[str] = None
    created_time: Optional[str] = None

    # Track if the row has any non-empty mapped content — empty rows are skipped
    any_value = False

    for col in columns:
        src = col.get("source_header") or ""
        idx = header_idx.get(src)
        if idx is None:
            continue
        raw = _cell(row, idx)
        ctype = col.get("column_type") or "text"
        display = col.get("display_name") or src

        if ctype == "key":
            key_idx = idx
            if raw:
                data[display] = raw
                any_value = True
            continue

        if raw is not None:
            any_value = True

        if ctype == "phone":
            normalized = normalize_morocco_phone(raw)
            if normalized:
                phones.append({"label": display, "number": normalized})
                if phone_primary is None:
                    phone_primary = normalized
            data[display] = normalized or raw
        elif ctype == "name":
            full_name = raw or full_name
            data[display] = raw
        elif ctype == "date":
            parsed = _parse_created_time(raw)
            if parsed:
                created_time = parsed
            data[display] = raw
        elif ctype == "ad_name":
            ad_name = raw or ad_name
            data[display] = raw
        elif ctype == "adset_name":
            adset_name = raw or adset_name
            data[display] = raw
        elif ctype == "platform":
            platform = (raw or "").lower() or platform
            data[display] = raw
        else:
            data[display] = raw

    # ── Auto-detection fallback ──────────────────────────────────────────
    # The admin doesn't have to mark ad-related columns as type=ad_name in
    # the mapping. If a sheet header looks like an ad / adset / platform
    # field, lift the value into the promoted column so the admin's ad
    # quality page can group by it. Explicit mappings still win.
    if not ad_name:
        v = _find_by_header(row, header_idx, _AD_NAME_HEADER)
        if v:
            ad_name = v
            any_value = True
    if not adset_name:
        v = _find_by_header(row, header_idx, _ADSET_NAME_HEADER)
        if v:
            adset_name = v
            any_value = True
    if not platform:
        v = _find_by_header(row, header_idx, _PLATFORM_HEADER)
        if v:
            platform = v.lower()
            any_value = True
    # Campaign name has no dedicated column yet; stash in data JSONB so
    # admin tooling can read it without a schema change.
    campaign = _find_by_header(row, header_idx, _CAMPAIGN_HEADER)
    if campaign:
        data.setdefault("campaign_name", campaign)
        any_value = True

    # Stash every unmapped sheet column into `data` too — the admin asked
    # to "extract the entire sheet to backend" so they (or future analytics)
    # can read any field without re-syncing or remapping. Mapped columns
    # already populated above keep their display_name keys; unmapped
    # columns use the raw header verbatim.
    for h, i in header_idx.items():
        if not h:
            continue
        # Already populated by the explicit mapping loop?
        already_mapped = any(
            (col.get("source_header") or "") == h for col in columns
        )
        if already_mapped:
            continue
        v = _cell(row, i)
        if v is None or v == "":
            continue
        data.setdefault(h, v)
        any_value = True

    if not any_value:
        return None

    row_key = _row_key(row, header_row, key_idx)

    # Collapse duplicate phone numbers (FB-prefilled + manual typed)
    seen = set()
    unique_phones = []
    for p in phones:
        if p["number"] in seen:
            continue
        seen.add(p["number"])
        unique_phones.append(p)

    return {
        "config_id": config["id"],
        "scope_type": config["scope_type"],
        "scope_id": config["scope_id"],
        "source_row_key": row_key,
        "data": data,
        "phone_primary": phone_primary,
        "phones": unique_phones,
        "full_name": full_name,
        "ad_name": ad_name,
        "adset_name": adset_name,
        "platform": platform,
        "created_time": created_time,
    }


# ---------------------------------------------------------------------------
# Agent pool selection per scope
# ---------------------------------------------------------------------------

def _agent_pool_for_scope(scope_type: str, scope_id: str) -> List[dict]:
    """Active agents in the given scope, ordered for fair round-robin,
    excluding anyone marked off for today."""
    sb = get_client()
    today = date.today().isoformat()

    q = sb.table("agents").select(
        "id, name, branch_id, last_distributed_at, branches(id, city)"
    ).eq("is_active", True)
    result = q.execute()
    agents = result.data or []

    matched = []
    for a in agents:
        br = a.get("branches") or {}
        if scope_type == "branch":
            if a.get("branch_id") == scope_id:
                matched.append(a)
        elif scope_type == "city":
            # scope_id for city configs stores the city UUID from `cities`
            # table. The branches table stores city as TEXT, so we resolve
            # by looking up the city name once per call.
            pass

    if scope_type == "city":
        # Resolve city_id -> city name, then match branches by city text
        city = sb.table("cities").select("name").eq("id", scope_id).execute()
        if not city.data:
            return []
        city_name = city.data[0]["name"]
        matched = [a for a in agents if (a.get("branches") or {}).get("city") == city_name]

    if not matched:
        return []

    ids = [a["id"] for a in matched]
    off = sb.table("agent_off_dates").select("agent_id") \
        .eq("off_date", today).in_("agent_id", ids).execute()
    off_ids = {r["agent_id"] for r in (off.data or [])}
    available = [a for a in matched if a["id"] not in off_ids]

    def sort_key(a):
        ts = a.get("last_distributed_at")
        return (1, ts) if ts else (0, "")

    available.sort(key=sort_key)
    return available


# ---------------------------------------------------------------------------
# Sync one config
# ---------------------------------------------------------------------------

def _load_config(config_id: str) -> Optional[dict]:
    sb = get_client()
    res = sb.table("lead_sheet_configs").select("*").eq("id", config_id).execute()
    if not res.data:
        return None
    return res.data[0]


def _load_columns(config_id: str) -> List[dict]:
    sb = get_client()
    res = sb.table("lead_sheet_columns").select("*") \
        .eq("config_id", config_id).order("display_order").execute()
    return res.data or []


def sync_config(config_id: str) -> dict:
    """Sync a single config. Returns a summary dict."""
    sb = get_client()
    config = _load_config(config_id)
    if not config:
        return {"ok": False, "error": "config not found"}
    if not config.get("sheet_id"):
        return {"ok": False, "error": "sheet_id not set"}

    columns = _load_columns(config_id)
    if not columns:
        return {"ok": False, "error": "no columns mapped"}

    try:
        rows = _fetch_sheet_values(config["sheet_id"], config.get("sheet_tab"))
    except Exception as e:
        _mark_error(config_id, f"fetch failed: {e}")
        return {"ok": False, "error": f"fetch failed: {e}"}

    if len(rows) < 2:
        _mark_sync(config_id, 0, None)
        return {"ok": True, "inserted": 0, "total_rows": max(0, len(rows) - 1), "assigned": 0}

    header_row = [(h or "").strip() for h in rows[0]]

    # Fetch existing row keys for this config so we only insert new ones
    existing = sb.table("ad_leads").select("id, source_row_key") \
        .eq("config_id", config_id).execute()
    known: Dict[str, str] = {r["source_row_key"]: r["id"] for r in (existing.data or [])}

    to_insert: List[dict] = []
    to_update: List[Tuple[str, dict]] = []
    to_delete: List[str] = []
    seen_batch = set()
    for r in rows[1:]:
        if _is_test_lead_row(r):
            # If a test-lead row previously slipped through and is in the DB,
            # its hash matches the current row — delete that DB record.
            stale_key = _row_key(r, header_row, None)
            if stale_key in known:
                to_delete.append(known[stale_key])
            continue
        lead = _build_lead_from_row(r, header_row, columns, config)
        if not lead:
            continue
        k = lead["source_row_key"]
        if k in seen_batch:
            continue
        seen_batch.add(k)
        if k in known:
            # Existing row — refresh mapping-derived fields so a remap
            # (e.g. marking a column as `phone`) fixes leads that were
            # synced before the mapping existed. Never touch assignment,
            # status, or timestamps that belong to the agent workflow.
            to_update.append((known[k], _strip_adset({
                "data": lead["data"],
                "phone_primary": lead["phone_primary"],
                "phones": lead["phones"],
                "full_name": lead["full_name"],
                "ad_name": lead["ad_name"],
                "adset_name": lead["adset_name"],
                "platform": lead["platform"],
                "created_time": lead["created_time"],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })))
        else:
            # Stamp status_changed_at on fresh inserts so the admin's
            # activity-today filter picks up new leads immediately, without
            # waiting for an agent to touch them. _strip_optional_cols
            # drops the key if the DB column doesn't exist yet.
            payload = dict(lead)
            payload["status_changed_at"] = datetime.now(timezone.utc).isoformat()
            to_insert.append(_strip_optional_cols(payload))

    inserted = 0
    if to_insert:
        for i in range(0, len(to_insert), 500):
            chunk = to_insert[i:i + 500]
            try:
                res = sb.table("ad_leads").insert(chunk).execute()
                inserted += len(res.data or [])
            except Exception as e:
                _mark_error(config_id, f"insert failed: {e}")
                return {"ok": False, "error": f"insert failed: {e}", "inserted": inserted}

    updated = 0
    for lead_id, patch in to_update:
        try:
            sb.table("ad_leads").update(patch).eq("id", lead_id).execute()
            updated += 1
        except Exception as e:
            log.warning("[ad_leads] update failed for %s: %s", lead_id, e)

    deleted = 0
    if to_delete:
        try:
            sb.table("ad_leads").delete().in_("id", to_delete).execute()
            deleted = len(to_delete)
        except Exception as e:
            log.warning("[ad_leads] test-lead cleanup failed: %s", e)

    _mark_sync(config_id, len(rows) - 1, None)
    assigned = distribute_unassigned_for_scope(config["scope_type"], config["scope_id"])

    return {
        "ok": True,
        "total_rows": len(rows) - 1,
        "inserted": inserted,
        "updated": updated,
        "deleted": deleted,
        "assigned": assigned,
    }


def _mark_sync(config_id: str, row_count: int, err: Optional[str]):
    sb = get_client()
    sb.table("lead_sheet_configs").update({
        "last_synced_at": datetime.now(timezone.utc).isoformat(),
        "last_row_count": row_count,
        "last_error": err,
    }).eq("id", config_id).execute()


def _mark_error(config_id: str, err: str):
    sb = get_client()
    sb.table("lead_sheet_configs").update({
        "last_synced_at": datetime.now(timezone.utc).isoformat(),
        "last_error": err,
    }).eq("id", config_id).execute()


# ---------------------------------------------------------------------------
# Sync all enabled configs (called by the scheduler)
# ---------------------------------------------------------------------------

def sync_all_enabled() -> dict:
    sb = get_client()
    res = sb.table("lead_sheet_configs").select("id").eq("enabled", True).execute()
    configs = res.data or []
    summaries = []
    for c in configs:
        summaries.append({"config_id": c["id"], **sync_config(c["id"])})
    return {"ok": True, "configs": summaries}


# Back-compat name used by main.py scheduler startup hook
def sync_leads_from_sheet() -> dict:
    return sync_all_enabled()


# ---------------------------------------------------------------------------
# Round-robin distribution for a scope
# ---------------------------------------------------------------------------

def distribute_unassigned_for_scope(scope_type: str, scope_id: str) -> int:
    sb = get_client()
    unassigned = sb.table("ad_leads").select("id") \
        .eq("scope_type", scope_type).eq("scope_id", scope_id) \
        .is_("assigned_agent_id", "null") \
        .order("created_time").execute()
    leads = unassigned.data or []
    if not leads:
        return 0

    pool = _agent_pool_for_scope(scope_type, scope_id)
    if not pool:
        log.warning("[ad_leads] no available agents for %s/%s; %d leads stay unassigned",
                    scope_type, scope_id, len(leads))
        return 0

    assigned = 0
    pool_idx = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for lead in leads:
        agent = pool[pool_idx % len(pool)]
        pool_idx += 1

        sb.table("ad_leads").update({
            "assigned_agent_id": agent["id"],
            "original_agent_id": agent["id"],
            "assigned_at": now_iso,
            "updated_at": now_iso,
        }).eq("id", lead["id"]).execute()

        sb.table("agents").update({
            "last_distributed_at": now_iso,
        }).eq("id", agent["id"]).execute()

        assigned += 1
    return assigned


def distribute_all_unassigned() -> int:
    """Re-run distribution across every enabled config."""
    sb = get_client()
    res = sb.table("lead_sheet_configs").select("scope_type, scope_id").eq("enabled", True).execute()
    total = 0
    for c in (res.data or []):
        total += distribute_unassigned_for_scope(c["scope_type"], c["scope_id"])
    return total
