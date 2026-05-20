"""
Google Drive client — searches project folders by name and extracts file content.

Setup: requires a Google Service Account with access to the KSE GBS Drive folder.
See SETUP_GUIDE.md → Частина 5 for instructions.
"""
from __future__ import annotations

import os
import io
import logging
import re
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Root folder that contains all project subfolders
# https://drive.google.com/drive/folders/1mmp2OLYEUn1Og0OEQEkK1wO4wZXeVveC
ROOT_FOLDER_ID = "1mmp2OLYEUn1Og0OEQEkK1wO4wZXeVveC"


def _get_service():
    """Build and return a Google Drive API service object."""
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_PATH", "google_credentials.json")
        creds = service_account.Credentials.from_service_account_file(
            creds_path,
            scopes=["https://www.googleapis.com/auth/drive.readonly"],
        )
        return build("drive", "v3", credentials=creds, cache_discovery=False)
    except Exception as e:
        logger.error(f"Failed to build Drive service: {e}")
        return None


def _search_folders(service, query: str, page_size: int = 20) -> list[dict]:
    """Helper: run a Drive files.list query and return the files list."""
    try:
        results = service.files().list(
            q=query,
            fields="files(id, name, modifiedTime, parents)",
            orderBy="modifiedTime desc",
            pageSize=page_size,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        return results.get("files", [])
    except Exception as e:
        logger.error(f"Drive search query failed: {e}")
        return []


def find_project_folder(project_name: str) -> dict | None:
    """
    Search for a project folder by name anywhere in Drive accessible to the service account.
    Strategy:
      1. Exact-match search across all accessible Drive (no parent restriction)
      2. If nothing found — word-by-word fallback
    If multiple folders match, returns the one modified most recently.

    Returns:
        {"id": "...", "name": "...", "url": "https://drive.google.com/...", "modified": "..."}
        or None if not found.
    """
    service = _get_service()
    if not service:
        logger.error("Drive service unavailable — check GOOGLE_SERVICE_ACCOUNT_PATH and credentials")
        return None

    safe_name = project_name.replace("'", "\\'")[:80]

    # ── Step 1: search by full name across all Drive ───────────────────────────
    query_global = (
        f"mimeType = 'application/vnd.google-apps.folder' "
        f"and name contains '{safe_name}' "
        f"and trashed = false"
    )
    folders = _search_folders(service, query_global, page_size=20)
    logger.info(f"Drive global search '{project_name}': {len(folders)} results")

    # ── Step 2: word-by-word fallback ─────────────────────────────────────────
    if not folders:
        words = [w for w in project_name.split() if len(w) > 3]
        for word in words[:3]:
            safe_word = word.replace("'", "\\'")
            query_word = (
                f"mimeType = 'application/vnd.google-apps.folder' "
                f"and name contains '{safe_word}' "
                f"and trashed = false"
            )
            folders = _search_folders(service, query_word, page_size=10)
            logger.info(f"Drive word search '{word}': {len(folders)} results")
            if folders:
                break

    if not folders:
        logger.info(f"No Drive folder found for: {project_name}")
        return None

    # Filter: prefer folders inside ROOT_FOLDER_ID tree (if any match)
    root_matches = [f for f in folders if ROOT_FOLDER_ID in (f.get("parents") or [])]
    chosen_list = root_matches if root_matches else folders

    best = chosen_list[0]
    return {
        "id": best["id"],
        "name": best["name"],
        "url": f"https://drive.google.com/drive/folders/{best['id']}",
        "modified": best.get("modifiedTime", ""),
        "all_matches": [
            {"name": f["name"], "url": f"https://drive.google.com/drive/folders/{f['id']}"}
            for f in chosen_list
        ],
    }


def list_files_in_folder(folder_id: str) -> list[dict]:
    """List files in a Drive folder (non-recursive). Works for both My Drive and Shared Drives."""
    service = _get_service()
    if not service:
        return []
    try:
        results = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="files(id, name, mimeType, modifiedTime, size)",
            orderBy="modifiedTime desc",
            pageSize=20,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files = results.get("files", [])
        logger.info(f"list_files_in_folder({folder_id}): {len(files)} files found")
        return files
    except Exception as e:
        logger.error(f"list_files_in_folder failed: {e}")
        return []


def read_file_text(file_id: str, mime_type: str, file_name: str = "") -> str | None:
    """
    Download a file from Drive and extract its text content.
    Uses file_parser for DOCX/PDF so tables (e.g. curriculum modules) are preserved.
    Supports: Google Docs, Google Sheets, PDF, DOCX, plain text.
    """
    from file_parser import parse_file

    service = _get_service()
    if not service:
        return None

    try:
        # Google Docs → export as DOCX to preserve table structure
        if mime_type == "application/vnd.google-apps.document":
            try:
                content = service.files().export(
                    fileId=file_id,
                    mimeType="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ).execute()
                return parse_file(
                    content, file_name or "document.docx",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            except Exception:
                # Fallback: plain text export
                content = service.files().export(
                    fileId=file_id, mimeType="text/plain"
                ).execute()
                return content.decode("utf-8", errors="ignore")[:12000]

        # Google Sheets → export as CSV
        elif mime_type == "application/vnd.google-apps.spreadsheet":
            content = service.files().export(
                fileId=file_id, mimeType="text/csv"
            ).execute()
            return content.decode("utf-8", errors="ignore")[:12000]

        # PDF, DOCX, plain text — download and parse via file_parser
        else:
            content = service.files().get_media(fileId=file_id).execute()
            return parse_file(content, file_name, mime_type)

    except Exception as e:
        logger.error(f"read_file_text failed for {file_id} ({file_name}): {e}")
        return None


def find_file_in_folder(folder_id: str, name_keyword: str) -> dict | None:
    """
    Search for a file whose name contains name_keyword (case-insensitive).
    Looks in the folder and one level of subfolders.
    """
    service = _get_service()
    if not service:
        return None

    def _search(fid: str) -> dict | None:
        try:
            results = service.files().list(
                q=f"'{fid}' in parents and trashed = false",
                fields="files(id, name, mimeType)",
                pageSize=30,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            for f in results.get("files", []):
                if name_keyword.lower() in f["name"].lower():
                    return f
            # Check subfolders one level deep
            for f in results.get("files", []):
                if f["mimeType"] == "application/vnd.google-apps.folder":
                    found = _search(f["id"])
                    if found:
                        return found
        except Exception as e:
            logger.error(f"find_file_in_folder search failed: {e}")
        return None

    return _search(folder_id)


def read_participants_count(folder_id: str) -> int | None:
    """
    Find 'Список учасників' (or similar) in a Drive folder and count participants.
    For Excel: counts data rows (excluding header).
    Returns integer count or None if not found / unreadable.
    """
    service = _get_service()
    if not service:
        return None

    keywords = ["учасник", "participants", "participant", "список"]
    file_info = None
    for kw in keywords:
        file_info = find_file_in_folder(folder_id, kw)
        if file_info:
            break

    if not file_info:
        logger.info(f"No participants file found in folder {folder_id}")
        return None

    logger.info(f"Found participants file: {file_info['name']}")

    try:
        mime = file_info["mimeType"]
        fid = file_info["id"]

        # Google Sheets → export as CSV and count rows
        if mime == "application/vnd.google-apps.spreadsheet":
            content = service.files().export(fileId=fid, mimeType="text/csv").execute()
            lines = [l for l in content.decode("utf-8", errors="ignore").splitlines()
                     if l.strip()]
            return max(0, len(lines) - 1)  # minus header

        # Excel → export as CSV via Google Sheets conversion
        elif mime in (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-excel",
        ):
            try:
                import openpyxl, io
                content = service.files().get_media(fileId=fid).execute()
                wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True)
                ws = wb.active
                rows = [r for r in ws.iter_rows(values_only=True)
                        if any(c is not None for c in r)]
                return max(0, len(rows) - 1)  # minus header
            except ImportError:
                # Fallback: try CSV export
                try:
                    content = service.files().export(
                        fileId=fid, mimeType="text/csv"
                    ).execute()
                    lines = [l for l in content.decode("utf-8", errors="ignore").splitlines()
                             if l.strip()]
                    return max(0, len(lines) - 1)
                except Exception:
                    return None

    except Exception as e:
        logger.error(f"read_participants_count failed: {e}")

    return None


def list_files_in_subfolder(folder_id: str, subfolder_keyword: str) -> list[dict]:
    """
    Find a subfolder by keyword in its name, then list its files.
    Returns [] if subfolder not found.
    """
    service = _get_service()
    if not service:
        return []

    try:
        results = service.files().list(
            q=(
                f"'{folder_id}' in parents "
                f"and mimeType = 'application/vnd.google-apps.folder' "
                f"and trashed = false"
            ),
            fields="files(id, name)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()

        for f in results.get("files", []):
            if subfolder_keyword.lower() in f["name"].lower():
                logger.info(f"Found subfolder '{f['name']}' for keyword '{subfolder_keyword}'")
                return list_files_in_folder(f["id"])
    except Exception as e:
        logger.error(f"list_files_in_subfolder failed: {e}")

    return []


def read_project_folder_contents(project_name: str) -> dict:
    """
    High-level: find the project folder by name and extract structured data.

    Automatically:
    - Reads НП/ОП from subfolder 'Програма' (prioritised)
    - Counts participants from 'Список учасників' file
    - Returns curriculum text with full table structure (for module extraction)

    Returns dict with folder URL, file contents, participant_count, and new_files list.
    """
    folder = find_project_folder(project_name)
    if not folder:
        return {"found": False, "project_name": project_name}

    folder_id = folder["id"]
    result: dict = {
        "found": True,
        "folder_name": folder["name"],
        "folder_url": folder["url"],
        "all_matches": folder.get("all_matches", []),
        "files": [],
        "participant_count": None,
    }

    # ── 1. Try to count participants automatically ────────────────────────────
    count = read_participants_count(folder_id)
    if count is not None:
        result["participant_count"] = count
        logger.info(f"Auto-detected participants: {count}")

    # ── 2. Read files from 'Програма' subfolder first (НП, ОП, modules) ──────
    program_files = list_files_in_subfolder(folder_id, "програма")
    if not program_files:
        program_files = list_files_in_subfolder(folder_id, "program")

    # ── 3. Also read top-level files ──────────────────────────────────────────
    top_files = list_files_in_folder(folder_id)

    # Merge: Програма files first, then top-level (deduplicate by id)
    seen_ids: set[str] = set()
    all_files = []
    for f in program_files + top_files:
        if f["id"] not in seen_ids:
            seen_ids.add(f["id"])
            all_files.append(f)

    if not all_files:
        result["note"] = "Папка знайдена, але файлів усередині немає."
        return result

    # ── 4. Prioritise curriculum documents ───────────────────────────────────
    CURRICULUM_MIMES = {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
        "application/pdf",
        "application/vnd.google-apps.document",
    }
    # Skip BizDev and participants files — they're handled separately
    SKIP_KEYWORDS = ["bizdev", "biz_dev", "пропозиція", "презентація_продаж"]

    def should_skip(name: str) -> bool:
        return any(kw in name.lower() for kw in SKIP_KEYWORDS)

    priority = [f for f in all_files
                if f["mimeType"] in CURRICULUM_MIMES and not should_skip(f["name"])]
    rest = [f for f in all_files
            if f["mimeType"] not in CURRICULUM_MIMES and not should_skip(f["name"])]
    ordered = priority + rest

    file_contents = []
    for f in ordered[:6]:
        text = read_file_text(f["id"], f["mimeType"], f["name"])
        if text:
            file_contents.append({
                "name": f["name"],
                "mime": f["mimeType"],
                "text": text[:12000],
            })

    result["files"] = file_contents
    return result


def get_new_files_in_folder(folder_id: str, known_file_ids: list[str]) -> list[dict]:
    """
    Returns files in folder that are NOT in known_file_ids.
    Used by the daily scheduler to detect new uploads.
    Checks top-level + one subfolder level.
    """
    service = _get_service()
    if not service:
        return []

    known = set(known_file_ids)
    all_files = list_files_in_folder(folder_id)

    # Also scan subfolders one level deep
    for f in list(all_files):
        if f["mimeType"] == "application/vnd.google-apps.folder":
            all_files.extend(list_files_in_folder(f["id"]))

    new_files = [f for f in all_files if f["id"] not in known]
    return new_files


# ── Tool definitions for Claude agent ─────────────────────────────────────────

DRIVE_TOOL_DEFINITIONS = [
    {
        "name": "find_project_folder",
        "description": (
            "Search for a project folder on Google Drive by project name. "
            "Searches inside the KSE GBS projects root folder using 'contains' match. "
            "If multiple folders match, returns the most recently modified. "
            "Use the returned URL to fill 'Syllabus / Посилання на папку проєкту' in Notion."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_name": {
                    "type": "string",
                    "description": "Project name to search for (partial match works)"
                }
            },
            "required": ["project_name"]
        }
    },
    {
        "name": "read_project_folder_contents",
        "description": (
            "Find the project folder on Drive and read all files inside it. "
            "Use this when manager shares a file or when you need to extract "
            "project data (dates, participants, modules, goals, KPI) from documents. "
            "Returns folder URL + text content of readable files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_name": {
                    "type": "string",
                    "description": "Project name to find on Drive"
                }
            },
            "required": ["project_name"]
        }
    },
]


def execute_drive_tool(name: str, inputs: dict) -> str:
    import json
    try:
        if name == "find_project_folder":
            result = find_project_folder(**inputs)
        elif name == "read_project_folder_contents":
            result = read_project_folder_contents(**inputs)
        else:
            result = {"error": f"Unknown drive tool: {name}"}
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})
