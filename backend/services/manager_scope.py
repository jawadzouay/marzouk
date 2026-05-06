"""
Branch-manager scope helpers.

Managers are agents with role='manager' and a scope of:
  - 'all'     → super-admin lite (sees every branch / every agent), used
                primarily so a senior team-lead can be added without giving
                them the "ads / spend / sources / settings" surface that
                only the global admin should touch.
  - 'city'    → every branch in one city (manager_scope_id is the city UUID)
  - 'branch'  → a single branch (manager_scope_id is the branch UUID)

The JWT carries `role`, `scope_type`, `scope_id`. Admin-side endpoints
that take an optional branch_id / city_id query param now also accept
managers via `require_admin_or_manager`, and the helpers below let the
endpoint translate the manager's scope into a list of branch_ids the
caller is allowed to see — overriding any caller-supplied filter.
"""
from typing import Optional, Tuple, List, Dict, Any
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt
import os

JWT_SECRET = os.getenv("JWT_SECRET")
ALGORITHM = "HS256"
_security = HTTPBearer()


def _decode(credentials: HTTPAuthorizationCredentials) -> Dict[str, Any]:
    try:
        return jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[ALGORITHM])
    except Exception:
        raise HTTPException(status_code=401, detail="Unauthorized")


def require_admin_or_manager(
    credentials: HTTPAuthorizationCredentials = Depends(_security),
) -> Dict[str, Any]:
    """Lets the global admin OR any branch-manager through. Endpoints
    using this dep MUST then call resolve_manager_branch_scope() to apply
    the data-narrowing filter when the caller is a manager."""
    payload = _decode(credentials)
    role = payload.get("role")
    if role not in ("admin", "manager"):
        raise HTTPException(status_code=403, detail="Admin or manager only")
    return payload


def require_admin_only(
    credentials: HTTPAuthorizationCredentials = Depends(_security),
) -> Dict[str, Any]:
    """Hard 403 for managers — used on endpoints the manager must not
    touch (ad spend, lead-source CRUD, sync-all, ad-quality, settings)."""
    payload = _decode(credentials)
    if payload.get("role") != "admin":
        raise HTTPException(status_code=403, detail="هذه الصفحة للمدير العام فقط")
    return payload


def manager_scope(payload: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Returns (scope_type, scope_id) when caller is a manager with a
    narrower-than-'all' scope, otherwise (None, None) which means
    "no constraint imposed by role"."""
    if payload.get("role") != "manager":
        return None, None
    st = (payload.get("scope_type") or "branch")
    if st == "all":
        return None, None
    return st, payload.get("scope_id")


def resolve_manager_branch_ids(sb, payload: Dict[str, Any]) -> Optional[List[str]]:
    """Returns a list of branch_ids the caller is allowed to see.
      - None  → no constraint (admin, or manager with scope='all')
      - []    → manager scoped to a branch/city that no longer exists
      - [...] → the concrete branch_ids the manager covers
    """
    st, sid = manager_scope(payload)
    if st is None:
        return None
    if st == "branch":
        return [sid] if sid else []
    if st == "city":
        if not sid:
            return []
        c = sb.table("cities").select("name").eq("id", sid).execute()
        if not c.data:
            return []
        brs = sb.table("branches").select("id").eq("city", c.data[0]["name"]).execute()
        return [b["id"] for b in (brs.data or [])]
    return None


def assert_agent_in_scope(sb, payload: Dict[str, Any], agent_id: str) -> None:
    """Raise 403 if a manager is touching an agent outside their scope.
    Admins always pass. Managers with scope='all' always pass."""
    if payload.get("role") != "manager":
        return
    branch_ids = resolve_manager_branch_ids(sb, payload)
    if branch_ids is None:
        return  # scope='all' = no narrowing
    a = sb.table("agents").select("branch_id").eq("id", agent_id).execute()
    if not a.data:
        raise HTTPException(404, "الوكيل غير موجود")
    if a.data[0].get("branch_id") not in branch_ids:
        raise HTTPException(403, "هذا الوكيل خارج نطاق إدارتك")
