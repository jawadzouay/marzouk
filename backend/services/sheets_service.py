from google.oauth2 import service_account
from googleapiclient.discovery import build
from dotenv import load_dotenv
import os
import json

load_dotenv()

SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]


def get_sheets_service():
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        raise Exception("GOOGLE_CREDENTIALS_JSON environment variable not set")
    creds_info = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(
        creds_info, scopes=SCOPES
    )
    return build("sheets", "v4", credentials=creds)


def ensure_sheet_tab(service, tab_name: str):
    spreadsheet = service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    sheets = [s["properties"]["title"] for s in spreadsheet["sheets"]]

    if tab_name not in sheets:
        body = {
            "requests": [{
                "addSheet": {
                    "properties": {"title": tab_name}
                }
            }]
        }
        service.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID, body=body).execute()

        # Add header row
        headers = [["Date", "Agent", "#", "Phone", "Name", "Level", "City", "Status", "Swap Count", "Submitted At"]]
        service.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f"{tab_name}!A1",
            valueInputOption="RAW",
            body={"values": headers}
        ).execute()


def append_leads_to_sheet(agent_name: str, leads: list, submission_date: str):
    service = get_sheets_service()
    ensure_sheet_tab(service, agent_name)
    ensure_sheet_tab(service, "All Leads")

    rows = []
    for i, lead in enumerate(leads, start=1):
        row = [
            submission_date,
            agent_name,
            i,
            lead.get("phone", ""),
            lead.get("name", ""),
            lead.get("level", ""),
            lead.get("city", ""),
            lead.get("status", ""),
            lead.get("swap_count", 0),
            lead.get("submitted_at", ""),
        ]
        rows.append(row)

    # Append to agent tab
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=f"{agent_name}!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": rows}
    ).execute()

    # Append to All Leads tab
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range="All Leads!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": rows}
    ).execute()


def append_to_archive(agent_name: str, leads: list, submission_date: str):
    service = get_sheets_service()
    ensure_sheet_tab(service, "Archive")

    rows = []
    for i, lead in enumerate(leads, start=1):
        row = [
            submission_date,
            agent_name,
            i,
            lead.get("phone", ""),
            lead.get("name", ""),
            lead.get("level", ""),
            lead.get("city", ""),
            lead.get("status", ""),
            lead.get("swap_count", 0),
            lead.get("submitted_at", ""),
        ]
        rows.append(row)

    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range="Archive!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": rows}
    ).execute()
