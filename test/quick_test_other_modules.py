import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta
import unittest
from unittest.mock import patch

import llm_processor
import storage


class TestStorageModule(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="studybot_test_")
        self.original_db_path = storage.DB_PATH
        storage.DB_PATH = os.path.join(self.temp_dir, "test_study_bot.db")
        storage.init_db()

    def tearDown(self):
        storage.DB_PATH = self.original_db_path
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_init_and_basic_counts(self):
        self.assertFalse(storage.has_topics())
        self.assertEqual(storage.total_count(), 0)
        self.assertEqual(storage.learned_count(), 0)
        self.assertTrue(storage.all_learned())

    def test_add_and_get_next_unsent(self):
        storage.add_topics([
            ("raw one", "interesting one"),
            ("raw two", "interesting two"),
        ])

        nxt = storage.get_next_unsent()
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt["raw_text"], "raw one")
        self.assertEqual(storage.total_count(), 2)
        self.assertTrue(storage.has_topics())

    def test_mark_sent_mark_learned_and_reset_sent(self):
        storage.add_topics([("raw text", "interesting")])
        topic = storage.get_next_unsent()
        topic_id = topic["id"]

        storage.mark_sent(topic_id, 12345)
        updated = storage.get_topic_by_id(topic_id)
        self.assertEqual(updated["sent"], 1)
        self.assertEqual(updated["message_id"], 12345)
        self.assertIsNotNone(updated["sent_at"])

        storage.store_quiz(topic_id, '[{"question":"Q?","options":["A)","B)"]}]', '["A"]')
        storage.mark_quiz_delivered(topic_id)
        storage.mark_quiz_answered(topic_id)

        storage.reset_sent(topic_id)
        reset = storage.get_topic_by_id(topic_id)
        self.assertEqual(reset["sent"], 0)
        self.assertEqual(reset["message_id"], None)
        self.assertEqual(reset["quiz_generated"], 0)
        self.assertEqual(reset["quiz_delivered"], 0)
        self.assertEqual(reset["quiz_answered"], 0)
        self.assertEqual(reset["quiz_questions"], None)
        self.assertEqual(reset["quiz_answers"], None)

        storage.mark_learned(topic_id)
        learned = storage.get_topic_by_id(topic_id)
        self.assertEqual(learned["learned"], 1)
        self.assertEqual(storage.learned_count(), 1)

    def test_quiz_flow_helpers(self):
        storage.add_topics([
            ("raw one", "interesting one"),
            ("raw two", "interesting two"),
        ])
        t1 = storage.get_next_unsent()["id"]
        storage.mark_sent(t1, 100)

        t2 = storage.get_next_unsent()["id"]
        storage.mark_sent(t2, 101)

        quiz_payload = json.dumps([
            {"question": "Q1", "options": ["A) x", "B) y", "C) z", "D) w"]}
        ])
        storage.store_quiz(t1, quiz_payload, json.dumps(["A"]))
        storage.mark_quiz_delivered(t1)

        self.assertEqual(storage.pending_quiz_count(), 1)
        oldest = storage.get_oldest_unanswered_quiz()
        self.assertEqual(oldest["id"], t1)

        storage.mark_quiz_answered(t1)
        self.assertEqual(storage.pending_quiz_count(), 0)

    def test_key_value_state(self):
        self.assertEqual(storage.get_state("missing", "fallback"), "fallback")
        storage.set_state("phase", "running")
        self.assertEqual(storage.get_state("phase"), "running")

    def test_get_topics_ready_for_quiz_respects_delay_and_iso_timestamps(self):
        storage.add_topics([("raw one", "interesting one")])
        topic_id = storage.get_next_unsent()["id"]
        storage.mark_sent(topic_id, 200)

        old_time = (datetime.utcnow() - timedelta(minutes=31)).isoformat()
        conn = storage.get_conn()
        conn.execute("UPDATE topics SET sent_at=? WHERE id=?", (old_time, topic_id))
        conn.commit()
        conn.close()

        ready = storage.get_topics_ready_for_quiz(delay_seconds=1800)
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0]["id"], topic_id)


class TestLlmProcessorModule(unittest.TestCase):
    def test_enrich_prompt_targets_exam_depth(self):
        captured = {}

        def fake_call(groq_key, prompt, max_tokens=900, temperature=0.7):
            captured["prompt"] = prompt
            return "ok"

        with patch("llm_processor._call_groq", side_effect=fake_call):
            llm_processor.make_interesting("raw text", "fake-key")

        prompt = captured["prompt"]
    self.assertIn("Teach the exact topic from the raw text like an exam tutor", prompt)
    self.assertIn("do not compress everything into a shallow summary", prompt)
    self.assertIn("Prefer 3-5 short paragraphs", prompt)

    def test_make_interesting_falls_back_on_error(self):
        raw = "Original factual text"
        with patch("llm_processor._call_groq", side_effect=Exception("api down")):
            out = llm_processor.make_interesting(raw, "fake-key")
        self.assertEqual(out, raw)

    def test_make_interesting_falls_back_when_key_dates_or_years_missing(self):
        raw = "Battle happened on June 19, 1861 and ended in 1862."
        rewritten = "The battle happened in an old era and changed everything."
        with patch("llm_processor._call_groq", return_value=rewritten):
            out = llm_processor.make_interesting(raw, "fake-key")
        self.assertEqual(out, raw)

    def test_generate_quiz_parses_json_and_limits_to_three(self):
        mocked_response = json.dumps({
            "questions": [
                {"question": "Q1", "options": ["A) 1", "B) 2", "C) 3", "D) 4"]},
                {"question": "Q2", "options": ["A) 1", "B) 2", "C) 3", "D) 4"]},
                {"question": "Q3", "options": ["A) 1", "B) 2", "C) 3", "D) 4"]},
                {"question": "Q4", "options": ["A) 1", "B) 2", "C) 3", "D) 4"]},
            ],
            "answers": ["A", "B", "C", "D"],
        })

        with patch("llm_processor._call_groq", return_value=f"```json\n{mocked_response}\n```"):
            quiz = llm_processor.generate_quiz("source text", "fake-key")

        self.assertIsNotNone(quiz)
        self.assertEqual(len(quiz["questions"]), 3)
        self.assertEqual(len(quiz["answers"]), 3)
        self.assertEqual(quiz["answers"], ["A", "B", "C"])

    def test_generate_quiz_rejects_invalid_answers(self):
        bad = json.dumps({
            "questions": [
                {"question": "Q1", "options": ["A) 1", "B) 2", "C) 3", "D) 4"]}
            ],
            "answers": ["E"],
        })

        with patch("llm_processor._call_groq", return_value=bad):
            quiz = llm_processor.generate_quiz("source text", "fake-key")

        self.assertIsNone(quiz)

    def test_generate_quiz_returns_none_on_exception(self):
        with patch("llm_processor._call_groq", side_effect=Exception("timeout")):
            quiz = llm_processor.generate_quiz("source text", "fake-key")
        self.assertIsNone(quiz)


if __name__ == "__main__":
    unittest.main()
