"""Portable class-notes resources and fixed-format contracts; no personal data."""

from pathlib import Path
import re
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "plugins/sylvia-learning/skills/class-notes"


class ClassNotesPackageTests(unittest.TestCase):
    def test_every_resource_is_linked_directly_from_entrypoint(self):
        entry = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        links = set(re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", entry))
        resources = {p.relative_to(SKILL).as_posix()
                     for folder in ("references", "assets", "scripts")
                     for p in (SKILL / folder).rglob("*") if p.is_file()}
        self.assertEqual(links, resources)
        self.assertLess(len(entry.splitlines()), 500)

    def test_resource_links_resolve_inside_package(self):
        for document in SKILL.rglob("*.md"):
            for target in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)",
                                     document.read_text(encoding="utf-8")):
                if "://" in target:
                    continue
                with self.subTest(document=document.name, target=target):
                    resolved = (document.parent / target).resolve()
                    self.assertTrue(resolved.is_relative_to(SKILL.resolve()))
                    self.assertTrue(resolved.is_file())

    def test_fixed_headings_match_format_contract(self):
        contract = (SKILL / "references/note-formats.md").read_text(encoding="utf-8")
        expected_counts = {"note-a.md": 8, "note-b.md": 8,
                           "english-transcript.md": 4, "course-index.md": 4,
                           "chapter-review.md": 6}
        for name, count in expected_counts.items():
            with self.subTest(template=name):
                contents = (SKILL / "assets" / name).read_text(encoding="utf-8")
                headings = re.findall(r"^## (.+)$", contents, re.M)
                self.assertEqual(len(headings), count)
                self.assertEqual(len(set(headings)), count)
                for heading in headings:
                    self.assertIn(heading, contract)
                self.assertEqual(len(re.findall(r"^# ", contents, re.M)), 1)

    def test_templates_have_closed_placeholders_and_folded_answers(self):
        for template in (SKILL / "assets").glob("*.md"):
            text = template.read_text(encoding="utf-8")
            placeholders = re.findall(r"\{\{[^{}\n]+\}\}", text)
            self.assertTrue(placeholders, template.name)
            remaining = re.sub(r"\{\{[^{}\n]+\}\}", "", text)
            self.assertNotIn("{{", remaining, template.name)
            self.assertNotIn("}}", remaining, template.name)
            if template.name in ("note-a.md", "note-b.md", "chapter-review.md"):
                self.assertEqual(text.count("<details>"), 1)
                self.assertEqual(text.count("</details>"), 1)
                self.assertIn("</summary>\n\n", text)

    def test_blank_configuration_remains_schema_one_and_unassigned(self):
        with (SKILL / "assets/config.example.toml").open("rb") as handle:
            data = tomllib.load(handle)
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["courses"], {})
        self.assertEqual(data["defaults"], {
            "fallback_tier": "A", "note_language": "zh-CN",
            "delivery": "task-artifacts", "review_answers": "collapsed"})

    def test_package_has_no_machine_specific_paths_or_symlinks(self):
        for path in SKILL.rglob("*"):
            self.assertFalse(path.is_symlink(), path)
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                self.assertNotRegex(text, r"/Users/|/home/|[A-Za-z]:\\Users\\")


if __name__ == "__main__":
    unittest.main()
