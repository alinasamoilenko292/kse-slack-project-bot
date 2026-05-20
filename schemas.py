"""
Project field schemas — defines required, critical, and recommended fields
per project type. This is the "brain" of the validation logic.
"""
from __future__ import annotations

from config import PROJECT_TYPES

# ── Always required (for ALL project types) ────────────────────────────────────
ALWAYS_REQUIRED = [
    "Назва",
    "Тип проєкту",
    "Відповідальна особа",
    "Дати проведення",
    "К-ть учасників",
    "Syllabus / Посилання на папку проєкту",
]

# ── Conditional required by project type ───────────────────────────────────────
TYPE_REQUIRED: dict[str, list[str]] = {
    "Грантова програма": [
        "Опис проєкту",
        "Цілі проєкту",
        "KPI проєкту",
        "Цільова аудиторія",
    ],
    "Корпоративна програма": [
        "Academic Director",
        "Формат",
    ],
    "Open program": [
        "Цільова аудиторія",
        "Формат",
    ],
    "Intensive Courses": [
        "Academic Director",
        "Формат",
        "Цільова аудиторія",
    ],
    "Курс MBAs": [
        "Academic Director",
        "Формат",
    ],
    "Магістерська програма": [
        "Academic Director",
        "Формат",
        "Цільова аудиторія",
    ],
    "Brigades": [
        "Цільова аудиторія",
        "Опис проєкту",
    ],
    "SFA": [
        "Цільова аудиторія",
        "Опис проєкту",
    ],
    "Тендер": [],
    "Модуль Надра": [],
    "Внутрішній": [],
    "Ed Innovation": [],
}

# ── Recommended (collected if available, not blocking) ─────────────────────────
TYPE_RECOMMENDED: dict[str, list[str]] = {
    "Грантова програма": [
        "Юрособа",
        "Learning outcomes",
    ],
    "Корпоративна програма": [
        "Юрособа",
        "Назва проєкту в 1с",
        "Цільова аудиторія",
        "Learning outcomes",
        "Фінальні проєкти (концепція)",
    ],
    "Open program": [
        "Юрособа",
        "Фінальні проєкти (концепція)",
    ],
    "Intensive Courses": [
        "Фінальні проєкти (концепція)",
        "Лінк на Moodle",
    ],
    "Курс MBAs": [
        "Юрособа",
        "Фінальні проєкти (концепція)",
        "Learning outcomes",
        "Лінк на Moodle",
    ],
    "Магістерська програма": [
        "Юрособа",
        "Фінальні проєкти (концепція)",
        "Лінк на Moodle",
    ],
    "Brigades": [
        "Юрособа",
    ],
    "SFA": [
        "Юрособа",
    ],
    "Тендер": [
        "Юрособа",
        "Опис проєкту",
    ],
    "Модуль Надра": [],
    "Внутрішній": [],
    "Ed Innovation": [],
}

# ── Sub-project: fields that can be copied from parent ─────────────────────────
SUBPROJECT_INHERIT_FROM_PARENT = [
    "Тип проєкту",
    "Відповідальна особа",
    "Academic Director",
    "Юрособа",
    "Цільова аудиторія",
    "Формат",
    "Тема навчання",
]

# ── Sub-project: fields that MUST be set individually ─────────────────────────
SUBPROJECT_INDIVIDUAL_REQUIRED = [
    "Назва",
    "Дати проведення",
    "Syllabus / Посилання на папку проєкту",
    # К-ть учасників is inherited unless it differs (e.g. EMBA)
]


def get_required_fields(project_type: str) -> list[str]:
    """Return full list of required fields for a given project type."""
    extra = TYPE_REQUIRED.get(project_type, [])
    return ALWAYS_REQUIRED + [f for f in extra if f not in ALWAYS_REQUIRED]


def get_recommended_fields(project_type: str) -> list[str]:
    """Return recommended (non-blocking) fields for a given project type."""
    return TYPE_RECOMMENDED.get(project_type, [])


def get_missing_required(project_data: dict, project_type: str) -> list[str]:
    """
    Given a dict of already-collected field values and a project type,
    return the list of field names that are still missing or empty.
    """
    required = get_required_fields(project_type)
    missing = []
    for field in required:
        value = project_data.get(field)
        if value is None or value == "" or value == [] or value == {}:
            missing.append(field)
    return missing


def get_missing_recommended(project_data: dict, project_type: str) -> list[str]:
    """Return list of recommended fields that are still empty."""
    recommended = get_recommended_fields(project_type)
    missing = []
    for field in recommended:
        value = project_data.get(field)
        if value is None or value == "" or value == [] or value == {}:
            missing.append(field)
    return missing


def format_missing_list(missing_fields: list[str], start_index: int = 1) -> str:
    """
    Format a list of missing fields as a numbered list for Slack messages.
    Example:
        1. Дати проведення
        2. К-ть учасників
        3. Посилання на папку проєкту
    """
    from config import FIELD_LABELS
    lines = []
    for i, field in enumerate(missing_fields, start=start_index):
        label = FIELD_LABELS.get(field, field)
        lines.append(f"{i}. {label}")
    return "\n".join(lines)


def parse_numbered_answers(text: str, missing_fields: list[str]) -> dict[str, str]:
    """
    Parse user responses in format:
        1 — value
        2 — value
    Returns dict of {field_name: value}.
    Skips numbers not present in the text.
    """
    import re
    result = {}
    lines = text.strip().split("\n")
    for line in lines:
        # Match patterns: "1 — value", "1 - value", "1. value", "1: value"
        match = re.match(r"^(\d+)\s*[—\-\.:\s]\s*(.+)$", line.strip())
        if match:
            idx = int(match.group(1)) - 1
            value = match.group(2).strip()
            if 0 <= idx < len(missing_fields):
                result[missing_fields[idx]] = value
    return result
