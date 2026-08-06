"""
Payment Bot — окремий Slack-застосунок для запису оплат у Google Sheets.
Приймає сповіщення про оплати з 1С через DM, пише у бюджетну таблицю.

Env vars потрібні (окремі від основного бота):
  PAYMENT_SLACK_BOT_TOKEN   — Bot User OAuth Token нового Slack-застосунку
  PAYMENT_SLACK_APP_TOKEN   — App-Level Token (Socket Mode)
  ANTHROPIC_API_KEY         — той самий що й у основного бота
  GOOGLE_SERVICE_ACCOUNT_PATH — той самий (google_credentials.json)
  BUDGET_SPREADSHEET_ID     — той самий
"""
from __future__ import annotations

import os
import logging
import concurrent.futures
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env.payment"))

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from payment_agent import run_payment_agent, clear_history

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = App(token=os.environ["PAYMENT_SLACK_BOT_TOKEN"])

# Hard ceiling — bot always replies within this time
AGENT_TIMEOUT = 130  # seconds

def _run_safe(slack_user_id: str, user_message: str) -> str:
    """Run payment agent with global timeout."""
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(
            run_payment_agent,
            slack_user_id=slack_user_id,
            user_message=user_message,
        )
        return future.result(timeout=AGENT_TIMEOUT)
    except concurrent.futures.TimeoutError:
        executor.shutdown(wait=False)
        logger.error(f"[TIMEOUT] Payment agent exceeded {AGENT_TIMEOUT}s for user {slack_user_id}")
        return (
            "⏳ Запит зайняв надто довго — Google Sheets тимчасово не відповідає.\n"
            "Спробуй надіслати сповіщення ще раз за хвилину."
        )
    except Exception as e:
        executor.shutdown(wait=False)
        logger.error(f"[ERROR] Payment agent: {e}", exc_info=True)
        return f"⚠️ Помилка: {e}"
    finally:
        executor.shutdown(wait=False)


@app.message()
def handle_dm(message, say, client):
    """Handle DMs — only process direct messages."""
    # Only DMs
    if message.get("channel_type") not in ("im",):
        return

    # Ignore edits, deletes, bot messages
    if message.get("subtype") in ("message_changed", "message_deleted", "bot_message"):
        return
    if message.get("edited"):
        return

    slack_user_id = message.get("user")
    text = message.get("text", "").strip()

    if not slack_user_id or not text:
        return

    # Reset conversation shortcut
    if text.lower() in ("скинути", "скидання", "reset", "/reset", "нова оплата"):
        clear_history(slack_user_id)
        say(text="🔄 Розмову скинуто. Надішли нове сповіщення про оплату.")
        return

    # Show typing indicator
    client.chat_postEphemeral(
        channel=message["channel"],
        user=slack_user_id,
        text="⏳ Обробляю...",
    )

    response = _run_safe(slack_user_id=slack_user_id, user_message=text)
    say(text=response)


if __name__ == "__main__":
    handler = SocketModeHandler(app, os.environ["PAYMENT_SLACK_APP_TOKEN"])
    logger.info("💰 Payment Bot starting...")
    handler.start()
