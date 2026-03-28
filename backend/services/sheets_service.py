from google.oauth2 import service_account
from googleapiclient.discovery import build
from dotenv import load_dotenv
import os
import json

load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

# Column layout (1-indexed for Sheets API, 0-indexed for list)
# A=Date  B=Agent  C=Branch  D=#  E=Phone  F=Name  G=Level  H=City
# I=Status  J=SwapCount  K=SubmittedAt  L=Note  M=LeadID
HEADERS = ["Date", "Agent", "Branch", "#", "Phone", "Name", "Level", "City",
           "Status", "Swap Count", "Submitted At", "Note", "Lead ID"]
COL_PHONE   = "E"   # column E
COL_NAME    = "F"   # column F
COL_LEVEL   = "G"   # column G
COL_CITY    = "H"   # column H
COL_STATUS  = "I"   # column I
COL_NOTE    = "L"   # column L
COL_LEAD_ID = "M"   # column M (used for lookups)
LEAD_ID_INDEX = 12  # 0-indexed position of Lead ID in row list


def make_tab_name(agent_name: str, branch_name: str = "") -> str:
    """Returns a unique tab name. Includes branch when provided to avoid name collisions."""
    if branch_name and branch_name.strip():
        return f"{agent_name} ({branch_name.strip()})"
    return agent_name


def get_sheet_id() -> str:
    """DB setting takes precedence over env var so admin can change it from dashboard."""
    try:
        from services.supabase_service import get_client
        row = get_client().table("settings").select("value").eq("key", "google_sheet_id").execute()
        if row.data:
            return row.data[0]["value"]
    except Exception:
        pass
    return os.getenv("GOOGLE_SHEET_ID", "")


def get_sheets_service():
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
    if not creds_json:
        raise Exception("GOOGLE_CREDENTIALS_JSON غير مضبوط في Railway — يرجى لصق محتوى ملف JSON كاملاً")

    # Strip any "Name: ... Value: " prefix that appears when copying from Railway UI
    if not creds_json.startswith("{"):
        brace_pos = creds_json.find("{")
        if brace_pos != -1:
            creds_json = creds_json[brace_pos:]

    if creds_json.startswith("{"):
        try:
            creds_info = json.loads(creds_json)
        except json.JSONDecodeError as e:
            raise Exception(f"GOOGLE_CREDENTIALS_JSON يحتوي على JSON غير صالح: {e}")
    else:
        try:
            with open(creds_json, "r") as f:
                creds_info = json.load(f)
        except FileNotFoundError:
            raise Exception(f"ملف بيانات الاعتماد غير موجود: {creds_json}")
        except json.JSONDecodeError as e:
            raise Exception(f"خطأ في ملف بيانات الاعتماد: {e}")

    creds = service_account.Credentials.from_service_account_info(
        creds_info, scopes=SCOPES
    )
    return build("sheets", "v4", credentials=creds)


def ensure_sheet_tab(service, sheet_id: str, tab_name: str):
    spreadsheet = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    sheets = [s["properties"]["title"] for s in spreadsheet["sheets"]]

    if tab_name not in sheets:
        body = {"requests": [{"addSheet": {"properties": {"title": tab_name}}}]}
        service.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body=body).execute()
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"'{tab_name}'!A1",
            valueInputOption="RAW",
            body={"values": [HEADERS]}
        ).execute()


def append_leads_to_sheet(agent_name: str, leads: list, submission_date: str, branch_name: str = ""):
    sheet_id = get_sheet_id()
    if not sheet_id:
        return
    service = get_sheets_service()
    tab_name = make_tab_name(agent_name, branch_name)
    ensure_sheet_tab(service, sheet_id, tab_name)
    ensure_sheet_tab(service, sheet_id, "All Leads")

    rows = []
    for i, lead in enumerate(leads, start=1):
        rows.append([
            submission_date, agent_name, branch_name or "", i,
            lead.get("phone", ""),  lead.get("name", ""),  lead.get("level", ""),
            lead.get("city", ""),   lead.get("status", ""),
            lead.get("swap_count", 0), lead.get("submitted_at", ""),
            lead.get("note", "") or "",
            lead.get("id", ""),
        ])

    service.spreadsheets().values().append(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A1",
        valueInputOption="RAW", insertDataOption="INSERT_ROWS",
        body={"values": rows}
    ).execute()
    service.spreadsheets().values().append(
        spreadsheetId=sheet_id, range="'All Leads'!A1",
        valueInputOption="RAW", insertDataOption="INSERT_ROWS",
        body={"values": rows}
    ).execute()


def update_lead_in_sheet(agent_name: str, lead_id: str, new_status: str = None, note: str = None, branch_name: str = "",
                         new_name: str = None, new_phone: str = None, new_level: str = None, new_city: str = None):
    """Find lead rows by Lead ID (col M) in agent tab + All Leads and update Status/Note."""
    sheet_id = get_sheet_id()
    if not sheet_id:
        return
    if new_status is None and note is None and new_name is None and new_phone is None and new_level is None and new_city is None:
        return

    service = get_sheets_service()
    tab_name = make_tab_name(agent_name, branch_name)
    # Also try the plain agent name tab for backwards compatibility with rows written before branch support
    tabs_to_try = list(dict.fromkeys([tab_name, agent_name, "All Leads"]))

    for tab in tabs_to_try:
        try:
            result = service.spreadsheets().values().get(
                spreadsheetId=sheet_id,
                range=f"'{tab}'!A:M"
            ).execute()
            rows = result.get("values", [])
        except Exception:
            continue

        for i, row in enumerate(rows):
            # Lead ID is column M (index 12); fall back to old index 11 for pre-migration rows
            found = (len(row) > LEAD_ID_INDEX and row[LEAD_ID_INDEX] == lead_id) or \
                    (len(row) > 11 and len(row) <= LEAD_ID_INDEX and row[11] == lead_id)
            if found:
                row_num = i + 1  # Sheets rows are 1-indexed
                updates = []
                if new_status is not None:
                    updates.append({
                        "range": f"'{tab}'!{COL_STATUS}{row_num}",
                        "values": [[new_status]]
                    })
                if note is not None:
                    updates.append({
                        "range": f"'{tab}'!{COL_NOTE}{row_num}",
                        "values": [[note]]
                    })
                if new_name is not None:
                    updates.append({"range": f"'{tab}'!{COL_NAME}{row_num}", "values": [[new_name]]})
                if new_phone is not None:
                    updates.append({"range": f"'{tab}'!{COL_PHONE}{row_num}", "values": [[new_phone]]})
                if new_level is not None:
                    updates.append({"range": f"'{tab}'!{COL_LEVEL}{row_num}", "values": [[new_level]]})
                if new_city is not None:
                    updates.append({"range": f"'{tab}'!{COL_CITY}{row_num}", "values": [[new_city]]})
                if updates:
                    try:
                        service.spreadsheets().values().batchUpdate(
                            spreadsheetId=sheet_id,
                            body={"valueInputOption": "RAW", "data": updates}
                        ).execute()
                    except Exception:
                        pass
                break  # Found and updated — stop searching this tab


def append_to_archive(agent_name: str, leads: list, submission_date: str, branch_name: str = ""):
    sheet_id = get_sheet_id()
    if not sheet_id:
        return
    service = get_sheets_service()
    ensure_sheet_tab(service, sheet_id, "Archive")

    rows = []
    for i, lead in enumerate(leads, start=1):
        rows.append([
            submission_date, agent_name, branch_name or "", i,
            lead.get("phone", ""),  lead.get("name", ""),  lead.get("level", ""),
            lead.get("city", ""),   lead.get("status", ""),
            lead.get("swap_count", 0), lead.get("submitted_at", ""),
            lead.get("note", "") or "",
            lead.get("id", ""),
        ])

    service.spreadsheets().values().append(
        spreadsheetId=sheet_id, range="'Archive'!A1",
        valueInputOption="RAW", insertDataOption="INSERT_ROWS",
        body={"values": rows}
    ).execute()
