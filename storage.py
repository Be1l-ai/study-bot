import os
import sqlite3
from datetime import datetime

DB_PATH = os.environ.get("DB_PATH", "study_bot.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(default_user_id: int | None = None):
    conn = get_conn()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS topics (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL DEFAULT 0,
            raw_text        TEXT NOT NULL,
            interesting     TEXT,

            -- topic delivery
            sent            INTEGER DEFAULT 0,
            learned         INTEGER DEFAULT 0,
            sent_at         TEXT,
            message_id      INTEGER,

            -- quiz (generated at 30-min mark, after topic msg is deleted)
            quiz_questions  TEXT,    -- JSON [{question, options}]  — sent to user
            quiz_answers    TEXT,    -- JSON ["A","C","B"]          — stored only, never sent
            quiz_generated  INTEGER DEFAULT 0,
            quiz_delivered  INTEGER DEFAULT 0,
            quiz_answered   INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS state (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS user_state (
            user_id INTEGER NOT NULL,
            key     TEXT NOT NULL,
            value   TEXT,
            PRIMARY KEY (user_id, key)
        );
        CREATE INDEX IF NOT EXISTS idx_topics_user_status
            ON topics (user_id, sent, learned);
        CREATE INDEX IF NOT EXISTS idx_topics_user_quiz
            ON topics (user_id, quiz_delivered, quiz_answered);
    ''')

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(topics)").fetchall()}
    if "user_id" not in columns:
        conn.execute("ALTER TABLE topics ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0")
        columns.add("user_id")

    if default_user_id is not None:
        conn.execute("UPDATE topics SET user_id=? WHERE user_id=0", (int(default_user_id),))

    conn.commit()
    conn.close()


# ── Topics ────────────────────────────────────────────────────────────────────

def add_topics(user_id: int, topics):
    """topics = list of (raw_text, interesting_text)"""
    conn = get_conn()
    conn.executemany(
        "INSERT INTO topics (user_id, raw_text, interesting) VALUES (?, ?, ?)",
        [(int(user_id), raw, interesting) for raw, interesting in topics],
    )
    conn.commit()
    conn.close()


def get_next_unsent(user_id: int):
    conn = get_conn()
    row = conn.execute(
        """SELECT * FROM topics
           WHERE user_id=? AND sent=0 AND learned=0
           ORDER BY id LIMIT 1""",
        (int(user_id),),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def mark_sent(user_id: int, topic_id, message_id):
    conn = get_conn()
    conn.execute(
        """UPDATE topics
           SET sent=1, sent_at=?, message_id=?
           WHERE id=? AND user_id=?""",
        (datetime.utcnow().isoformat(), message_id, topic_id, int(user_id)),
    )
    conn.commit()
    conn.close()


def mark_learned(user_id: int, topic_id):
    conn = get_conn()
    conn.execute(
        "UPDATE topics SET learned=1 WHERE id=? AND user_id=?",
        (topic_id, int(user_id)),
    )
    conn.commit()
    conn.close()


def reset_sent(user_id: int, topic_id):
    """Wipe delivery + quiz state so the topic gets re-sent (after a failed quiz)."""
    conn = get_conn()
    conn.execute(
        """UPDATE topics
           SET sent=0, sent_at=NULL, message_id=NULL,
               quiz_generated=0, quiz_delivered=0, quiz_answered=0,
               quiz_questions=NULL, quiz_answers=NULL
           WHERE id=? AND user_id=?""",
        (topic_id, int(user_id)),
    )
    conn.commit()
    conn.close()


def get_topic_by_id(user_id: int, topic_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM topics WHERE id=? AND user_id=?",
        (topic_id, int(user_id)),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# ── Quiz ──────────────────────────────────────────────────────────────────────

def store_quiz(user_id: int, topic_id, questions_json: str, answers_json: str):
    """Save generated quiz. questions_json contains no correct-answer info."""
    conn = get_conn()
    conn.execute(
        """UPDATE topics
           SET quiz_questions=?, quiz_answers=?, quiz_generated=1
           WHERE id=? AND user_id=?""",
        (questions_json, answers_json, topic_id, int(user_id)),
    )
    conn.commit()
    conn.close()


def mark_quiz_delivered(user_id: int, topic_id):
    conn = get_conn()
    conn.execute(
        "UPDATE topics SET quiz_delivered=1 WHERE id=? AND user_id=?",
        (topic_id, int(user_id)),
    )
    conn.commit()
    conn.close()


def mark_quiz_answered(user_id: int, topic_id):
    conn = get_conn()
    conn.execute(
        "UPDATE topics SET quiz_answered=1 WHERE id=? AND user_id=?",
        (topic_id, int(user_id)),
    )
    conn.commit()
    conn.close()


def get_topics_ready_for_quiz(user_id: int, delay_seconds: int = 1800):
    """Topics sent delay_seconds ago that haven't had a quiz generated yet."""
    try:
        seconds = max(1, int(delay_seconds))
    except Exception:
        seconds = 1800

    offset = f"-{seconds} seconds"
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM topics
           WHERE user_id=? AND sent=1 AND learned=0 AND quiz_generated=0
             AND datetime(replace(sent_at, 'T', ' ')) < datetime('now', ?)""",
        (int(user_id), offset),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_oldest_unanswered_quiz(user_id: int):
    """For /answer: the oldest delivered-but-unanswered quiz."""
    conn = get_conn()
    row = conn.execute(
        """SELECT * FROM topics
           WHERE user_id=? AND quiz_delivered=1 AND quiz_answered=0 AND learned=0
           ORDER BY id LIMIT 1""",
        (int(user_id),),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def pending_quiz_count(user_id: int):
    conn = get_conn()
    n = conn.execute(
        """SELECT COUNT(*) FROM topics
           WHERE user_id=? AND quiz_delivered=1 AND quiz_answered=0""",
        (int(user_id),),
    ).fetchone()[0]
    conn.close()
    return n


# ── Counts / flags ────────────────────────────────────────────────────────────

def total_count(user_id: int):
    conn = get_conn()
    n = conn.execute(
        "SELECT COUNT(*) FROM topics WHERE user_id=?",
        (int(user_id),),
    ).fetchone()[0]
    conn.close()
    return n


def learned_count(user_id: int):
    conn = get_conn()
    n = conn.execute(
        "SELECT COUNT(*) FROM topics WHERE user_id=? AND learned=1",
        (int(user_id),),
    ).fetchone()[0]
    conn.close()
    return n


def all_learned(user_id: int):
    conn = get_conn()
    n = conn.execute(
        "SELECT COUNT(*) FROM topics WHERE user_id=? AND learned=0",
        (int(user_id),),
    ).fetchone()[0]
    conn.close()
    return n == 0


def has_topics(user_id: int):
    return total_count(user_id) > 0


def clear_all(user_id: int | None = None):
    conn = get_conn()
    if user_id is None:
        conn.execute("DELETE FROM topics")
        conn.execute("DELETE FROM user_state")
    else:
        conn.execute("DELETE FROM topics WHERE user_id=?", (int(user_id),))
        conn.execute("DELETE FROM user_state WHERE user_id=?", (int(user_id),))
    conn.commit()
    conn.close()


# ── Key-value state ───────────────────────────────────────────────────────────

def set_state(key, value):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (key, str(value))
    )
    conn.commit()
    conn.close()


def get_state(key, default=None):
    conn = get_conn()
    row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    conn.close()
    return row[0] if row else default


def set_user_state(user_id: int, key, value):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO user_state (user_id, key, value) VALUES (?, ?, ?)",
        (int(user_id), str(key), str(value)),
    )
    conn.commit()
    conn.close()


def get_user_state(user_id: int, key, default=None):
    conn = get_conn()
    row = conn.execute(
        "SELECT value FROM user_state WHERE user_id=? AND key=?",
        (int(user_id), str(key)),
    ).fetchone()
    conn.close()
    return row[0] if row else default


def get_active_user_ids():
    conn = get_conn()
    rows = conn.execute("SELECT DISTINCT user_id FROM topics").fetchall()
    conn.close()
    return [r[0] for r in rows]


def export_user_data(user_id: int) -> dict:
    conn = get_conn()
    topic_rows = conn.execute(
        "SELECT * FROM topics WHERE user_id=? ORDER BY id",
        (int(user_id),),
    ).fetchall()
    state_rows = conn.execute(
        "SELECT key, value FROM user_state WHERE user_id=?",
        (int(user_id),),
    ).fetchall()
    conn.close()

    return {
        "schema_version": 1,
        "exported_at": datetime.utcnow().isoformat(),
        "user_id": int(user_id),
        "topics": [dict(r) for r in topic_rows],
        "user_state": [{"key": r["key"], "value": r["value"]} for r in state_rows],
    }


def import_user_data(user_id: int, payload: dict, mode: str = "replace") -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Invalid payload")
    if payload.get("schema_version") != 1:
        raise ValueError("Unsupported schema_version")
    if mode not in ("replace", "merge"):
        raise ValueError("Unsupported import mode")

    topics = payload.get("topics", []) or []
    state_items = payload.get("user_state", []) or []

    conn = get_conn()
    if mode == "replace":
        conn.execute("DELETE FROM topics WHERE user_id=?", (int(user_id),))
        conn.execute("DELETE FROM user_state WHERE user_id=?", (int(user_id),))

    topic_inserted = 0
    for item in topics:
        raw_text = item.get("raw_text")
        if not raw_text:
            continue

        interesting = item.get("interesting")
        sent = int(item.get("sent", 0))
        learned = int(item.get("learned", 0))
        sent_at = item.get("sent_at")
        message_id = item.get("message_id")
        quiz_questions = item.get("quiz_questions")
        quiz_answers = item.get("quiz_answers")
        quiz_generated = int(item.get("quiz_generated", 0))
        quiz_delivered = int(item.get("quiz_delivered", 0))
        quiz_answered = int(item.get("quiz_answered", 0))

        conn.execute(
            """INSERT INTO topics (
                   user_id, raw_text, interesting,
                   sent, learned, sent_at, message_id,
                   quiz_questions, quiz_answers,
                   quiz_generated, quiz_delivered, quiz_answered
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(user_id), raw_text, interesting,
                sent, learned, sent_at, message_id,
                quiz_questions, quiz_answers,
                quiz_generated, quiz_delivered, quiz_answered,
            ),
        )
        topic_inserted += 1

    state_inserted = 0
    for item in state_items:
        key = item.get("key")
        if key is None:
            continue
        value = item.get("value", "")
        conn.execute(
            "INSERT OR REPLACE INTO user_state (user_id, key, value) VALUES (?, ?, ?)",
            (int(user_id), str(key), str(value)),
        )
        state_inserted += 1

    conn.commit()
    conn.close()
    return {"topics": topic_inserted, "state": state_inserted}
