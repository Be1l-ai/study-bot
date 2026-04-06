import os
import sqlite3
from datetime import datetime

DB_PATH = os.environ.get("DB_PATH", "study_bot.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS topics (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
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
    ''')
    conn.commit()
    conn.close()


# ── Topics ────────────────────────────────────────────────────────────────────

def add_topics(topics):
    """topics = list of (raw_text, interesting_text)"""
    conn = get_conn()
    conn.executemany(
        "INSERT INTO topics (raw_text, interesting) VALUES (?, ?)", topics
    )
    conn.commit()
    conn.close()


def get_next_unsent():
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM topics WHERE sent=0 AND learned=0 ORDER BY id LIMIT 1"
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def mark_sent(topic_id, message_id):
    conn = get_conn()
    conn.execute(
        "UPDATE topics SET sent=1, sent_at=?, message_id=? WHERE id=?",
        (datetime.utcnow().isoformat(), message_id, topic_id),
    )
    conn.commit()
    conn.close()


def mark_learned(topic_id):
    conn = get_conn()
    conn.execute("UPDATE topics SET learned=1 WHERE id=?", (topic_id,))
    conn.commit()
    conn.close()


def reset_sent(topic_id):
    """Wipe delivery + quiz state so the topic gets re-sent (after a failed quiz)."""
    conn = get_conn()
    conn.execute(
        """UPDATE topics
           SET sent=0, sent_at=NULL, message_id=NULL,
               quiz_generated=0, quiz_delivered=0, quiz_answered=0,
               quiz_questions=NULL, quiz_answers=NULL
           WHERE id=?""",
        (topic_id,),
    )
    conn.commit()
    conn.close()


def get_topic_by_id(topic_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ── Quiz ──────────────────────────────────────────────────────────────────────

def store_quiz(topic_id, questions_json: str, answers_json: str):
    """Save generated quiz. questions_json contains no correct-answer info."""
    conn = get_conn()
    conn.execute(
        "UPDATE topics SET quiz_questions=?, quiz_answers=?, quiz_generated=1 WHERE id=?",
        (questions_json, answers_json, topic_id),
    )
    conn.commit()
    conn.close()


def mark_quiz_delivered(topic_id):
    conn = get_conn()
    conn.execute("UPDATE topics SET quiz_delivered=1 WHERE id=?", (topic_id,))
    conn.commit()
    conn.close()


def mark_quiz_answered(topic_id):
    conn = get_conn()
    conn.execute("UPDATE topics SET quiz_answered=1 WHERE id=?", (topic_id,))
    conn.commit()
    conn.close()


def get_topics_ready_for_quiz():
    """Topics sent 30+ min ago that haven't had a quiz generated yet."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM topics
           WHERE sent=1 AND learned=0 AND quiz_generated=0
             AND sent_at < datetime('now', '-30 minutes')"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_oldest_unanswered_quiz():
    """For /answer: the oldest delivered-but-unanswered quiz."""
    conn = get_conn()
    row = conn.execute(
        """SELECT * FROM topics
           WHERE quiz_delivered=1 AND quiz_answered=0 AND learned=0
           ORDER BY id LIMIT 1"""
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def pending_quiz_count():
    conn = get_conn()
    n = conn.execute(
        "SELECT COUNT(*) FROM topics WHERE quiz_delivered=1 AND quiz_answered=0"
    ).fetchone()[0]
    conn.close()
    return n


# ── Counts / flags ────────────────────────────────────────────────────────────

def total_count():
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
    conn.close()
    return n


def learned_count():
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) FROM topics WHERE learned=1").fetchone()[0]
    conn.close()
    return n


def all_learned():
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) FROM topics WHERE learned=0").fetchone()[0]
    conn.close()
    return n == 0


def has_topics():
    return total_count() > 0


def clear_all():
    conn = get_conn()
    conn.execute("DELETE FROM topics")
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
