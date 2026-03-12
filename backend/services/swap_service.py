import random
from datetime import datetime, timedelta
from services.supabase_service import get_client


def get_eligible_leads_for_swap():
    sb = get_client()
    now = datetime.utcnow().isoformat()
    result = sb.table("leads").select("*").in_("status", ["B.V", "N.R"]).eq("locked", True).eq("is_blacklisted", False).lte("swap_eligible_at", now).lt("swap_count", 3).execute()
    return result.data


def assign_swap(lead: dict):
    sb = get_client()

    # Get all active agents except original and current
    agents_result = sb.table("agents").select("id").eq("is_active", True).execute()
    all_agents = [a["id"] for a in agents_result.data]

    excluded = {lead["original_agent"], lead["current_agent"]}
    eligible_agents = [a for a in all_agents if a not in excluded]

    if not eligible_agents:
        # Archive the lead if no eligible agents
        sb.table("leads").update({"status": "archived"}).eq("id", lead["id"]).execute()
        return None

    new_agent = random.choice(eligible_agents)
    new_swap_count = lead["swap_count"] + 1

    if new_swap_count >= 3:
        # Third swap — archive after this
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

    # Log history
    sb.table("lead_history").insert({
        "lead_id": lead["id"],
        "agent_id": new_agent,
        "action": "swapped",
        "status_before": lead["status"],
        "status_after": lead["status"],
        "note": f"Swap #{new_swap_count}"
    }).execute()

    return new_agent


def redistribute_agent_leads(agent_id: str):
    sb = get_client()

    # Get all active leads of fired agent
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
