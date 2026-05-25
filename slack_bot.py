"""
Slack bot — thin event handler using Slack Bolt for Python.
Receives messages, passes them to Claude agent, sends formatted responses back.
"""
from __future__ import annotations

import os
import logging
import requests
from dotenv import load_dotenv
load_dotenv()  # must be before any os.environ reads

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from agent import run_agent, clear_history
from user_resolver import resolve as resolve_user
from config import PROJECT_TYPES, TARGET_AUDIENCES, ACADEMIC_DIRECTORS, FORMATS
from file_parser import parse_file

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = App(token=os.environ["SLACK_BOT_TOKEN"])

# Slack channel ID for payment notifications (bs-slack-payment)
# Bot must be added to this channel to receive its messages.
PAYMENT_CHANNEL_ID = os.environ.get("PAYMENT_CHANNEL_ID", "C0AT59F5F2P")

# Init usage tracking DB
try:
    from usage_tracker import init_db
    init_db()
except Exception as _db_err:
    logger.warning(f"UsageTracker DB init failed: {_db_err}")

# Start proactive scheduler (daily Drive check + weekly reminders + usage stats)
try:
    from scheduler import start_scheduler
    _scheduler = start_scheduler(app)
except Exception as _sched_err:
    logger.warning(f"Scheduler not started: {_sched_err}")


# ── File text extraction ───────────────────────────────────────────────────────

def extract_file_text(file_info: dict) -> str | None:
    """
    Download a file from Slack and extract its text content.
    Supports: .docx, .doc, .pdf, .txt, .json and any text/* type.
    """
    mimetype = file_info.get("mimetype", "")
    filename = file_info.get("name", "attachment")

    # Prefer url_private_download, fallback to url_private
    url = file_info.get("url_private_download") or file_info.get("url_private")

    logger.info(f"[FILE] name={filename!r} mimetype={mimetype!r} url={'yes' if url else 'MISSING'}")

    if not url:
        logger.warning(f"[FILE] No download URL for {filename}")
        return f"[Файл: {filename} — не вдалося отримати URL для завантаження]"

    # Block only known binary/media types we can't process
    unsupported_prefixes = ["image/", "video/", "audio/"]
    if any(mimetype.startswith(p) for p in unsupported_prefixes):
        logger.info(f"[FILE] Skipping unsupported type: {mimetype}")
        return f"[Файл: {filename} — тип {mimetype} не підтримується для читання]"

    try:
        headers = {"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"}
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()

        logger.info(f"[FILE] Downloaded {filename}: {len(response.content)} bytes, content-type={response.headers.get('content-type','?')!r}")

        result = parse_file(response.content, filename, mimetype)
        logger.info(f"[FILE] Parsed {filename}: {len(result)} chars, preview={result[:120]!r}")
        return result

    except Exception as e:
        logger.error(f"[FILE] Failed to download/parse {filename}: {e}", exc_info=True)
        return f"[Не вдалося завантажити або прочитати файл: {filename} ({e})]"


# ── Block Kit helpers ──────────────────────────────────────────────────────────

def send_project_type_selector(client, channel: str, user: str) -> None:
    """Send a dropdown to choose project type."""
    client.chat_postEphemeral(
        channel=channel,
        user=user,
        text="Оберіть тип проєкту:",
        blocks=[
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "📁 *Оберіть тип проєкту:*"}
            },
            {
                "type": "actions",
                "block_id": "project_type_select",
                "elements": [
                    {
                        "type": "static_select",
                        "action_id": "project_type_chosen",
                        "placeholder": {"type": "plain_text", "text": "Тип проєкту..."},
                        "options": [
                            {
                                "text": {"type": "plain_text", "text": pt},
                                "value": pt
                            }
                            for pt in PROJECT_TYPES
                        ]
                    }
                ]
            }
        ]
    )


def send_action_menu(client, channel: str, user: str) -> None:
    """Send the main action menu as buttons."""
    client.chat_postEphemeral(
        channel=channel,
        user=user,
        text="Що робимо?",
        blocks=[
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "👋 Привіт! Що хочеш зробити?"}
            },
            {
                "type": "actions",
                "block_id": "main_menu",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "action_new_project",
                        "text": {"type": "plain_text", "text": "➕ Створити проєкт"},
                        "style": "primary",
                        "value": "new_project"
                    },
                    {
                        "type": "button",
                        "action_id": "action_update_project",
                        "text": {"type": "plain_text", "text": "✏️ Оновити проєкт"},
                        "value": "update_project"
                    },
                    {
                        "type": "button",
                        "action_id": "action_check_missing",
                        "text": {"type": "plain_text", "text": "🔍 Що бракує?"},
                        "value": "check_missing"
                    },
                    {
                        "type": "button",
                        "action_id": "action_subprojects",
                        "text": {"type": "plain_text", "text": "📦 Підпроєкти"},
                        "value": "subprojects"
                    },
                ]
            }
        ]
    )


def send_confirmation_buttons(client, channel: str, user: str, summary: str) -> None:
    """Send a confirmation block with Confirm / Edit / Cancel buttons."""
    client.chat_postEphemeral(
        channel=channel,
        user=user,
        text=summary,
        blocks=[
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": summary}
            },
            {
                "type": "actions",
                "block_id": "confirmation",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "confirm_yes",
                        "text": {"type": "plain_text", "text": "✅ Підтверджую"},
                        "style": "primary",
                        "value": "yes"
                    },
                    {
                        "type": "button",
                        "action_id": "confirm_edit",
                        "text": {"type": "plain_text", "text": "✏️ Редагувати"},
                        "value": "edit"
                    },
                    {
                        "type": "button",
                        "action_id": "confirm_cancel",
                        "text": {"type": "plain_text", "text": "❌ Скасувати"},
                        "style": "danger",
                        "value": "cancel"
                    },
                ]
            }
        ]
    )


def send_reminder_buttons(client, channel: str, user: str, text: str) -> None:
    """Send reminder options after creating a project with missing fields."""
    client.chat_postEphemeral(
        channel=channel,
        user=user,
        text=text,
        blocks=[
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": text}
            },
            {
                "type": "actions",
                "block_id": "reminder_options",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "fill_now",
                        "text": {"type": "plain_text", "text": "✍️ Дозаповнити зараз"},
                        "style": "primary",
                        "value": "fill_now"
                    },
                    {
                        "type": "button",
                        "action_id": "remind_tomorrow",
                        "text": {"type": "plain_text", "text": "⏰ Нагадати завтра"},
                        "value": "remind_tomorrow"
                    },
                    {
                        "type": "button",
                        "action_id": "remind_week",
                        "text": {"type": "plain_text", "text": "📅 Нагадати за тиждень"},
                        "value": "remind_week"
                    },
                ]
            }
        ]
    )


# ── Event handlers ─────────────────────────────────────────────────────────────

@app.command("/project")
def handle_project_command(ack, command, client):
    """Handle /project slash command — show main action menu."""
    ack()
    send_action_menu(client, command["channel_id"], command["user_id"])


@app.command("/stats")
def handle_stats_command(ack, command, client):
    """
    /stats [weeks]  — usage digest for the last N weeks (default 1).
    Anyone can call it; stats cover the whole team.
    """
    ack()
    user_id = command["user_id"]
    channel = command["channel_id"]

    raw = (command.get("text") or "").strip()
    try:
        weeks_back = max(0, min(int(raw), 12)) if raw.isdigit() else 0
    except ValueError:
        weeks_back = 0

    from usage_tracker import get_weekly_stats, format_weekly_report
    try:
        stats = get_weekly_stats(weeks_back=weeks_back)
        report = format_weekly_report(stats)
    except Exception as e:
        logger.error(f"[/stats] Failed: {e}")
        report = "⚠️ Не вдалося отримати статистику. Спробуй пізніше."

    # Send as ephemeral so only the caller sees it
    client.chat_postEphemeral(channel=channel, user=user_id, text=report)


def _is_payment_notification(text: str) -> bool:
    """Heuristic: does this message look like a 1C payment notification?"""
    keywords = ["оплачено", "контрагент", "сума документу", "дата платежу"]
    text_lower = text.lower()
    # At least 2 of the keywords must be present
    return sum(1 for kw in keywords if kw in text_lower) >= 2


def _handle_payment_channel_message(message: dict, client) -> None:
    """
    Auto-process a payment notification from the bs-slack-payment channel.
    Forwards the full message text to the bot's DM with the sender so the agent
    can parse and record it interactively.
    """
    slack_user_id = message.get("user")
    if not slack_user_id:
        return

    text = message.get("text", "")

    # Also check attachments (1C notifications often come as bot messages with attachments)
    attachments = message.get("attachments", [])
    attachment_text = " ".join(
        a.get("fallback", "") or a.get("text", "") or a.get("pretext", "")
        for a in attachments
    )
    full_text = (text + "\n" + attachment_text).strip()

    if not full_text or not _is_payment_notification(full_text):
        return

    logger.info(f"[PAYMENT] Detected payment notification from {slack_user_id}")

    # Forward to the user's DM as if they sent it there
    user_info = resolve_user(slack_user_id)
    prefixed_message = (
        f"[Сповіщення про оплату з каналу #bs-slack-payment]\n{full_text}"
    )
    response_text = run_agent(
        slack_user_id=slack_user_id,
        user_message=prefixed_message,
        notion_user_id=user_info.get("notion_id"),
        user_email=user_info.get("email"),
        display_name=user_info.get("display_name"),
    )

    # Reply in DM (not in the payment channel to avoid noise)
    try:
        client.chat_postMessage(channel=slack_user_id, text=response_text)
    except Exception as e:
        logger.error(f"[PAYMENT] Could not DM {slack_user_id}: {e}")


@app.message()
def handle_message(message, say, client):
    """Handle any DM or message in channel where bot is mentioned."""
    channel = message.get("channel", "")
    channel_type = message.get("channel_type")

    # ── Payment channel: auto-detect and process in background ──────────────
    if channel == PAYMENT_CHANNEL_ID:
        _handle_payment_channel_message(message, client)
        return

    # ── Regular messages: only respond to DMs or when mentioned ─────────────
    if channel_type not in ("im", "mpim"):
        # In channels, only respond if bot is mentioned
        if f"<@{app._client.token}" not in message.get("text", ""):
            return

    # Ignore edited messages and bot messages to prevent double-processing
    if message.get("subtype") in ("message_changed", "message_deleted", "bot_message"):
        return
    if message.get("edited"):
        return

    slack_user_id = message.get("user")
    if not slack_user_id:
        return

    text = message.get("text", "").strip()

    # Handle attached files
    file_text = None
    files = message.get("files", [])
    if files:
        file_text = ""
        for f in files:
            extracted = extract_file_text(f)
            if extracted:
                file_text += f"\n--- {f.get('name', 'file')} ---\n{extracted}"

    if not text and not file_text:
        return

    # ── Stats shortcut: "статистика" / "stats" ────────────────────────────────
    if text.lower().strip() in ("статистика", "stats", "/stats"):
        from usage_tracker import get_weekly_stats, format_weekly_report
        try:
            stats = get_weekly_stats(weeks_back=1)
            report = format_weekly_report(stats)
        except Exception as e:
            logger.error(f"[stats keyword] Failed: {e}")
            report = "⚠️ Не вдалося отримати статистику."
        say(text=report)
        return

    # ── Drive check shortcut: manual trigger for diagnostics ─────────────────
    if text.lower().strip() in ("перевір драйв", "check drive", "/check-drive"):
        say(text="⏳ Запускаю перевірку Drive...")
        try:
            from scheduler import run_drive_check_now
            result = run_drive_check_now()
            say(text=f"✅ {result}")
        except Exception as e:
            logger.error(f"[check-drive] Failed: {e}", exc_info=True)
            say(text=f"⚠️ Помилка при перевірці Drive: {e}")
        return

    # Show typing indicator
    client.chat_postEphemeral(
        channel=channel,
        user=slack_user_id,
        text="⏳ Обробляю..."
    )

    user_info = resolve_user(slack_user_id)
    response_text = run_agent(
        slack_user_id=slack_user_id,
        user_message=text,
        notion_user_id=user_info.get("notion_id"),
        user_email=user_info.get("email"),
        display_name=user_info.get("display_name"),
        file_content=file_text,
    )

    # Check if response contains a confirmation block signal
    # Agent adds "[CONFIRM]" marker when it's ready for confirmation
    if "[CONFIRM]" in response_text:
        parts = response_text.split("[CONFIRM]", 1)
        if len(parts) == 2:
            say(text=parts[0].strip())
            send_confirmation_buttons(client, channel, slack_user_id, parts[1].strip())
            return

    # Check for reminder trigger
    if "[MISSING_FIELDS]" in response_text:
        parts = response_text.split("[MISSING_FIELDS]", 1)
        say(text=parts[0].strip())
        send_reminder_buttons(client, channel, slack_user_id, parts[1].strip())
        return

    say(text=response_text)


# ── Button/action handlers ─────────────────────────────────────────────────────

@app.action("action_new_project")
def action_new_project(ack, body, client):
    ack()
    user = body["user"]["id"]
    channel = body["container"]["channel_id"]
    send_project_type_selector(client, channel, user)


@app.action("project_type_chosen")
def action_project_type_chosen(ack, body, client, say):
    ack()
    user = body["user"]["id"]
    channel = body["container"]["channel_id"]
    project_type = body["actions"][0]["selected_option"]["value"]

    _ui = resolve_user(user)
    response = run_agent(
        slack_user_id=user,
        notion_user_id=_ui.get("notion_id"),
        user_email=_ui.get("email"),
        display_name=_ui.get("display_name"),
        user_message=f"Хочу створити новий проєкт. Тип: {project_type}",
    )
    client.chat_postMessage(channel=channel, text=response)


@app.action("action_update_project")
def action_update_project(ack, body, client):
    ack()
    user = body["user"]["id"]
    channel = body["container"]["channel_id"]
    _ui = resolve_user(user)
    notion_user_id = _ui.get("notion_id")

    if not notion_user_id:
        client.chat_postMessage(
            channel=channel,
            text="⚠️ Не вдалося визначити твій акаунт Notion. Напиши назву проєкту який хочеш оновити."
        )
        return

    from notion_tools import get_active_projects_for_user
    projects = get_active_projects_for_user(notion_user_id)

    if not projects:
        client.chat_postMessage(
            channel=channel,
            text="У тебе немає активних проєктів де ти вказана як Відповідальна особа. Напиши назву проєкту і я знайду."
        )
        return

    options = []
    for p in projects[:24]:
        label = p["name"]
        if p.get("dates"):
            label += f" ({p['dates'][:10]})"
        if p.get("status"):
            label += f" · {p['status']}"
        options.append({
            "text": {"type": "plain_text", "text": label[:75]},
            "value": p["id"]
        })

    client.chat_postEphemeral(
        channel=channel,
        user=user,
        text="Оберіть проєкт для оновлення:",
        blocks=[
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"✏️ *Твої активні проєкти ({len(projects)}):*"}
            },
            {
                "type": "actions",
                "block_id": "project_to_update",
                "elements": [
                    {
                        "type": "static_select",
                        "action_id": "project_selected_for_update",
                        "placeholder": {"type": "plain_text", "text": "Обери проєкт..."},
                        "options": options
                    }
                ]
            }
        ]
    )


@app.action("project_selected_for_update")
def action_project_selected_for_update(ack, body, client):
    ack()
    user = body["user"]["id"]
    channel = body["container"]["channel_id"]
    project_id = body["actions"][0]["selected_option"]["value"]
    project_name = body["actions"][0]["selected_option"]["text"]["text"]
    _ui = resolve_user(user)
    response = run_agent(
        slack_user_id=user,
        notion_user_id=_ui.get("notion_id"),
        user_email=_ui.get("email"),
        display_name=_ui.get("display_name"),
        user_message=f"Хочу оновити проєкт: {project_name}. Notion ID проєкту: {project_id}. Покажи що бракує.",
    )
    client.chat_postMessage(channel=channel, text=response)


@app.action("action_check_missing")
def action_check_missing(ack, body, client):
    ack()
    user = body["user"]["id"]
    channel = body["container"]["channel_id"]
    _ui = resolve_user(user)
    response = run_agent(
        slack_user_id=user,
        notion_user_id=_ui.get("notion_id"),
        user_email=_ui.get("email"),
        display_name=_ui.get("display_name"),
        user_message="Покажи мої проєкти з незаповненими полями.",
    )
    client.chat_postMessage(channel=channel, text=response)


@app.action("action_subprojects")
def action_subprojects(ack, body, client):
    ack()
    user = body["user"]["id"]
    channel = body["container"]["channel_id"]
    _ui = resolve_user(user)
    response = run_agent(
        slack_user_id=user,
        notion_user_id=_ui.get("notion_id"),
        user_email=_ui.get("email"),
        display_name=_ui.get("display_name"),
        user_message="Хочу створити підпроєкти (модулі) до існуючого проєкту.",
    )
    client.chat_postMessage(channel=channel, text=response)


@app.action("confirm_yes")
def action_confirm_yes(ack, body, client, say):
    ack()
    user = body["user"]["id"]
    channel = body["container"]["channel_id"]
    _ui = resolve_user(user)
    response = run_agent(
        slack_user_id=user,
        notion_user_id=_ui.get("notion_id"),
        user_email=_ui.get("email"),
        display_name=_ui.get("display_name"),
        user_message="Так, підтверджую. Записуй у Notion.",
    )
    client.chat_postMessage(channel=channel, text=response)


@app.action("confirm_edit")
def action_confirm_edit(ack, body, client):
    ack()
    user = body["user"]["id"]
    channel = body["container"]["channel_id"]
    _ui = resolve_user(user)
    response = run_agent(
        slack_user_id=user,
        notion_user_id=_ui.get("notion_id"),
        user_email=_ui.get("email"),
        display_name=_ui.get("display_name"),
        user_message="Хочу відредагувати деякі поля перед збереженням.",
    )
    client.chat_postMessage(channel=channel, text=response)


@app.action("confirm_cancel")
def action_confirm_cancel(ack, body, client):
    ack()
    user = body["user"]["id"]
    channel = body["container"]["channel_id"]
    clear_history(user)
    client.chat_postMessage(channel=channel, text="❌ Скасовано. Розмову скинуто.")


@app.action("fill_now")
def action_fill_now(ack, body, client):
    ack()
    user = body["user"]["id"]
    channel = body["container"]["channel_id"]
    _ui = resolve_user(user)
    response = run_agent(
        slack_user_id=user,
        notion_user_id=_ui.get("notion_id"),
        user_email=_ui.get("email"),
        display_name=_ui.get("display_name"),
        user_message="Дозаповнити зараз — покажи, що бракує.",
    )
    client.chat_postMessage(channel=channel, text=response)


@app.action("remind_tomorrow")
def action_remind_tomorrow(ack, body, client):
    ack()
    user = body["user"]["id"]
    channel = body["container"]["channel_id"]
    # TODO: integrate with a scheduler (e.g. APScheduler or Celery)
    client.chat_postMessage(
        channel=channel,
        text="⏰ Нагадаю завтра. (Scheduler поки не підключений — додай APScheduler або cron)"
    )


@app.action("remind_week")
def action_remind_week(ack, body, client):
    ack()
    user = body["user"]["id"]
    channel = body["container"]["channel_id"]
    client.chat_postMessage(
        channel=channel,
        text="📅 Нагадаю через тиждень. (Scheduler поки не підключений)"
    )


# ── Message Shortcut handler ───────────────────────────────────────────────────
# Triggered when user right-clicks a message → selects the shortcut.
# Shortcut callback_id must be configured in api.slack.com → Interactivity & Shortcuts.
# Use callback_id = "send_to_bot"

@app.shortcut("send_to_bot")
def handle_message_shortcut(ack, shortcut, client):
    """
    Message Shortcut: user right-clicks any message → 'Надіслати боту'.
    Bot receives the original message text + attachments and processes it as a DM.
    """
    ack()  # must acknowledge within 3 seconds

    slack_user_id = shortcut["user"]["id"]
    message = shortcut.get("message", {})

    # Collect message text
    text = message.get("text", "").strip()

    # Also collect attachment fallback text (1C notifications use attachments)
    attachments = message.get("attachments", [])
    attachment_text = "\n".join(
        a.get("fallback", "") or a.get("text", "") or a.get("pretext", "")
        for a in attachments
        if a.get("fallback") or a.get("text") or a.get("pretext")
    ).strip()

    full_text = (text + ("\n" + attachment_text if attachment_text else "")).strip()

    if not full_text:
        client.chat_postMessage(
            channel=slack_user_id,
            text="⚠️ Не вдалося прочитати повідомлення (воно порожнє або не містить тексту)."
        )
        return

    # Send a quick acknowledgement in DM
    client.chat_postMessage(
        channel=slack_user_id,
        text="⏳ Обробляю повідомлення..."
    )

    user_info = resolve_user(slack_user_id)
    prefixed = f"[Повідомлення переслане через шортkat]\n{full_text}"
    response_text = run_agent(
        slack_user_id=slack_user_id,
        user_message=prefixed,
        notion_user_id=user_info.get("notion_id"),
        user_email=user_info.get("email"),
        display_name=user_info.get("display_name"),
    )

    client.chat_postMessage(channel=slack_user_id, text=response_text)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    logger.info("🤖 Project Documentation Bot starting...")
    handler.start()
