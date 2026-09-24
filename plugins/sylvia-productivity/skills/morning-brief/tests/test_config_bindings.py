"""Synthetic private registries only; no real config, device or scheduler access."""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import config_bindings as bindings
from config_store import ConfigStore, fingerprint


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="morning-bindings-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "registry"
        self.store = ConfigStore(str(self.root))
        self.config = json.loads((SCRIPTS.parent / "assets" / "config.example.json").read_text())
        self.config["config_id"] = "fixture"
        self.config["storage"]["state_dir"] = str(self.base / "private-state")
        self.store.save(self.config, 0, profile="fixture", apply=True)
        self.resolved = self.store.resolve("fixture")

    def change(self, mutate):
        current = self.store.resolve("fixture")
        candidate = copy.deepcopy(current["config"])
        mutate(candidate)
        self.store.save(candidate, current["config"]["config_revision"], profile="fixture", apply=True)
        self.resolved = self.store.resolve("fixture")
        return self.resolved

    def ack(self, target, evidence="Operator observed synthetic expected behavior", binding_id=None, apply=True):
        return bindings.acknowledge(self.resolved, target, self.resolved["fingerprint"], binding_id or "synthetic-" + target,
                                    evidence, apply=apply)

    def ack_both(self):
        for target in bindings.TARGETS:
            result = self.ack(target)
            self.assertTrue(result["ok"], result)

    def target_file(self, target):
        return self.root / "bindings" / "fixture" / (target + ".json")

    def legacy_phone_record(self):
        """Synthesize exactly the pre-v2 signature and chained record format."""
        self.assertTrue(self.ack("iphone")["ok"])
        path = self.target_file("iphone")
        document = json.loads(path.read_text())
        record = document["records"][0]
        config = self.resolved["config"]
        record["binding_parameters"] = {
            "schema_version": 1, "target": "iphone", "profile": self.resolved["profile"],
            "registry_dir": self.resolved["registry_dir"], "profile_dir": self.resolved["profile_dir"],
            "config_fingerprint": self.resolved["fingerprint"],
            "parameters": {"protocol_version": 1, "config_id": config["config_id"],
                           "config_revision": config["config_revision"], "timezone": config["timezone"],
                           "storage": {"notes": copy.deepcopy(config["storage"]["notes"])},
                           "schedule": {key: copy.deepcopy(config["schedule"][key])
                                        for key in ("weekdays", "generate_at", "ready_by", "wake_at")}}}
        record["binding_signature"] = bindings._digest(record["binding_parameters"])
        record["record_sha256"] = bindings._digest({key: value for key, value in record.items() if key != "record_sha256"})
        path.write_bytes(bindings._encoded(document))
        return copy.deepcopy(record)

    def test_first_saved_configuration_leaves_both_targets_pending(self):
        result = bindings.status(self.resolved)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["configuration_saved"])
        self.assertFalse(result["deployment_ready"])
        self.assertEqual(result["pending_targets"], ["iphone", "automation"])
        self.assertFalse((self.root / "bindings").exists())

    def test_handoff_payload_minimizes_private_source_details_and_writes_nothing(self):
        result = bindings.handoff(self.resolved)
        self.assertTrue(result["ok"], result)
        iphone = result["targets"]["iphone"]["payload"]
        self.assertEqual(set(iphone), {"protocol_version", "config_id", "storage"})
        self.assertEqual(iphone["protocol_version"], 2)
        self.assertEqual(set(iphone["storage"]), {"notes"})
        for forbidden in ("config_revision", "timezone", "schedule", "fingerprint", "state_dir", "registry_dir", "profile_dir"):
            self.assertNotIn(forbidden, json.dumps(iphone))
        for forbidden in ("modules", "topics", "city", "Vault", "calendar_ids", "synthetic-observatory"):
            self.assertNotIn(forbidden, json.dumps(result))
        automation = result["targets"]["automation"]
        self.assertEqual(automation["payload"]["schedule"], self.resolved["config"]["schedule"])
        self.assertIn("$morning-brief", automation["prompt"])
        self.assertIn("--profile fixture", automation["prompt"])
        self.assertIn("--registry-dir", automation["prompt"])
        self.assertIn("--require-ready", automation["prompt"])
        self.assertIn("安装目录中的 scripts/morning-brief.py", automation["prompt"])
        self.assertIn("python3", automation["prompt"])
        self.assertNotIn(self.resolved["config_path"], automation["prompt"])
        self.assertNotIn("RRULE", json.dumps(result))
        self.assertFalse(result["external_actions_performed"])
        self.assertFalse((self.root / "bindings").exists())

    def test_ack_default_is_zero_write_dry_run(self):
        result = bindings.acknowledge(self.resolved, "iphone", self.resolved["fingerprint"], "synthetic-phone", "User reported matching parameters")
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["applied"])
        self.assertFalse(result["recorded"])
        self.assertEqual(result["targets"]["iphone"]["status"], "pending")
        self.assertFalse((self.root / "bindings").exists())

    def test_both_acknowledgements_are_required_and_are_operator_reports(self):
        first = self.ack("iphone")
        self.assertTrue(first["ok"], first)
        self.assertFalse(first["deployment_ready"])
        self.assertEqual(first["pending_targets"], ["automation"])
        second = self.ack("automation")
        self.assertTrue(second["deployment_ready"], second)
        for target in bindings.TARGETS:
            self.assertEqual(second["targets"][target]["kind"], "operator_report_not_automatic_proof")
            self.assertFalse(second["targets"][target]["live_state_checked"])
        self.assertNotIn("Operator observed", json.dumps(second))

    def test_content_only_revision_reuses_phone_channel_and_latest_automation_signature(self):
        self.ack_both()
        old = bindings.status(self.resolved)
        self.change(lambda value: value["modules"]["updates"]["scope"]["topics"][0].update(query="合成的新主题，不是用户偏好"))
        result = bindings.status(self.resolved)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["targets"]["iphone"]["status"], "verified")
        self.assertEqual(result["targets"]["automation"]["status"], "verified")
        self.assertEqual(result["targets"]["automation"]["signature"], old["targets"]["automation"]["signature"])
        self.assertEqual(result["targets"]["iphone"]["signature"], old["targets"]["iphone"]["signature"])
        self.assertEqual(result["targets"]["iphone"]["verification_basis"], "verified_delivery_channel")
        self.assertTrue(result["targets"]["iphone"]["reused_for_current_config"])
        self.assertEqual(result["targets"]["automation"]["verification_basis"], "same_binding_signature")
        self.assertTrue(result["targets"]["automation"]["reused_for_current_config"])

    def test_schedule_and_timezone_changes_require_only_new_automation_observation(self):
        changes = [lambda value: value["schedule"].update(generate_at="06:06"),
                   lambda value: value["schedule"].update(weekdays=[1, 2, 3, 4, 5]),
                   lambda value: value["schedule"].update(generation_buffer_minutes=11),
                   lambda value: value.update(timezone="Asia/Tokyo")]
        for change in changes:
            self.ack_both()
            self.change(change)
            result = bindings.status(self.resolved)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["pending_targets"], ["automation"])

    def test_A_B_restore_A_does_not_revive_historical_binding(self):
        self.ack_both()
        signature_a = bindings.status(self.resolved)["targets"]["automation"]["signature"]
        self.change(lambda value: value["schedule"].update(generate_at="06:06"))
        self.ack_both()
        signature_b = bindings.status(self.resolved)["targets"]["automation"]["signature"]
        self.assertNotEqual(signature_a, signature_b)
        self.store.restore(1, self.resolved["config"]["config_revision"], profile="fixture", apply=True)
        self.resolved = self.store.resolve("fixture")
        result = bindings.status(self.resolved)
        self.assertEqual(result["targets"]["automation"]["signature"], signature_a)
        self.assertEqual(result["targets"]["automation"]["last_observed_signature"], signature_b)
        self.assertEqual(result["pending_targets"], ["automation"])
        self.assertTrue(self.ack("automation")["recorded"])
        document = json.loads(self.target_file("automation").read_text())
        self.assertEqual(len(document["records"]), 3)

    def test_restore_A_without_observing_B_can_reuse_actual_latest_A(self):
        self.ack_both()
        self.change(lambda value: value["schedule"].update(generate_at="06:06"))
        self.store.restore(1, self.resolved["config"]["config_revision"], profile="fixture", apply=True)
        self.resolved = self.store.resolve("fixture")
        result = bindings.status(self.resolved)
        self.assertEqual(result["targets"]["automation"]["status"], "verified")
        self.assertEqual(result["targets"]["iphone"]["status"], "verified")

    def test_v2_phone_binding_survives_all_mutable_preference_categories_and_restore(self):
        self.ack_both()
        initial = bindings.handoff(self.resolved)["targets"]["iphone"]
        evidence = self.target_file("iphone").read_bytes()
        changes = [lambda value: value["modules"]["updates"]["scope"]["topics"][0].update(query="OTHER SYNTHETIC TOPIC"),
                   lambda value: value["modules"]["updates"].update(max_items=2),
                   lambda value: value["modules"]["calendar"].update(enabled=True, scope={"calendar_ids": ["synthetic-calendar-only"]}),
                   lambda value: value["modules"]["calendar"].update(enabled=False),
                   lambda value: value["windows"]["lookback"]["start"].update(time="20:00"),
                   lambda value: value["windows"]["lookahead"]["end"].update(time="20:00"),
                   lambda value: value["schedule"].update(generate_at="06:06"),
                   lambda value: value.update(timezone="Asia/Tokyo"),
                   lambda value: value["storage"].update(retention_days=8)]
        for change in changes:
            previous = self.resolved["fingerprint"]
            self.change(change)
            self.assertNotEqual(self.resolved["fingerprint"], previous)
            phone = bindings.handoff(self.resolved)["targets"]["iphone"]
            self.assertEqual(phone["status"], "verified")
            self.assertEqual(phone["signature"], initial["signature"])
            self.assertEqual(phone["payload"], initial["payload"])
            self.assertEqual(phone["verification_basis"], "verified_delivery_channel")
            self.assertEqual(self.target_file("iphone").read_bytes(), evidence)
        self.store.restore(1, self.resolved["config"]["config_revision"], profile="fixture", apply=True)
        self.resolved = self.store.resolve("fixture")
        phone = bindings.handoff(self.resolved)["targets"]["iphone"]
        self.assertEqual(phone["status"], "verified")
        self.assertEqual(phone["signature"], initial["signature"])
        self.assertEqual(self.target_file("iphone").read_bytes(), evidence)

    def test_legacy_phone_binding_requires_one_explicit_protocol_upgrade_then_stays_valid(self):
        legacy = self.legacy_phone_record()
        self.ack("automation")
        before = self.target_file("iphone").read_bytes()
        for reader in (bindings.status, bindings.handoff):
            result = reader(self.resolved)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["pending_targets"], ["iphone"])
            self.assertEqual(result["targets"]["iphone"]["pending_reason"], "phone_protocol_upgrade_required")
            self.assertEqual(result["targets"]["iphone"]["legacy_protocol_version"], 1)
            self.assertEqual(self.target_file("iphone").read_bytes(), before)
        self.assertFalse(self.ack("iphone", apply=False)["recorded"])
        self.assertEqual(self.target_file("iphone").read_bytes(), before)
        result = self.ack("iphone", evidence="Operator verified v2 delivery channel")
        self.assertTrue(result["deployment_ready"], result)
        records = json.loads(self.target_file("iphone").read_text())["records"]
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0], legacy)
        self.assertEqual(records[1]["previous_record_sha256"], legacy["record_sha256"])
        self.assertEqual(records[1]["binding_parameters"]["schema_version"], 2)
        self.change(lambda value: value["modules"]["updates"].update(max_items=2))
        self.assertTrue(bindings.status(self.resolved)["deployment_ready"])
        self.assertEqual(len(json.loads(self.target_file("iphone").read_text())["records"]), 2)

    def test_legacy_phone_record_tampering_is_not_dismissed_as_upgrade_pending(self):
        self.legacy_phone_record()
        path = self.target_file("iphone")
        pristine = path.read_bytes()
        document = json.loads(pristine)
        document["records"][0]["evidence"] = "ALTERED SYNTHETIC EVIDENCE"
        path.write_bytes(bindings._encoded(document))
        self.assertEqual(bindings.status(self.resolved)["code"], "BINDING_INTEGRITY_MISMATCH")
        document = json.loads(pristine)
        record = document["records"][0]
        record["binding_parameters"]["config_fingerprint"] = "f" * 64
        record["binding_signature"] = bindings._digest(record["binding_parameters"])
        record["record_sha256"] = bindings._digest({key: value for key, value in record.items() if key != "record_sha256"})
        path.write_bytes(bindings._encoded(document))
        self.assertEqual(bindings.status(self.resolved)["code"], "INVALID_BINDING_RECORD")

    def test_v2_phone_record_cannot_smuggle_preferences_into_channel_signature(self):
        self.ack("iphone")
        path = self.target_file("iphone")
        pristine = path.read_bytes()
        for extra in ("config_revision", "timezone", "schedule", "config_fingerprint", "registry_dir", "profile_dir"):
            document = json.loads(pristine)
            record = document["records"][0]
            record["binding_parameters"]["parameters"][extra] = "UNEXPECTED SYNTHETIC VALUE"
            record["binding_signature"] = bindings._digest(record["binding_parameters"])
            record["record_sha256"] = bindings._digest({key: value for key, value in record.items() if key != "record_sha256"})
            path.write_bytes(bindings._encoded(document))
            self.assertEqual(bindings.status(self.resolved)["code"], "INVALID_BINDING_RECORD")

    def test_stale_expected_fingerprint_is_rejected_without_write(self):
        result = bindings.acknowledge(self.resolved, "iphone", "0" * 64, "synthetic", "Observed", apply=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "STALE_ACKNOWLEDGEMENT")
        self.assertFalse((self.root / "bindings").exists())

    def test_old_immutable_snapshot_does_not_prove_current_configuration(self):
        old = copy.deepcopy(self.resolved)
        self.change(lambda value: value["modules"]["updates"].update(max_items=2))
        self.assertTrue(Path(old["config_path"]).exists())
        result = bindings.acknowledge(old, "iphone", old["fingerprint"], "synthetic", "Observed old config", apply=True)
        self.assertEqual(result["code"], "STALE_RESOLVED_CONFIG")
        self.assertFalse((self.root / "bindings").exists())

    def test_evidence_appends_and_only_latest_identical_ack_is_idempotent(self):
        self.assertTrue(self.ack("automation", evidence="First observed run")["recorded"])
        initial = json.loads(self.target_file("automation").read_text())["records"][0]
        self.assertFalse(self.ack("automation", evidence="First observed run")["recorded"])
        self.assertTrue(self.ack("automation", evidence="Second observed run")["recorded"])
        document = json.loads(self.target_file("automation").read_text())
        self.assertEqual(document["records"][0], initial)
        self.assertEqual(len(document["records"]), 2)
        self.assertTrue(self.ack("automation", evidence="First observed run")["recorded"])
        self.assertEqual(len(json.loads(self.target_file("automation").read_text())["records"]), 3)

    def test_cross_session_reads_only_persisted_binding_evidence(self):
        self.ack_both()
        program = "import json,sys; sys.path.insert(0,sys.argv[1]); from config_store import ConfigStore; from config_bindings import status; print(json.dumps(status(ConfigStore(sys.argv[2]).resolve('fixture'))))"
        completed = subprocess.run([sys.executable, "-c", program, str(SCRIPTS), str(self.root)], text=True, capture_output=True, check=False, timeout=10)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, "")
        result = json.loads(completed.stdout)
        self.assertTrue(result["deployment_ready"], result)
        self.assertFalse(result["live_state_checked"])

    def test_ack_can_run_inside_store_CAS_lock_without_recursive_locking(self):
        with self.store.locked_resolve("fixture", expected_fingerprint=self.resolved["fingerprint"]) as current:
            result = bindings.acknowledge(current, "iphone", current["fingerprint"], "synthetic-phone", "User reported parameter and behavior verification", apply=True)
        self.assertTrue(result["ok"], result)

    def test_binding_writer_lock_is_nonblocking(self):
        with bindings._binding_lock(bindings._context(self.resolved)):
            result = self.ack("iphone")
        self.assertEqual(result["code"], "BINDING_WRITER_BUSY")
        self.assertFalse(self.target_file("iphone").exists())

    def test_new_profile_does_not_inherit_other_profile_bindings(self):
        self.ack_both()
        alternate = copy.deepcopy(self.config)
        alternate["config_id"] = "other"
        alternate["storage"]["state_dir"] = str(self.base / "another-state")
        self.store.save(alternate, 0, profile="other", apply=True)
        result = bindings.status(self.store.resolve("other"))
        self.assertEqual(result["pending_targets"], ["iphone", "automation"])

    def test_registry_and_profile_locations_are_binding_signature_boundaries(self):
        self.ack_both()
        old = bindings.status(self.resolved)
        old_signature = old["targets"]["automation"]["signature"]
        alternate_store = ConfigStore(str(self.base / "registry-two"))
        alternate_store.save(self.config, 0, profile="fixture", profile_dir=str(self.base / "custom-private-profile"), apply=True)
        current = alternate_store.resolve("fixture")
        result = bindings.status(current)
        self.assertEqual(result["pending_targets"], ["iphone", "automation"])
        self.assertNotEqual(result["targets"]["automation"]["signature"], old_signature)
        self.assertEqual(result["targets"]["iphone"]["signature"], old["targets"]["iphone"]["signature"])
        location_only = copy.deepcopy(self.resolved)
        location_only["profile_dir"] = str(self.base / "other-location")
        self.assertNotEqual(bindings._digest(bindings._signature_input(location_only, "automation")), old_signature)
        location_only["fingerprint"] = "f" * 64
        location_only["config_path"] = str(self.base / "other-snapshot.json")
        location_only["config"]["config_revision"] = 17
        self.assertEqual(bindings._digest(bindings._signature_input(location_only, "iphone")), old["targets"]["iphone"]["signature"])

    def test_state_directory_is_in_automation_signature(self):
        original = bindings._signature_input(self.resolved, "automation")
        moved = copy.deepcopy(self.resolved)
        moved["config"]["storage"]["state_dir"] = str(self.base / "moved-state")
        self.assertNotEqual(bindings._digest(original), bindings._digest(bindings._signature_input(moved, "automation")))

    def test_permissions_are_private_for_all_generated_bindings(self):
        self.ack_both()
        for directory in (self.root / "bindings", self.root / "bindings" / "fixture"):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        for path in (self.target_file("iphone"), self.target_file("automation"), self.root / "bindings" / ".bindings.lock"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_evidence_tampering_and_wrong_target_fail_closed(self):
        self.ack("iphone")
        path = self.target_file("iphone")
        pristine = path.read_bytes()
        document = json.loads(pristine)
        document["records"][0]["evidence"] = "UNAPPROVED altered evidence"
        path.write_text(json.dumps(document))
        result = bindings.status(self.resolved)
        self.assertEqual(result["code"], "BINDING_INTEGRITY_MISMATCH")
        self.assertFalse(result["deployment_ready"])
        document = json.loads(pristine)
        document["target"] = "automation"
        path.write_text(json.dumps(document))
        self.assertEqual(bindings.status(self.resolved)["code"], "INVALID_BINDING_RECORD")

    def test_wrong_profile_or_signature_cannot_be_treated_as_verified(self):
        self.ack("automation")
        path = self.target_file("automation")
        pristine = path.read_bytes()
        for mutate in (lambda doc: doc.update(profile="other"),
                       lambda doc: doc["records"][0].update(binding_signature="f" * 64),
                       lambda doc: doc["records"][0].update(kind="automatic_proof")):
            document = json.loads(pristine)
            mutate(document)
            path.write_text(json.dumps(document))
            self.assertFalse(bindings.status(self.resolved)["ok"])

    def test_deleted_or_reordered_old_record_breaks_hash_chain(self):
        self.ack("automation", evidence="First run")
        self.ack("automation", evidence="Second run")
        path = self.target_file("automation")
        document = json.loads(path.read_text())
        document["records"].pop(0)
        path.write_text(json.dumps(document))
        self.assertEqual(bindings.status(self.resolved)["code"], "INVALID_BINDING_RECORD")

    def test_strict_json_rejects_duplicate_keys_nonfinite_and_corruption(self):
        self.ack("iphone")
        for content in ('{"schema_version":1,"schema_version":1}', '{"value":NaN}', '{"value":1e999}', "broken JSON"):
            self.target_file("iphone").write_text(content)
            result = bindings.status(self.resolved)
            self.assertEqual(result["code"], "INVALID_BINDING_JSON")
            self.assertNotIn(content, json.dumps(result))

    def test_symlink_and_insecure_binding_file_fail_closed(self):
        self.ack("iphone")
        path = self.target_file("iphone")
        path.chmod(0o644)
        self.assertEqual(bindings.status(self.resolved)["code"], "FILE_NOT_PRIVATE")
        path.chmod(0o600)
        moved = path.with_name("moved.json")
        path.rename(moved)
        path.symlink_to(moved)
        self.assertEqual(bindings.status(self.resolved)["code"], "SYMLINK_NOT_ALLOWED")

    def test_symlink_binding_directory_and_git_ancestor_are_rejected(self):
        alternate = self.base / "outside"
        alternate.mkdir(mode=0o700)
        (self.root / "bindings").symlink_to(alternate, target_is_directory=True)
        self.assertEqual(bindings.status(self.resolved)["code"], "SYMLINK_NOT_ALLOWED")
        (self.root / "bindings").unlink()
        (self.root / ".git").mkdir()
        self.assertEqual(bindings.status(self.resolved)["code"], "BINDINGS_OUTSIDE_REPOSITORY_REQUIRED")

    def test_arbitrary_setup_state_is_not_binding_evidence(self):
        state = Path(self.config["storage"]["state_dir"])
        state.mkdir(mode=0o700)
        setup = state / "setup"
        setup.mkdir(mode=0o700)
        (setup / "verified.json").write_text('{"iphone":"verified","automation":"verified"}')
        result = bindings.status(self.resolved)
        self.assertEqual(result["pending_targets"], ["iphone", "automation"])

    def test_invalid_target_evidence_and_apply_flag_do_not_write(self):
        for target, evidence, apply in (("manual", "Observed", True), ("iphone", "", True), ("iphone", "private\nbody", True), ("iphone", "Observed", 1)):
            result = bindings.acknowledge(self.resolved, target, self.resolved["fingerprint"], "synthetic", evidence, apply=apply)
            self.assertFalse(result["ok"])
        self.assertFalse((self.root / "bindings").exists())

    def test_fingerprint_matches_store_and_handoff_does_not_mutate_resolved(self):
        before = copy.deepcopy(self.resolved)
        self.assertEqual(bindings._digest(self.resolved["config"]), fingerprint(self.resolved["config"]))
        result = bindings.handoff(self.resolved)
        result["targets"]["iphone"]["payload"]["storage"]["notes"]["folder"] = "OTHER SYNTHETIC FOLDER"
        result["targets"]["automation"]["payload"]["schedule"]["weekdays"].clear()
        self.assertEqual(self.resolved, before)


if __name__ == "__main__":
    unittest.main()
