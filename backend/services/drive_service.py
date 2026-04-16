from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from dotenv import load_dotenv
import os
import json
import io
import logging

load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/drive"
]


def _parse_credentials():
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
    if not creds_json:
        raise Exception("GOOGLE_CREDENTIALS_JSON not set")

    if not creds_json.startswith("{"):
        brace_pos = creds_json.find("{")
        if brace_pos != -1:
            creds_json = creds_json[brace_pos:]

    if creds_json.startswith("{"):
        return json.loads(creds_json)
    else:
        with open(creds_json, "r") as f:
            return json.load(f)


def get_drive_service():
    creds_info = _parse_credentials()
    creds = service_account.Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)


def get_parent_folder_id() -> str:
    """Get optional parent folder from settings table."""
    try:
        from services.supabase_service import get_client
        row = get_client().table("settings").select("value").eq("key", "drive_parent_folder_id").execute()
        if row.data:
            return row.data[0]["value"]
    except Exception:
        pass
    return ""


def create_agent_folder(agent_name: str, branch_name: str = "") -> str:
    """Create a Google Drive folder for an agent. Returns folder_id."""
    service = get_drive_service()
    folder_name = f"{agent_name} ({branch_name})" if branch_name else agent_name

    metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder"
    }

    parent_id = get_parent_folder_id()
    if parent_id:
        metadata["parents"] = [parent_id]

    folder = service.files().create(body=metadata, fields="id").execute()
    logging.info(f"[DRIVE] Created folder '{folder_name}' → {folder['id']}")
    return folder["id"]


def upload_photo(folder_id: str, file_bytes: bytes, filename: str, mime_type: str = "image/jpeg") -> str:
    """Upload a photo to the agent's Drive folder. Returns file_id."""
    service = get_drive_service()

    metadata = {
        "name": filename,
        "parents": [folder_id]
    }

    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mime_type, resumable=False)
    uploaded = service.files().create(body=metadata, media_body=media, fields="id").execute()
    logging.info(f"[DRIVE] Uploaded '{filename}' → {uploaded['id']}")
    return uploaded["id"]
