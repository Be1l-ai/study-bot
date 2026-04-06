# Study Bot

A Telegram bot that turns any PDF into a timed study session with quizzes.

## How it works

1. You send a PDF to the bot
2. It extracts text page-by-page, strips headers/footers, and groups pages into topics by shared dates and place names
3. Each topic is rewritten by Groq (Llama 3.3 70B) to be more engaging — no facts added or removed
4. Every 10 minutes you receive a new topic
5. After 30 minutes the topic message is deleted and a 3-question quiz is sent
6. Pass (≥60%) → topic marked as learned. Fail → topic gets resent
7. Once all topics are learned, session ends and bot waits for a new PDF

---

## Setup (5 steps)

### Step 1 — Get your API keys

**Telegram bot token**
- Message [@BotFather](https://t.me/BotFather) → `/newbot` → follow prompts → copy the token

**Your Telegram Chat ID**
- Message [@userinfobot](https://t.me/userinfobot) → it replies with your ID

**Groq API key**
- Sign up at [console.groq.com](https://console.groq.com) → API Keys → Create

---

### Step 2 — Push code to GitHub

```bash
git init
git add .
git commit -m "initial"
gh repo create study-bot --private --push --source=.
```

---

### Step 3 — Deploy on Railway

1. Go to [railway.app](https://railway.app) and sign in with GitHub
2. Click **New Project → Deploy from GitHub repo** → select `study-bot`
3. Railway will detect the `Procfile` and configure it as a **worker** automatically
4. Go to your project → **Variables** tab → add these:

| Variable | Value |
|---|---|
| `BOT_TOKEN` | your Telegram bot token |
| `GROQ_KEY` | your Groq API key |
| `CHAT_ID` | your Telegram chat ID |
| `TOPIC_INTERVAL` | `600` (10 min) or `900` (15 min) |
| `QUIZ_DELAY` | `1800` (30 min) |

5. Railway will automatically redeploy. Your bot is live.

> **Note:** Railway's free Hobby tier gives $5/month free credit. A small worker like this uses roughly $0.50–$1/month so it fits comfortably.

---

### Step 4 — Test it

1. Open Telegram, find your bot, send `/start`
2. Send any PDF
3. Wait for the first topic (arrives within ~1 minute)

---

## Commands

| Command | What it does |
|---|---|
| `/start` | Welcome message |
| `/status` | Show learned vs remaining topics |
| `/skip` | Skip and mark current topic as learned |
| `/reset` | Clear everything, ready for a new PDF |

---

## Local development

```bash
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# copy and fill in your keys
cp .env.example .env

# run
python bot.py
```

> SQLite database (`study_bot.db`) is created automatically in the working directory.

---

## File overview

```
study-bot/
├── bot.py            — Telegram bot + background scheduler loop
├── pdf_processor.py  — PDF → pages → clean → topic groups
├── llm_processor.py  — Groq API: enrich topics, generate quizzes
├── storage.py        — SQLite wrapper (topics, state)
├── requirements.txt
├── Procfile          — Railway worker config
└── .env.example      — Environment variable template
```
