"""
Configuration for Project Documentation Bot
Based on real Projects database schema: 173122fe-7695-80c6-96b3-000b08f81c95
"""

# ── Notion ─────────────────────────────────────────────────────────────────────
NOTION_DATABASE_ID = "173122fe-7695-802d-8044-deaf506eb93e"

# ── Project types (exact values from Notion select field) ──────────────────────
PROJECT_TYPES = [
    "Корпоративна програма",
    "Грантова програма",
    "Тендер",
    "Open program",
    "Intensive Courses",
    "Brigades",
    "Курс MBAs",
    "Магістерська програма",
    "Модуль Надра",
    "SFA",
    "Внутрішній",
    "Ed Innovation",
]

# ── Statuses ────────────────────────────────────────────────────────────────────
STATUSES = ["Backlog", "Planning", "In progress", "Paused", "Done", "Cancelled"]
DEFAULT_STATUS = "Planning"

# ── Academic Directors (multi_select — exact option names) ──────────────────────
ACADEMIC_DIRECTORS = [
    "Anastasiia Povzhyk",
    "Mykhailo Kolisnyk",
    "Oleksii Hromyko",
    "Olena Khomenko",
    "Maryna Huz",
    "Oleg Zubchenok",
    "Центр взаємодії з владою",
]

# ── Target audiences (multi_select) ────────────────────────────────────────────
TARGET_AUDIENCES = [
    "Наші студенти",
    "Entry",
    "C-level",
    "Власники бізнесів",
    "Керівники",
    "Учасники зі статусом ветерана та/або з родинними зв'язками із ветераном",
    "Менеджери середньої ланки",
    "Проєктні менеджери",
    "Підприємці України",
]

# ── Legal entities (Юрособа select) ────────────────────────────────────────────
LEGAL_ENTITIES = ["ГО", "БФ РОУ", "БФ", "ТОВ", "ПУ"]

# ── Formats (multi_select) ──────────────────────────────────────────────────────
FORMATS = ["Гібрид", "Онлайн", "Офлайн"]

# ── Learning topics (multi_select) ─────────────────────────────────────────────
LEARNING_TOPICS = ["Лідерство", "Підприємництво"]

# ── Field display names (for bot messages) ─────────────────────────────────────
FIELD_LABELS = {
    "Назва": "Назва проєкту",
    "Тип проєкту": "Тип проєкту",
    "Відповідальна особа": "Відповідальна особа (Project Manager)",
    "Дати проведення": "Дати проведення",
    "К-ть учасників": "К-ть учасників",
    "Syllabus / Посилання на папку проєкту": "Посилання на папку проєкту",
    "Статус": "Статус",
    "Опис проєкту": "Опис проєкту",
    "Цілі проєкту": "Цілі проєкту",
    "KPI проєкту": "KPI проєкту",
    "Цільова аудиторія": "Цільова аудиторія",
    "Academic Director": "Academic Director",
    "Юрособа": "Юрособа",
    "Формат": "Формат навчання",
    "Назва проєкту в 1с": "Назва проєкту в 1С",
    "Plan": "Бюджет (план)",
    "Лінк на Moodle": "Лінк на Moodle",
    "Папка фінальних проєктів": "Папка фінальних проєктів",
    "Фінальні проєкти (концепція)": "Концепція фінального завдання",
    "Learning outcomes": "Learning outcomes",
    "Кредити ЄКТС": "Кредити ЄКТС",
}

# ── Fields that require explicit user confirmation before update ────────────────
CRITICAL_FIELDS = {
    "Тип проєкту",
    "Відповідальна особа",
    "Юрособа",
    "План",
    "Parent project",
    "Статус",  # except when setting to Planning on new project
}

# ── Actions bot is NEVER allowed to perform ────────────────────────────────────
FORBIDDEN_ACTIONS = [
    "delete_page",
    "delete_property",
    "create_property",
    "rename_property",
    "modify_database_schema",
    "bulk_overwrite",
]

# ── Notion property types (for API payload construction) ───────────────────────
PROPERTY_TYPES = {
    "Назва": "title",
    "Тип проєкту": "select",
    "Статус": "status",
    "Відповідальна особа": "people",
    "Coordinator": "people",
    "Project Manager": "people",
    "Marketer": "people",
    "Дати проведення": "date",
    "К-ть учасників": "number",
    "Кредити ЄКТС": "number",
    "План": "number",
    "Факт": "number",
    "Syllabus / Посилання на папку проєкту": "files",
    "Лінк на Moodle": "url",
    "Папка фінальних проєктів": "url",
    "Опис проєкту": "rich_text",
    "Цілі проєкту": "rich_text",
    "KPI проєкту": "rich_text",
    "Фінальні проєкти (концепція)": "rich_text",
    "Learning outcomes": "rich_text",
    "Результати ретро-сесії": "rich_text",
    "Оцінка від АД": "rich_text",
    "Фідбек менеджера на роботу АД": "rich_text",
    "Назва проєкту в 1с": "rich_text",
    "Academic Director": "multi_select",
    "Цільова аудиторія": "multi_select",
    "Формат": "multi_select",
    "Тема навчання": "multi_select",
    "Юрособа": "select",
    "Чи погоджений бюджет з Олею?": "select",
    "Чи проведена зустріч замовника та викладача?": "select",
    "Parent project": "relation",
    "Sub-projects": "relation",
}
