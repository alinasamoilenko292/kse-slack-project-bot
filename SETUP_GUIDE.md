# 🛠️ Покроковий гайд налаштування Project Documentation Bot

Три частини: Notion, Slack App, деплой backend.

---

## Частина 1 — Notion Integration (15 хв)

### 1.1 Створити Notion Integration

1. Відкрий [notion.so/profile/integrations](https://notion.so/profile/integrations) з **робочого акаунту** KSE GBS
2. Натисни **"New integration"**
3. Заповни:
   - Name: `Project Bot`
   - Associated workspace: `KSE GBS` (або як називається ваш workspace)
   - Capabilities: **Read content ✅, Update content ✅, Insert content ✅**
   - User capabilities: **Read user information including email ✅**
4. Натисни **Save**
5. Скопіюй **Internal Integration Secret** — це і є `NOTION_TOKEN` (формат: `secret_...`)

### 1.2 Дати боту доступ до бази Projects

1. Відкрий базу **Projects** у Notion
2. Натисни `...` (три крапки) у верхньому правому куті сторінки
3. → **Connections** → **Add connections** → знайди `Project Bot`
4. Підтверди доступ

### 1.3 Маппінг менеджерів — нічого робити не потрібно

Бот вирішує це автоматично:

1. Отримує email менеджера зі Slack API при кожному повідомленні
2. Шукає цей email серед усіх користувачів Notion (`/v1/users`)
3. Якщо не знайдено там (гості іноді не видні) — сканує існуючі проєкти в базі, де ці люди вже стоять як "Відповідальна особа", і бере ID звідти
4. Кешує результат у пам'яті — повторні повідомлення не викликають додаткових запитів

**Якщо менеджер зовсім новий і ще жодного разу не стояв у жодному проєкті:**
Бот все одно створить проєкт, але поле "Відповідальна особа" залишить порожнім і додасть email у примітку. Адмін може потім вручну поставити людину у Notion — і наступного разу бот вже знайде її автоматично.

> 💡 **Нічого підтримувати не потрібно.** Ні файлів, ні маппінгів. Нова людина в команді — просто дай їй доступ до бази Projects у Notion, і бот знайде її сам.

---

## Частина 2 — Slack App (20 хв)

### 2.1 Створити Slack App

1. Відкрий [api.slack.com/apps](https://api.slack.com/apps)
2. Натисни **"Create New App"** → **"From scratch"**
3. Назви: `Project Bot`, оберіть ваш Slack workspace → **Create App**

### 2.2 Bot Token Scopes (дозволи)

Перейди: **OAuth & Permissions** → прокрути вниз до **Bot Token Scopes** → натисни **Add an OAuth Scope**

Додай ці scopes:
```
app_mentions:read
channels:history
channels:read
chat:write
chat:write.public
commands
files:read
groups:history
im:history
im:read
im:write
mpim:history
users:read
users:read.email
```

### 2.3 Install App

1. Прокрути вгору на сторінці **OAuth & Permissions**
2. Натисни **"Install to Workspace"** → **Allow**
3. Скопіюй **Bot User OAuth Token** — це `SLACK_BOT_TOKEN` (формат: `xoxb-...`)

### 2.4 App-Level Token (для Socket Mode)

1. Перейди: **Basic Information** → прокрути до **App-Level Tokens**
2. Натисни **"Generate Token and Scopes"**
3. Name: `socket_token`, додай scope: `connections:write`
4. Натисни **Generate** → скопіюй token — це `SLACK_APP_TOKEN` (формат: `xapp-...`)

### 2.5 Увімкнути Socket Mode

1. Перейди: **Socket Mode** → увімкни **Enable Socket Mode ✅**

### 2.6 Event Subscriptions

1. Перейди: **Event Subscriptions** → увімкни **Enable Events ✅**
2. У розділі **Subscribe to bot events** додай:
   - `message.channels`
   - `message.groups`
   - `message.im`
   - `message.mpim`
   - `app_mention`

### 2.7 Slash Command (опціонально але рекомендовано)

1. Перейди: **Slash Commands** → **Create New Command**
2. Command: `/project`
3. Short Description: `Створити або оновити проєкт`
4. Usage Hint: `[new | update | check]`
5. → **Save**

### 2.8 Interactivity (для кнопок і дропдаунів)

1. Перейди: **Interactivity & Shortcuts**
2. Увімкни **Interactivity ✅**
3. Request URL: залиш поки пустим (Socket Mode не потребує URL)

### 2.9 Signing Secret

Перейди: **Basic Information** → **App Credentials** → скопіюй **Signing Secret** — це `SLACK_SIGNING_SECRET`

### 2.10 Встановити бота у приватний канал або DM

В Slack знайди бота `Project Bot` → **Add to channels** → або просто напиши йому в DM.

---

## Частина 3 — Anthropic API Key (5 хв)

> ⚠️ Використовуй **робочий акаунт** KSE GBS, не особистий!

1. Відкрий [console.anthropic.com](https://console.anthropic.com) з робочого акаунту KSE GBS
2. Перейди: **API Keys** (ліве меню)
3. Натисни **"Create Key"**
4. Name: `project-bot`
5. Скопіюй ключ — це `ANTHROPIC_API_KEY` (формат: `sk-ant-...`)

> **Про Claude Console Managed Agents:** поки що використовуємо Anthropic API напряму.
> Claude Console → Agents — це UI-оболонка над тим самим API. Якщо захочеш перенести
> system prompt туди пізніше, просто скопіюй текст з `agent.py` → SYSTEM_PROMPT
> у поле "System prompt" в Claude Console. Functionality не зміниться.

---

## Частина 4 — Запуск backend (10 хв)

### 4.1 Локально для тестування

```bash
# 1. Клонуй або скопіюй папку slack_project_bot

# 2. Створи .env файл
cp .env.example .env
# Відредагуй .env і встав всі токени

# 3. Встанови залежності
pip install -r requirements.txt

# 4. Запусти
python slack_bot.py
```

Ти маєш побачити: `🤖 Project Documentation Bot starting...`

### 4.2 Деплой на Render (безкоштовно для старту)

1. Завантаж код у GitHub репозиторій (приватний)
2. Відкрий [render.com](https://render.com) → **New** → **Web Service**
3. Підключи GitHub репо
4. Налаштування:
   - **Name:** `project-bot`
   - **Environment:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python slack_bot.py`
5. Перейди до **Environment** → додай всі змінні з `.env`:
   - `SLACK_BOT_TOKEN`
   - `SLACK_APP_TOKEN`
   - `SLACK_SIGNING_SECRET`
   - `ANTHROPIC_API_KEY`
   - `NOTION_TOKEN`
6. **Deploy** → готово

> 💡 Render безкоштовний tier "засинає" після 15 хв неактивності.
> Для production використовуй Render Starter ($7/міс) або Railway.

---

## Тест після запуску

1. Відкрий Slack → знайди бота `Project Bot`
2. Напиши: `/project` — має з'явитися меню з кнопками
3. Або напиши: `Хочу створити новий корпоративний проєкт для компанії X`
4. Бот має відповісти і попросити деталі

---

## Структура файлів

```
slack_project_bot/
├── slack_bot.py           ← Slack bot (тонкий шар, ~400 рядків)
├── agent.py               ← Claude AI logic + system prompt
├── notion_tools.py        ← Notion API operations
├── user_resolver.py       ← Slack email → Notion user ID (авто, без маппінгу)
├── schemas.py             ← Required fields per project type
├── config.py              ← Field names, options, database ID
├── requirements.txt
├── .env.example
└── SETUP_GUIDE.md         ← цей файл
```

---

## Що додати пізніше (MVP 2.0)

- [ ] Scheduler для щотижневих нагадувань (APScheduler або cron)
- [ ] Redis для збереження стану розмов між перезапусками
- [ ] Підтримка більшої кількості форматів файлів (DOCX, XLSX)
- [ ] Окремий канал для digest-повідомлень по всіх проєктах з прогалинами
- [ ] Логування у Google Sheets або окрему Notion базу
