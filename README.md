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

### Step 3 — Deploy on Render

1. Create a new **Web Service** in the Render dashboard and connect this GitHub repo.
2. Render should detect `render.yaml`. If you prefer manual setup, use these values:

| Setting | Value |
|---|---|
| Environment | Python |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `python bot.py` |
| Plan | Free |

3. Add these environment variables:

| Variable | Value |
|---|---|
| `BOT_TOKEN` | your Telegram bot token |
| `GROQ_KEY` | your Groq API key |
| `CHAT_ID` | your Telegram chat ID |
| `TOPIC_INTERVAL` | `600` (10 min) or `900` (15 min) |
| `QUIZ_DELAY` | `1800` (30 min) |
| `PORT` | `10000` |

4. Add an external uptime check to hit your service every 10 minutes.
	- Set it to request `https://<your-app>.onrender.com/health`
	- UptimeRobot, Better Stack, or a similar ping service works
	- That keeps the free web service warm so it is less likely to sleep

The bot now exposes a tiny HTTP health endpoint for Render while Telegram polling runs in the background.

Note: Render free web services do not have persistent disks, so the SQLite file is ephemeral. That is usually fine here because sessions end after the PDF is learned.

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
├── Procfile          — worker command for generic hosts
├── render.yaml       — Render web service config
└── .env.example      — Environment variable template
```
