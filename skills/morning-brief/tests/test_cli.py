import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import subprocess
import io
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("morning_cli", SCRIPTS / "morning-brief.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class CliTests(unittest.TestCase):
    def test_doctor_is_offline(self):
        result = cli.doctor()
        self.assertFalse(result["native_apps_contacted"])
        self.assertFalse(result["permissions_checked"])

    def test_commit_uncertainty_is_not_reported_as_failed_validation_or_unsaved(self):
        output = io.BytesIO()
        with patch.object(cli, "run", side_effect=cli.ValidationError("config_store_commit_outcome_uncertain; resolve/history before retrying")), patch.object(cli.sys, "stdout", SimpleNamespace(buffer=output)):
            code = cli.main(["doctor"])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(result["error"], "configuration_commit_uncertain")
        self.assertIsNone(result["configuration_saved"])

    def test_json_rejects_duplicates_and_nan(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            for value in ('{"a":1,"a":2}', '{"a":NaN}'):
                path.write_text(value)
                with self.assertRaises(ValueError):
                    cli.load_json(path)

    def test_private_atomic_idempotence_and_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "child" / "note.txt"
            cli.write_new_or_identical(path, b"one")
            cli.write_new_or_identical(path, b"one")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ValueError):
                cli.write_new_or_identical(path, b"two")
            self.assertEqual(path.read_bytes(), b"one")

    def test_symlink_and_git_state_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "real").mkdir(mode=0o700)
            (root / "link").symlink_to(root / "real")
            with self.assertRaises(ValueError):
                cli.private_dir(root / "link")
            (root / ".git").mkdir()
            with self.assertRaises(ValueError):
                cli.private_dir(root / "state", create=True)

    def test_setup_state_scoped_to_configuration(self):
        config = json.loads((SCRIPTS.parent / "assets" / "config.example.json").read_text())
        with tempfile.TemporaryDirectory() as directory:
            config["storage"]["state_dir"] = str(Path(directory) / "state")
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(config))
            args = cli.make_parser().parse_args(["checkpoint", "--config", str(path), "--stage", "offline", "--evidence", "synthetic assertions passed"])
            result = cli.run(args)
            self.assertEqual(result["reported_stages"], ["offline"])
            self.assertFalse(result["all_stages_reported"])
            config["config_revision"] += 1
            path.write_text(json.dumps(config))
            result = cli.run(cli.make_parser().parse_args(["setup-status", "--config", str(path)]))
            self.assertEqual(result["reported_stages"], [])


class PersistentCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.registry = self.root / "registry"
        self.config = json.loads((SCRIPTS.parent / "assets/config.example.json").read_text())
        self.config["storage"]["state_dir"] = str(self.root / "state")
        self.profile = self.config["config_id"]
        self.input = self.root / "proposal.json"
        self.candidate = self.root / "candidate.json"
        self.candidate.write_bytes((SCRIPTS.parent / "assets/candidate.example.json").read_bytes())
        self.write_proposal()

    def write_proposal(self):
        self.input.write_text(json.dumps(self.config), encoding="utf-8")
        self.input.chmod(0o600)

    def call(self, *arguments):
        return cli.run(cli.make_parser().parse_args([*arguments, "--registry-dir", str(self.registry)]))

    def save(self, expect=0, apply=True):
        arguments = ["config", "save", "--profile", self.profile, "--input", str(self.input),
                     "--expect-revision", str(expect), "--make-default"]
        if apply:
            arguments.append("--apply")
        return self.call(*arguments)

    def acknowledge_both(self):
        current = self.call("config", "show", "--profile", self.profile)
        for target in ("iphone", "automation"):
            result = self.call("config", "acknowledge", "--profile", self.profile,
                               "--target", target, "--expect-fingerprint", current["fingerprint"],
                               "--binding-id", "synthetic-" + target,
                               "--evidence", "Synthetic fixture verification, no real device contacted", "--apply")
            self.assertTrue(result["ok"])

    def test_preview_no_write_and_new_process_finds_default(self):
        self.assertTrue(self.save(apply=False)["ok"])
        self.assertFalse(self.registry.exists())
        result = self.save()
        self.assertTrue(result["configuration_saved"])
        self.assertFalse(result["deployment"]["deployment_ready"])
        completed = subprocess.run([sys.executable, str(SCRIPTS / "morning-brief.py"), "config", "show",
                                    "--registry-dir", str(self.registry)], capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stdout)
        recovered = json.loads(completed.stdout)
        self.assertEqual(recovered["config"], self.config)
        self.assertTrue(Path(recovered["config_path"]).is_file())
        self.assertTrue(self.call("validate-config")["configuration_managed"])

    def test_pending_blocks_normal_publication_setup_test_is_explicit(self):
        self.save()
        with patch.object(cli, "invoke_publisher", return_value={"ok": True, "status": "local_verified"}) as native:
            result = self.call("publish", "--profile", self.profile, "--candidate", str(self.candidate), "--apply")
            self.assertEqual(result["error"], "configuration_not_deployed")
            self.assertFalse(result["notes_contacted"])
            native.assert_not_called()
            result = self.call("publish", "--profile", self.profile, "--candidate", str(self.candidate), "--apply", "--setup-test")
            self.assertTrue(result["setup_test"])
            self.assertFalse(result["deployment_ready"])
            native.assert_called_once()

    def test_preference_update_can_publish_without_phone_changes(self):
        self.save()
        self.acknowledge_both()
        phone_path = self.registry / "bindings" / self.profile / "iphone.json"
        phone_before = phone_path.read_bytes()
        handoff_before = self.call("config", "handoff")["targets"]["iphone"]["payload"]
        self.assertTrue(self.call("config", "status", "--require-ready")["ok"])
        old_render = self.call("render", "--candidate", str(self.candidate))
        with patch.object(cli, "invoke_publisher", return_value={"ok": True, "status": "local_verified"}) as native:
            result = self.call("publish", "--candidate", str(self.candidate), "--apply")
            self.assertTrue(result["deployment_ready"])
            self.assertFalse(result["setup_test"])
            native.assert_called_once()
        self.config["modules"]["updates"]["max_items"] += 1
        self.write_proposal()
        updated = self.save(expect=1)
        self.assertEqual(updated["config_revision"], 2)
        self.assertTrue(updated["deployment"]["deployment_ready"])
        self.assertTrue(self.call("config", "status", "--require-ready")["ok"])
        self.assertEqual(phone_before, phone_path.read_bytes())
        self.assertEqual(handoff_before, self.call("config", "handoff")["targets"]["iphone"]["payload"])
        candidate = json.loads(self.candidate.read_text())
        candidate["config_revision"] = 2
        self.candidate.write_text(json.dumps(candidate))
        new_render = self.call("render", "--candidate", str(self.candidate))
        self.assertNotEqual(old_render["package_path"], new_render["package_path"])
        self.assertEqual(json.loads(Path(old_render["package_path"]).read_text())["config_revision"], 1)
        with patch.object(cli, "invoke_publisher", return_value={"ok": True, "status": "local_verified"}) as native:
            result = self.call("publish", "--candidate", str(self.candidate), "--apply")
            self.assertTrue(result["deployment_ready"])
            native.assert_called_once()
            self.assertEqual(native.call_args.args[1]["package"]["config_revision"], 2)
        setup = self.call("setup-status")
        self.assertEqual(set(setup["inherited_stages"]), {"iphone_read", "iphone_alarm"})
        self.assertNotIn("iphone_read", setup["remaining"])
        self.assertFalse(setup["all_stages_reported"])

    def test_schedule_update_and_restore_require_only_mac_adaptation(self):
        self.save()
        self.acknowledge_both()
        phone_path = self.registry / "bindings" / self.profile / "iphone.json"
        phone_before = phone_path.read_bytes()
        self.config["schedule"]["generate_at"] = "06:06"
        self.write_proposal()
        result = self.save(expect=1)
        self.assertEqual(result["deployment"]["pending_targets"], ["automation"])
        current = self.call("config", "show")
        self.call("config", "acknowledge", "--target", "automation", "--expect-fingerprint", current["fingerprint"],
                  "--binding-id", "synthetic-automation", "--evidence", "Synthetic changed Mac schedule verified", "--apply")
        self.assertTrue(self.call("config", "status", "--require-ready")["ok"])
        restored = self.call("config", "restore", "--revision", "1", "--expect-revision", "2", "--apply")
        self.assertEqual(restored["deployment"]["pending_targets"], ["automation"])
        self.assertEqual(phone_before, phone_path.read_bytes())
        self.assertNotIn("iphone_alarm", self.call("setup-status")["remaining"])

    def test_new_preferences_and_restore_reach_fixed_receiver_via_real_offline_publisher(self):
        # Exercise production core/store/bindings/publisher/receiver together;
        # only Apple Events is replaced by the in-memory Notes transport.
        from test_notes_publisher import FakeBridge, notes
        from phone_protocol import select_note
        self.save()
        self.acknowledge_both()
        receiver = self.call("config", "handoff")["targets"]["iphone"]["payload"]
        phone_path = self.registry / "bindings" / self.profile / "iphone.json"
        phone_before = phone_path.read_bytes()
        transport = FakeBridge()
        def publish(command, request):
            self.assertEqual(command[-2:], ["publish", "--apply"])
            return notes.execute(request, operation="publish", apply=True, bridge=transport)
        with patch.object(cli, "invoke_publisher", side_effect=publish):
            for config_revision, report_revision in ((1, 99), (2, 1), (3, 1)):
                if config_revision == 2:
                    self.config["modules"]["updates"]["max_items"] += 1
                    self.write_proposal()
                    self.save(expect=1)
                elif config_revision == 3:
                    self.call("config", "restore", "--revision", "1", "--expect-revision", "2", "--apply")
                candidate = json.loads(self.candidate.read_text())
                candidate.update(config_revision=config_revision, revision=report_revision)
                self.candidate.write_text(json.dumps(candidate))
                result = self.call("publish", "--candidate", str(self.candidate), "--apply")
                self.assertTrue(result["ok"], result)
                self.assertTrue(result["deployment_ready"])
                received = select_note(receiver["config_id"], [note["body_text"] for note in transport.notes],
                                       "2026-09-02T06:15:00+08:00")
                self.assertEqual(received["status"], "READY", received)
                self.assertEqual(received["metadata"]["config_revision"], config_revision)
                self.assertEqual(received["metadata"]["revision"], report_revision)
                self.assertTrue(received["not_latest_verified"])
                self.assertEqual(receiver, self.call("config", "handoff")["targets"]["iphone"]["payload"])
                self.assertEqual(phone_before, phone_path.read_bytes())
        self.assertEqual(transport.create_count, 3)

    def test_unmanaged_config_cannot_bypass_publication_gate(self):
        with patch.object(cli, "invoke_publisher") as native, self.assertRaises(cli.ValidationError):
            self.call("publish", "--config", str(self.input), "--candidate", str(self.candidate), "--apply", "--setup-test")
        native.assert_not_called()
        self.assertFalse(self.registry.exists())

    def test_update_during_render_stops_stale_publication(self):
        self.save()
        self.acknowledge_both()
        original_build = cli.build_package
        def changed_config(*args, **kwargs):
            package = original_build(*args, **kwargs)
            changed = json.loads(json.dumps(self.config))
            changed["modules"]["updates"]["max_items"] += 1
            cli.ConfigStore(str(self.registry)).save(changed, expected_revision=1, profile=self.profile, apply=True)
            return package
        with patch.object(cli, "build_package", side_effect=changed_config), patch.object(cli, "invoke_publisher") as native:
            with self.assertRaises(cli.ValidationError):
                self.call("publish", "--candidate", str(self.candidate), "--apply")
            native.assert_not_called()

    def test_saved_config_remains_reported_when_binding_state_is_damaged(self):
        with patch.object(cli.config_bindings, "status", side_effect=cli.ValidationError("invalid binding state")):
            result = self.save()
        self.assertTrue(result["configuration_saved"])
        self.assertFalse(result["deployment"]["deployment_ready"])
        self.assertEqual(self.call("config", "show")["config"]["config_revision"], 1)

    def test_setup_test_cannot_bypass_corrupt_binding_state(self):
        self.save()
        with patch.object(cli.config_bindings, "status", return_value={"ok": False, "deployment_ready": False, "code": "INVALID_BINDING_RECORD"}), patch.object(cli, "invoke_publisher") as native:
            result = self.call("publish", "--candidate", str(self.candidate), "--apply", "--setup-test")
            self.assertEqual(result["error"], "binding_state_requires_inspection")
            self.assertFalse(result["notes_contacted"])
            native.assert_not_called()

    def test_save_result_does_not_mix_another_sessions_current_deployment(self):
        saved = self.save()
        self.config["modules"]["updates"]["max_items"] = 4
        self.write_proposal()
        self.save(expect=1)
        checked = cli.deployment_after_save(cli.ConfigStore(self.registry), saved)
        self.assertTrue(checked["configuration_saved"])
        self.assertFalse(checked["deployment_assessed"])
        self.assertFalse(checked["deployment"]["deployment_ready"])

    def test_restore_is_new_revision_and_preview_does_not_activate(self):
        self.save()
        self.config["modules"]["updates"]["max_items"] = 4
        self.write_proposal()
        self.save(expect=1)
        self.call("config", "restore", "--revision", "1", "--expect-revision", "2")
        self.assertEqual(self.call("config", "show")["config"]["config_revision"], 2)
        self.call("config", "restore", "--revision", "1", "--expect-revision", "2", "--apply")
        restored = self.call("config", "show")["config"]
        self.assertEqual(restored["config_revision"], 3)
        self.assertEqual(restored["modules"]["updates"]["max_items"], 3)


if __name__ == "__main__":
    unittest.main()
