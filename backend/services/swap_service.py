import random
from datetime import datetime, timedelta
from services.supabase_service import get_client


def get_swap_level():
    sb = get_client()
    try:
        res = sb.table("swap_settings").select("swap_level").eq("id", 1).execute()
        return res.data[0]["swap_level"] if res.data else 1
    except Exception:
        return 1


def set_swap_level(level: int):
    sb = get_client()
    sb.table("swap_settings").upsert({"id": 1, "swap_level": level}).execute()


def get_agent_branch_city(agent_id: str, sb):
    """Returns (branch_id, city) for an agent."""
    agent = sb.table("agents").select("branch_id").eq("id", agent_id).execute().data
    if not agent or not agent[0].get("branch_id"):
        return (None, None)
    branch_id = agent[0]["branch_id"]
    branch = sb.table("branches").select("id,city").eq("id", branch_id).execute().data
    if not branch:
        return (branch_id, None)
    return (branch_id, branch[0].get("city"))


def get_eligible_agents_for_level(lead: dict, level: int, sb) -> list:
    """Returns list of eligible agent IDs based on swap level."""
    excluded = {lead.get("original_agent"), lead.get("current_agent")}

    if level == 1:
        # All active agents globally
        agents = sb.table("agents").select("id").eq("is_active", True).execute().data
        return [a["id"] for a in agents if a["id"] not in excluded]

    elif level == 2:
        # Same city only
        orig_branch_id, orig_city = get_agent_branch_city(lead.get("original_agent"), sb)
        if not orig_city:
            # Fallback to global if city unknown
            agents = sb.table("agents").select("id").eq("is_active", True).execute().data
            return [a["id"] for a in agents if a["id"] not in excluded]
        branches_in_city = sb.table("branches").select("id").eq("city", orig_city).execute().data
        branch_ids = [b["id"] for b in branches_in_city]
        if not branch_ids:
            return []
        agents = sb.table("agents").select("id").eq("is_active", True).in_("branch_id", branch_ids).execute().data
        return [a["id"] for a in agents if a["id"] not in excluded]

    elif level == 3:
        # Same branch only
        orig_branch_id, _ = get_agent_branch_city(lead.get("original_agent"), sb)
        if not orig_branch_id:
            # Fallback to global if branch unknown
            agents = sb.table("agents").select("id").eq("is_active", True).execute().data
            return [a["id"] for a in agents if a["id"] not in excluded]
        agents = sb.table("agents").select("id").eq("is_active", True).eq("branch_id", orig_branch_id).execute().data
        return [a["id"] for a in agents if a["id"] not in excluded]

    elif level == 4:
        # Manual — no auto-assignment
        return []

    return []


def get_eligible_leads_for_swap():
    sb = get_client()
    now = datetime.utcnow().isoformat()
    result = sb.table("leads").select("*").in_("status", ["B.V", "N.R"]).eq("locked", True).eq("is_blacklisted", False).lte("swap_eligible_at", now).lt("swap_count", 3).execute()
    return result.data


def assign_swap(lead: dict, level: int = None):
    sb = get_client()

    if level is None:
        level = get_swap_level()

    eligible_agents = get_eligible_agents_for_level(lead, level, sb)

    if level == 4 or not eligible_agents:
        if not eligible_agents and level != 4:
            # Archive if truly no agents available
            sb.table("leads").update({"status": "archived"}).eq("id", lead["id"]).execute()
        return None

    new_agent = random.choice(eligible_agents)
    new_swap_count = lead["swap_count"] + 1

    if new_swap_count >= 3:
        sb.table("leads").update({
            "current_agent": new_agent,
            "swap_count": new_swap_count,
            "swap_eligible_at": None
        }).eq("id", lead["id"]).execute()
    else:
        next_eligible = (datetime.utcnow() + timedelta(days=4)).isoformat()
        sb.table("leads").update({
            "current_agent": new_agent,
            "swap_count": new_swap_count,
            "swap_eligible_at": next_eligible
        }).eq("id", lead["id"]).execute()

    sb.table("lead_history").insert({
        "lead_id": lead["id"],
        "agent_id": new_agent,
        "action": "swapped",
        "status_before": lead["status"],
        "status_after": lead["status"],
        "note": f"Swap #{new_swap_count} — Level {level}"
    }).execute()

    return new_agent


def manual_assign_swap(lead_id: str, target_agent_id: str):
    """Admin manually assigns a lead to a specific agent."""
    sb = get_client()
    lead = sb.table("leads").select("*").eq("id", lead_id).execute().data
    if not lead:
        return False
    lead = lead[0]
    new_swap_count = lead["swap_count"] + 1

    updates = {"current_agent": target_agent_id, "swap_count": new_swap_count}
    if new_swap_count < 3:
        updates["swap_eligible_at"] = (datetime.utcnow() + timedelta(days=4)).isoformat()
    else:
        updates["swap_eligible_at"] = None

    sb.table("leads").update(updates).eq("id", lead_id).execute()
    sb.table("lead_history").insert({
        "lead_id": lead_id,
        "agent_id": target_agent_id,
        "action": "swapped",
        "status_before": lead["status"],
        "status_after": lead["status"],
        "note": f"Swap #{new_swap_count} — Manual by admin"
    }).execute()
    return True


def redistribute_agent_leads(agent_id: str):
    sb = get_client()
    leads_result = sb.table("leads").select("*").eq("current_agent", agent_id).in_("status", ["B.V", "N.R", "RDV"]).execute()
    leads = leads_result.data

    agents_result = sb.table("agents").select("id").eq("is_active", True).execute()
    active_agents = [a["id"] for a in agents_result.data if a["id"] != agent_id]

    if not active_agents:
        return

    for lead in leads:
        new_agent = random.choice(active_agents)
        sb.table("leads").update({"current_agent": new_agent}).eq("id", lead["id"]).execute()
        sb.table("lead_history").insert({
            "lead_id": lead["id"],
            "agent_id": new_agent,
            "action": "swapped",
            "status_before": lead["status"],
            "status_after": lead["status"],
            "note": "Agent fired — auto redistributed"
        }).execute()
