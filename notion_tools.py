"""
Notion API tool implementations.
These are the functions the Claude agent can call to read/write the Projects database.
Database ID: 173122fe-7695-80c6-96b3-000b08f81c95
"""
from __future__ import annotations

import os
import logging
from datetime import datetime
from dotenv import load_dotenv
from notion_client import Client as NotionClient
from config import (
    NOTION_DATABASE_ID, PROPERTY_TYPES, CRITICAL_FIELDS,
    DEFAULT_STATUS, FIELD_LABELS
)

load_dotenv()

logger = logging.getLogger(__name__)
notion = NotionClient(auth=os.environ["NOTION_TOKEN"])


# ── Helpers ────────────────────────────────────────────────────────────────────

def _build_property_payload(field: str, value) -> dict:
    """Convert a field name + value into a Notion API property payload."""
    prop_type = PROPERTY_TYPES.get(field)
    if not prop_type:
        raise ValueError(f"Unknown property: {field}")

    if prop_type == "title":
        return {"title": [{"text": {"content": str(value)}}]}

    elif prop_type == "rich_text":
        return {"rich_text": [{"text": {"content": str(value)}}]}

    elif prop_type == "select":
        return {"select": {"name": str(value)}}

    elif prop_type == "multi_select":
        if isinstance(value, str):
            value = [v.strip() for v in value.split(",")]
        return {"multi_select": [{"name": v} for v in value]}

    elif prop_type == "status":
        return {"status": {"name": str(value)}}

    elif prop_type == "number":
        return {"number": float(value)}

    elif prop_type == "url":
        return {"url": str(value)}

    elif prop_type == "date":
        # value can be "DD.MM.YYYY" or "DD.MM.YYYY–DD.MM.YYYY" or ISO format
        if isinstance(value, dict):
            return {"date": value}
        start, end = _parse_date_range(str(value))
        payload = {"start": start}
        if end:
            payload["end"] = end
        return {"date": payload}

    elif prop_type == "people":
        # value is a Notion user ID or list of IDs
        if isinstance(value, str):
            value = [value]
        return {"people": [{"id": uid} for uid in value]}

    elif prop_type == "files":
        # For external URLs (Google Drive links)
        if isinstance(value, str):
            return {"files": [{"name": "Folder link", "type": "external",
                               "external": {"url": value}}]}
        return {"files": []}

    elif prop_type == "relation":
        if isinstance(value, str):
            value = [value]
        return {"relation": [{"id": pid} for pid in value]}

    raise ValueError(f"Unhandled property type: {prop_type}")


def _parse_date_range(value: str) -> tuple[str, str | None]:
    """Parse date strings like '12.06.2025–14.06.2025' or '2025-06-12'."""
    import re
    # Split on dash/em-dash
    parts = re.split(r"[–\-—]", value, maxsplit=1)
    def to_iso(d: str) -> str:
        d = d.strip()
        for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d", "%d.%m.%y"):
            try:
                return datetime.strptime(d, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return d  # Return as-is if already ISO or unknown format
    start = to_iso(parts[0])
    end = to_iso(parts[1]) if len(parts) > 1 and parts[1].strip() else None
    return start, end


def _page_to_dict(page: dict) -> dict:
    """Extract useful fields from a Notion page API response."""
    props = page.get("properties", {})
    result = {"id": page["id"], "url": page.get("url", "")}

    for field, prop_type in PROPERTY_TYPES.items():
        prop = props.get(field)
        if not prop:
            continue
        try:
            if prop_type == "title":
                result[field] = "".join(t["plain_text"] for t in prop.get("title", []))
            elif prop_type == "rich_text":
                result[field] = "".join(t["plain_text"] for t in prop.get("rich_text", []))
            elif prop_type == "select":
                sel = prop.get("select")
                result[field] = sel["name"] if sel else None
            elif prop_type == "multi_select":
                result[field] = [o["name"] for o in prop.get("multi_select", [])]
            elif prop_type == "status":
                st = prop.get("status")
                result[field] = st["name"] if st else None
            elif prop_type == "number":
                result[field] = prop.get("number")
            elif prop_type == "url":
                result[field] = prop.get("url")
            elif prop_type == "date":
                d = prop.get("date")
                if d:
                    result[field] = d.get("start")
                    end = d.get("end")
                    if end:
                        result[field] = f"{result[field]}–{end}"
            elif prop_type == "people":
                result[field] = [p.get("name", p.get("id", "")) for p in prop.get("people", [])]
            elif prop_type == "files":
                files = prop.get("files", [])
                if files:
                    f = files[0]
                    result[field] = f.get("external", {}).get("url") or f.get("file", {}).get("url")
            elif prop_type == "relation":
                result[field] = [r["id"] for r in prop.get("relation", [])]
        except Exception:
            pass  # Skip fields that fail to parse

    return result


# ── Public tool functions ──────────────────────────────────────────────────────

def find_project(query: str, owner_notion_id: str | None = None) -> list[dict]:
    """
    Search for projects by name. Optionally filter by owner.
    Returns list of matching projects (id, name, status, dates, owner).
    """
    filters = []
    if query:
        filters.append({
            "property": "Назва",
            "title": {"contains": query}
        })
    if owner_notion_id:
        filters.append({
            "property": "Відповідальна особа",
            "people": {"contains": owner_notion_id}
        })

    notion_filter = (
        {"and": filters} if len(filters) > 1
        else filters[0] if filters
        else {}
    )

    kwargs = {
        "database_id": NOTION_DATABASE_ID,
        "page_size": 10,
    }
    if notion_filter:
        kwargs["filter"] = notion_filter

    response = notion.databases.query(**kwargs)
    return [_page_to_dict(p) for p in response.get("results", [])]


def get_project(project_id: str) -> dict:
    """Fetch full project data by Notion page ID."""
    page = notion.pages.retrieve(page_id=project_id)
    return _page_to_dict(page)


def create_project(fields: dict, slack_user_id: str = "") -> dict:
    """
    Create a new project in the Projects database.
    `fields` is a dict of {field_name: value}.
    Always sets Статус = Planning.
    Returns the created page data.
    """
    properties = {}

    # Always set status to Planning on creation
    fields.setdefault("Статус", DEFAULT_STATUS)

    for field, value in fields.items():
        if value is None or value == "" or value == []:
            continue
        if field not in PROPERTY_TYPES:
            continue
        try:
            properties[field] = _build_property_payload(field, value)
        except Exception as e:
            logger.warning(f"Skipping field {field}: {e}")

    page = notion.pages.create(
        parent={"database_id": NOTION_DATABASE_ID},
        properties=properties,
    )

    result = _page_to_dict(page)
    logger.info(f"Created project: {result.get('Назва')} | by Slack user: {slack_user_id}")
    return result


def update_project(project_id: str, fields: dict, slack_user_id: str = "") -> dict:
    """
    Update specific fields on an existing project.
    Does NOT allow updating critical fields without confirmation (enforced at agent level).
    Returns updated page data.
    """
    properties = {}
    for field, value in fields.items():
        if value is None or value == "" or value == []:
            continue
        if field not in PROPERTY_TYPES:
            continue
        try:
            properties[field] = _build_property_payload(field, value)
        except Exception as e:
            logger.warning(f"Skipping field {field}: {e}")

    page = notion.pages.update(page_id=project_id, properties=properties)
    result = _page_to_dict(page)
    logger.info(
        f"Updated project {project_id} | fields: {list(fields.keys())} | by: {slack_user_id}"
    )
    return result


def get_missing_fields(project_id: str, project_type: str) -> dict:
    """
    Check which required fields are missing for a given project.
    Returns {"required_missing": [...], "recommended_missing": [...]}.
    """
    from schemas import get_missing_required, get_missing_recommended
    project = get_project(project_id)
    return {
        "required_missing": get_missing_required(project, project_type),
        "recommended_missing": get_missing_recommended(project, project_type),
    }


def get_projects_with_missing_fields(owner_notion_id: str) -> list[dict]:
    """
    Get all active projects owned by this person that have missing required fields.
    Used for weekly reminder digest.
    """
    from schemas import get_missing_required

    response = notion.databases.query(
        database_id=NOTION_DATABASE_ID,
        filter={
            "and": [
                {
                    "property": "Відповідальна особа",
                    "people": {"contains": owner_notion_id}
                },
                {
                    "property": "Статус",
                    "status": {"does_not_equal": "Done"}
                },
                {
                    "property": "Статус",
                    "status": {"does_not_equal": "Cancelled"}
                },
            ]
        },
        page_size=25,
    )

    results = []
    for page in response.get("results", []):
        data = _page_to_dict(page)
        project_type = data.get("Тип проєкту", "")
        missing = get_missing_required(data, project_type)
        if missing:
            results.append({
                "id": data["id"],
                "name": data.get("Назва", "—"),
                "url": data["url"],
                "missing": missing,
                "status": data.get("Статус"),
            })
    return results


def get_active_projects_for_user(owner_notion_id: str) -> list[dict]:
    """
    Get active projects where this person is Відповідальна особа.
    Excludes Done and Cancelled. Used for the "Update project" dropdown.
    """
    INACTIVE_STATUSES = {"Done", "Cancelled"}
    try:
        response = notion.databases.query(
            database_id=NOTION_DATABASE_ID,
            filter={
                "property": "Відповідальна особа",
                "people": {"contains": owner_notion_id}
            },
            sorts=[{"property": "Last edited time", "direction": "descending"}],
            page_size=50,
        )
        results = []
        for page in response.get("results", []):
            data = _page_to_dict(page)
            # Filter out done/cancelled in Python — avoids Notion multi-condition issues
            if data.get("Статус") in INACTIVE_STATUSES:
                continue
            results.append({
                "id": data["id"],
                "name": data.get("Назва", "—"),
                "status": data.get("Статус", ""),
                "dates": data.get("Дати проведення", ""),
                "type": data.get("Тип проєкту", ""),
                "url": data["url"],
            })
        return results
    except Exception as e:
        logger.error(f"get_active_projects_for_user failed: {e}")
        return []


def create_subproject(parent_project_id: str, fields: dict,
                      slack_user_id: str = "") -> dict:
    """
    Create a sub-project linked to a parent project.
    Inherits specified fields from parent, then applies `fields` overrides.
    """
    from schemas import SUBPROJECT_INHERIT_FROM_PARENT as INHERIT_FIELDS
    parent = get_project(parent_project_id)

    # Start with inherited fields from parent
    inherited = {}
    for f in INHERIT_FIELDS:
        val = parent.get(f)
        if val is not None:
            inherited[f] = val

    # Override with explicitly provided fields
    merged = {**inherited, **fields}

    # Link to parent
    merged["Parent project"] = [parent_project_id]

    return create_project(merged, slack_user_id=slack_user_id)


def get_all_active_projects() -> list[dict]:
    """
    Fetch all active projects (not Done/Cancelled) for the scheduler.
    Also returns raw owner Notion IDs as '_owner_ids' for reverse Slack lookup.
    """
    INACTIVE = {"Done", "Cancelled"}
    cursor = None
    results = []

    while True:
        kwargs = {
            "database_id": NOTION_DATABASE_ID,
            "page_size": 50,
            "sorts": [{"property": "Last edited time", "direction": "descending"}],
        }
        if cursor:
            kwargs["start_cursor"] = cursor

        response = notion.databases.query(**kwargs)

        for page in response.get("results", []):
            data = _page_to_dict(page)
            if data.get("Статус") in INACTIVE:
                continue
            # Also store raw owner Notion IDs for scheduler reverse lookup
            raw_owners = page.get("properties", {}).get(
                "Відповідальна особа", {}
            ).get("people", [])
            data["_owner_ids"] = [p["id"] for p in raw_owners if p.get("id")]
            results.append(data)

        if not response.get("has_more"):
            break
        cursor = response.get("next_cursor")

    return results


def log_action(slack_user_id: str, project_id: str,
               action: str, changed_fields: list[str]) -> None:
    """Log an agent action for audit purposes (writes to console; extend to DB later)."""
    logger.info(
        f"[ACTION] user={slack_user_id} | project={project_id} "
        f"| action={action} | fields={changed_fields}"
    )


# ── Tool definitions for Claude API ───────────────────────────────────────────
# This is the schema passed to Claude's `tools` parameter.

TOOL_DEFINITIONS = [
    {
        "name": "find_project",
        "description": (
            "Search for existing projects in the Projects database by name or owner. "
            "Use this before creating to check for duplicates."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Project name or partial name to search for"
                },
                "owner_notion_id": {
                    "type": "string",
                    "description": "Optional Notion user ID to filter by owner"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "get_project",
        "description": "Get full details of a project by its Notion page ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Notion page ID of the project"
                }
            },
            "required": ["project_id"]
        }
    },
    {
        "name": "create_project",
        "description": (
            "Create a new project in the Projects database. "
            "Only call this AFTER showing the user a confirmation summary and getting approval. "
            "Статус is automatically set to Planning."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fields": {
                    "type": "object",
                    "description": (
                        "Dict of field_name → value. Field names must match exactly: "
                        "Назва, Тип проєкту, Відповідальна особа (Notion user ID), "
                        "Дати проведення (DD.MM.YYYY or DD.MM.YYYY–DD.MM.YYYY), "
                        "К-ть учасників (number), "
                        "Syllabus / Посилання на папку проєкту (Google Drive URL), "
                        "Опис проєкту, Цілі проєкту, KPI проєкту, "
                        "Цільова аудиторія (list of strings), "
                        "Academic Director (list of names), "
                        "Юрособа, Формат (list), Тема навчання (list)"
                    )
                },
                "slack_user_id": {
                    "type": "string",
                    "description": "Slack user ID for audit logging"
                }
            },
            "required": ["fields"]
        }
    },
    {
        "name": "update_project",
        "description": (
            "Update fields on an existing project. "
            "For critical fields (Тип проєкту, Відповідальна особа, Юрособа, "
            "План, Parent project, Статус), always show confirmation first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Notion page ID of the project to update"
                },
                "fields": {
                    "type": "object",
                    "description": "Dict of field_name → new value"
                },
                "slack_user_id": {
                    "type": "string",
                    "description": "Slack user ID for audit logging"
                }
            },
            "required": ["project_id", "fields"]
        }
    },
    {
        "name": "get_missing_fields",
        "description": "Check which required fields are still missing for a project.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "project_type": {"type": "string"}
            },
            "required": ["project_id", "project_type"]
        }
    },
    {
        "name": "get_projects_with_missing_fields",
        "description": "Get all active projects owned by this person that have missing required fields. Used for reminders.",
        "input_schema": {
            "type": "object",
            "properties": {
                "owner_notion_id": {
                    "type": "string",
                    "description": "Notion user ID of the project owner"
                }
            },
            "required": ["owner_notion_id"]
        }
    },
    {
        "name": "create_subproject",
        "description": (
            "Create a sub-project (module) linked to a parent project. "
            "Inherits fields from parent automatically. "
            "Always confirm the full list with user before calling."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "parent_project_id": {"type": "string"},
                "fields": {
                    "type": "object",
                    "description": "Fields specific to this sub-project (Назва, Дати проведення, Syllabus link, optional К-ть учасників)"
                },
                "slack_user_id": {"type": "string"}
            },
            "required": ["parent_project_id", "fields"]
        }
    },
]


def execute_tool(name: str, inputs: dict) -> str:
    """Dispatch a tool call from the Claude agent to the correct function."""
    import json
    try:
        if name == "find_project":
            result = find_project(**inputs)
        elif name == "get_project":
            result = get_project(**inputs)
        elif name == "create_project":
            result = create_project(**inputs)
        elif name == "update_project":
            result = update_project(**inputs)
        elif name == "get_missing_fields":
            result = get_missing_fields(**inputs)
        elif name == "get_projects_with_missing_fields":
            result = get_projects_with_missing_fields(**inputs)
        elif name == "create_subproject":
            result = create_subproject(**inputs)
        else:
            result = {"error": f"Unknown tool: {name}"}
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        logger.error(f"Tool {name} failed: {e}", exc_info=True)
        return json.dumps({"error": str(e)})
