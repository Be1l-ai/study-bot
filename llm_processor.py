import json
import re
import logging

import requests

logger = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL    = "llama-3.3-70b-versatile"


def _call_groq(groq_key: str, prompt: str, max_tokens: int = 900, temperature: float = 0.7) -> str:
    headers = {"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"}
    payload = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        res = requests.post(GROQ_URL, json=payload, headers=headers, timeout=30)
        res.raise_for_status()
        return res.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error("Groq API error: %s", e)
        raise


# ─────────────────────────────────────────────
# 1. Make a topic interesting
# ─────────────────────────────────────────────

ENRICH_PROMPT = """You are a study companion helping someone memorize academic content.

Take the raw text below and rewrite it in an engaging, vivid, story-driven way.
Keep it casual, light, and a little playful — like a smart friend explaining it fast.

STRICT RULES — never break these:
- Keep every name, date, place, and number EXACTLY as written. Do not add or invent any.
- Do not remove any factual information from the source.
- Use analogies, comparisons, or a narrative tone to make it memorable.
- Prefer 2-3 short paragraphs.
- You may include 1 tiny bullet list if it makes the key points easier to scan.
- Keep the wording clean, modern, and not overly formal.
- Plain text only — no markdown.

RAW TEXT:
{text}"""


def make_interesting(raw_text: str, groq_key: str) -> str:
    try:
        return _call_groq(
            groq_key,
            ENRICH_PROMPT.format(text=raw_text[:3000]),
            max_tokens=900,
            temperature=0.75,
        )
    except Exception:
        return raw_text  # fallback: send raw text unchanged


# ─────────────────────────────────────────────
# 2. Generate quiz — returns questions (to show)
#    and answers (to store) separately
# ─────────────────────────────────────────────

QUIZ_PROMPT = """You are a quiz maker for a study bot.

Based ONLY on the text below, write exactly 3 multiple-choice questions.
Each question must test a concrete fact: a date, a name, a place, or an event.

Return ONLY a valid JSON object — no preamble, no explanation, no markdown fences.

Format:
{{
  "questions": [
    {{
      "question": "...",
      "options": ["A) ...", "B) ...", "C) ...", "D) ..."]
    }}
  ],
  "answers": ["A", "C", "B"]
}}

The "answers" array must contain one letter per question, in order.
Do NOT include the correct letter inside the "questions" array.

SOURCE TEXT:
{text}"""


def generate_quiz(raw_text: str, groq_key: str) -> dict | None:
    """
    Returns:
        {
          "questions": [{"question": str, "options": [str, ...]}],  # shown to user
          "answers":   ["A", "C", "B"]                              # stored only
        }
    or None on failure.
    """
    try:
        raw = _call_groq(
            groq_key,
            QUIZ_PROMPT.format(text=raw_text[:3000]),
            max_tokens=700,
            temperature=0.3,
        )
        raw = re.sub(r"```(?:json)?|```", "", raw).strip()
        data = json.loads(raw)

        questions = data.get("questions", [])
        answers   = data.get("answers",   [])

        # Validate
        if len(questions) != len(answers):
            return None
        for q in questions:
            if "question" not in q or "options" not in q or len(q["options"]) < 2:
                return None
        for a in answers:
            if a not in ("A", "B", "C", "D"):
                return None

        return {"questions": questions[:3], "answers": answers[:3]}

    except Exception as e:
        logger.error("Quiz generation failed: %s", e)
        return None
