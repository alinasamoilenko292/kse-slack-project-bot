"""
Proactive scheduler for BS Project Setup Bot.

Two jobs:
1. Daily Drive check (14:00 Kyiv) — detect new files in project folders,
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
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MISSED

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
    return {}


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


# ── Helper: extract Drive folder ID from URL ──────────────────────────────────

def _extract_drive_folder_id(url: str) -> str | None:
    """
    Extract Google Drive folder ID from various URL formats:
      https://drive.google.com/drive/folders/FOLDER_ID[?usp=sharing]
      https://drive.google.com/open?id=FOLDER_ID
    """
    if not url:
        return None
    m = re.search(r'/folders/([a-zA-Z0-9_-]+)', url)
    if m:
        return m.group(1)
    m = re.search(r'[?&]id=([a-zA-Z0-9_-]+)', url)
    if m:
        return m.group(1)
    return None


# ── Job 1: Daily Drive check ──────────────────────────────────────────────────

def daily_drive_check() -> str:
    """
    For each active project with a Drive folder link in Notion,
    check for files uploaded/modified in the last 24h (72h on Monday).
    Notify responsible persons if any found.

    Runs Mon–Fri at 14:00 Kyiv. Monday covers the weekend (72h window).
    Returns a summary string — also usable for manual diagnostics via bot.
    """
    now_utc = datetime.now(timezone.utc)
    now_kyiv = datetime.now(KYIV_TZ)
    is_monday = now_kyiv.weekday() == 0
    hours_back = 72 if is_monday else 24
    since = now_utc - timedelta(hours=hours_back)

    period_label = "вихідних (72 год)" if is_monday else "останніх 24 годин"
    logger.info(
        f"[Scheduler] Running daily Drive check "
        f"({'Monday — 72h window' if is_monday else '24h window'}), "
        f"since {since.strftime('%Y-%m-%d %H:%M UTC')}"
    )

    try:
        from notion_tools import get_all_active_projects
        from drive_client import get_recent_files_in_folder
        from user_resolver import get_slack_id_for_notion_user
    except Exception as e:
        logger.error(f"[Scheduler] Import error: {e}", exc_info=True)
        return f"Помилка імпорту: {e}"

    try:
        projects = get_all_active_projects()
    except Exception as e:
        logger.error(f"[Scheduler] Could not fetch active projects: {e}", exc_info=True)
        return f"Не вдалося отримати проєкти: {e}"

    logger.info(f"[Scheduler] {len(projects)} active projects to check")

    checked = 0
    notified = 0
    skipped_no_folder = 0

    for project in projects:
        project_name = project.get("Назва", "?")
        owner_ids = project.get("_owner_ids", [])

        # Get Drive folder ID from Notion URL
        folder_url = project.get("Syllabus / Посилання на папку проєкту")
        folder_id = _extract_drive_folder_id(folder_url) if folder_url else None

        if not folder_id:
            # Fallback: search by name in Drive
            try:
                from drive_client import find_project_folder
                folder = find_project_folder(project_name)
                if folder:
                    folder_id = folder["id"]
            except Exception as e:
                logger.warning(f"[Scheduler] Name search failed for '{project_name}': {e}")

        if not folder_id:
            skipped_no_folder += 1
            continue

        # Find recently modified files
        try:
            recent_files = get_recent_files_in_folder(folder_id, since)
        except Exception as e:
            logger.warning(f"[Scheduler] Drive check failed for '{project_name}': {e}")
            continue

        checked += 1

        if not recent_files:
            logger.debug(f"[Scheduler] '{project_name}': no recent files")
            continue

        # Build notification
        file_list = "\n".join(f"  • {f['name']}" for f in recent_files[:10])
        extra = f"\n  _...і ще {len(recent_files) - 10} файл(ів)_" if len(recent_files) > 10 else ""
        text = (
            f"👋 Привіт! У папці проєкту *{project_name}* "
            f"за {period_label} з'явилися нові файли:\n"
            f"{file_list}{extra}\n\n"
            f"Хочеш додати дані звідти в Notion? "
            f"Просто напиши мені — я прочитаю файли і запропоную що оновити."
        )
        logger.info(
            f"[Scheduler] '{project_name}': {len(recent_files)} recent file(s), "
            f"notifying {len(owner_ids)} owner(s)"
        )

        for notion_uid in owner_ids:
            slack_id = get_slack_id_for_notion_user(notion_uid)
            if slack_id:
                _send_dm(slack_id, text)
                notified += 1
                logger.info(f"[Scheduler] DM sent → Slack {slack_id} (Notion: {notion_uid})")
            else:
                logger.warning(
                    f"[Scheduler] Cannot resolve Slack ID for Notion user {notion_uid} "
                    f"(project: '{project_name}'). "
                    f"Fix: user should send any message to the bot once to warm the cache, "
                    f"OR enable 'Read user information including email addresses' "
                    f"at notion.so/my-integrations."
                )

    summary = (
        f"Drive check ({period_label}) done. "
        f"Checked: {checked}/{len(projects)}, "
        f"sent: {notified} DM(s), "
        f"skipped (no folder): {skipped_no_folder}."
    )
    logger.info(f"[Scheduler] {summary}")
    return summary


# ── Job 2: Weekly missing fields reminder ────────────────────────────────────

def weekly_missing_fields_reminder() -> None:
    """
    For each active project manager, send a summary of projects
    with unfilled required fields. Runs every Tuesday at 10:00 Kyiv.
    """
    logger.info("[Scheduler] Running weekly missing-fields reminder...")

    try:
        from notion_tools import get_all_active_projects
        from schemas import get_missing_required
        from user_resolver import get_slack_id_for_notion_user
    except Exception as e:
        logger.error(f"[Scheduler] Import error in weekly_missing_fields_reminder: {e}", exc_info=True)
        return

    try:
        projects = get_all_active_projects()
    except Exception as e:
        logger.error(f"[Scheduler] Could not fetch active projects: {e}", exc_info=True)
        return

    logger.info(f"[Scheduler] Checking missing fields for {len(projects)} active projects")

    # Group missing fields by owner
    owner_issues: dict[str, list[dict]] = {}  # notion_user_id → [{name, missing, url}]

    for project in projects:
        try:
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
        except Exception as e:
            logger.warning(f"[Scheduler] Error processing project {project.get('Назва', '?')}: {e}")
            continue

    if not owner_issues:
        logger.info("[Scheduler] No missing fields found across active projects.")
        return

    sent = 0
    for notion_uid, issues in owner_issues.items():
        try:
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
            sent += 1
        except Exception as e:
            logger.error(f"[Scheduler] Failed to send reminder to {notion_uid}: {e}")

    logger.info(f"[Scheduler] Sent reminders to {sent}/{len(owner_issues)} managers.")


# ── Public: manual Drive check (for bot diagnostics) ─────────────────────────

def run_drive_check_now() -> str:
    """
    Run daily_drive_check immediately and return a human-readable summary.
    Call this from the bot when user types 'перевір драйв' / 'check drive'.
    """
    return daily_drive_check()


# ── APScheduler event listener ────────────────────────────────────────────────

def _scheduler_event_listener(event) -> None:
    """Log job outcomes so failures are visible in bot logs."""
    if event.exception:
        logger.error(
            f"[Scheduler] ❌ Job '{event.job_id}' FAILED with exception: {event.exception}",
            exc_info=event.exception,
        )
    elif hasattr(event, "retval"):
        # EVENT_JOB_EXECUTED
        logger.info(f"[Scheduler] ✅ Job '{event.job_id}' completed successfully.")
    else:
        # EVENT_JOB_MISSED
        logger.warning(
            f"[Scheduler] ⚠️ Job '{event.job_id}' was MISSED "
            f"(scheduled time: {getattr(event, 'scheduled_run_time', '?')}). "
            "It will run at the next scheduled time."
        )


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

    # Log all job outcomes (errors, completions, missed runs)
    scheduler.add_listener(
        _scheduler_event_listener,
        EVENT_JOB_ERROR | EVENT_JOB_EXECUTED | EVENT_JOB_MISSED,
    )

    # Daily Drive check — Mon–Fri 14:00 Kyiv
    # Monday: 72h window (covers Fri afternoon + weekend)
    # Tue–Fri: 24h window
    # misfire_grace_time=3600: if bot was down at 14:00 and restarts within 1h, still runs
    # max_instances=1: never run two instances simultaneously
    scheduler.add_job(
        daily_drive_check,
        trigger="cron",
        day_of_week="mon-fri",
        hour=14,
        minute=0,
        id="daily_drive_check",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=3600,
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
        max_instances=1,
        misfire_grace_time=3600,
    )

    scheduler.start()
    logger.info(
        "[Scheduler] Started. "
        "Daily Drive check: 14:00 Kyiv. "
        "Weekly reminder: Tue 10:00 Kyiv."
    )
    return scheduler
