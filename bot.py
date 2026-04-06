"""
Study Bot

Topic schedule:
  Every TOPIC_INTERVAL seconds a new topic is sent.
  30 minutes after a topic is sent its message is deleted and a quiz is queued.
  While a quiz is being generated + sent, topic delivery is paused.
  After the quiz message is delivered, the TOPIC_INTERVAL clock resets —
  so the next topic arrives TOPIC_INTERVAL seconds after the quiz, not after
  the previous topic. This ensures you receive at most one message per window.

Quiz flow:
  /answer  →  bot shows the oldest pending quiz, one question at a time
              with A / B / C / D inline buttons.
  Answers are compared to the stored correct answers (never sent to you).
  Pass (≥60 %) → topic marked learned.
  Fail         → topic reset, resent on the next topic slot.
  Multiple quizzes can accumulate; /answer always picks the oldest.
"""

import asyncio
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import storage
from llm_processor import generate_quiz, make_interesting
from pdf_processor import process_pdf

# ── Config ────────────────────────────────────────────────────────────────────

def _load_env_file(path: str = ".env") -> None:
    """Load key=value pairs from a local .env file into os.environ."""
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue

        # Allow quoted values in .env while preserving existing exported vars.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("\"", "'"):
            value = value[1:-1]
        else:
            # Support inline comments in unquoted values, e.g. "600  # seconds".
            value = value.split("#", 1)[0].strip()

        os.environ.setdefault(key, value)


_load_env_file()

BOT_TOKEN      = os.environ["BOT_TOKEN"]
GROQ_KEY       = os.environ["GROQ_KEY"]
CHAT_ID        = os.environ["CHAT_ID"]
TOPIC_INTERVAL = int(os.environ.get("TOPIC_INTERVAL", "900"))   # default 15 min
QUIZ_DELAY     = int(os.environ.get("QUIZ_DELAY",     "1800"))  # default 30 min

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Scheduler state (in-memory) ───────────────────────────────────────────────

# Unix timestamp of when the last topic (or quiz) was sent.
# The next topic fires only after TOPIC_INTERVAL seconds from this value.
last_message_at: float = 0.0

# True while a quiz is being generated / sent — blocks topic delivery.
quiz_sending: bool = False

# Active /answer session: which quiz the user is currently working through.
# { "topic_id": int, "q_index": int, "score": int, "questions": [...], "answers": [...] }
answer_session: dict | None = None


# ═════════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════════

def _is_me(update: Update) -> bool:
    return str(update.effective_chat.id) == CHAT_ID


def _format_question(q: dict, index: int, total: int) -> str:
    opts = "\n".join(q["options"])
    return f"🧪 Quiz  (Q{index + 1}/{total})\n\n{q['question']}\n\n{opts}"


def _answer_keyboard(topic_id: int, q_index: int, options: list[str]) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(opt[0], callback_data=f"ans_{topic_id}_{q_index}_{opt[0]}")
        for opt in options
    ]
    return InlineKeyboardMarkup([buttons])


# ═════════════════════════════════════════════════════════════════════════════
# Commands
# ═════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_me(update):
        return
    await update.message.reply_text(
        "📚 Study Bot ready!\n\n"
        "Send me a PDF to begin your session.\n\n"
        "/answer  — answer your oldest pending quiz\n"
        "/status  — see progress\n"
        "/skip    — skip current topic\n"
        "/reset   — clear everything"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_me(update):
        return
    if not storage.has_topics():
        await update.message.reply_text("No active session. Send me a PDF!")
        return
    total   = storage.total_count()
    learned = storage.learned_count()
    pending = storage.pending_quiz_count()
    await update.message.reply_text(
        f"📊 Progress\n\n"
        f"✅ Learned  : {learned} / {total}\n"
        f"🧪 Quizzes pending: {pending}\n"
        f"⏳ Remaining: {total - learned}"
    )


async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_me(update):
        return
    topic = storage.get_next_unsent()
    if not topic:
        await update.message.reply_text("Nothing to skip right now.")
        return
    storage.mark_learned(topic["id"])
    await update.message.reply_text("⏭ Topic skipped and marked as learned.")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_me(update):
        return
    global last_message_at, quiz_sending, answer_session
    storage.clear_all()
    last_message_at = 0.0
    quiz_sending    = False
    answer_session  = None
    await update.message.reply_text("🔄 Reset. Send me a new PDF!")


# ── /answer ───────────────────────────────────────────────────────────────────

async def cmd_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start (or resume) answering the oldest pending quiz."""
    global answer_session
    if not _is_me(update):
        return

    if answer_session:
        # Already in a session — remind user to finish it
        q = answer_session["questions"][answer_session["q_index"]]
        await update.message.reply_text(
            "You already have an active quiz. Answer the question below 👇",
            reply_markup=_answer_keyboard(
                answer_session["topic_id"],
                answer_session["q_index"],
                q["options"],
            ),
        )
        await update.message.reply_text(_format_question(
            q, answer_session["q_index"], len(answer_session["questions"])
        ))
        return

    topic = storage.get_oldest_unanswered_quiz()
    if not topic:
        pending = storage.pending_quiz_count()
        if pending == 0:
            await update.message.reply_text("No pending quizzes right now. Keep studying! 📖")
        return

    questions = json.loads(topic["quiz_questions"])
    answers   = json.loads(topic["quiz_answers"])

    answer_session = {
        "topic_id":  topic["id"],
        "q_index":   0,
        "score":     0,
        "questions": questions,
        "answers":   answers,
    }

    await _send_current_question(context.bot)


async def _send_current_question(bot):
    if not answer_session:
        return
    q       = answer_session["questions"][answer_session["q_index"]]
    total   = len(answer_session["questions"])
    text    = _format_question(q, answer_session["q_index"], total)
    markup  = _answer_keyboard(
        answer_session["topic_id"],
        answer_session["q_index"],
        q["options"],
    )
    await bot.send_message(chat_id=CHAT_ID, text=text, reply_markup=markup)


# ── Inline button answer handler ──────────────────────────────────────────────

async def handle_answer_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """callback_data format: ans_{topic_id}_{q_index}_{letter}"""
    global answer_session, last_message_at

    query = update.callback_query
    await query.answer()

    if not _is_me(update) or not answer_session:
        return

    parts = query.data.split("_")
    if len(parts) != 4 or parts[0] != "ans":
        return

    topic_id = int(parts[1])
    q_index  = int(parts[2])
    chosen   = parts[3]

    # Guard: button must match the active session
    if topic_id != answer_session["topic_id"] or q_index != answer_session["q_index"]:
        await query.edit_message_text("This question is no longer active. Use /answer.")
        return

    correct = answer_session["answers"][q_index]
    q       = answer_session["questions"][q_index]

    if chosen == correct:
        answer_session["score"] += 1
        feedback = f"✅ Correct! ({chosen})"
    else:
        correct_label = next((o for o in q["options"] if o.startswith(correct)), correct)
        feedback = f"❌ Wrong. Correct: {correct_label}"

    # Edit the question message to show result
    await query.edit_message_text(
        f"🧪 Quiz  (Q{q_index + 1}/{len(answer_session['questions'])})\n\n"
        f"{q['question']}\n\n"
        f"Your answer: {chosen}\n{feedback}"
    )

    answer_session["q_index"] += 1

    if answer_session["q_index"] >= len(answer_session["questions"]):
        # ── Quiz complete ──
        score   = answer_session["score"]
        total_q = len(answer_session["questions"])
        passed  = score >= round(total_q * 0.6)  # 60 % threshold
        tid     = answer_session["topic_id"]
        answer_session = None

        storage.mark_quiz_answered(tid)
        await asyncio.sleep(0.5)

        if passed:
            storage.mark_learned(tid)
            learned   = storage.learned_count()
            all_total = storage.total_count()
            await context.bot.send_message(
                chat_id=CHAT_ID,
                text=(
                    f"🎓 Passed! {score}/{total_q}\n"
                    f"Topic learned. ({learned}/{all_total} done)"
                ),
            )
            if storage.all_learned():
                await context.bot.send_message(
                    chat_id=CHAT_ID,
                    text=(
                        "🏆 You've learned every topic in this PDF!\n\n"
                        "Send me a new PDF whenever you're ready."
                    ),
                )
                storage.clear_all()
        else:
            storage.reset_sent(tid)
            # Reset the topic-send clock so this resent topic arrives on the next normal slot
            await context.bot.send_message(
                chat_id=CHAT_ID,
                text=(
                    f"📚 Not quite — {score}/{total_q}. Need 60 % to pass.\n"
                    f"This topic will be resent on the next slot. You've got this! 💪"
                ),
            )

        # Remind about remaining queued quizzes
        remaining_q = storage.pending_quiz_count()
        if remaining_q > 0:
            await context.bot.send_message(
                chat_id=CHAT_ID,
                text=f"📋 You still have {remaining_q} quiz(es) pending. Use /answer when ready.",
            )
    else:
        # Next question
        await asyncio.sleep(0.5)
        await _send_current_question(context.bot)


# ═════════════════════════════════════════════════════════════════════════════
# PDF handler
# ═════════════════════════════════════════════════════════════════════════════

async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_message_at

    if not _is_me(update):
        return

    doc = update.message.document
    if not doc or not doc.file_name.lower().endswith(".pdf"):
        await update.message.reply_text("Please send a .pdf file.")
        return

    if storage.has_topics():
        await update.message.reply_text(
            "⚠️ You have an unfinished session!\n"
            "Use /reset first if you want to start a new PDF."
        )
        return

    status_msg = await update.message.reply_text("📥 Downloading PDF…")

    tg_file = await context.bot.get_file(doc.file_id)
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        await tg_file.download_to_drive(tmp.name)
        pdf_path = tmp.name

    await status_msg.edit_text("🔍 Parsing — removing headers/footers, grouping topics…")

    try:
        raw_topics = process_pdf(pdf_path)
    except Exception as e:
        logger.error("PDF processing error: %s", e)
        await status_msg.edit_text(f"❌ Failed to parse PDF: {e}")
        return
    finally:
        try:
            os.unlink(pdf_path)
        except Exception:
            pass

    if not raw_topics:
        await status_msg.edit_text("❌ Couldn't extract any text from this PDF.")
        return

    await status_msg.edit_text(
        f"🧠 Found {len(raw_topics)} topic(s). Enriching with Groq…\n"
        f"(~{len(raw_topics) * 3}s)"
    )

    processed: list[tuple[str, str]] = []
    for i, raw in enumerate(raw_topics, 1):
        await status_msg.edit_text(f"🧠 Enriching topic {i}/{len(raw_topics)}…")
        interesting = make_interesting(raw, GROQ_KEY)
        processed.append((raw, interesting))

    storage.add_topics(processed)
    last_message_at = 0.0  # send first topic on next loop tick

    await status_msg.edit_text(
        f"✅ Session started!\n\n"
        f"📖 {len(processed)} topics loaded\n"
        f"⏰ Topics every {TOPIC_INTERVAL // 60} min\n"
        f"🧪 Quiz 30 min after each topic — use /answer when ready\n"
        f"First topic arrives shortly."
    )


# ═════════════════════════════════════════════════════════════════════════════
# Scheduler helpers
# ═════════════════════════════════════════════════════════════════════════════

async def _send_next_topic(bot) -> bool:
    """Send next unsent topic. Returns True if sent."""
    topic = storage.get_next_unsent()
    if not topic:
        return False
    text = (
        f"📖 Topic\n\n"
        f"{topic['interesting']}\n\n"
        f"─────────────────────\n"
        f"Quiz in 30 min — use /answer when it arrives"
    )
    try:
        msg = await bot.send_message(chat_id=CHAT_ID, text=text)
        storage.mark_sent(topic["id"], msg.message_id)
        logger.info("Sent topic id=%s", topic["id"])
        return True
    except Exception as e:
        logger.error("Failed to send topic: %s", e)
        return False


async def _run_quiz_for_topic(bot, topic: dict):
    """Delete topic message, generate quiz, store Q+A, send only questions."""
    global quiz_sending, last_message_at

    quiz_sending = True
    logger.info("Generating quiz for topic id=%s", topic["id"])

    # Delete the study message from chat (keep in DB)
    if topic.get("message_id"):
        try:
            await bot.delete_message(chat_id=CHAT_ID, message_id=topic["message_id"])
        except Exception:
            pass

    result = generate_quiz(topic["raw_text"], GROQ_KEY)

    if not result:
        # Quiz generation failed — auto-mark as learned, don't block forever
        logger.warning("Quiz gen failed for topic id=%s, auto-learned", topic["id"])
        storage.mark_learned(topic["id"])
        storage.set_state(f"quiz_generated_{topic['id']}", "1")  # prevent retry loop
        quiz_sending = False
        return

    questions_json = json.dumps(result["questions"])
    answers_json   = json.dumps(result["answers"])
    storage.store_quiz(topic["id"], questions_json, answers_json)

    # Build the quiz message — questions only, no answers
    lines = [f"🧪 Quiz ready for topic #{topic['id']}\n"]
    for i, q in enumerate(result["questions"], 1):
        lines.append(f"Q{i}. {q['question']}")
        for opt in q["options"]:
            lines.append(f"   {opt}")
        lines.append("")
    lines.append("Use /answer to start answering.")

    try:
        await bot.send_message(chat_id=CHAT_ID, text="\n".join(lines))
        storage.mark_quiz_delivered(topic["id"])
        logger.info("Quiz delivered for topic id=%s", topic["id"])
    except Exception as e:
        logger.error("Failed to send quiz message: %s", e)

    # Reset the topic-send clock after quiz delivery so the next topic
    # arrives TOPIC_INTERVAL seconds from NOW (not from the previous topic).
    import time
    last_message_at = time.time()
    quiz_sending    = False


# ═════════════════════════════════════════════════════════════════════════════
# Background scheduler loop
# ═════════════════════════════════════════════════════════════════════════════

async def scheduler_loop(app: Application):
    import time

    global last_message_at, quiz_sending

    await asyncio.sleep(10)
    logger.info("Scheduler loop started.")

    while True:
        try:
            now = time.time()

            # ── 1. Trigger quizzes for overdue topics (one at a time) ────────
            for topic in storage.get_topics_ready_for_quiz():
                await _run_quiz_for_topic(app.bot, topic)
                # _run_quiz_for_topic already resets last_message_at
                now = time.time()
                break  # handle only one per loop tick to keep messages spaced

            # ── 2. Send next topic if interval has elapsed and no quiz sending
            if (
                not quiz_sending
                and storage.has_topics()
                and not storage.all_learned()
                and (now - last_message_at) >= TOPIC_INTERVAL
            ):
                sent = await _send_next_topic(app.bot)
                if sent:
                    last_message_at = time.time()

        except Exception as e:
            logger.error("Scheduler error: %s", e)

        await asyncio.sleep(60)


# ═════════════════════════════════════════════════════════════════════════════
# Startup
# ═════════════════════════════════════════════════════════════════════════════

async def on_startup(app: Application):
    storage.init_db()
    asyncio.create_task(scheduler_loop(app))
    logger.info("Bot started.")


# ═════════════════════════════════════════════════════════════════════════════
# Main
# ═════════════════════════════════════════════════════════════════════════════

def build_app() -> Application:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(on_startup)
        .build()
    )

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("skip",   cmd_skip))
    app.add_handler(CommandHandler("reset",  cmd_reset))
    app.add_handler(CommandHandler("answer", cmd_answer))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    app.add_handler(CallbackQueryHandler(handle_answer_button, pattern=r"^ans_"))
    return app


def main():
    # Retry on transient Telegram timeouts instead of crashing at startup.
    while True:
        app = build_app()

        logger.info("Polling…")
        try:
            app.run_polling(
                drop_pending_updates=True,
                timeout=30,
                read_timeout=30,
                write_timeout=30,
                connect_timeout=30,
                pool_timeout=30,
                bootstrap_retries=5,
            )
            break
        except TimedOut as e:
            logger.warning("Telegram API timed out: %s. Retrying in 5 seconds...", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
