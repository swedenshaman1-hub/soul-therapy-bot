import os
import tempfile
import unittest

import learning_system as learning_db
import main


def _package(prefix: str, count: int = 3) -> dict:
    return {
        "title": f"{prefix} lesson",
        "intro": f"{prefix} intro",
        "summary": f"{prefix} summary",
        "segments": [
            {
                "explanation": f"{prefix} explanation {index}",
                "question": f"{prefix} question {index}?",
                "reference_answer": f"{prefix} answer {index}",
                "hint": f"{prefix} hint {index}",
                "example": f"{prefix} example {index}",
            }
            for index in range(count)
        ],
    }


class LearningStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = learning_db.DB_PATH
        learning_db.DB_PATH = os.path.join(self.temp.name, "learning.db")
        learning_db._SCHEMA_READY_PATH = None

    def tearDown(self):
        learning_db.DB_PATH = self.old_path
        learning_db._SCHEMA_READY_PATH = None
        self.temp.cleanup()

    def test_onboarding_profile_is_persistent(self):
        profile = learning_db.ensure_profile(101)
        self.assertFalse(learning_db.profile_complete(profile))
        profile = learning_db.update_profile(
            101,
            experience="practice",
            goal="engineering",
            daily_minutes=60,
            delivery_format="mixed",
            onboarding_step="complete",
        )
        self.assertTrue(learning_db.profile_complete(profile))
        self.assertEqual(learning_db.get_profile(101)["daily_minutes"], 60)

    def test_lesson_progress_survives_session_completion(self):
        lesson = _package("primary", 2)
        session = learning_db.save_session(202, "foundations", lesson)
        self.assertEqual(session["segment_index"], 0)
        learning_db.record_score(202, 3)
        learning_db.update_session(202, segment_index=1)
        learning_db.record_score(202, 2)
        mastery = learning_db.complete_lesson(202)
        self.assertEqual(mastery, 2)
        self.assertIsNone(learning_db.get_session(202))
        self.assertEqual(learning_db.progress_summary(202)["completed"], 1)

    def test_lesson_cache_can_be_rebuilt_without_erasing_progress(self):
        package = _package("cached")
        learning_db.save_cached_lesson("key", "foundations", package)
        self.assertEqual(learning_db.get_cached_lesson("key")["title"], "cached lesson")
        self.assertEqual(learning_db.clear_lesson_cache(), 1)
        self.assertIsNone(learning_db.get_cached_lesson("key"))


class MultiNotebookLessonTests(unittest.TestCase):
    def test_directory_only_notebook_does_not_displace_real_materials(self):
        responses = [
            ("primary", __import__("json").dumps(_package("primary"), ensure_ascii=False), 299),
            ("folder", __import__("json").dumps(_package("folder"), ensure_ascii=False), 1),
        ]
        merged = main._merge_lesson_packages(
            responses,
            learning_db.get_lesson("foundations"),
            3,
        )
        self.assertTrue(all(item["notebook_id"] == "primary" for item in merged["segments"]))

    def test_two_indexed_notebooks_are_interleaved(self):
        responses = [
            ("first", __import__("json").dumps(_package("first"), ensure_ascii=False), 299),
            ("second", __import__("json").dumps(_package("second"), ensure_ascii=False), 250),
        ]
        merged = main._merge_lesson_packages(
            responses,
            learning_db.get_lesson("foundations"),
            4,
        )
        self.assertEqual(
            [item["notebook_id"] for item in merged["segments"]],
            ["first", "second", "first", "second"],
        )

    def test_notebook_ids_are_deduplicated(self):
        previous = os.environ.get("NOTEBOOK_IDS")
        try:
            os.environ["NOTEBOOK_IDS"] = "one, two;one\ntwo"
            self.assertEqual(main._configured_notebook_ids(), ("one", "two"))
        finally:
            if previous is None:
                os.environ.pop("NOTEBOOK_IDS", None)
            else:
                os.environ["NOTEBOOK_IDS"] = previous


if __name__ == "__main__":
    unittest.main()
