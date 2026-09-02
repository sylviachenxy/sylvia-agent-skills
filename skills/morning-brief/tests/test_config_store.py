"""Synthetic configuration persistence checks; never uses default user storage."""
import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import config_store
from config_store import ConfigStore, fingerprint
from brief_core import ValidationError


class ConfigStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.registry = self.base / "registry"
        self.store = ConfigStore(self.registry)
        self.example = json.loads((SCRIPTS.parent / "assets/config.example.json").read_text())

    def config(self, name="one", revision=1):
        value = copy.deepcopy(self.example)
        value["config_id"] = name
        value["config_revision"] = revision
        value["storage"]["state_dir"] = str(self.base / (name + "-state"))
        value["modules"]["updates"]["scope"]["topics"][0]["query"] = "PRIVATE SYNTHETIC PREFERENCE"
        return value

    def saved(self, name="one", revision=1, **kwargs):
        return self.store.save(self.config(name, revision), expected_revision=0, profile=name, apply=True, **kwargs)

    def current(self, name="one"):
        return self.store.resolve(name)["config"]

    def edit(self, name="one", items=2):
        value = self.current(name)
        value["modules"]["updates"]["max_items"] = items
        return value

    def tree(self):
        return {str(path.relative_to(self.base)): (path.stat().st_mode & 0o777, path.read_bytes() if path.is_file() else None)
                for path in self.base.rglob("*")}

    def test_constructor_and_missing_reads_never_create_storage(self):
        with patch.object(Path, "home", return_value=self.base):
            default = ConfigStore()
            self.assertEqual(default.registry_dir, self.base / "Library/Application Support/morning-brief")
        self.assertEqual(self.store.list_profiles()["profiles"], [])
        self.assertIsNone(self.store.list_profiles()["default_profile"])
        with self.assertRaisesRegex(ValidationError, "no_profiles"):
            self.store.resolve()
        self.assertEqual(self.tree(), {})

    def test_save_preview_has_zero_writes(self):
        preview = self.store.save(self.config(), expected_revision=0, profile="one", make_default=True)
        self.assertEqual(preview["status"], "preview")
        self.assertTrue(preview["changed"])
        self.assertEqual(preview["config_revision"], 1)
        self.assertEqual(self.tree(), {})

    def test_default_identity_and_private_snapshot_layout(self):
        result = self.saved()
        self.assertEqual(result["status"], "saved")
        self.assertEqual(set(self.store.resolve()), {"config", "config_path", "fingerprint", "profile", "registry_dir", "profile_dir"})
        self.assertEqual(self.store.resolve()["profile"], "one")
        self.assertIsNone(self.store.list_profiles()["default_profile"])
        self.assertFalse(self.store.list_profiles()["profiles"][0]["is_default"])
        path = Path(result["config_path"])
        self.assertEqual(path.parent, self.registry / "profiles/one/revisions")
        self.assertEqual(path.name, "r000001-" + result["fingerprint"] + ".json")
        for node in self.registry.rglob("*"):
            self.assertEqual(stat.S_IMODE(node.stat().st_mode), 0o700 if node.is_dir() else 0o600)
        registry = json.loads(self.store.registry_path.read_text())
        self.assertEqual(set(registry), {"schema_version", "default_profile", "profiles"})
        self.assertEqual(set(registry["profiles"]["one"]), {"directory", "history"})
        self.assertFalse(any(node.name == "config.json" for node in self.registry.rglob("*")))

    def test_fingerprint_matches_existing_cli_encoding(self):
        value = self.config()
        expected = hashlib.sha256((json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")).hexdigest()
        self.assertEqual(fingerprint(value), expected)
        self.assertEqual(self.saved()["fingerprint"], expected)

    def test_import_existing_positive_revision_then_increment(self):
        first = self.saved(revision=17)
        self.assertEqual(first["config_revision"], 17)
        result = self.store.save(self.edit(), expected_revision=17, profile="one", apply=True)
        self.assertEqual(result["config_revision"], 18)
        self.assertEqual([x["revision"] for x in self.store.history("one")["history"]], [17, 18])

    def test_semantic_noop_does_not_increment_or_replace_registry(self):
        self.saved()
        before = self.tree()
        value = dict(reversed(list(self.current().items())))
        result = self.store.save(value, expected_revision=1, profile="one", apply=True)
        self.assertEqual(result["status"], "unchanged")
        self.assertFalse(result["changed"])
        self.assertEqual(result["changed_fields"], [])
        self.assertEqual(self.tree(), before)

    def test_update_revision_owned_by_store_and_changed_fields_redacted(self):
        self.saved()
        result = self.store.save(self.edit(), expected_revision=1, profile="one", apply=True)
        self.assertEqual(result["config_revision"], 2)
        self.assertEqual(result["changed_fields"], ["modules.updates.max_items"])
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotIn("PRIVATE", json.dumps(self.store.list_profiles()))
        self.assertNotIn("PRIVATE", json.dumps(self.store.history()))

    def test_stale_expected_and_input_revisions_refused(self):
        self.saved()
        value = self.edit()
        for expected, revision in ((0, 1), (2, 1), (1, 2)):
            value["config_revision"] = revision
            with self.subTest(expected=expected, revision=revision), self.assertRaisesRegex(ValidationError, "stale"):
                self.store.save(value, expected_revision=expected, profile="one", apply=True)
        self.assertEqual(self.current()["config_revision"], 1)

    def test_identity_state_directory_and_profile_directory_immutable(self):
        self.saved()
        for field in ("config_id", "state_dir", "profile_dir"):
            value = self.edit()
            kwargs = {}
            if field == "config_id":
                value["config_id"] = "different"
            elif field == "state_dir":
                value["storage"]["state_dir"] = str(self.base / "different-state")
            else:
                kwargs["profile_dir"] = str(self.base / "different-profile")
            with self.subTest(field=field), self.assertRaisesRegex(ValidationError, "immutable|profile_must_equal_config_id"):
                self.store.save(value, expected_revision=1, profile="one", apply=True, **kwargs)
        self.assertEqual(self.current()["config_revision"], 1)

    def test_notes_delivery_target_cannot_change_in_preview_or_apply(self):
        self.saved()
        before = self.tree()
        for apply in (False, True):
            for field, changed in (("account", "OTHER SYNTHETIC ACCOUNT"),
                                   ("folder", "OTHER SYNTHETIC FOLDER"), ("shared", True)):
                value = self.edit()
                value["storage"]["notes"][field] = changed
                with self.subTest(apply=apply, field=field), self.assertRaises(ValidationError) as caught:
                    self.store.save(value, expected_revision=1, profile="one", apply=apply)
                if field != "shared":
                    self.assertIn("notes_target_immutable", str(caught.exception))
                self.assertNotIn("SYNTHETIC", str(caught.exception))
                self.assertEqual(self.tree(), before)

    def test_notes_identity_is_fixed_at_first_committed_registration_not_revision_one(self):
        initial = self.config(revision=17)
        initial["storage"]["notes"] = {"account": "SYNTHETIC ACCOUNT", "folder": "SYNTHETIC DELIVERY", "shared": False}
        self.store.save(initial, 0, profile="one", apply=True)
        self.store.save(self.edit(), 17, profile="one", apply=True)
        self.store.restore(17, 18, profile="one", apply=True)
        self.assertEqual(self.current()["storage"]["notes"], initial["storage"]["notes"])
        self.assertEqual(self.current()["config_revision"], 19)

    def test_restore_cannot_revive_a_legacy_historical_notes_target(self):
        self.saved()
        # Model a pre-immutability committed history, without native operations:
        # current target A, historical middle target B, latest target A again.
        original_notes = copy.deepcopy(self.current()["storage"]["notes"])
        registry = self.store._load_registry()
        entry = registry["profiles"]["one"]
        for revision, notes in ((2, dict(original_notes, folder="LEGACY SYNTHETIC TARGET")), (3, original_notes)):
            value = self.config(revision=revision)
            value["storage"]["notes"] = notes
            record = {"revision": revision, "fingerprint": fingerprint(value)}
            self.store._write_snapshot(self.store._snapshot_path(Path(entry["directory"]), record), value)
            entry["history"].append(record)
        self.store._commit_registry(registry)
        before = self.tree()
        for apply in (False, True):
            with self.subTest(apply=apply), self.assertRaisesRegex(ValidationError, "notes_target_immutable"):
                self.store.restore(2, 3, profile="one", apply=apply)
            self.assertEqual(self.tree(), before)
        self.assertEqual([record["revision"] for record in self.store.history("one")["history"]], [1, 2, 3])
        self.assertEqual(self.store.restore(1, 3, profile="one", apply=True)["config_revision"], 4)

    def test_legacy_current_notes_drift_needs_separate_delivery_migration(self):
        self.saved()
        registry = self.store._load_registry()
        entry = registry["profiles"]["one"]
        drifted = self.config(revision=2)
        drifted["storage"]["notes"]["folder"] = "LEGACY SYNTHETIC TARGET"
        record = {"revision": 2, "fingerprint": fingerprint(drifted)}
        self.store._write_snapshot(self.store._snapshot_path(Path(entry["directory"]), record), drifted)
        entry["history"].append(record)
        self.store._commit_registry(registry)
        before = self.tree()
        for apply in (False, True):
            with self.assertRaisesRegex(ValidationError, "notes_target_immutable"):
                self.store.save(self.edit(), 2, profile="one", apply=apply)
            with self.assertRaisesRegex(ValidationError, "notes_target_immutable"):
                self.store.restore(1, 2, profile="one", apply=apply)
            self.assertEqual(self.tree(), before)

    def test_multiple_profiles_require_explicit_default_and_use_is_preview_first(self):
        self.saved("one")
        self.saved("two")
        with self.assertRaisesRegex(ValidationError, "default_required"):
            self.store.resolve()
        before = self.tree()
        result = self.store.use("two")
        self.assertEqual(result["status"], "preview")
        self.assertEqual(self.tree(), before)
        self.assertIsNone(self.store.list_profiles()["default_profile"])
        result = self.store.use("two", apply=True)
        self.assertEqual(result["status"], "saved")
        self.assertEqual(self.store.resolve()["profile"], "two")
        self.assertEqual(self.store.list_profiles()["default_profile"], "two")

    def test_make_default_can_commit_without_configuration_revision_change(self):
        self.saved()
        result = self.store.save(self.current(), expected_revision=1, profile="one", make_default=True, apply=True)
        self.assertEqual(result["config_revision"], 1)
        self.assertEqual(result["changed_fields"], ["default_profile"])
        self.assertEqual(len(self.store.history()["history"]), 1)
        self.assertEqual(self.store.list_profiles()["default_profile"], "one")

    def test_custom_directory_is_discoverable_in_a_new_session(self):
        custom = self.base / "custom-profile"
        result = self.saved(profile_dir=custom, make_default=True)
        other = ConfigStore(self.registry)
        resolved = other.resolve()
        self.assertEqual(resolved["profile_dir"], str(custom))
        self.assertEqual(resolved["config_path"], result["config_path"])
        self.assertEqual(resolved["config"], self.config())
        self.assertFalse((custom / "config.json").exists())

    def test_restore_uses_a_new_revision_and_dryrun_is_inert(self):
        initial = self.saved()
        self.store.save(self.edit(items=2), expected_revision=1, profile="one", apply=True)
        before = self.tree()
        preview = self.store.restore(1, expected_revision=2, profile="one")
        self.assertEqual(preview["config_revision"], 3)
        self.assertEqual(self.tree(), before)
        result = self.store.restore(1, expected_revision=2, profile="one", apply=True)
        self.assertEqual(result["config_revision"], 3)
        self.assertNotEqual(result["fingerprint"], initial["fingerprint"])
        self.assertEqual(self.current()["modules"]["updates"]["max_items"], 3)
        self.assertEqual([x["revision"] for x in self.store.history()["history"]], [1, 2, 3])
        self.assertEqual(self.store.restore(3, expected_revision=3, profile="one", apply=True)["status"], "unchanged")

    def test_restore_rejects_stale_or_uncommitted_revision(self):
        self.saved()
        for target, expected in ((2, 1), (1, 2)):
            with self.assertRaises(ValidationError):
                self.store.restore(target, expected_revision=expected, profile="one", apply=True)

    def test_crash_before_registry_keeps_old_active_and_orphan_is_reused(self):
        self.saved()
        value = self.edit()
        old_registry = self.store.registry_path.read_bytes()
        with patch.object(self.store, "_commit_registry", side_effect=OSError("PRIVATE failure")):
            with self.assertRaises(ValidationError) as caught:
                self.store.save(value, expected_revision=1, profile="one", apply=True)
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertEqual(self.store.registry_path.read_bytes(), old_registry)
        self.assertEqual(self.current()["config_revision"], 1)
        self.assertEqual(len(self.store.history()["history"]), 1)
        snapshots = list((self.registry / "profiles/one/revisions").glob("*.json"))
        self.assertEqual(len(snapshots), 2)
        result = ConfigStore(self.registry).save(value, expected_revision=1, profile="one", apply=True)
        self.assertEqual(result["config_revision"], 2)
        self.assertEqual(len(list((self.registry / "profiles/one/revisions").glob("*.json"))), 2)

    def test_registry_replace_failure_keeps_old_active(self):
        self.saved()
        old_registry = self.store.registry_path.read_bytes()
        with patch.object(config_store.os, "replace", side_effect=OSError("PRIVATE replace failure")):
            with self.assertRaises(ValidationError) as caught:
                self.store.save(self.edit(), expected_revision=1, profile="one", apply=True)
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertEqual(self.store.registry_path.read_bytes(), old_registry)
        self.assertEqual(self.current()["config_revision"], 1)
        self.assertEqual(len(self.store.history()["history"]), 1)

    def test_post_replace_directory_sync_failure_is_uncertain_and_resolvable(self):
        self.saved()
        actual = config_store._sync_directory
        root_calls = 0
        def fail_post_replace(path):
            nonlocal root_calls
            if Path(path) == self.registry:
                root_calls += 1
                if root_calls == 2:
                    raise OSError("PRIVATE durability failure")
            return actual(path)
        with patch.object(config_store, "_sync_directory", side_effect=fail_post_replace):
            with self.assertRaisesRegex(ValidationError, "commit_outcome_uncertain") as caught:
                self.store.save(self.edit(), expected_revision=1, profile="one", apply=True)
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertEqual(ConfigStore(self.registry).resolve("one")["config"]["config_revision"], 2)
        self.assertEqual([record["revision"] for record in self.store.history()["history"]], [1, 2])

    def test_failed_first_commit_not_discovered_and_alternative_retry_ignores_orphan(self):
        with patch.object(self.store, "_commit_registry", side_effect=OSError("synthetic crash")):
            with self.assertRaises(ValidationError):
                self.saved()
        self.assertEqual(ConfigStore(self.registry).list_profiles()["profiles"], [])
        value = self.config()
        value["modules"]["updates"]["max_items"] = 2
        self.store.save(value, expected_revision=0, profile="one", apply=True)
        self.assertEqual(len(self.store.history()["history"]), 1)
        self.assertEqual(len(list((self.registry / "profiles/one/revisions").glob("*.json"))), 2)

    def test_uncommitted_first_snapshot_does_not_anchor_notes_delivery_identity(self):
        initial = self.config()
        with patch.object(self.store, "_commit_registry", side_effect=OSError("synthetic crash")):
            with self.assertRaises(ValidationError):
                self.store.save(initial, 0, profile="one", apply=True)
        actual = self.config()
        actual["storage"]["notes"]["folder"] = "ACTUAL SYNTHETIC COMMITTED TARGET"
        self.store.save(actual, 0, profile="one", apply=True)
        self.assertEqual(len(self.store.history("one")["history"]), 1)
        self.assertEqual(self.current()["storage"]["notes"], actual["storage"]["notes"])
        before = self.tree()
        with self.assertRaisesRegex(ValidationError, "notes_target_immutable"):
            self.store.save(initial, 1, profile="one", apply=True)
        self.assertEqual(self.tree(), before)

    def test_existing_snapshot_is_immutable(self):
        self.saved()
        value = self.edit()
        prospective = self.store.save(value, expected_revision=1, profile="one")
        orphan = Path(prospective["config_path"])
        orphan.write_text('{"synthetic":"conflict"}')
        orphan.chmod(0o600)
        before = orphan.read_bytes()
        with self.assertRaisesRegex(ValidationError, "orphan_snapshot_conflict"):
            self.store.save(value, expected_revision=1, profile="one", apply=True)
        self.assertEqual(orphan.read_bytes(), before)
        self.assertEqual(self.current()["config_revision"], 1)

    def test_two_writers_only_one_cas_update_commits(self):
        self.saved()
        first, second = self.edit(items=1), self.edit(items=2)
        barrier = threading.Barrier(2)
        def worker(value):
            barrier.wait()
            try:
                return ConfigStore(self.registry).save(value, expected_revision=1, profile="one", apply=True)["status"]
            except ValidationError as error:
                return str(error)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker, value) for value in (first, second)]
            results = [item.result(timeout=10) for item in futures]
        self.assertEqual(results.count("saved"), 1)
        self.assertEqual(sum("stale_revision" in item for item in results), 1)
        self.assertEqual(self.current()["config_revision"], 2)
        self.assertEqual(len(self.store.history()["history"]), 2)

    def test_locked_resolve_holds_writer_lock_and_checks_fingerprint(self):
        self.saved()
        resolved = self.store.resolve()
        with self.assertRaisesRegex(ValidationError, "stale_fingerprint"):
            with self.store.locked_resolve("one", expected_fingerprint="0" * 64):
                self.fail("stale fingerprint entered context")
        with patch.object(config_store, "LOCK_WAIT_SECONDS", 0.05):
            with self.store.locked_resolve("one", expected_fingerprint=resolved["fingerprint"]) as held:
                self.assertEqual(held, resolved)
                self.assertEqual(ConfigStore(self.registry).resolve("one"), held)
                with self.assertRaisesRegex(ValidationError, "busy"):
                    ConfigStore(self.registry).save(self.edit(), expected_revision=1, profile="one", apply=True)
        self.store.save(self.edit(), expected_revision=1, profile="one", apply=True)

    def test_permission_errors_are_not_silently_repaired(self):
        self.registry.mkdir(mode=0o755)
        with self.assertRaisesRegex(ValidationError, "0700"):
            self.saved()
        self.assertEqual(stat.S_IMODE(self.registry.stat().st_mode), 0o755)
        self.registry.chmod(0o700)
        self.saved()
        snapshot = Path(self.store.resolve()["config_path"])
        for path in (self.store.registry_path, snapshot):
            path.chmod(0o644)
            with self.assertRaisesRegex(ValidationError, "0600"):
                self.store.resolve()
            path.chmod(0o600)

    def test_symlink_root_custom_profile_snapshot_and_state_refused(self):
        real = self.base / "real"
        real.mkdir(mode=0o700)
        link = self.base / "link"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(ValidationError, "symlink"):
            ConfigStore(link).list_profiles()
        with self.assertRaisesRegex(ValidationError, "symlink"):
            self.saved(profile_dir=link)
        value = self.config(); value["storage"]["state_dir"] = str(link / "state")
        with self.assertRaisesRegex(ValidationError, "symlink"):
            self.store.save(value, expected_revision=0, profile="one", apply=True)
        self.saved()
        snapshot = Path(self.store.resolve()["config_path"])
        outside = self.base / "outside.json"
        snapshot.rename(outside)
        snapshot.symlink_to(outside)
        with self.assertRaisesRegex(ValidationError, "symlink"):
            self.store.resolve()

    def test_git_and_skill_paths_refused_even_when_not_created(self):
        for marker in (".git", "SKILL.md"):
            root = self.base / marker.replace(".", "-")
            root.mkdir(mode=0o700)
            (root / marker).write_text("synthetic")
            with self.assertRaisesRegex(ValidationError, "git_or_skill"):
                ConfigStore(root / "registry").save(self.config(), expected_revision=0, apply=True)
            self.assertFalse((root / "registry").exists())

    def test_custom_directories_must_not_overlap_profiles_or_registry(self):
        self.saved()
        one = Path(self.store.resolve()["profile_dir"])
        for directory in (self.registry, self.base, one, one / "nested"):
            with self.subTest(directory=directory), self.assertRaises(ValidationError):
                self.saved("two", profile_dir=directory)

    def test_corrupt_registry_does_not_fallback_or_adopt_files(self):
        self.saved()
        original = self.store.registry_path.read_bytes()
        for raw in (b'{"schema_version":1,"schema_version":1}', b'{"x":NaN}', b'[]', b'{}', b'{"x":1e99999}', b'{}{}', b'{"PRIVATE":1}', b'x' * (config_store.MAX_BYTES + 1)):
            self.store.registry_path.write_bytes(raw)
            with self.assertRaises(ValidationError) as caught:
                self.store.list_profiles()
            self.assertNotIn("PRIVATE", str(caught.exception))
        self.store.registry_path.write_bytes(original)
        registry = json.loads(original)
        registry["default_profile"] = "unknown"
        self.store.registry_path.write_text(json.dumps(registry))
        with self.assertRaisesRegex(ValidationError, "corrupt_default"):
            self.store.resolve()

    def test_missing_committed_snapshot_and_tampered_payload_fail_closed(self):
        self.saved()
        snapshot = Path(self.store.resolve()["config_path"])
        original = snapshot.read_bytes()
        snapshot.write_text(json.dumps(self.config("different")))
        with self.assertRaisesRegex(ValidationError, "identity_mismatch|integrity"):
            self.store.resolve()
        snapshot.write_bytes(original)
        snapshot.unlink()
        with self.assertRaisesRegex(ValidationError, "missing_committed"):
            self.store.list_profiles()

    def test_profile_equals_config_id_and_casefold_duplicates_refused(self):
        with self.assertRaisesRegex(ValidationError, "profile_must_equal"):
            self.store.save(self.config(), expected_revision=0, profile="alias", apply=True)
        self.assertFalse(self.registry.exists())
        self.saved("one")
        with self.assertRaisesRegex(ValidationError, "case_conflict"):
            self.saved("ONE")
        self.assertEqual(len(self.store.list_profiles()["profiles"]), 1)

    def test_corrupt_registry_alias_cannot_read_valid_custom_snapshot(self):
        self.saved(profile_dir=self.base / "custom")
        registry = json.loads(self.store.registry_path.read_text())
        registry["profiles"]["alias"] = registry["profiles"].pop("one")
        self.store.registry_path.write_text(json.dumps(registry))
        with self.assertRaisesRegex(ValidationError, "profile_identity_mismatch"):
            self.store.resolve("alias")

    def test_invalid_request_never_creates_root_and_redacts_config(self):
        value = self.config()
        value["PRIVATE SECRET FIELD"] = "PRIVATE BODY"
        with self.assertRaises(ValidationError) as caught:
            self.store.save(value, expected_revision=0, profile="one", apply=True)
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertFalse(self.registry.exists())
        for expected in (True, -1, "0"):
            with self.assertRaises(ValidationError):
                self.store.save(self.config(), expected_revision=expected, profile="one", apply=True)
        for profile in ("../escape", "bad/name", ".hidden", ""):
            with self.assertRaises(ValidationError):
                self.store.save(self.config(), expected_revision=0, profile=profile, apply=True)
        self.assertFalse(self.registry.exists())


if __name__ == "__main__":
    unittest.main()
