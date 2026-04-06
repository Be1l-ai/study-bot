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

### Step 3 — Deploy on Fly.io

1. Install the Fly CLI and sign in:

```bash
fly auth login
```

2. Launch the app from this repo:

```bash
fly launch
```

3. When Fly asks about the existing config, keep `fly.toml` and deploy as a **worker**.
4. Create the persistent volume for SQLite data:

```bash
fly volumes create study_bot_data --size 1 --region iad
```

5. Set your secrets:

```bash
fly secrets set BOT_TOKEN=... GROQ_KEY=... CHAT_ID=...
```

6. Deploy:

```bash
fly deploy
```

7. If needed, scale the worker to 1 machine:

```bash
fly scale count 1
```

Add these environment variables in `fly.toml` or as secrets if you prefer:

| Variable | Value |
|---|---|
| `BOT_TOKEN` | your Telegram bot token |
| `GROQ_KEY` | your Groq API key |
| `CHAT_ID` | your Telegram chat ID |
| `TOPIC_INTERVAL` | `600` (10 min) or `900` (15 min) |
| `QUIZ_DELAY` | `1800` (30 min) |
| `DB_PATH` | `/data/study_bot.db` |

The SQLite database is stored on a Fly volume at `/data/study_bot.db`, so your session state survives restarts.

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
├── Dockerfile        — container build for Fly.io
├── fly.toml          — Fly.io worker + volume config
└── .env.example      — Environment variable template
```
