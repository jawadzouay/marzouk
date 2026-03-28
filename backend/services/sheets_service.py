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

    # Support raw JSON content OR a file path
    if creds_json.startswith("{"):
        try:
            creds_info = json.loads(creds_json)
        except json.JSONDecodeError as e:
            raise Exception(f"GOOGLE_CREDENTIALS_JSON يحتوي على JSON غير صالح: {e}")
    else:
        # Treat as file path
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
        headers = [["Date", "Agent", "#", "Phone", "Name", "Level", "City", "Status", "Swap Count", "Submitted At"]]
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{tab_name}!A1",
            valueInputOption="RAW",
            body={"values": headers}
        ).execute()


def append_leads_to_sheet(agent_name: str, leads: list, submission_date: str):
    sheet_id = get_sheet_id()
    if not sheet_id:
        return
    service = get_sheets_service()
    ensure_sheet_tab(service, sheet_id, agent_name)
    ensure_sheet_tab(service, sheet_id, "All Leads")

    rows = []
    for i, lead in enumerate(leads, start=1):
        rows.append([
            submission_date, agent_name, i,
            lead.get("phone", ""), lead.get("name", ""), lead.get("level", ""),
            lead.get("city", ""), lead.get("status", ""),
            lead.get("swap_count", 0), lead.get("submitted_at", ""),
        ])

    service.spreadsheets().values().append(
        spreadsheetId=sheet_id, range=f"{agent_name}!A1",
        valueInputOption="RAW", insertDataOption="INSERT_ROWS",
        body={"values": rows}
    ).execute()
    service.spreadsheets().values().append(
        spreadsheetId=sheet_id, range="All Leads!A1",
        valueInputOption="RAW", insertDataOption="INSERT_ROWS",
        body={"values": rows}
    ).execute()


def append_to_archive(agent_name: str, leads: list, submission_date: str):
    sheet_id = get_sheet_id()
    if not sheet_id:
        return
    service = get_sheets_service()
    ensure_sheet_tab(service, sheet_id, "Archive")

    rows = []
    for i, lead in enumerate(leads, start=1):
        rows.append([
            submission_date, agent_name, i,
            lead.get("phone", ""), lead.get("name", ""), lead.get("level", ""),
            lead.get("city", ""), lead.get("status", ""),
            lead.get("swap_count", 0), lead.get("submitted_at", ""),
        ])

    service.spreadsheets().values().append(
        spreadsheetId=sheet_id, range="Archive!A1",
        valueInputOption="RAW", insertDataOption="INSERT_ROWS",
        body={"values": rows}
    ).execute()
