"""Synthetic evidence only; each test owns a temporary vault and machine registry."""

import copy
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
SCRIPT = SKILL / "scripts/speaking_store.py"
spec = importlib.util.spec_from_file_location("speaking_store", SCRIPT)
store_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(store_module)
Store = store_module.Store
StoreError = store_module.StoreError


def sample(sid="IS-one", day="2026-09-17"):
    return {
        "schema_version": 1, "session_id": sid, "profile_revision": 1,
        "occurred_at": day + "T19:00:00+08:00", "mode": "daily", "status": "completed",
        "duration_minutes": 15,
        "conditions": {"full_test": False, "uninterrupted": True, "timing_verified": False,
                       "timekeeper": None, "timings": {}, "source": "synthetic_fixture"},
        "attempts": [{"id": "A1", "part": 1, "kind": "cold", "prompt_id": "P-" + sid,
                      "topic": "synthetic-topic", "prompt": "Synthetic question for " + sid,
                      "familiarity": "unseen", "support": "none", "modality": "live_audio",
                      "evidence": "Synthetic observation, not a real learner.", "evidence_kind": "observation"}],
        "criteria": {key: {"status": "partial", "evidence_ids": ["A1"], "note": "Synthetic limited evidence."}
                     for key in store_module.CRITERIA},
        "focus_next": ["Explain one comparison on a new topic."],
        "review_items": [], "review_results": [], "limitations": ["Synthetic fixture; no real audio was collected."],
    }


def mock():
    result = sample()
    result["mode"] = "mock"
    result["conditions"].update(
        full_test=True, timing_verified=True, timekeeper="synthetic external timer",
        timings={"part1": 260, "part2": 200, "part3": 270, "part2_preparation": 60, "part2_speech": 120})
    first = result["attempts"][0]
    result["attempts"] = []
    for part in (1, 2, 3):
        attempt = copy.deepcopy(first)
        attempt.update(id=f"A{part}", part=part, prompt_id=f"P-test-{part}", prompt=f"Synthetic part {part}")
        result["attempts"].append(attempt)
    for value in result["criteria"].values():
        value["evidence_ids"] = ["A1", "A2", "A3"]
    return result


def review_item():
    return {"review_id": "R-comparison", "criterion": "GRA", "task": "Compare two unfamiliar options.",
            "due_date": "2026-09-19"}


class SpeakingStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.vault = self.base / "Vault with spaces"
        (self.vault / ".obsidian").mkdir(parents=True)
        self.registry = self.base / "machine-registry"
        self.store = Store(self.registry)
        with self.store.locked():
            self.store.init("learner", str(self.vault), "Asia/Shanghai")
        self.root = self.vault / "Learning/IELTS-Speaking/learner"

    def tearDown(self):
        self.temp.cleanup()

    def archive(self, data):
        with self.store.locked():
            return self.store.archive("learner", data)

    def context(self, day="2026-09-30"):
        return self.store.context("learner", day)

    def test_preferences_survive_new_store_and_reject_stale_change(self):
        with self.store.locked():
            saved = self.store.configure("learner", {"normal_minutes": 20, "feedback_language": "English"}, 1)
        self.assertTrue(saved["views_verified"])
        another = Store(self.registry)
        self.assertEqual(another.locate()[2]["settings"]["normal_minutes"], 20)
        self.assertEqual(another.locate()[2]["settings"]["feedback_language"], "English")
        with self.assertRaises(StoreError):
            self.store.configure("learner", {"normal_minutes": 30}, 1)
        self.assertTrue((self.root / "profile-history/000001.json").exists())
        self.assertTrue((self.root / "profile-history/000002.json").exists())

    def test_preference_restore_is_a_new_revision(self):
        original = self.store.locate()[2]["settings"]
        self.store.configure("learner", {"target_band": 8.5}, 1)
        restored = self.store.configure("learner", original, 2)
        self.assertEqual(restored["profile"]["revision"], 3)
        self.assertEqual(restored["profile"]["settings"]["target_band"], 8)

    def test_archive_is_idempotent_but_conflicting_payload_is_rejected(self):
        data = sample()
        self.assertTrue(self.archive(data)["views_verified"])
        self.assertTrue(self.archive(data)["already_archived"])
        altered = copy.deepcopy(data)
        altered["focus_next"] = ["A different proposed action."]
        with self.assertRaises(StoreError):
            self.archive(altered)
        self.assertEqual(self.context()["counts"]["practice_sessions"], 1)

    def test_strict_mock_requires_actual_format_and_evidence_conditions(self):
        self.assertEqual(store_module.validate_session(mock()), "strict_mock")
        cases = [
            lambda s: s["attempts"][0].update(support="intent"),
            lambda s: s["attempts"][0].update(familiarity="seen"),
            lambda s: s["attempts"][0].update(kind="retry"),
            lambda s: s["conditions"].update(timing_verified=False),
            lambda s: s["conditions"].update(uninterrupted=False),
            lambda s: s["conditions"].update(timekeeper=None),
            lambda s: s.update(status="partial"),
            lambda s: s["conditions"]["timings"].update(part2_preparation=90),
            lambda s: s["conditions"]["timings"].update(part2_speech=150),
            lambda s: s["attempts"].reverse(),
        ]
        for change in cases:
            data = mock()
            change(data)
            self.assertEqual(store_module.validate_session(data), "rehearsal")

    def test_transcript_does_not_support_fluency_or_pronunciation(self):
        data = sample()
        data["attempts"][0]["modality"] = "transcript"
        with self.assertRaises(StoreError):
            store_module.validate_session(data)
        for key in ("FC", "P"):
            data["criteria"][key].update(status="not_observed", evidence_ids=[])
        self.assertEqual(store_module.validate_session(data), "text_practice")
        data["criteria"]["LR"]["band_estimate"] = [7, 8]
        with self.assertRaises(StoreError):
            store_module.validate_session(data)

    def test_band_estimate_needs_complete_cross_part_evidence(self):
        data = mock()
        data["criteria"]["LR"]["band_estimate"] = [7, 8]
        self.assertEqual(store_module.validate_session(data), "strict_mock")
        data["criteria"]["LR"]["evidence_ids"] = ["A1"]
        with self.assertRaises(StoreError):
            store_module.validate_session(data)

    def test_fake_evidence_reference_is_rejected(self):
        data = sample()
        data["criteria"]["P"]["evidence_ids"] = ["A999"]
        with self.assertRaises(StoreError):
            self.archive(data)
        self.assertFalse((self.root / "sessions/IS-one.json").exists())

    def test_prior_prompt_cannot_become_unseen_by_changing_id(self):
        data = sample()
        self.archive(data)
        next_one = sample("IS-two", "2026-09-19")
        next_one["attempts"][0]["prompt"] = data["attempts"][0]["prompt"].upper()
        with self.assertRaises(StoreError):
            self.archive(next_one)
        next_one["attempts"][0]["familiarity"] = "seen"
        self.assertTrue(self.archive(next_one)["archived"])

    def test_prior_prompt_id_cannot_become_unseen_by_changing_words(self):
        self.archive(sample())
        next_one = sample("IS-two", "2026-09-19")
        next_one["attempts"][0]["prompt_id"] = "P-IS-one"
        with self.assertRaises(StoreError):
            self.archive(next_one)

    def test_in_session_repeat_must_be_marked_seen(self):
        data = sample()
        again = dict(data["attempts"][0], id="A2", kind="retry")
        data["attempts"].append(again)
        with self.assertRaises(StoreError):
            self.archive(data)
        again["familiarity"] = "seen"
        self.assertTrue(self.archive(data)["archived"])

    def test_due_review_moves_after_genuine_independent_transfer(self):
        data = sample()
        data["review_items"] = [review_item()]
        self.archive(data)
        self.assertFalse(self.context("2026-09-18")["due_reviews"])
        self.assertEqual(len(self.context("2026-09-19")["due_reviews"]), 1)
        result = sample("IS-transfer", "2026-09-19")
        result["attempts"][0]["kind"] = "transfer"
        result["review_results"] = [{"review_id": "R-comparison", "outcome": "independent_success",
                                     "attempt_id": "A1", "reason": "Synthetic successful comparison."}]
        self.archive(result)
        queue = self.context("2026-09-19")["all_reviews"][0]
        self.assertEqual(queue["due_date"], "2026-09-26")
        self.assertEqual(queue["independent_dates"], ["2026-09-19"])
        self.assertNotIn("mastered", queue)
        self.assertFalse(self.context("2026-09-25")["due_reviews"])

    def test_supported_answer_cannot_count_as_independent_success(self):
        data = sample()
        data["review_items"] = [review_item()]
        self.archive(data)
        result = sample("IS-next", "2026-09-19")
        result["attempts"][0].update(kind="transfer", support="model")
        result["review_results"] = [{"review_id": "R-comparison", "outcome": "independent_success",
                                     "attempt_id": "A1", "reason": "Synthetic result."}]
        with self.assertRaises(StoreError):
            self.archive(result)
        result["review_results"][0]["outcome"] = "supported"
        self.archive(result)
        self.assertEqual(self.context()["all_reviews"][0]["due_date"], "2026-09-21")

    def test_text_transfer_cannot_count_as_independent_speaking_success(self):
        data = sample()
        data["attempts"][0]["modality"] = "text"
        for key in ("FC", "P"):
            data["criteria"][key].update(status="not_observed", evidence_ids=[])
        data["review_results"] = [{"review_id": "R-any", "outcome": "independent_success",
                                   "attempt_id": "A1", "reason": "Only text."}]
        with self.assertRaises(StoreError):
            store_module.validate_session(data)

    def test_queue_retirement_does_not_count_as_a_practice(self):
        data = sample()
        data["review_items"] = [review_item()]
        self.archive(data)
        edit = sample("IS-review-edit", "2026-09-20")
        edit.update(mode="review_update", duration_minutes=0, attempts=[], focus_next=[])
        for value in edit["criteria"].values():
            value.update(status="not_observed", evidence_ids=[])
        edit["review_results"] = [{"review_id": "R-comparison", "outcome": "retired", "reason": "Learner requested removal from queue."}]
        self.archive(edit)
        self.assertEqual(self.context()["counts"]["practice_sessions"], 1)
        self.assertFalse(self.context()["due_reviews"])

    def test_missing_review_and_repeated_review_ids_fail_before_write(self):
        data = sample()
        data["review_results"] = [{"review_id": "R-missing", "outcome": "needs_work",
                                   "attempt_id": "A1", "reason": "Synthetic gap."}]
        with self.assertRaises(StoreError):
            self.archive(data)
        self.assertFalse((self.root / "sessions/IS-one.json").exists())

    def test_correction_preserves_history_without_double_counting(self):
        original = sample()
        self.archive(original)
        correction = copy.deepcopy(original)
        correction.update(session_id="IS-corrected", supersedes="IS-one", focus_next=["Corrected observation."])
        self.archive(correction)
        self.assertEqual(self.context()["counts"]["practice_sessions"], 1)
        self.assertTrue((self.root / "sessions/IS-one.json").exists())
        self.assertEqual(self.context()["recent_sessions"][0]["session"]["session_id"], "IS-corrected")
        branch = dict(correction, session_id="IS-branch")
        with self.assertRaises(StoreError):
            self.archive(branch)

    def test_correction_cannot_orphan_later_review_evidence(self):
        original = sample()
        original["review_items"] = [review_item()]
        self.archive(original)
        next_one = sample("IS-next", "2026-09-20")
        next_one["review_results"] = [{"review_id": "R-comparison", "outcome": "needs_work",
                                      "attempt_id": "A1", "reason": "Synthetic gap."}]
        self.archive(next_one)
        correction = copy.deepcopy(original)
        correction.update(session_id="IS-corrected", supersedes="IS-one", review_items=[])
        with self.assertRaises(StoreError):
            self.archive(correction)
        self.assertFalse((self.root / "sessions/IS-corrected.json").exists())

    def test_later_seen_retry_does_not_invalidate_correction_of_original_cold_attempt(self):
        original = sample()
        self.archive(original)
        retry = sample("IS-retry", "2026-09-20")
        retry["attempts"][0].update(prompt_id="P-IS-one", prompt=original["attempts"][0]["prompt"],
                                    kind="retry", familiarity="seen")
        self.archive(retry)
        corrected = dict(original, session_id="IS-corrected", supersedes="IS-one")
        self.assertTrue(self.archive(corrected)["archived"])
        self.assertEqual(self.context()["counts"]["practice_sessions"], 2)

    def test_older_baseline_remains_available_after_many_daily_sessions(self):
        first = mock()
        first["mode"] = "baseline"
        self.archive(first)
        for day in range(18, 25):
            self.archive(sample(f"IS-day-{day}", f"2026-09-{day}"))
        context = self.context()
        self.assertEqual(len(context["recent_sessions"]), 6)
        self.assertEqual(context["comparison_samples"]["first_baseline"]["session"]["session_id"], "IS-one")
        self.assertEqual(context["spoken_part_coverage_last_14_days"]["3"], 1)
        self.assertEqual(context["counts"]["audio_sessions"], 8)

    def test_rebuild_preserves_personal_note_and_unknown_frontmatter(self):
        self.archive(sample())
        note = self.root / "sessions/IS-one.md"
        text = note.read_text().replace("---\n", '---\nmy_tag: "personal"\n', 1)
        note.write_text(text + "\nA personal reflection.\n")
        index = self.root / "Practice.md"
        index.write_text(index.read_text() + "\nMy own goal.\n")
        self.store.rebuild("learner")
        self.assertIn('my_tag: "personal"', note.read_text())
        self.assertTrue(note.read_text().endswith("A personal reflection.\n"))
        self.assertTrue(index.read_text().endswith("My own goal.\n"))

    def test_partial_view_failure_can_recover_without_duplicate(self):
        index = self.root / "Practice.md"
        saved = index.read_bytes()
        index.write_text("Unmanaged personal contents.\n")
        result = self.archive(sample())
        self.assertTrue(result["archived"])
        self.assertFalse(result["views_verified"])
        self.assertEqual(index.read_text(), "Unmanaged personal contents.\n")
        retry = self.archive(sample())
        self.assertTrue(retry["already_archived"])
        self.assertFalse(retry["views_verified"])
        index.write_bytes(saved)
        self.assertTrue(self.store.rebuild("learner")["views_verified"])
        self.assertEqual(self.context()["counts"]["practice_sessions"], 1)

    def test_historical_profile_snapshot_survives_mid_session_change(self):
        data = sample()
        self.store.configure("learner", {"timezone": "America/Los_Angeles", "normal_minutes": 20}, 1)
        self.archive(data)
        record = self.context()["recent_sessions"][0]
        self.assertEqual(record["settings_snapshot"]["timezone"], "Asia/Shanghai")
        self.assertEqual(record["session"]["profile_revision"], 1)

    def test_quote_opt_out_applies_even_to_pending_old_profile_session(self):
        data = sample()
        data["attempts"][0]["evidence_kind"] = "quote"
        self.store.configure("learner", {"store_quotes": False}, 1)
        with self.assertRaises(StoreError):
            self.archive(data)
        data["attempts"][0]["evidence_kind"] = "observation"
        self.assertTrue(self.archive(data)["archived"])

    def test_local_date_is_fixed_at_observation_timezone(self):
        data = sample()
        data["occurred_at"] = "2026-09-17T18:30:00+00:00"
        self.archive(data)
        self.assertEqual(self.context("2026-09-17")["counts"]["practice_sessions"], 0)
        self.assertEqual(self.context("2026-09-18")["counts"]["practice_sessions"], 1)
        self.store.configure("learner", {"timezone": "UTC"}, 1)
        self.assertEqual(self.context("2026-09-18")["recent_sessions"][0]["local_date"], "2026-09-18")

    def test_multiple_profiles_require_selection(self):
        self.store.init("second", str(self.vault), "UTC")
        with self.assertRaises(StoreError):
            self.store.locate()
        self.assertEqual(self.store.locate("second")[2]["settings"]["timezone"], "UTC")

    def test_existing_archive_can_bind_on_new_machine_without_resetting(self):
        self.store.configure("learner", {"normal_minutes": 17}, 1)
        new_machine = Store(self.base / "other-machine")
        with new_machine.locked():
            new_machine.init("learner", str(self.vault), "UTC")
        self.assertEqual(new_machine.locate()[2]["settings"]["normal_minutes"], 17)
        self.assertEqual(new_machine.locate()[2]["settings"]["timezone"], "Asia/Shanghai")

    def test_rebind_checks_archive_identity(self):
        destination = self.base / "Synced Vault"
        shutil.copytree(self.vault, destination)
        self.store.rebind("learner", str(destination))
        self.assertEqual(self.store.locate()[0], destination.resolve())
        wrong_vault = self.base / "Another Vault"
        (wrong_vault / ".obsidian").mkdir(parents=True)
        other = Store(self.base / "wrong-registry")
        with other.locked():
            other.init("learner", str(wrong_vault), "UTC")
        with self.assertRaises(StoreError):
            self.store.rebind("learner", str(wrong_vault))

    def test_symlink_cannot_redirect_learner_writes(self):
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        (self.root / "sessions").symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(StoreError):
            self.archive(sample())
        self.assertFalse(list(elsewhere.iterdir()))

    def test_action_requires_matching_goal_and_profile_binding(self):
        data = sample()
        data["action_id"] = "G-2026-001-A001"
        with self.assertRaises(StoreError):
            self.archive(data)
        data["goal_id"] = "G-2026-002"
        with self.assertRaises(StoreError):
            self.archive(data)
        data["goal_id"] = "G-2026-001"
        self.assertTrue(self.archive(data)["archived"])

    def test_audio_reference_is_local_existing_and_cannot_escape(self):
        data = sample()
        data["attempts"][0]["audio_ref"] = "../outside.m4a"
        with self.assertRaises(StoreError):
            self.archive(data)
        data["attempts"][0]["audio_ref"] = "Audio/missing.m4a"
        with self.assertRaises(StoreError):
            self.archive(data)

    def test_record_tampering_is_detected(self):
        self.archive(sample())
        path = self.root / "sessions/IS-one.json"
        record = json.loads(path.read_text())
        record["session"]["duration_minutes"] = 150
        path.write_text(json.dumps(record))
        with self.assertRaises(StoreError):
            self.context()

    def test_packaged_example_is_valid_text_only(self):
        example = json.loads((SKILL / "assets/session.example.json").read_text())
        self.assertEqual(store_module.validate_session(example), "text_practice")
        result = self.archive(example)
        self.assertTrue(result["views_verified"])

    def test_cli_reopens_archive_from_different_working_directory(self):
        self.archive(sample())
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--registry-dir", str(self.registry), "context", "--on", "2026-09-30"],
            cwd=self.base, capture_output=True, text=True, check=True)
        output = json.loads(result.stdout)
        self.assertTrue(output["ok"])
        self.assertEqual(output["counts"]["practice_sessions"], 1)
        self.assertIn("IS-one", output["recent_sessions"][0]["session"]["session_id"])

    def test_cli_first_use_configure_archive_and_reopen(self):
        prefix = [sys.executable, str(SCRIPT), "--registry-dir", str(self.base / "cli-machine"),
                  "--profile", "cli-learner"]

        def run(*arguments):
            result = subprocess.run(prefix + list(arguments), cwd=self.base, capture_output=True,
                                    text=True, check=True)
            self.assertEqual(result.stderr, "")
            return json.loads(result.stdout)

        initialized = run("init", "--vault", str(self.vault), "--timezone", "Asia/Shanghai")
        self.assertTrue(initialized["views_verified"])
        configured = run("configure", "--input", str(SKILL / "assets/preferences.example.json"),
                         "--expected-revision", "1")
        self.assertEqual(configured["profile"]["revision"], 2)
        archived = run("archive", "--input", str(SKILL / "assets/session.example.json"))
        self.assertTrue(archived["views_verified"])
        resumed = run("context", "--on", "2026-09-19")
        self.assertEqual(resumed["profile"]["settings"]["normal_minutes"], 20)
        self.assertEqual(resumed["counts"]["practice_sessions"], 1)
        self.assertEqual(resumed["counts"]["audio_sessions"], 0)
        self.assertEqual(len(resumed["due_reviews"]), 1)

    def test_cli_rejects_nonfinite_json_and_invalid_timezone_without_traceback(self):
        bad = self.base / "bad.json"
        bad.write_text('{"normal_minutes": NaN}')
        prefix = [sys.executable, str(SCRIPT), "--registry-dir", str(self.registry), "--profile", "learner",
                  "configure", "--input", str(bad), "--expected-revision", "1"]
        result = subprocess.run(prefix, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(result.stdout)["ok"])
        self.assertEqual(result.stderr, "")
        bad.write_text('{"timezone": "Not/AZone"}')
        result = subprocess.run(prefix, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(result.stdout)["ok"])
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
