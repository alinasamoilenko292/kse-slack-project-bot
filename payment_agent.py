"""
Simplified Claude agent for the Payment Bot.
Only budget tools — no Notion, no Drive.
"""
from __future__ import annotations

import os
import logging
import time
import concurrent.futures
from anthropic import Anthropic
from budget_tools import BUDGET_TOOL_DEFINITIONS, execute_budget_tool
from payment_system_prompt import PAYMENT_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], timeout=90.0)

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 2048
MAX_TOOL_LOOPS = 6
TOOL_TIMEOUT = 30   # seconds per tool call
TOOL_RETRY_MAX = 1
TOOL_RETRY_WAIT = 4

# ── Conversation history ───────────────────────────────────────────────────────
_conversations: dict[str, list[dict]] = {}

def get_history(user_id: str) -> list[dict]:
    return _conversations.get(user_id, [])

def set_history(user_id: str, history: list[dict]) -> None:
    _conversations[user_id] = history[-16:]  # keep last 16 messages

def clear_history(user_id: str) -> None:
    _conversations.pop(user_id, None)


# ── Cached prompt helpers ──────────────────────────────────────────────────────
def _cached_system() -> list[dict]:
    return [{"type": "text", "text": PAYMENT_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]

def _cached_tools() -> list:
    tools = [dict(t) for t in BUDGET_TOOL_DEFINITIONS]
    if tools:
        tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
    return tools


# ── Main agent function ────────────────────────────────────────────────────────

def run_payment_agent(slack_user_id: str, user_message: str) -> str:
    """
    Process a payment message through Claude.
    Returns the bot's text response.
    """
    history = get_history(slack_user_id)
    history.append({"role": "user", "content": user_message})

    loop_count = 0
    while loop_count < MAX_TOOL_LOOPS:
        loop_count += 1

        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=_cached_system(),
            tools=_cached_tools(),
            messages=history,
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )

        history.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            text = "".join(
                block.text for block in response.content if hasattr(block, "text")
            )
            set_history(slack_user_id, history)
            return text.strip()

        elif response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                logger.info(f"[payment] Tool call: {block.name}({block.input})")
                result = None

                for _attempt in range(TOOL_RETRY_MAX + 1):
                    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                    try:
                        future = executor.submit(execute_budget_tool, block.name, block.input)
                        result = future.result(timeout=TOOL_TIMEOUT)
                        executor.shutdown(wait=False)
                        break
                    except concurrent.futures.TimeoutError:
                        executor.shutdown(wait=False)
                        if _attempt < TOOL_RETRY_MAX:
                            logger.warning(f"[payment] {block.name} timeout, retry {_attempt + 1}...")
                            time.sleep(TOOL_RETRY_WAIT)
                        else:
                            logger.error(f"[payment] {block.name} timed out after all retries")
                            result = (
                                f"Помилка: Google Sheets не відповів за {TOOL_TIMEOUT}с. "
                                "Повідом користувача що таблиця тимчасово недоступна."
                            )
                    except Exception as e:
                        executor.shutdown(wait=False)
                        logger.error(f"[payment] {block.name} raised: {e}")
                        result = f"Помилка: {e}"
                        break

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

            history.append({"role": "user", "content": tool_results})

        else:
            set_history(slack_user_id, history)
            return "Сталася помилка. Спробуй надіслати сповіщення ще раз."

    set_history(slack_user_id, history)
    return "Агент завершив роботу (ліміт ітерацій). Спробуй переформулювати."
