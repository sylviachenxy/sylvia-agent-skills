"""Synthetic regressions for legacy naming, independent states and safe log updates."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audit_layout as auditor
import render_logs as logs
import scan_materials as scanner


class LayoutLogTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="class-notes-layout-test-")
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name).resolve()
        config = self.root / ".config.toml"
        config.write_text('schema_version = 1\n[storage]\narchive_materials = true\n', encoding="utf-8")
        self.layout = scanner.load_layout(config, self.root)
        self.ledger = {"schema_version": 1, "updated_at": "2026-09-22T10:00:00+08:00",
                       "lessons": [], "references": []}

    def file(self, name, body=b"synthetic material"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return path

    def save(self):
        self.file(".class-notes/processing.json", json.dumps(self.ledger, ensure_ascii=False).encode())

    def lesson(self, name="示例课程_2026-09-22.md"):
        source = "示例课程/笔记素材/录音\n.m4a"
        path = self.file(source)
        note = f"示例课程/正式课堂笔记/{name}"
        self.file(note, b"synthetic verified note")
        lesson = {"lesson_id": "example-1", "course_folder": "示例课程", "lecture_date": "2026-09-22",
                  "processing_started": True, "status": "complete", "archive_status": "complete",
                  "requires_transcript": False,
                  "materials": [{"path": source, "sha256": scanner.digest(path)[0], "coverage": "full"}],
                  "outputs": [{"path": note, "role": "notes", "verified": True}]}
        self.ledger["lessons"].append(lesson)
        self.save()
        return lesson

    def reasons(self):
        return {item["reason"] for item in auditor.audit(self.layout)["issues"]}

    def test_old_main_name_detected_but_registered_index_exempt(self):
        lesson = self.lesson("0922.md")
        index = "示例课程/正式课堂笔记/示例课程_课程索引.md"
        self.file(index)
        lesson["outputs"].append({"path": index, "role": "index", "verified": True})
        self.save()
        names = [item["path"] for item in auditor.audit(self.layout)["issues"] if item["reason"] == "noncanonical_main_note"]
        self.assertEqual(names, [lesson["outputs"][0]["path"]])

    def test_reference_only_course_is_not_skipped(self):
        path = self.file("参考课/考核.md")
        self.ledger["references"].append({"path": "参考课/考核.md", "sha256": scanner.digest(path)[0],
                                         "reason": "考核说明", "course_folder": "参考课"})
        self.save()
        result = auditor.audit(self.layout)
        self.assertEqual(result["courses"], ["参考课"])
        self.assertEqual(sum(i["reason"] == "missing_standard_directory" for i in result["issues"]), 2)
        self.assertIn("outside_standard_directories", {i["reason"] for i in result["issues"]})

    def test_empty_confirmed_course_gets_structure_findings(self):
        (self.root / "空课程").mkdir()
        result = auditor.audit(self.layout, ["空课程"])
        self.assertFalse(result["layout_pass"])
        self.assertEqual(len(result["issues"]), 3)

    def test_audio_and_registered_raw_markdown_in_formal_are_flagged(self):
        lesson = self.lesson()
        self.file("示例课程/正式课堂笔记/原音频.m4a")
        path = self.file("示例课程/正式课堂笔记/原手记.md")
        lesson["materials"].append({"path": path.relative_to(self.root).as_posix(),
                                    "sha256": scanner.digest(path)[0], "coverage": "full"})
        self.save()
        self.assertEqual(sum(i["reason"] == "source_in_formal_notes" for i in auditor.audit(self.layout)["issues"]), 2)

    def test_unknown_formal_markdown_requires_role_review(self):
        self.lesson()
        self.file("示例课程/正式课堂笔记/未知.md")
        self.assertIn("unregistered_formal_file_review_role", self.reasons())

    def test_collision_and_unconfirmed_date_are_not_renamed(self):
        lesson = self.lesson("0922.md")
        target = self.file("示例课程/正式课堂笔记/示例课程_2026-09-22.md", b"other note")
        self.assertIn("rename_target_exists", self.reasons())
        self.assertEqual(target.read_bytes(), b"other note")
        lesson["lecture_date"] = "2026-02-30"
        self.save()
        self.assertIn("unconfirmed_lecture_date_or_name", self.reasons())

    def test_audit_is_read_only_and_includes_missing_current_paths(self):
        lesson = self.lesson()
        lesson["materials"][0]["original_path"] = lesson["materials"][0]["path"]
        lesson["materials"][0]["path"] = "示例课程/笔记素材/不存在.m4a"
        self.save()
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertIn("registered_current_path_missing", self.reasons())
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_complete_and_incomplete_are_independent_of_archive(self):
        lesson = self.lesson()
        self.assertEqual(logs.content_status(lesson, self.layout)[0], "complete")
        lesson["status"] = "partial"
        lesson["pending_actions"] = ["补齐原语言完整听写"]
        lesson["requires_transcript"] = True
        self.save()
        result = logs.build_logs(self.layout, write=True)
        text = result["logs"][0]["content"]
        self.assertIn("— incomplete", text)
        self.assertIn("素材归位：complete", text)
        self.assertIn("补齐原语言完整听写", text)
        self.assertEqual(scanner.scan(self.layout)["counts"], {"partial": 1})

    def test_pending_requires_explicit_not_started_and_draft_can_be_unknown(self):
        lesson = self.lesson()
        lesson.update(status="draft", processing_started=False, outputs=[])
        self.assertEqual(logs.content_status(lesson, self.layout)[0], "pending")
        lesson.pop("processing_started")
        self.assertEqual(logs.content_status(lesson, self.layout)[0], "待核定")
        lesson["outputs"] = [{"path": "示例课程/正式课堂笔记/计划但不存在.md", "role": "notes", "verified": False}]
        self.assertEqual(logs.content_status(lesson, self.layout)[0], "待核定")
        lesson["processing_started"] = True
        self.assertEqual(logs.content_status(lesson, self.layout)[0], "incomplete")

    def test_invalid_not_started_complete_is_rejected(self):
        lesson = self.lesson()
        lesson["processing_started"] = False
        self.save()
        with self.assertRaises(ValueError):
            scanner.load_ledger(self.root)

    def test_missing_transcript_downgrades_stale_complete(self):
        lesson = self.lesson()
        lesson["requires_transcript"] = True
        self.assertEqual(logs.content_status(lesson, self.layout)[0], "incomplete")

    def test_changed_source_downgrades_stale_complete(self):
        lesson = self.lesson()
        self.file(lesson["materials"][0]["path"], b"changed source")
        self.assertEqual(logs.content_status(lesson, self.layout)[0], "incomplete")

    def test_changed_verified_output_downgrades_stale_complete(self):
        lesson = self.lesson()
        output = lesson["outputs"][0]
        output["sha256"] = scanner.digest(self.root / output["path"])[0]
        self.file(output["path"], b"changed output")
        self.assertEqual(logs.content_status(lesson, self.layout)[0], "incomplete")

    def test_unregistered_material_is_unknown_not_pending(self):
        self.lesson()
        self.file("示例课程/笔记素材/未知.mp3", b"different")
        text = logs.build_logs(self.layout)["logs"][0]["content"]
        self.assertIn("待核定（pending_review）", text)
        self.assertIn("pending 0", text)

    def test_reference_and_empty_placeholder_do_not_create_lessons(self):
        name = "参考课/笔记素材/占位.md"
        path = self.file(name, b"")
        self.ledger["references"].append({"path": name, "sha256": scanner.digest(path)[0], "reason": "空占位"})
        self.save()
        text = logs.build_logs(self.layout)["logs"][0]["content"]
        self.assertIn("尚无已登记课次", text)
        self.assertIn("空占位", text)

    def test_preview_read_only_and_repeat_write_idempotent(self):
        self.lesson()
        result = logs.build_logs(self.layout)
        path = self.root / result["logs"][0]["path"]
        self.assertFalse(path.exists())
        logs.build_logs(self.layout, write=True)
        timestamp = path.stat().st_mtime_ns
        self.assertFalse(logs.build_logs(self.layout, write=True)["logs"][0]["changed"])
        self.assertEqual(path.stat().st_mtime_ns, timestamp)
        self.assertTrue(auditor.audit(self.layout)["layout_pass"])

    def test_manual_notes_preserved_outside_managed_region(self):
        self.lesson()
        result = logs.build_logs(self.layout, write=True)
        path = self.root / result["logs"][0]["path"]
        path.write_text(path.read_text() + "我的手工备注。\n", encoding="utf-8")
        self.ledger["updated_at"] = "later"
        self.save()
        logs.build_logs(self.layout, write=True)
        self.assertTrue(path.read_text().endswith("我的手工备注。\n"))

    def test_unmanaged_log_is_not_excluded_or_overwritten(self):
        self.lesson()
        path = self.file("示例课程/笔记素材/log.md", b"user original source")
        with self.assertRaises(ValueError):
            logs.build_logs(self.layout, write=True)
        self.assertEqual(path.read_bytes(), b"user original source")
        self.assertIn("unmanaged_log_conflict", self.reasons())
        self.assertEqual(len(scanner.scan(self.layout)["materials"]), 2)

    def test_managed_log_excluded_and_newline_links_encoded(self):
        self.lesson()
        text = logs.build_logs(self.layout, write=True)["logs"][0]["content"]
        self.assertIn("%0A.m4a", text)
        self.assertEqual(len(scanner.scan(self.layout)["materials"]), 1)

    def test_symlink_log_never_followed(self):
        self.lesson()
        outside = self.file(".unrelated.md", b"never replace")
        (self.root / "示例课程/笔记素材/log.md").symlink_to(outside)
        with self.assertRaises(ValueError):
            logs.build_logs(self.layout, write=True)
        self.assertEqual(outside.read_bytes(), b"never replace")
        self.assertIn("unavailable_log", self.reasons())


if __name__ == "__main__":
    unittest.main()
