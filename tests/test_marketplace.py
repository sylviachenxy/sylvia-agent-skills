"""Distribution contracts: synthetic fixtures only, no accounts or installations."""

import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from validate_marketplace import validate, read_json
from smoke_marketplace import compare_package


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="sylvia-catalog-test-")
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.catalog_path = self.root / ".agents/plugins/marketplace.json"
        self.catalog = copy.deepcopy(read_json(ROOT / ".agents/plugins/marketplace.json"))
        self.write_json(self.catalog_path, self.catalog)
        shutil.copy2(ROOT / "LICENSE", self.root / "LICENSE")
        shutil.copy2(ROOT / "README.md", self.root / "README.md")
        for entry in self.catalog["plugins"]:
            plugin = self.root / entry["source"]["path"]
            source = ROOT / entry["source"]["path"]
            self.write_json(plugin / ".codex-plugin/plugin.json", read_json(source / ".codex-plugin/plugin.json"))
            shutil.copy2(source / "LICENSE", plugin / "LICENSE")
            shutil.copy2(source / "README.md", plugin / "README.md")
            for skill in (source / "skills").iterdir():
                target = plugin / "skills" / skill.name
                target.mkdir(parents=True)
                (target / "SKILL.md").write_text(
                    f"---\nname: {skill.name}\ndescription: Synthetic test skill\n---\nTest body.\n")
        self.plugin = self.root / "plugins/sylvia-learning"
        self.manifest_path = self.plugin / ".codex-plugin/plugin.json"
        self.manifest = read_json(self.manifest_path)
        self.skill = self.plugin / "skills/deep-reading-coach"

    def write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def invalid(self, pattern):
        with self.assertRaisesRegex((ValueError, OSError), pattern):
            validate(self.root)

    def test_repository_has_exact_domain_partition(self):
        result = validate(ROOT)
        groups = {p["name"]: {s["name"] for s in p["skills"]} for p in result["plugins"]}
        self.assertEqual(groups, {
            "sylvia-learning": {"deep-reading-coach", "cet4-speaking-partner", "ielts-speaking-coach",
                                "shanghai-gaokao-english-tutor"},
            "sylvia-productivity": {"goal-planner", "personal-scheduler", "weekly-review", "morning-brief"}})

    def test_valid_fixture(self):
        self.assertEqual(validate(self.root)["skill_count"], 8)

    def test_duplicate_plugin_fails(self):
        self.catalog["plugins"].append(self.catalog["plugins"][0])
        self.write_json(self.catalog_path, self.catalog)
        self.invalid("duplicate plugin")

    def test_absolute_parent_and_mismatched_source_fail(self):
        for path in ("/tmp/elsewhere", "./plugins/../../elsewhere", "./plugins/sylvia-productivity"):
            self.catalog["plugins"][0]["source"]["path"] = path
            self.write_json(self.catalog_path, self.catalog)
            self.invalid("source must")

    def test_missing_directory_fails(self):
        self.plugin.rename(self.root / "not-a-plugin")
        self.invalid("missing plugin directory")

    def test_unregistered_plugin_fails(self):
        (self.root / "plugins/forgotten").mkdir()
        self.invalid("unregistered")

    def test_duplicate_version_authority_fails(self):
        self.catalog["plugins"][0]["version"] = "0.1.0"
        self.write_json(self.catalog_path, self.catalog)
        self.invalid("version authority")

    def test_invalid_semver_fails(self):
        for version in (None, "1", "01.0.0", "0.1.0-01"):
            self.manifest["version"] = version
            self.write_json(self.manifest_path, self.manifest)
            self.invalid("invalid semver")

    def test_second_manifest_fails(self):
        self.write_json(self.plugin / "plugin.json", self.manifest)
        self.invalid("second manifest")

    def test_mismatched_name_fails(self):
        self.manifest["name"] = "something-else"
        self.write_json(self.manifest_path, self.manifest)
        self.invalid("name mismatch")

    def test_changed_license_fails(self):
        (self.plugin / "LICENSE").write_text("Different grant")
        self.invalid("license changed")

    def test_new_authorization_components_fail(self):
        self.manifest["mcpServers"] = "./.mcp.json"
        self.write_json(self.manifest_path, self.manifest)
        self.invalid("external components")

    def test_automatic_install_policy_fails(self):
        self.catalog["plugins"][0]["policy"]["installation"] = "INSTALLED_BY_DEFAULT"
        self.write_json(self.catalog_path, self.catalog)
        self.invalid("policy")

    def test_skill_name_mismatch_fails(self):
        (self.skill / "SKILL.md").write_text("---\nname: wrong-name\ndescription: Test\n---\n")
        self.invalid("frontmatter name mismatch")

    def test_duplicate_skill_fails(self):
        shutil.copytree(self.skill, self.root / "plugins/sylvia-productivity/skills/deep-reading-coach")
        self.invalid("duplicate skill")

    def test_dangling_and_external_and_internal_symlinks_fail(self):
        for destination in (self.root / "missing", ROOT / "LICENSE", self.skill / "SKILL.md"):
            link = self.skill / "linked-resource"
            link.symlink_to(destination)
            self.invalid("symlink")
            link.unlink()

    def test_whole_skill_symlink_fails_even_inside_repository(self):
        target = self.root / "moved-skill"
        self.skill.rename(target)
        self.skill.symlink_to(target, target_is_directory=True)
        self.invalid("symlink")

    def test_duplicate_json_key_fails(self):
        self.catalog_path.write_text('{"name":"first","name":"second"}')
        self.invalid("duplicate JSON key")

    def test_unbundled_starter_skill_fails(self):
        self.manifest["interface"]["defaultPrompt"] = ["Use $missing-skill"]
        self.write_json(self.manifest_path, self.manifest)
        self.invalid("unbundled skill")

    def test_missing_documentation_entry_fails(self):
        (self.plugin / "README.md").write_text("Incomplete")
        self.invalid("missing README skill")

    def test_empty_installed_plugin_cannot_pass(self):
        target = self.root / "empty-install"
        target.mkdir()
        with self.assertRaisesRegex(ValueError, "missing/extra"):
            compare_package(self.plugin, target)

    def test_changed_resource_and_executable_mode_fail(self):
        (self.skill / "run.sh").write_text("#!/bin/sh\nexit 0\n")
        (self.skill / "run.sh").chmod(0o755)
        target = self.root / "installed"
        shutil.copytree(self.skill, target)
        self.assertEqual(compare_package(self.skill, target), 2)
        (target / "run.sh").chmod(0o644)
        with self.assertRaisesRegex(ValueError, "content/mode"):
            compare_package(self.skill, target)
        warnings = []
        self.assertEqual(compare_package(self.skill, target, gh_metadata=True, mode_warnings=warnings), 2)
        self.assertEqual(warnings, ["run.sh"])
        (target / "run.sh").chmod(0o755)
        (target / "run.sh").write_text("Changed")
        with self.assertRaisesRegex(ValueError, "content/mode"):
            compare_package(self.skill, target)

    def test_gh_metadata_is_allowed_but_body_changes_are_not(self):
        target = self.root / "gh-install"
        shutil.copytree(self.skill, target)
        body = (target / "SKILL.md").read_text()
        (target / "SKILL.md").write_text(body.replace("description:", "metadata:\n  github-path: example\ndescription:"))
        self.assertEqual(compare_package(self.skill, target, gh_metadata=True), 1)
        (target / "SKILL.md").write_text(body + "Changed body")
        with self.assertRaisesRegex(ValueError, "changed the skill body"):
            compare_package(self.skill, target, gh_metadata=True)

    def test_gh_only_normalizes_leading_body_blank_lines(self):
        target = self.root / "gh-install"
        shutil.copytree(self.skill, target)
        body = (self.skill / "SKILL.md").read_text()
        (self.skill / "SKILL.md").write_text(body.replace("---\nTest body", "---\n\nTest body"))
        self.assertEqual(compare_package(self.skill, target, gh_metadata=True), 1)


if __name__ == "__main__":
    unittest.main()
