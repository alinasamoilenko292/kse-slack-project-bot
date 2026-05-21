"""
Usage tracker for BS Project Setup Bot.

Logs every interaction to SQLite (data/usage.db).
Provides weekly stats for the admin digest.

Action types:
  message           — any incoming message
  create_project    — create_project tool was called
  update_project    — update_project tool was called
  create_subproject — create_subproject tool was called
  record_payment    — record_payment tool was called
  read_file         — read_drive_file / read_project_folder_contents called
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "data" / "usage.db"

# ── Tool name → action type mapping ──────────────────────────────────────────
TOOL_ACTION_MAP: dict[str, str] = {
    "create_project":              "create_project",
    "create_subproject":           "create_subproject",
    "update_project":              "update_project",
    "record_payment":              "record_payment",
    "read_drive_file":             "read_file",
    "read_project_folder_contents": "read_file",
}


# ── DB setup ──────────────────────────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           TEXT    NOT NULL,
                slack_user_id TEXT   NOT NULL,
                display_name TEXT,
                action_type  TEXT    NOT NULL,
                project_name TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON events(ts)")
        conn.commit()
    logger.info("[UsageTracker] DB ready at %s", DB_PATH)


# ── Logging ───────────────────────────────────────────────────────────────────

def log_event(
    slack_user_id: str,
    action_type: str,
    display_name: str | None = None,
    project_name: str | None = None,
) -> None:
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO events (ts, slack_user_id, display_name, action_type, project_name) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    slack_user_id,
                    display_name,
                    action_type,
                    project_name,
                ),
            )
            conn.commit()
    except Exception as e:
        logger.error("[UsageTracker] log_event failed: %s", e)


def log_tool_calls(
    slack_user_id: str,
    tool_names: list[str],
    display_name: str | None = None,
) -> None:
    """Call after agent loop — log one event per unique action type used."""
    logged_actions: set[str] = set()
    for tool in tool_names:
        action = TOOL_ACTION_MAP.get(tool)
        if action and action not in logged_actions:
            log_event(slack_user_id, action, display_name)
            logged_actions.add(action)


# ── Stats ─────────────────────────────────────────────────────────────────────

def get_weekly_stats(weeks_back: int = 1) -> dict:
    """
    Returns stats for the last `weeks_back` full ISO week(s).
    A "week" starts on Monday 00:00 Kyiv → we use UTC approximation here
    (good enough for a digest).
    """
    now = datetime.now(timezone.utc)
    # Start of the target week (Monday 00:00 UTC)
    days_since_monday = now.weekday()
    week_start = (now - timedelta(days=days_since_monday + 7 * weeks_back)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    week_end = week_start + timedelta(days=7)

    start_iso = week_start.isoformat()
    end_iso   = week_end.isoformat()

    with _get_conn() as conn:
        # Total messages
        total_messages = conn.execute(
            "SELECT COUNT(*) FROM events WHERE ts >= ? AND ts < ? AND action_type = 'message'",
            (start_iso, end_iso),
        ).fetchone()[0]

        # Unique users
        unique_users = conn.execute(
            "SELECT COUNT(DISTINCT slack_user_id) FROM events "
            "WHERE ts >= ? AND ts < ? AND action_type = 'message'",
            (start_iso, end_iso),
        ).fetchone()[0]

        # Action counts
        action_rows = conn.execute(
            "SELECT action_type, COUNT(*) as cnt FROM events "
            "WHERE ts >= ? AND ts < ? AND action_type != 'message' "
            "GROUP BY action_type ORDER BY cnt DESC",
            (start_iso, end_iso),
        ).fetchall()
        actions = {r["action_type"]: r["cnt"] for r in action_rows}

        # Per-user breakdown
        user_rows = conn.execute(
            """
            SELECT slack_user_id,
                   MAX(display_name) as display_name,
                   SUM(CASE WHEN action_type = 'message' THEN 1 ELSE 0 END) as messages,
                   SUM(CASE WHEN action_type = 'create_project' THEN 1 ELSE 0 END) as created,
                   SUM(CASE WHEN action_type = 'update_project' THEN 1 ELSE 0 END) as updated,
                   SUM(CASE WHEN action_type = 'record_payment' THEN 1 ELSE 0 END) as payments,
                   SUM(CASE WHEN action_type = 'read_file' THEN 1 ELSE 0 END) as files,
                   SUM(CASE WHEN action_type = 'create_subproject' THEN 1 ELSE 0 END) as subprojects
            FROM events
            WHERE ts >= ? AND ts < ?
            GROUP BY slack_user_id
            ORDER BY messages DESC
            """,
            (start_iso, end_iso),
        ).fetchall()
        users = [dict(r) for r in user_rows]

    return {
        "week_start": week_start.strftime("%d.%m"),
        "week_end":   (week_end - timedelta(days=1)).strftime("%d.%m.%Y"),
        "total_messages": total_messages,
        "unique_users":   unique_users,
        "actions":        actions,
        "users":          users,
    }


def format_weekly_report(stats: dict) -> str:
    """Format stats dict into a Slack-ready message."""
    s = stats
    lines = [
        f"📊 *Статистика бота за тиждень ({s['week_start']}–{s['week_end']})*\n",
        f"Всього повідомлень: *{s['total_messages']}*",
        f"Активних менеджерів: *{s['unique_users']}*\n",
    ]

    # Actions summary
    a = s["actions"]
    if a:
        lines.append("🛠 *По функціях:*")
        labels = {
            "create_project":   "Проєктів створено",
            "update_project":   "Оновлень проєктів",
            "create_subproject": "Підпроєктів створено",
            "record_payment":   "Оплат записано",
            "read_file":        "Файлів прочитано",
        }
        for key, label in labels.items():
            if key in a:
                lines.append(f"  • {label}: {a[key]}")
        lines.append("")

    # Per-user breakdown
    if s["users"]:
        lines.append("👤 *По менеджерах:*")
        for u in s["users"]:
            name = u.get("display_name") or u["slack_user_id"]
            msg  = u["messages"]
            details = []
            if u["created"]:    details.append(f"📁 створено: {u['created']}")
            if u["updated"]:    details.append(f"✏️ оновлень: {u['updated']}")
            if u["payments"]:   details.append(f"💰 оплат: {u['payments']}")
            if u["files"]:      details.append(f"📄 файлів: {u['files']}")
            if u["subprojects"]:details.append(f"🔗 підпроєктів: {u['subprojects']}")
            detail_str = "  " + " | ".join(details) if details else ""
            lines.append(f"• *{name}* — {msg} повідомлень")
            if detail_str:
                lines.append(detail_str)

    if s["total_messages"] == 0:
        lines.append("_Цього тижня бот не використовувався._")

    return "\n".join(lines)
