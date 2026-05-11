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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
import threading

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

import storage

BOT_TOKEN      = os.environ["BOT_TOKEN"]
GROQ_KEY       = os.environ["GROQ_KEY"]
TOPIC_INTERVAL = int(os.environ.get("TOPIC_INTERVAL", "900"))   # default 15 min
QUIZ_DELAY     = int(os.environ.get("QUIZ_DELAY",     "1800"))  # default 30 min
WEB_PORT       = int(os.environ.get("PORT", "10000"))


def _parse_allowed_chat_ids() -> set[str]:
    raw = os.environ.get("ALLOWED_CHAT_IDS") or os.environ.get("CHAT_ID")
    if not raw:
        return set()
    return {item.strip() for item in raw.split(",") if item.strip()}


ALLOWED_CHAT_IDS = _parse_allowed_chat_ids()


def _default_user_id() -> int | None:
    if len(ALLOWED_CHAT_IDS) != 1:
        return None
    only_value = next(iter(ALLOWED_CHAT_IDS))
    try:
        return int(only_value)
    except ValueError:
        return None


DEFAULT_USER_ID = _default_user_id()

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Scheduler state (in-memory) ───────────────────────────────────────────────

# Active /answer session per user.
# user_id -> { "topic_id": int, "q_index": int, "score": int, "questions": [...], "answers": [...] }
answer_sessions: dict[int, dict] = {}

# True while a quiz is being generated / sent — blocks topic delivery for that user.
quiz_sending_by_user: dict[int, bool] = {}

# Pending /import request per user (value is mode: replace|merge).
pending_imports: dict[int, str] = {}


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._send_health_response(include_body=True)

    def do_HEAD(self):
        self._send_health_response(include_body=False)

    def _send_health_response(self, include_body: bool):
        if self.path in ("/", "/health", "/healthz"):
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if include_body:
                self.wfile.write(body)
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        return


def start_health_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", WEB_PORT), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Health server listening on port %s", WEB_PORT)
    return server


# ═════════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════════

def _is_allowed(update: Update) -> bool:
    if not update.effective_chat:
        return False
    if not ALLOWED_CHAT_IDS:
        return True
    return str(update.effective_chat.id) in ALLOWED_CHAT_IDS


def _is_allowed_user_id(user_id: int) -> bool:
    if not ALLOWED_CHAT_IDS:
        return True
    return str(user_id) in ALLOWED_CHAT_IDS


def _get_last_message_at(user_id: int) -> float:
    raw = storage.get_user_state(user_id, "last_message_at", "0")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _set_last_message_at(user_id: int, timestamp: float) -> None:
    storage.set_user_state(user_id, "last_message_at", str(timestamp))


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
    if not _is_allowed(update):
        return
    await update.message.reply_text(
        "📚 Study Bot ready!\n\n"
        "Send me a PDF to begin your session.\n\n"
        "/answer  — answer your oldest pending quiz\n"
        "/status  — see progress\n"
        "/skip    — skip current topic\n"
        "/reset   — clear everything\n"
        "/export  — backup your data\n"
        "/import  — restore from a backup"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    user_id = update.effective_chat.id
    if not storage.has_topics(user_id):
        await update.message.reply_text("No active session. Send me a PDF!")
        return
    total   = storage.total_count(user_id)
    learned = storage.learned_count(user_id)
    pending = storage.pending_quiz_count(user_id)
    await update.message.reply_text(
        f"📊 Progress\n\n"
        f"✅ Learned  : {learned} / {total}\n"
        f"🧪 Quizzes pending: {pending}\n"
        f"⏳ Remaining: {total - learned}"
    )


async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    user_id = update.effective_chat.id
    topic = storage.get_next_unsent(user_id)
    if not topic:
        await update.message.reply_text("Nothing to skip right now.")
        return
    storage.mark_learned(user_id, topic["id"])
    await update.message.reply_text("⏭ Topic skipped and marked as learned.")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    user_id = update.effective_chat.id
    storage.clear_all(user_id)
    storage.set_user_state(user_id, "last_message_at", "0")
    answer_sessions.pop(user_id, None)
    quiz_sending_by_user.pop(user_id, None)
    pending_imports.pop(user_id, None)
    await update.message.reply_text("🔄 Reset. Send me a new PDF!")


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    user_id = update.effective_chat.id
    chat_id = update.effective_chat.id

    payload = storage.export_user_data(user_id)
    filename = f"study_bot_backup_{user_id}.json"

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
        json.dump(payload, tmp, ensure_ascii=True, indent=2)
        tmp_path = tmp.name

    try:
        with open(tmp_path, "rb") as file_obj:
            await context.bot.send_document(chat_id=chat_id, document=file_obj, filename=filename)
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


async def cmd_import(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    user_id = update.effective_chat.id
    mode = "replace"
    if context.args and context.args[0].lower() == "merge":
        mode = "merge"
    pending_imports[user_id] = mode
    await update.message.reply_text(
        "📦 Send your JSON backup file in this chat to import it."
    )


# ── /answer ───────────────────────────────────────────────────────────────────

async def cmd_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start (or resume) answering the oldest pending quiz."""
    if not _is_allowed(update):
        return
    user_id = update.effective_chat.id
    chat_id = update.effective_chat.id
    session = answer_sessions.get(user_id)

    if session:
        # Already in a session — remind user to finish it
        q = session["questions"][session["q_index"]]
        await update.message.reply_text(
            "You already have an active quiz. Answer the question below 👇",
            reply_markup=_answer_keyboard(
                session["topic_id"],
                session["q_index"],
                q["options"],
            ),
        )
        await update.message.reply_text(
            _format_question(q, session["q_index"], len(session["questions"]))
        )
        return

    topic = storage.get_oldest_unanswered_quiz(user_id)
    if not topic:
        pending = storage.pending_quiz_count(user_id)
        if pending == 0:
            await update.message.reply_text("No pending quizzes right now. Keep studying! 📖")
        return

    questions = json.loads(topic["quiz_questions"])
    answers   = json.loads(topic["quiz_answers"])

    answer_sessions[user_id] = {
        "topic_id":  topic["id"],
        "q_index":   0,
        "score":     0,
        "questions": questions,
        "answers":   answers,
    }

    await _send_current_question(context.bot, user_id, chat_id)


async def _send_current_question(bot, user_id: int, chat_id: int):
    session = answer_sessions.get(int(user_id))
    if not session:
        return
    q       = session["questions"][session["q_index"]]
    total   = len(session["questions"])
    text    = _format_question(q, session["q_index"], total)
    markup  = _answer_keyboard(
        session["topic_id"],
        session["q_index"],
        q["options"],
    )
    await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)


# ── Inline button answer handler ──────────────────────────────────────────────

async def handle_answer_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """callback_data format: ans_{topic_id}_{q_index}_{letter}"""
    query = update.callback_query
    await query.answer()

    if not _is_allowed(update):
        return

    user_id = update.effective_chat.id
    session = answer_sessions.get(user_id)
    if not session:
        return

    parts = query.data.split("_")
    if len(parts) != 4 or parts[0] != "ans":
        return

    topic_id = int(parts[1])
    q_index  = int(parts[2])
    chosen   = parts[3]

    # Guard: button must match the active session
    if topic_id != session["topic_id"] or q_index != session["q_index"]:
        await query.edit_message_text("This question is no longer active. Use /answer.")
        return

    correct = session["answers"][q_index]
    q       = session["questions"][q_index]

    if chosen == correct:
        session["score"] += 1
        feedback = f"✅ Correct! ({chosen})"
    else:
        correct_label = next((o for o in q["options"] if o.startswith(correct)), correct)
        feedback = f"❌ Wrong. Correct: {correct_label}"

    # Edit the question message to show result
    await query.edit_message_text(
        f"🧪 Quiz  (Q{q_index + 1}/{len(session['questions'])})\n\n"
        f"{q['question']}\n\n"
        f"Your answer: {chosen}\n{feedback}"
    )

    session["q_index"] += 1

    if session["q_index"] >= len(session["questions"]):
        # ── Quiz complete ──
        score   = session["score"]
        total_q = len(session["questions"])
        passed  = score >= round(total_q * 0.6)  # 60 % threshold
        tid     = session["topic_id"]
        answer_sessions.pop(user_id, None)

        storage.mark_quiz_answered(user_id, tid)
        await asyncio.sleep(0.5)

        if passed:
            storage.mark_learned(user_id, tid)
            learned   = storage.learned_count(user_id)
            all_total = storage.total_count(user_id)
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"🎓 Passed! {score}/{total_q}\n"
                    f"Topic learned. ({learned}/{all_total} done)"
                ),
            )
            if storage.all_learned(user_id):
                await context.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "🏆 You've learned every topic in this PDF!\n\n"
                        "Send me a new PDF whenever you're ready."
                    ),
                )
                storage.clear_all(user_id)
                quiz_sending_by_user.pop(user_id, None)
                pending_imports.pop(user_id, None)
        else:
            storage.reset_sent(user_id, tid)
            # Reset the topic-send clock so this resent topic arrives on the next normal slot
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"📚 Not quite — {score}/{total_q}. Need 60 % to pass.\n"
                    f"This topic will be resent on the next slot. You've got this! 💪"
                ),
            )

        # Remind about remaining queued quizzes
        remaining_q = storage.pending_quiz_count(user_id)
        if remaining_q > 0:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"📋 You still have {remaining_q} quiz(es) pending. Use /answer when ready.",
            )
    else:
        # Next question
        await asyncio.sleep(0.5)
        await _send_current_question(context.bot, user_id, user_id)


# ═════════════════════════════════════════════════════════════════════════════
# PDF handler
# ═════════════════════════════════════════════════════════════════════════════

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return

    doc = update.message.document
    if not doc:
        return

    user_id = update.effective_chat.id
    filename = (doc.file_name or "").lower()
    is_pdf = filename.endswith(".pdf") or doc.mime_type == "application/pdf"
    is_json = filename.endswith(".json") or doc.mime_type == "application/json"

    if is_json:
        if user_id not in pending_imports:
            await update.message.reply_text("Use /import first, then send your backup file here.")
            return

        mode = pending_imports.get(user_id, "replace")
        status_msg = await update.message.reply_text("⬇️ Downloading backup…")
        tg_file = await context.bot.get_file(doc.file_id)
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            await tg_file.download_to_drive(tmp.name)
            json_path = tmp.name

        try:
            data = json.loads(Path(json_path).read_text(encoding="utf-8"))
            result = storage.import_user_data(user_id, data, mode=mode)
        except Exception as e:
            logger.error("Import failed: %s", e)
            await status_msg.edit_text(f"❌ Import failed: {e}")
        else:
            await status_msg.edit_text(
                f"✅ Import complete. Topics: {result['topics']}, State: {result['state']}"
            )
        finally:
            pending_imports.pop(user_id, None)
            try:
                os.unlink(json_path)
            except Exception:
                pass
        return

    if not is_pdf:
        await update.message.reply_text("Please send a PDF or a JSON backup file.")
        return

    if storage.has_topics(user_id):
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

    storage.add_topics(user_id, processed)
    _set_last_message_at(user_id, 0.0)  # send first topic on next loop tick

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

async def _send_next_topic(bot, user_id: int) -> bool:
    """Send next unsent topic. Returns True if sent."""
    topic = storage.get_next_unsent(user_id)
    if not topic:
        return False
    text = (
        f"📖 Topic\n\n"
        f"{topic['interesting']}\n\n"
        f"─────────────────────\n"
        f"Quiz in 30 min — use /answer when it arrives"
    )
    try:
        msg = await bot.send_message(chat_id=user_id, text=text)
        storage.mark_sent(user_id, topic["id"], msg.message_id)
        logger.info("Sent topic id=%s user_id=%s", topic["id"], user_id)
        return True
    except Exception as e:
        logger.error("Failed to send topic: %s", e)
        return False


async def _run_quiz_for_topic(bot, user_id: int, topic: dict):
    """Delete topic message, generate quiz, store Q+A, send only questions."""
    quiz_sending_by_user[user_id] = True
    logger.info("Generating quiz for topic id=%s user_id=%s", topic["id"], user_id)

    # Delete the study message from chat (keep in DB)
    if topic.get("message_id"):
        try:
            await bot.delete_message(chat_id=user_id, message_id=topic["message_id"])
        except Exception:
            pass

    result = generate_quiz(topic["raw_text"], GROQ_KEY)

    if not result:
        # Quiz generation failed — auto-mark as learned, don't block forever
        logger.warning("Quiz gen failed for topic id=%s, auto-learned", topic["id"])
        storage.mark_learned(user_id, topic["id"])
        storage.set_state(f"quiz_generated_{topic['id']}", "1")  # prevent retry loop
        quiz_sending_by_user[user_id] = False
        return

    questions_json = json.dumps(result["questions"])
    answers_json   = json.dumps(result["answers"])
    storage.store_quiz(user_id, topic["id"], questions_json, answers_json)

    # Build the quiz message — questions only, no answers
    lines = [f"🧪 Quiz ready for topic #{topic['id']}\n"]
    for i, q in enumerate(result["questions"], 1):
        lines.append(f"Q{i}. {q['question']}")
        for opt in q["options"]:
            lines.append(f"   {opt}")
        lines.append("")
    lines.append("Use /answer to start answering.")

    try:
        await bot.send_message(chat_id=user_id, text="\n".join(lines))
        storage.mark_quiz_delivered(user_id, topic["id"])
        logger.info("Quiz delivered for topic id=%s user_id=%s", topic["id"], user_id)
    except Exception as e:
        logger.error("Failed to send quiz message: %s", e)

    # Reset the topic-send clock after quiz delivery so the next topic
    # arrives TOPIC_INTERVAL seconds from NOW (not from the previous topic).
    import time
    _set_last_message_at(user_id, time.time())
    quiz_sending_by_user[user_id] = False


# ═════════════════════════════════════════════════════════════════════════════
# Background scheduler loop
# ═════════════════════════════════════════════════════════════════════════════

async def scheduler_loop(app: Application):
    import time
    await asyncio.sleep(10)
    logger.info("Scheduler loop started.")

    while True:
        try:
            now = time.time()
            user_ids = storage.get_active_user_ids()

            for user_id in user_ids:
                if not _is_allowed_user_id(user_id):
                    continue

                if quiz_sending_by_user.get(user_id, False):
                    continue

                ready = storage.get_topics_ready_for_quiz(user_id, QUIZ_DELAY)
                if ready:
                    await _run_quiz_for_topic(app.bot, user_id, ready[0])
                    now = time.time()
                    continue

                if not storage.has_topics(user_id) or storage.all_learned(user_id):
                    continue

                last_message_at = _get_last_message_at(user_id)
                if (now - last_message_at) >= TOPIC_INTERVAL:
                    sent = await _send_next_topic(app.bot, user_id)
                    if sent:
                        _set_last_message_at(user_id, time.time())

        except Exception as e:
            logger.error("Scheduler error: %s", e)

        await asyncio.sleep(60)


# ═════════════════════════════════════════════════════════════════════════════
# Startup
# ═════════════════════════════════════════════════════════════════════════════

async def on_startup(app: Application):
    storage.init_db(default_user_id=DEFAULT_USER_ID)
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
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("import", cmd_import))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(CallbackQueryHandler(handle_answer_button, pattern=r"^ans_"))
    return app


def main():
    # Retry on transient Telegram timeouts instead of crashing at startup.
    start_health_server()
    while True:
        app = build_app()

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

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
        finally:
            try:
                loop.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
