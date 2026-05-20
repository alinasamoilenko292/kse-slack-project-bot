"""
Proactive scheduler for BS Project Setup Bot.

Two jobs:
1. Daily Drive check (09:00 Kyiv) — detect new files in project folders,
   notify the responsible person in Slack.
2. Weekly missing-fields reminder (Tuesday 10:00 Kyiv) — for each active
   project with unfilled required fields, DM the responsible person.

State is persisted in data/scheduler_state.json so restarts don't cause
duplicate notifications.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import pytz
from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)

STATE_FILE = Path(__file__).parent / "data" / "scheduler_state.json"
KYIV_TZ = pytz.timezone("Europe/Kyiv")

# Injected at startup
_slack_app = None


# ── State persistence ─────────────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Could not load scheduler state: {e}")
    return {"drive_files": {}, "last_1c_reminders": {}}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.error(f"Could not save scheduler state: {e}")


# ── Helper: send DM ───────────────────────────────────────────────────────────

def _send_dm(slack_user_id: str, text: str) -> None:
    if not _slack_app:
        logger.warning("Scheduler: slack_app not injected, cannot send DM")
        return
    try:
        _slack_app.client.chat_postMessage(channel=slack_user_id, text=text)
        logger.info(f"Scheduler DM sent to {slack_user_id}")
    except Exception as e:
        logger.error(f"Scheduler DM failed for {slack_user_id}: {e}")


# ── Job 1: Daily Drive check ──────────────────────────────────────────────────

def daily_drive_check() -> None:
    """
    For each active project that has a known Drive folder,
    check whether new files appeared since the last run.
    Notify the responsible person if yes.
    """
    logger.info("[Scheduler] Running daily Drive check...")

    from notion_tools import get_all_active_projects
    from drive_client import find_project_folder, get_new_files_in_folder
    from user_resolver import get_slack_id_for_notion_user

    state = _load_state()
    drive_state: dict = state.get("drive_files", {})

    try:
        projects = get_all_active_projects()
    except Exception as e:
        logger.error(f"[Scheduler] Could not fetch active projects: {e}")
        return

    for project in projects:
        project_id = project.get("id")
        project_name = project.get("Назва", "")
        owners = project.get("Відповідальна особа", [])

        if not project_name or not owners:
            continue

        # Find Drive folder
        try:
            folder = find_project_folder(project_name)
        except Exception:
            continue

        if not folder:
            continue

        folder_id = folder["id"]
        proj_state = drive_state.get(project_id, {"folder_id": folder_id, "file_ids": []})
        known_ids = proj_state.get("file_ids", [])

        # Get new files
        try:
            new_files = get_new_files_in_folder(folder_id, known_ids)
        except Exception as e:
            logger.warning(f"[Scheduler] Drive check failed for {project_name}: {e}")
            continue

        if new_files:
            # Build notification message
            file_list = "\n".join(f"  • {f['name']}" for f in new_files[:10])
            text = (
                f"👋 Привіт! У папці проєкту *{project_name}* з'явилися нові файли:\n"
                f"{file_list}\n\n"
                f"Хочеш додати дані звідти в Notion? "
                f"Просто напиши мені — я прочитаю файли і запропоную що оновити."
            )

            # Notify all responsible persons
            for owner_name in owners:
                slack_id = _resolve_owner_to_slack(owner_name, project)
                if slack_id:
                    _send_dm(slack_id, text)

            # Update known file IDs
            all_ids = list(set(known_ids + [f["id"] for f in new_files]))
            proj_state["file_ids"] = all_ids

        proj_state["folder_id"] = folder_id
        proj_state["last_check"] = datetime.now(timezone.utc).isoformat()
        drive_state[project_id] = proj_state

    state["drive_files"] = drive_state
    _save_state(state)
    logger.info(f"[Scheduler] Daily Drive check done. Checked {len(projects)} projects.")


# ── Job 2: Weekly missing fields reminder ────────────────────────────────────

def weekly_missing_fields_reminder() -> None:
    """
    For each active project manager, send a summary of projects
    with unfilled required fields. Runs every Tuesday at 10:00 Kyiv.
    """
    logger.info("[Scheduler] Running weekly missing-fields reminder...")

    from notion_tools import get_all_active_projects
    from schemas import get_missing_required
    from user_resolver import get_slack_id_for_notion_user

    try:
        projects = get_all_active_projects()
    except Exception as e:
        logger.error(f"[Scheduler] Could not fetch active projects: {e}")
        return

    # Group missing fields by owner
    owner_issues: dict[str, list[dict]] = {}  # notion_user_id → [{name, missing, url}]

    for project in projects:
        project_type = project.get("Тип проєкту", "")
        missing = get_missing_required(project, project_type)
        if not missing:
            continue

        owners_raw = project.get("_owner_ids", [])
        for notion_uid in owners_raw:
            if notion_uid not in owner_issues:
                owner_issues[notion_uid] = []
            owner_issues[notion_uid].append({
                "name": project.get("Назва", "—"),
                "url": project.get("url", ""),
                "missing": missing,
            })

    if not owner_issues:
        logger.info("[Scheduler] No missing fields found across active projects.")
        return

    for notion_uid, issues in owner_issues.items():
        slack_id = get_slack_id_for_notion_user(notion_uid)
        if not slack_id:
            logger.warning(f"[Scheduler] Cannot resolve Slack ID for Notion user {notion_uid}")
            continue

        lines = [
            "📋 *Щотижневе нагадування* — ось твої проєкти з незаповненими полями:\n"
        ]
        for issue in issues[:10]:
            field_list = ", ".join(issue["missing"])
            url = f" (<{issue['url']}|відкрити>)" if issue["url"] else ""
            lines.append(f"• *{issue['name']}*{url}\n  ❌ Бракує: {field_list}")

        lines.append(
            "\nНапиши мені назву проєкту — я допоможу дозаповнити прямо тут."
        )

        _send_dm(slack_id, "\n".join(lines))

    logger.info(f"[Scheduler] Sent reminders to {len(owner_issues)} managers.")


# ── Helper: resolve owner name → Slack ID ────────────────────────────────────

def _resolve_owner_to_slack(owner_name: str, project: dict) -> str | None:
    """
    Try to get Slack ID for a project owner.
    Uses notion_id stored in project data when available.
    """
    from user_resolver import get_slack_id_for_notion_user

    owner_ids = project.get("_owner_ids", [])
    for uid in owner_ids:
        slack_id = get_slack_id_for_notion_user(uid)
        if slack_id:
            return slack_id
    return None


# ── Startup ───────────────────────────────────────────────────────────────────

def start_scheduler(slack_app) -> BackgroundScheduler:
    """
    Start the background scheduler. Call once at bot startup.

    Args:
        slack_app: the Slack Bolt App instance (for sending DMs)

    Returns:
        The running BackgroundScheduler instance.
    """
    global _slack_app
    _slack_app = slack_app

    scheduler = BackgroundScheduler(timezone=KYIV_TZ)

    # Daily Drive check — 14:00 Kyiv
    scheduler.add_job(
        daily_drive_check,
        trigger="cron",
        hour=14,
        minute=0,
        id="daily_drive_check",
        replace_existing=True,
    )

    # Weekly missing-fields reminder — Tuesday 10:00 Kyiv
    scheduler.add_job(
        weekly_missing_fields_reminder,
        trigger="cron",
        day_of_week="tue",
        hour=10,
        minute=0,
        id="weekly_missing_fields",
        replace_existing=True,
    )

    scheduler.start()
    logger.info(
        "[Scheduler] Started. "
        "Daily Drive check: 14:00 Kyiv. "
        "Weekly reminder: Tue 10:00 Kyiv."
    )
    return scheduler
