"""Offline inventory behavior using synthetic classrooms only."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from zoneinfo import ZoneInfo


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/scan_materials.py"
spec = importlib.util.spec_from_file_location("class_notes_scan", SCRIPT)
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="class-notes-test-")
        self.addCleanup(self.scratch.cleanup)
        self.base = Path(self.scratch.name)
        self.root = self.base / "上课 资料"
        self.root.mkdir()
        self.config = self.base / "config.toml"
        self.config.write_text('schema_version = 1\n[storage]\nclass_root = '
                               + json.dumps(str(self.root), ensure_ascii=False)
                               + '\narchive_materials = true\n[courses]\n', encoding="utf-8")
        self.layout = scanner.load_layout(self.config)
        self.ledger = {"schema_version": 1, "lessons": [], "references": []}

    def file(self, name, body=b"synthetic source"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return path

    def save_ledger(self):
        self.file(".class-notes/processing.json", json.dumps(self.ledger, ensure_ascii=False).encode())

    def lesson(self, source="课程甲/笔记素材/录音.m4a", transcript=False):
        audio = self.file(source)
        note = "课程甲/正式课堂笔记/笔记.md"
        self.file(note, b"verified synthetic note")
        entry = {"lesson_id": "lesson-1", "course_folder": "课程甲", "status": "complete",
                 "archive_status": "complete", "requires_transcript": transcript,
                 "materials": [{"path": source, "original_path": "录音.m4a",
                                "sha256": hashlib.sha256(audio.read_bytes()).hexdigest(), "coverage": "full"}],
                 "outputs": [{"path": note, "role": "notes", "verified": True}]}
        self.ledger["lessons"].append(entry)
        self.save_ledger()
        return entry

    def states(self):
        return {item["path"]: item["status"] for item in scanner.scan(self.layout)["materials"]}

    def test_root_and_archived_pending_materials_are_both_discovered(self):
        self.file("新录音.m4a")
        self.file("课程甲/笔记素材/旧但未整理.md")
        self.file("课程甲/正式课堂笔记/成稿.md")
        self.file(".class-notes/scratch.md")
        self.assertEqual(set(self.states()), {"新录音.m4a", "课程甲/笔记素材/旧但未整理.md"})
        self.assertEqual(set(self.states().values()), {"pending_review"})

    def test_old_timestamps_are_not_a_date_filter(self):
        path = self.file("很早的待办.pdf")
        os.utime(path, (1, 1))
        self.assertIn("很早的待办.pdf", self.states())

    def test_timestamps_preserve_creation_and_modification_as_separate_clues(self):
        times = scanner.file_times(SimpleNamespace(st_birthtime=0, st_mtime=3600), ZoneInfo("Asia/Shanghai"))
        self.assertEqual(times["created_at"], "1970-01-01T08:00:00+08:00")
        self.assertEqual(times["modified_at"], "1970-01-01T09:00:00+08:00")

    def test_ctime_is_never_fabricated_as_creation_time(self):
        times = scanner.file_times(SimpleNamespace(st_ctime=123456, st_mtime=0))
        self.assertIsNone(times["created_at"])
        self.assertEqual(times["modified_at"], "1970-01-01T00:00:00+00:00")

    def test_schedule_is_context_not_pending_lesson_material(self):
        self.config.write_text(self.config.read_text() + '\n[matching]\nschedule_dir = "本学期课程表"\ntimezone = "Asia/Shanghai"\n')
        self.layout = scanner.load_layout(self.config)
        self.file("本学期课程表/课程表.pdf", b"synthetic schedule fixture")
        self.file("匿名录音.m4a")
        result = scanner.scan(self.layout)
        self.assertEqual([item["path"] for item in result["materials"]], ["匿名录音.m4a"])
        self.assertEqual([item["path"] for item in result["context_files"]], ["本学期课程表/课程表.pdf"])
        self.assertEqual(result["timezone"], "Asia/Shanghai")
        self.assertIn("timestamps", result["materials"][0])
        self.assertIsNone(result["materials"][0]["course_hint"])

    def test_missing_schedule_is_reported_without_losing_pending_materials(self):
        self.layout["schedule"] = "不存在的课表"
        self.file("匿名录音.m4a")
        result = scanner.scan(self.layout)
        self.assertEqual(len(result["materials"]), 1)
        self.assertEqual(result["issues"][0]["reason"], "schedule_directory_missing")

    def test_schedule_escape_and_invalid_timezone_are_rejected(self):
        original = self.config.read_text()
        for extra in ('schedule_dir = "../outside"', 'timezone = "not/a/real/timezone"'):
            with self.subTest(extra=extra):
                self.config.write_text(original + '\n[matching]\n' + extra + '\n')
                with self.assertRaises(ValueError):
                    scanner.load_layout(self.config)

    def test_complete_run_is_idempotent_and_read_only(self):
        self.lesson()
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        first, second = scanner.scan(self.layout), scanner.scan(self.layout)
        self.assertEqual(first, second)
        self.assertEqual(list(self.states().values()), ["complete"])
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_modified_registered_source_requires_update(self):
        self.lesson()
        self.file("课程甲/笔记素材/录音.m4a", b"new extra source material")
        self.assertEqual(list(self.states().values()), ["needs_update"])

    def test_missing_note_is_not_complete(self):
        entry = self.lesson()
        (self.root / entry["outputs"][0]["path"]).unlink()
        self.assertEqual(list(self.states().values()), ["needs_repair"])

    def test_required_transcript_must_exist_and_be_verified(self):
        entry = self.lesson(transcript=True)
        self.assertEqual(list(self.states().values()), ["needs_repair"])
        name = "课程甲/正式课堂笔记/完整听写.md"
        self.file(name, b"transcript")
        output = {"path": name, "role": "transcript", "verified": False}
        entry["outputs"].append(output)
        self.save_ledger()
        self.assertEqual(list(self.states().values()), ["needs_repair"])
        output["verified"] = True
        self.save_ledger()
        self.assertEqual(list(self.states().values()), ["complete"])

    def test_empty_or_wrong_directory_note_is_not_complete(self):
        entry = self.lesson()
        self.file(entry["outputs"][0]["path"], b"")
        self.assertEqual(list(self.states().values()), ["needs_repair"])
        entry["outputs"][0]["path"] = "课程甲/笔记素材/不是真正输出.md"
        self.file(entry["outputs"][0]["path"], b"source masquerading as output")
        self.save_ledger()
        self.assertEqual(self.states()["课程甲/笔记素材/录音.m4a"], "needs_repair")

    def test_same_file_cannot_be_both_note_and_complete_transcript(self):
        entry = self.lesson(transcript=True)
        entry["outputs"].append({"path": entry["outputs"][0]["path"], "role": "transcript", "verified": True})
        self.save_ledger()
        self.assertEqual(list(self.states().values()), ["needs_repair"])

    def test_missing_registered_source_is_reported_even_with_no_candidate_file(self):
        entry = self.lesson()
        (self.root / entry["materials"][0]["path"]).unlink()
        result = scanner.scan(self.layout)
        self.assertEqual(result["materials"], [])
        self.assertEqual(result["issues"][0]["lesson_id"], "lesson-1")
        self.assertIn("registered_source_missing", result["issues"][0]["reason"])

    def test_special_files_are_not_read(self):
        os.mkfifo(self.root / "not-a-real-audio.wav")
        result = scanner.scan(self.layout)
        self.assertEqual(result["materials"], [])
        self.assertEqual(result["issues"][0]["reason"], "not_a_regular_file")

    def test_partial_and_failed_work_stays_pending(self):
        entry = self.lesson()
        entry["materials"][0]["coverage"] = "partial"
        self.save_ledger()
        self.assertEqual(list(self.states().values()), ["partial"])
        entry["status"] = "blocked"
        self.save_ledger()
        self.assertEqual(list(self.states().values()), ["blocked"])

    def test_finished_notes_with_unarchived_source_need_only_archiving(self):
        entry = self.lesson(source="录音.m4a")
        entry["archive_status"] = "pending"
        self.save_ledger()
        self.assertEqual(list(self.states().values()), ["needs_archiving"])

    def test_disabled_archiving_never_demands_a_move(self):
        self.lesson(source="录音.m4a")
        self.layout["archive"] = False
        self.assertEqual(list(self.states().values()), ["complete"])

    def test_duplicate_or_renamed_source_needs_review_not_deletion(self):
        self.lesson()
        duplicate = self.file("重新投递.m4a")
        result = scanner.scan(self.layout)
        self.assertEqual(self.states()["重新投递.m4a"], "duplicate_review")
        self.assertEqual(len(result["duplicate_groups"]), 1)
        self.assertTrue(duplicate.exists())

    def test_legacy_note_does_not_complete_material_by_matching_filename(self):
        self.file("课程甲/正式课堂笔记/0922.md", b"legacy note without source record")
        self.file("课程甲/笔记素材/0922.m4a")
        self.assertEqual(list(self.states().values()), ["pending_review"])

    def test_reference_exclusion_is_content_sensitive(self):
        path = self.file("课程表.pdf")
        self.ledger["references"].append({"path": "课程表.pdf", "sha256": scanner.digest(path)[0],
                                           "reason": "Schedule, not a lecture source"})
        self.save_ledger()
        self.assertEqual(list(self.states().values()), ["reference"])
        path.write_bytes(b"changed content")
        self.assertEqual(list(self.states().values()), ["pending_review"])

    def test_newline_in_filename_survives_cli_json(self):
        name = "课程甲/笔记素材/原稿\n.md"
        self.file(name)
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), "--config", str(self.config)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["materials"][0]["path"], name)

    def test_corrupt_state_and_path_escape_fail_without_reinitializing(self):
        path = self.file(".class-notes/processing.json", b"broken JSON")
        with self.assertRaises(ValueError):
            scanner.scan(self.layout)
        self.assertEqual(path.read_bytes(), b"broken JSON")
        entry = self.lesson()
        entry["outputs"][0]["path"] = "../external.md"
        self.save_ledger()
        with self.assertRaises(ValueError):
            scanner.scan(self.layout)

    def test_symlinks_are_reported_and_never_followed(self):
        outside = self.base / "outside.txt"
        outside.write_text("private outside fixture")
        (self.root / "alias.txt").symlink_to(outside)
        (self.root / "alias-directory").symlink_to(self.base, target_is_directory=True)
        result = scanner.scan(self.layout)
        self.assertEqual(result["materials"], [])
        self.assertEqual(len(result["issues"]), 2)

    def test_old_configuration_accepts_explicit_root_without_rewriting(self):
        original = 'schema_version = 1\n[defaults]\nfallback_tier = "B"\n[courses]\n'
        self.config.write_text(original)
        with self.assertRaises(ValueError):
            scanner.load_layout(self.config)
        layout = scanner.load_layout(self.config, self.root)
        self.assertFalse(layout["archive"])
        self.assertEqual(self.config.read_text(), original)

    def test_invalid_same_directory_configuration_is_rejected(self):
        self.config.write_text('schema_version = 1\n[storage]\nclass_root = '
                               + json.dumps(str(self.root))
                               + '\nnotes_subdir = "相同"\nmaterials_subdir = "相同"\n')
        with self.assertRaises(ValueError):
            scanner.load_layout(self.config)


if __name__ == "__main__":
    unittest.main()
