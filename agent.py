"""
Claude agent — conversation logic and tool execution.
Uses Anthropic's Messages API with tool_use.

Optimisations applied:
  - Prompt caching on system prompt + tool definitions (~70% savings on those tokens)
  - History capped at 20 messages (was 40)
  - Tool routing: only relevant tool subsets sent per request
  - claude-haiku for short confirmations ("так", "ні", etc.)
"""
from __future__ import annotations

import os
import logging
from anthropic import Anthropic
from notion_tools import TOOL_DEFINITIONS, execute_tool
from drive_client import DRIVE_TOOL_DEFINITIONS, execute_drive_tool
from budget_tools import BUDGET_TOOL_DEFINITIONS, execute_budget_tool
from system_prompt import SYSTEM_PROMPT
from usage_tracker import log_event, log_tool_calls

logger = logging.getLogger(__name__)
client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

MODEL_SMART = "claude-sonnet-4-6"
MODEL_FAST  = "claude-haiku-4-5-20251001"
MAX_TOKENS  = 4096
MAX_TOKENS_FAST = 1024
MAX_TOOL_LOOPS  = 5

# ── Short confirmations → Haiku ────────────────────────────────────────────────
_CONFIRMATIONS = {
    "так", "ні", "yes", "no", "ок", "ok", "ого", "далі",
    "продовжуй", "зупинись", "скасуй", "підтверджую", "погоджуюсь",
    "1", "2", "3",
}

def _is_simple_confirmation(text: str) -> bool:
    return text.strip().lower() in _CONFIRMATIONS


# ── Tool routing ───────────────────────────────────────────────────────────────
_BUDGET_KEYWORDS = {
    "оплат", "бюджет", "контрагент", "сума документу", "дата платежу",
    "1с", "usd", "дол", "платіж", "fact_", "курс",
}
_DRIVE_KEYWORDS = {
    "файл", "drive", "папк", "docx", "pdf", "програм", "список учасників",
    "завантаж", "зчитай", "освітня", "навчальний план",
}

def _select_tools(message: str) -> list:
    """Return only the tool subsets relevant to this message."""
    msg = message.lower()
    tools = list(TOOL_DEFINITIONS)  # Notion always included

    needs_budget = any(kw in msg for kw in _BUDGET_KEYWORDS)
    needs_drive  = any(kw in msg for kw in _DRIVE_KEYWORDS)

    if needs_budget:
        tools = tools + BUDGET_TOOL_DEFINITIONS
    if needs_drive:
        tools = tools + DRIVE_TOOL_DEFINITIONS

    # If nothing specific detected, include all (safe fallback)
    if not needs_budget and not needs_drive:
        tools = tools + DRIVE_TOOL_DEFINITIONS + BUDGET_TOOL_DEFINITIONS

    return tools


# ── Prompt caching helpers ─────────────────────────────────────────────────────
def _cached_system() -> list[dict]:
    """Wrap system prompt for caching."""
    return [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]


def _cached_tools(tools: list) -> list:
    """Add cache_control to the last tool so the full list is cached."""
    if not tools:
        return tools
    tools = [dict(t) for t in tools]
    tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
    return tools


# ── Conversation state ─────────────────────────────────────────────────────────
# In-memory store: {slack_user_id: [{"role": ..., "content": ...}]}
# Replace with Redis for production server deployment.
_conversations: dict[str, list[dict]] = {}

def get_history(slack_user_id: str) -> list[dict]:
    return _conversations.get(slack_user_id, [])

def set_history(slack_user_id: str, history: list[dict]) -> None:
    _conversations[slack_user_id] = history[-20:]  # keep last 20 (was 40)

def clear_history(slack_user_id: str) -> None:
    _conversations.pop(slack_user_id, None)


# ── Main agent function ────────────────────────────────────────────────────────

def run_agent(
    slack_user_id: str,
    user_message: str,
    notion_user_id: str | None = None,
    user_email: str | None = None,
    display_name: str | None = None,
    file_content: str | None = None,
) -> str:
    """
    Process a user message through the Claude agent.
    Returns the agent's text response.
    """
    history = get_history(slack_user_id)

    # Track incoming message
    log_event(slack_user_id, "message", display_name)

    # Build user turn content
    content = user_message
    if file_content:
        content += f"\n\n[Вміст прикріпленого файлу:]\n{file_content}"

    # Per-message context (who is sending)
    ctx_parts = [f"Slack user ID = {slack_user_id}"]
    if display_name:
        ctx_parts.append(f"Name = {display_name}")
    if user_email:
        ctx_parts.append(f"Email = {user_email}")
    if notion_user_id:
        ctx_parts.append(f"Notion user ID = {notion_user_id}")
    else:
        ctx_parts.append(
            "Notion user ID = NOT FOUND — "
            "if setting Відповідальна особа, skip the person field "
            f"and add a note: 'Відповідальна особа (email): {user_email or slack_user_id}' "
            "in Опис проєкту"
        )

    context_prefix = f"[System context: {', '.join(ctx_parts)}]\n\n"
    history.append({"role": "user", "content": context_prefix + content})

    # Choose model and tools
    is_confirmation = _is_simple_confirmation(user_message) and len(history) > 1
    model      = MODEL_FAST if is_confirmation else MODEL_SMART
    max_tokens = MAX_TOKENS_FAST if is_confirmation else MAX_TOKENS
    tools      = _select_tools(user_message)

    logger.info(f"model={model} tools={len(tools)} history={len(history)} confirmation={is_confirmation}")

    # Agentic loop
    loop_count = 0
    while loop_count < MAX_TOOL_LOOPS:
        loop_count += 1

        # If Haiku triggered a tool_use, subsequent loops need Sonnet for reliability
        if loop_count > 1 and model == MODEL_FAST:
            model      = MODEL_SMART
            max_tokens = MAX_TOKENS

        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=_cached_system(),
            tools=_cached_tools(tools),
            messages=history,
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )

        history.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    text += block.text
            set_history(slack_user_id, history)
            # Log tool actions used in this session turn
            used_tools = [
                b.name
                for msg in history if isinstance(msg.get("content"), list)
                for b in (msg["content"] if isinstance(msg["content"], list) else [])
                if hasattr(b, "type") and b.type == "tool_use"
            ]
            if used_tools:
                log_tool_calls(slack_user_id, used_tools, display_name)
            return text.strip()

        elif response.stop_reason == "tool_use":
            drive_tool_names   = {t["name"] for t in DRIVE_TOOL_DEFINITIONS}
            budget_tool_names  = {t["name"] for t in BUDGET_TOOL_DEFINITIONS}

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    logger.info(f"Tool call: {block.name}({block.input})")
                    if block.name in drive_tool_names:
                        result = execute_drive_tool(block.name, block.input)
                    elif block.name in budget_tool_names:
                        result = execute_budget_tool(block.name, block.input)
                    else:
                        result = execute_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

            history.append({"role": "user", "content": tool_results})

        else:
            set_history(slack_user_id, history)
            return "Сталася помилка при обробці запиту. Спробуй ще раз."

    set_history(slack_user_id, history)
    return "Агент завершив роботу (досягнуто ліміт ітерацій). Спробуй переформулювати запит."
