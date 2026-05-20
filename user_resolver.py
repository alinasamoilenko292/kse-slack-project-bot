"""
Resolves Slack user email → Notion user ID without any manual mapping.

Strategy:
1. Get manager's email from Slack API
2. Search Notion /v1/users (returns full members — works for most accounts)
3. If not found there, scan the Projects database for people who already
   appear in person fields — this catches guests with page-level access
4. Cache all results in memory (resets on restart, but re-fills quickly)
5. If still not found — create the project anyway, store email as text note,
   and log so admin can add the person to Notion manually once
"""
from __future__ import annotations

import os
import logging
from notion_client import Client
from slack_sdk import WebClient

logger = logging.getLogger(__name__)

NOTION_DATABASE_ID = "173122fe-7695-80c6-96b3-000b08f81c95"
PERSON_FIELDS = ["Відповідальна особа", "Coordinator", "Project Manager", "Marketer"]

notion = Client(auth=os.environ["NOTION_TOKEN"])
slack_client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])

# In-memory cache: email (lowercase) → notion_user_id
_cache: dict[str, str] = {}
# Track emails we've already scanned the DB for (to avoid re-scanning)
_scanned_emails: set[str] = set()
# Reverse cache: notion_user_id → slack_user_id  (populated during normal bot use)
_notion_to_slack: dict[str, str] = {}


# ── Slack ──────────────────────────────────────────────────────────────────────

def get_slack_email(slack_user_id: str) -> str | None:
    """Fetch the email address of a Slack user."""
    try:
        result = slack_client.users_info(user=slack_user_id)
        email = result["user"]["profile"].get("email", "")
        return email.lower() if email else None
    except Exception as e:
        logger.warning(f"Could not get Slack email for {slack_user_id}: {e}")
        return None


# ── Notion lookup ──────────────────────────────────────────────────────────────

def _search_notion_workspace_users(email: str) -> str | None:
    """
    Search /v1/users for the email.
    Works for full workspace members. Guests may or may not appear
    depending on their workspace role.
    """
    cursor = None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        try:
            response = notion.users.list(**params)
        except Exception as e:
            logger.warning(f"Notion users.list failed: {e}")
            return None

        for user in response.get("results", []):
            if user.get("type") == "person":
                user_email = user.get("person", {}).get("email", "").lower()
                # Cache everyone we find
                if user_email:
                    _cache[user_email] = user["id"]
                if user_email == email:
                    return user["id"]

        if not response.get("has_more"):
            break
        cursor = response.get("next_cursor")

    return None


def _search_notion_db_for_email(email: str) -> str | None:
    """
    Scan existing Projects to find guests who appear in person fields.
    Guests with page/database-level access show up in page properties
    even if they're not in /v1/users.
    This is a one-time scan per unique email — results are cached.
    """
    if email in _scanned_emails:
        return None  # Already scanned, nothing found

    _scanned_emails.add(email)
    logger.info(f"Scanning Projects DB to find Notion guest: {email}")

    cursor = None
    while True:
        kwargs = {"database_id": NOTION_DATABASE_ID, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        try:
            response = notion.databases.query(**kwargs)
        except Exception as e:
            logger.warning(f"DB scan failed: {e}")
            return None

        for page in response.get("results", []):
            props = page.get("properties", {})
            for field in PERSON_FIELDS:
                prop = props.get(field, {})
                for person in prop.get("people", []):
                    person_email = person.get("person", {}).get("email", "").lower()
                    notion_id = person.get("id")
                    if person_email and notion_id:
                        _cache[person_email] = notion_id
                    if person_email == email:
                        return notion_id

        if not response.get("has_more"):
            break
        cursor = response.get("next_cursor")

    return None


def resolve(slack_user_id: str) -> dict:
    """
    Main entry point. Returns:
    {
        "email": "manager@kse.org.ua",
        "notion_id": "uuid..." or None,
        "found": True/False,
        "display_name": "Name from Slack"
    }
    """
    # Get email from Slack
    email = get_slack_email(slack_user_id)
    display_name = _get_slack_display_name(slack_user_id)

    if not email:
        return {"email": None, "notion_id": None, "found": False,
                "display_name": display_name}

    # Check cache first
    if email in _cache:
        return {"email": email, "notion_id": _cache[email],
                "found": True, "display_name": display_name}

    # 1. Try workspace users endpoint
    notion_id = _search_notion_workspace_users(email)

    # 2. If not found, scan existing DB records (catches guests)
    if not notion_id:
        notion_id = _search_notion_db_for_email(email)

    if notion_id:
        _cache[email] = notion_id
        _notion_to_slack[notion_id] = slack_user_id  # reverse mapping for scheduler
        logger.info(f"Resolved {email} → {notion_id}")
    else:
        logger.warning(
            f"Could not find Notion user for email: {email}. "
            f"Project will be created without Відповідальна особа set as person. "
            f"To fix: invite {email} to the Projects database in Notion."
        )

    return {
        "email": email,
        "notion_id": notion_id,
        "found": notion_id is not None,
        "display_name": display_name,
    }


def get_slack_id_for_notion_user(notion_id: str) -> str | None:
    """
    Reverse lookup: Notion user ID → Slack user ID.
    Populated automatically as users interact with the bot.
    Falls back to scanning the Slack workspace if not in cache.
    """
    if notion_id in _notion_to_slack:
        return _notion_to_slack[notion_id]

    # Try to find via Notion email → Slack workspace lookup
    try:
        notion_user = notion.users.retrieve(user_id=notion_id)
        email = notion_user.get("person", {}).get("email", "").lower()
        if not email:
            return None
        # Search Slack for this email
        result = slack_client.users_lookupByEmail(email=email)
        slack_id = result["user"]["id"]
        _notion_to_slack[notion_id] = slack_id
        return slack_id
    except Exception as e:
        logger.warning(f"Reverse lookup failed for Notion user {notion_id}: {e}")
        return None


def _get_slack_display_name(slack_user_id: str) -> str:
    try:
        result = slack_client.users_info(user=slack_user_id)
        profile = result["user"]["profile"]
        return profile.get("display_name") or profile.get("real_name") or slack_user_id
    except Exception:
        return slack_user_id
