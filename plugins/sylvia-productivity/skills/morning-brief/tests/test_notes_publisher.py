"""Offline behavioral tests; no real Apple Notes account is contacted."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "notes_publisher.py"
SPEC = importlib.util.spec_from_file_location("notes_publisher", SCRIPT)
notes = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(notes)


def package(revision=1, readiness="READY", extra="合成内容，不是真实私人数据。\nhttps://example.org/source?a=1&b=2",
            config_revision=1, protocol_version=2):
    result = {
        "schema_version": 1, "config_id": "fixture", "config_revision": config_revision,
        "brief_id": "mb-fixture12345678", "revision": revision,
        "applicable_date": "2026-09-02", "timezone": "Asia/Shanghai",
        "generated_at": "2026-09-02T06:15:00+08:00", "readiness": readiness,
        "fresh_until": "2026-09-02T10:00:00+08:00",
    }
    result["title"] = "合成晨间简报 · 2026-09-02 · r%02d · mb-fixture12345678" % revision
    markers = [
        "MB:SCHEMA=" + str(protocol_version), "MB:CONFIG=fixture",
        "MB:CONFIG-REVISION=" + str(config_revision), "MB:DATE=2026-09-02",
        "MB:TIMEZONE=Asia/Shanghai", "MB:BRIEF=mb-fixture12345678", "MB:REVISION=" + str(revision),
        "MB:STATUS=" + readiness, "MB:GENERATED=" + result["generated_at"],
        "MB:FRESH-UNTIL=" + result["fresh_until"],
    ]
    if protocol_version == 2:
        result.update(protocol_version=2, valid_from="2026-09-02T00:00:00+08:00", valid_until="2026-09-03T00:00:00+08:00")
        result["title"] = f"晨间简报 · 2026-09-02 · mb-fixture12345678 · c{config_revision:02d} · r{revision:02d}"
        markers.extend(["MB:VALID-FROM=" + result["valid_from"], "MB:VALID-UNTIL=" + result["valid_until"]])
    result["content_text"] = "\n".join([extra, "", *markers])
    return rehash(result)


def rehash(result):
    result["content_sha256"] = notes.digest(result["content_text"])
    result["body_text"] = (result["title"] + "\nMB:BEGIN\nMB:CONTENT-BEGIN\n" + result["content_text"] +
                           "\nMB:CONTENT-END\nMB:CONTENT-SHA256=" + result["content_sha256"] + "\nMB:END")
    result["body_sha256"] = notes.digest(result["body_text"])
    return result


def note_for(candidate):
    return {"note_id": "fixture-note-id", "title": candidate["title"],
            "body_text": candidate["body_text"] + "\n", "body_html": notes.text_to_html(candidate["body_text"]),
            "shared": False, "password_protected": False, "truncated": False}


class FakeBridge:
    def __init__(self, existing=None):
        self.notes = copy.deepcopy(existing or [])
        self.calls = []
        self.create_count = 0
        self.fail_lookup = False
        self.fail_create = False
        self.commit_before_failure = False
        self.fail_after_create = False
        self.create_transform = None
        self.complete = True

    def call(self, action, request):
        self.calls.append(action)
        if action == "lookup":
            if self.fail_lookup or self.fail_after_create and self.create_count:
                raise notes.NativeError("NATIVE_TIMEOUT")
        else:
            self.create_count += 1
            if not self.fail_create or self.commit_before_failure:
                created = note_for(request["package"])
                if self.create_transform:
                    self.create_transform(created)
                self.notes.append(created)
            if self.fail_create:
                raise notes.NativeError("NATIVE_TIMEOUT", uncertain=True)
        return {"ok": True, "complete": self.complete, "truncated": not self.complete,
                "notes": copy.deepcopy([item for item in self.notes
                                        if item.get("title") == request["package"]["title"] or item.get("conflict")]),
                "created": action == "create"}


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="morning-notes-test-")
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "private-state"
        self.request = {"authorized": True, "account": "Fixture Account", "folder": "Fixture Folder",
                        "state_dir": str(self.state), "package": package()}
        self.bridge = FakeBridge()

    def execute(self, operation="publish", apply=True):
        return notes.execute(self.request, operation, apply, self.bridge)

    def test_dry_run_has_no_native_contact_or_local_write(self):
        result = self.execute(apply=False)
        self.assertEqual(result["status"], "dry_run")
        self.assertTrue(result["ok"])
        self.assertEqual(self.bridge.calls, [])
        self.assertFalse(self.state.exists())
        self.assertNotIn("body_text", result)
        self.assertEqual(result["iphone_sync"], "unverified")

    def test_explicit_authorization_required_even_for_verify(self):
        self.request["authorized"] = False
        result = self.execute("verify", False)
        self.assertEqual(result["code"], "AUTHORIZATION_REQUIRED")
        self.assertEqual(self.bridge.calls, [])
        self.assertFalse(self.state.exists())

    def test_create_then_retry_is_idempotent(self):
        first = self.execute()
        second = self.execute()
        self.assertEqual(first["status"], "local_verified")
        self.assertEqual(first["action"], "created")
        self.assertEqual(second["action"], "existing")
        self.assertEqual(self.bridge.create_count, 1)
        self.assertEqual(self.bridge.calls, ["lookup", "create", "lookup", "lookup"])
        self.assertEqual(first["iphone_sync"], "unverified")

    def test_partial_package_does_not_become_ready(self):
        self.request["package"] = package(readiness="PARTIAL", extra="日历不可用；其他启用模块可用。")
        result = self.execute()
        self.assertEqual(result["status"], "local_verified")
        self.assertEqual(result["readiness"], "PARTIAL")

    def test_existing_user_annotations_are_never_overwritten(self):
        existing = note_for(self.request["package"])
        existing["body_text"] += "用户自己的补充\n"
        existing["body_html"] += "<div>用户自己的补充</div>"
        self.bridge.notes = [existing]
        result = self.execute()
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["code"], "VISIBLE_BODY_MISMATCH")
        self.assertEqual(self.bridge.create_count, 0)
        self.assertEqual(self.bridge.notes, [existing])
        self.assertNotIn("用户自己的补充", json.dumps(result, ensure_ascii=False))

    def test_duplicate_exact_title_is_conflict(self):
        self.bridge.notes = [{"conflict": True}, {"conflict": True}]
        result = self.execute()
        self.assertEqual(result["code"], "DUPLICATE_VERSION")
        self.assertEqual(self.bridge.create_count, 0)

    def test_duplicate_version_persists_conflict_and_blocks_new_revision(self):
        self.bridge.notes = [{"conflict": True}, {"conflict": True}]
        self.assertEqual(self.execute()["code"], "DUPLICATE_VERSION")
        self.bridge.notes = []
        self.request["package"] = package(revision=2)
        before = len(self.bridge.calls)
        self.assertEqual(self.execute()["code"], "UNRESOLVED_PREVIOUS_REVISION")
        self.assertEqual(len(self.bridge.calls), before)
        self.assertEqual(self.bridge.create_count, 0)

    def test_duplicate_after_verified_version_also_updates_conflict(self):
        self.assertEqual(self.execute()["status"], "local_verified")
        self.bridge.notes = [{"conflict": True}, {"conflict": True}]
        self.assertEqual(self.execute("verify", False)["code"], "DUPLICATE_VERSION")
        self.request["package"] = package(revision=2)
        self.assertEqual(self.execute()["code"], "UNRESOLVED_PREVIOUS_REVISION")
        self.assertEqual(self.bridge.create_count, 1)

    def test_not_found_verify_never_creates(self):
        result = self.execute("verify", False)
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(self.bridge.calls, ["lookup"])

    def test_verify_rejects_apply(self):
        result = self.execute("verify", True)
        self.assertEqual(result["code"], "VERIFY_IS_READ_ONLY")
        self.assertEqual(self.bridge.calls, [])

    def test_read_error_is_not_zero_matches(self):
        self.bridge.fail_lookup = True
        result = self.execute()
        self.assertEqual(result["status"], "read_error")
        self.assertEqual(self.bridge.create_count, 0)

    def test_incomplete_lookup_cannot_create(self):
        self.bridge.complete = False
        result = self.execute()
        self.assertEqual(result["code"], "INCOMPLETE_NATIVE_LOOKUP")
        self.assertEqual(result["status"], "read_error")
        self.assertEqual(self.bridge.create_count, 0)

    def test_truncated_readback_is_not_verified(self):
        self.bridge.create_transform = lambda item: item.update(truncated=True)
        result = self.execute()
        self.assertEqual(result["code"], "TRUNCATED_READBACK")
        self.assertFalse(result["local_verified"])
        self.assertEqual(self.bridge.create_count, 1)
        self.assertEqual(self.execute()["status"], "conflict")
        self.assertEqual(self.bridge.create_count, 1)

    def test_partial_visible_body_and_full_hidden_payload_fail(self):
        full = note_for(self.request["package"])
        full["body_html"] = "<div>truncated visible</div><!--" + self.request["package"]["body_text"] + "-->"
        self.bridge.notes = [full]
        self.assertEqual(self.execute()["status"], "conflict")
        self.assertEqual(self.bridge.create_count, 0)

    def test_unknown_commit_found_on_retry_does_not_duplicate(self):
        self.bridge.fail_create = True
        self.bridge.commit_before_failure = True
        self.assertEqual(self.execute()["status"], "uncertain")
        self.bridge.fail_create = False
        result = self.execute()
        self.assertEqual(result["status"], "local_verified")
        self.assertEqual(result["action"], "existing")
        self.assertEqual(self.bridge.create_count, 1)

    def test_unknown_commit_not_yet_visible_never_recreates(self):
        self.bridge.fail_create = True
        self.assertEqual(self.execute()["status"], "uncertain")
        self.bridge.fail_create = False
        self.assertEqual(self.execute()["status"], "uncertain")
        self.assertEqual(self.execute("verify", False)["status"], "uncertain")
        self.assertEqual(self.bridge.create_count, 1)

    def test_unresolved_commit_blocks_new_revision(self):
        self.bridge.fail_create = True
        self.assertEqual(self.execute()["status"], "uncertain")
        self.request["package"] = package(revision=2)
        result = self.execute()
        self.assertEqual(result["code"], "UNRESOLVED_PREVIOUS_REVISION")
        self.assertEqual(self.bridge.create_count, 1)

    def test_unresolved_new_revision_does_not_block_readonly_old_verification(self):
        first = self.request["package"]
        self.assertEqual(self.execute()["status"], "local_verified")
        self.bridge.notes = []
        self.request["package"] = package(revision=2)
        self.bridge.fail_create = True
        self.assertEqual(self.execute()["status"], "uncertain")
        self.bridge.notes = [note_for(first)]
        self.request["package"] = first
        self.assertEqual(self.execute("verify", False)["status"], "local_verified")
        self.assertEqual(self.bridge.create_count, 2)

    def test_unknown_commit_then_incomplete_conflict_still_blocks_new_revision(self):
        self.bridge.fail_create = True
        self.bridge.commit_before_failure = True
        self.bridge.create_transform = lambda item: item.update(body_text="Incomplete visible note")
        self.assertEqual(self.execute()["status"], "uncertain")
        self.bridge.fail_create = False
        self.assertEqual(self.execute("verify", False)["status"], "conflict")
        self.request["package"] = package(revision=2)
        before = len(self.bridge.calls)
        result = self.execute()
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["code"], "UNRESOLVED_PREVIOUS_REVISION")
        self.assertEqual(len(self.bridge.calls), before)
        self.assertEqual(self.bridge.create_count, 1)

    def test_existing_content_conflict_blocks_new_revision_until_resolved(self):
        existing = note_for(self.request["package"])
        existing["body_text"] += "Private annotation"
        self.bridge.notes = [existing]
        self.assertEqual(self.execute()["status"], "conflict")
        self.request["package"] = package(revision=2)
        self.assertEqual(self.execute()["code"], "UNRESOLVED_PREVIOUS_REVISION")
        self.assertEqual(self.bridge.create_count, 0)

    def test_error_after_create_stays_uncertain_until_readback(self):
        self.bridge.fail_after_create = True
        self.assertEqual(self.execute()["status"], "uncertain")
        self.bridge.fail_after_create = False
        self.assertEqual(self.execute("verify", False)["status"], "local_verified")
        self.assertEqual(self.bridge.create_count, 1)

    def test_verified_version_disappearance_does_not_recreate(self):
        self.assertEqual(self.execute()["status"], "local_verified")
        self.bridge.notes = []
        result = self.execute()
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(self.bridge.create_count, 1)

    def test_same_revision_changed_body_is_rejected_before_native_contact(self):
        self.execute()
        count = len(self.bridge.calls)
        self.request["package"] = package(extra="同版本不应改写")
        self.assertEqual(self.execute()["code"], "IMMUTABLE_REVISION_MISMATCH")
        self.assertEqual(len(self.bridge.calls), count)

    def test_config_revision_and_report_revision_form_independent_identity(self):
        first = self.request["package"]
        self.assertEqual(self.execute()["status"], "local_verified")
        self.request["package"] = package(revision=2)
        self.assertEqual(self.execute()["status"], "local_verified")
        self.request["package"] = package(config_revision=2)
        result = self.execute()
        self.assertEqual(result["status"], "local_verified")
        self.assertEqual((result["config_revision"], result["revision"], result["protocol_version"]), (2, 1, 2))
        self.assertEqual(self.bridge.create_count, 3)
        self.assertEqual(self.bridge.notes[0], note_for(first))
        journal = json.loads(next(self.state.glob("notes-*.json")).read_text())
        self.assertEqual(journal["schema_version"], 2)
        self.assertEqual(set(journal["records"]), {"c1.r1", "c1.r2", "c2.r1"})
        self.request["package"] = first
        self.assertEqual(self.execute("verify", False)["status"], "local_verified")
        self.assertEqual(self.bridge.create_count, 3)

    def test_unresolved_write_and_conflict_block_new_config_revision(self):
        self.bridge.fail_create = True
        self.bridge.commit_before_failure = True
        self.assertEqual(self.execute()["status"], "uncertain")
        self.request["package"] = package(config_revision=2)
        before = len(self.bridge.calls)
        self.assertEqual(self.execute()["code"], "UNRESOLVED_PREVIOUS_REVISION")
        self.assertEqual(len(self.bridge.calls), before)
        self.request["package"] = package()
        self.bridge.notes[0]["body_text"] += "私人注释"
        self.assertEqual(self.execute("verify", False)["status"], "conflict")
        self.request["package"] = package(config_revision=2)
        self.assertEqual(self.execute()["code"], "UNRESOLVED_PREVIOUS_REVISION")
        self.assertEqual(self.bridge.create_count, 1)

    def test_immutable_mismatch_remains_blocking_until_original_readback(self):
        first = self.request["package"]
        self.assertEqual(self.execute()["status"], "local_verified")
        self.request["package"] = package(extra="同身份异文")
        self.assertEqual(self.execute()["code"], "IMMUTABLE_REVISION_MISMATCH")
        self.request["package"] = package(config_revision=2)
        self.assertEqual(self.execute()["code"], "UNRESOLVED_PREVIOUS_REVISION")
        self.request["package"] = first
        self.assertEqual(self.execute("verify", False)["status"], "local_verified")
        self.request["package"] = package(config_revision=2)
        self.assertEqual(self.execute()["status"], "local_verified")
        self.assertEqual(self.bridge.create_count, 2)

    def write_legacy_journal(self, old, state="uncertain"):
        with notes.state_lock(str(self.state)) as directory:
            journal = notes.Journal(directory, self.request)
            record = {"state": state, "body_sha256": old["body_sha256"], "title_sha256": notes.digest(old["title"])}
            if state == "verified":
                record["note_id"] = "fixture-note-id"
            journal.path.write_text(json.dumps({"schema_version": 1, "records": {str(old["revision"]): record}}))
            journal.path.chmod(0o600)
            return journal.path

    def test_legacy_package_verify_only_and_explicit_result_protocol(self):
        self.request["package"] = package(protocol_version=1)
        for apply in (False, True):
            self.assertEqual(self.execute(apply=apply)["code"], "LEGACY_PROTOCOL_VERIFY_ONLY")
        self.assertEqual(self.bridge.calls, [])
        self.assertFalse(self.state.exists())
        self.bridge.notes = [note_for(self.request["package"])]
        result = self.execute("verify", False)
        self.assertEqual(result["status"], "local_verified")
        self.assertEqual(result["protocol_version"], 1)
        self.assertTrue(result["legacy_verification_only"])
        self.assertEqual(self.bridge.create_count, 0)
        journal = json.loads(next(self.state.glob("notes-*.json")).read_text())
        self.assertEqual(set(journal["records"]), {"p1.c1.r1"})

    def test_legacy_unknown_journal_requires_original_package_before_v2_publish(self):
        old = package(protocol_version=1, config_revision=3)
        path = self.write_legacy_journal(old)
        self.request["package"] = package(config_revision=4)
        self.assertEqual(self.execute()["code"], "UNRESOLVED_PREVIOUS_REVISION")
        self.assertEqual(self.bridge.calls, [])
        self.assertEqual(json.loads(path.read_text())["schema_version"], 1)
        self.request["package"] = old
        self.assertEqual(self.execute("verify", False)["status"], "uncertain")
        self.assertEqual(self.bridge.create_count, 0)
        self.bridge.notes = [note_for(old)]
        self.assertEqual(self.execute("verify", False)["status"], "local_verified")
        self.request["package"] = package(config_revision=4)
        self.assertEqual(self.execute()["status"], "local_verified")
        journal = json.loads(path.read_text())
        self.assertEqual(journal["schema_version"], 2)
        self.assertEqual(set(journal["records"]), {"legacy.r1", "c4.r1"})
        self.assertEqual(journal["records"]["legacy.r1"]["body_sha256"], old["body_sha256"])
        self.assertEqual(self.bridge.create_count, 1)

    def test_verified_legacy_record_does_not_collide_with_same_v2_config_report(self):
        old = package(protocol_version=1)
        path = self.write_legacy_journal(old, "verified")
        self.bridge.notes = [note_for(old)]
        self.assertEqual(self.execute()["status"], "local_verified")
        journal = json.loads(path.read_text())
        self.assertEqual(set(journal["records"]), {"legacy.r1", "c1.r1"})
        self.assertEqual(self.bridge.notes[0], note_for(old))
        self.assertEqual(self.bridge.create_count, 1)

    def test_legacy_journal_config_ambiguity_never_guessed(self):
        old = package(protocol_version=1, config_revision=3)
        path = self.write_legacy_journal(old, "verified")
        self.request["package"] = package(protocol_version=1, config_revision=4)
        self.assertEqual(self.execute("verify", False)["code"], "IMMUTABLE_REVISION_MISMATCH")
        self.assertEqual(self.bridge.calls, [])
        journal = json.loads(path.read_text())
        self.assertEqual(journal["records"]["legacy.r1"]["body_sha256"], old["body_sha256"])
        self.assertEqual(journal["records"]["legacy.r1"]["state"], "conflict")
        self.request["package"] = package(config_revision=5)
        self.assertEqual(self.execute()["code"], "UNRESOLVED_PREVIOUS_REVISION")

    def test_lock_prevents_concurrent_writer(self):
        with notes.state_lock(str(self.state)):
            self.assertEqual(self.execute()["code"], "PUBLISHER_BUSY")
        self.assertEqual(self.bridge.calls, [])

    def test_private_state_only_contains_hashes_and_locator(self):
        self.execute()
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        journal = list(self.state.glob("notes-*.json"))[0]
        data = journal.read_text()
        self.assertEqual(journal.stat().st_mode & 0o777, 0o600)
        for secret in (self.request["package"]["body_text"], "Fixture Account", "Fixture Folder", "合成内容"):
            self.assertNotIn(secret, data)

    def test_insecure_state_directory_is_rejected(self):
        self.state.mkdir(mode=0o755)
        self.assertEqual(self.execute()["code"], "STATE_DIR_NOT_PRIVATE")
        self.assertEqual(self.bridge.calls, [])

    def test_symlink_state_directory_is_rejected(self):
        target = Path(self.temp.name) / "another-dir"
        target.mkdir(mode=0o700)
        self.state.symlink_to(target, target_is_directory=True)
        self.assertEqual(self.execute()["code"], "STATE_DIR_NOT_PRIVATE")
        self.assertEqual(self.bridge.calls, [])

    def test_corrupt_state_fails_closed(self):
        self.execute()
        journal = list(self.state.glob("notes-*.json"))[0]
        journal.write_text('{"records": "private content"}')
        before = len(self.bridge.calls)
        self.assertEqual(self.execute()["code"], "INVALID_STATE_JOURNAL")
        self.assertEqual(len(self.bridge.calls), before)

    def test_journal_duplicate_keys_and_malformed_records_fail_closed(self):
        self.execute()
        journal = list(self.state.glob("notes-*.json"))[0]
        for content in ('{"schema_version":1,"schema_version":1,"records":{}}',
                        '{"schema_version":true,"records":{}}',
                        '{"schema_version":1,"records":{"0":{}}}',
                        '{"schema_version":2,"records":{"c0.r1":{}}}',
                        '{"schema_version":2,"records":{"c1.r0":{}}}',
                        '{"schema_version":2,"records":{"1":{}}}',
                        '{"schema_version":2,"records":{"legacy.r0":{}}}',
                        '{"schema_version":2,"records":{"p2.c1.r1":{}}}',
                        '{"schema_version":1,"records":{"1":{"state":"verified","body_sha256":NaN}}}'):
            journal.write_text(content)
            with self.subTest(content=content):
                self.assertEqual(self.execute()["code"], "INVALID_STATE_JOURNAL")

    def test_body_hash_tampering_rejected_offline(self):
        self.request["package"]["body_text"] += "被替换"
        self.assertEqual(self.execute()["code"], "PACKAGE_HASH_MISMATCH")
        self.assertEqual(self.bridge.calls, [])

    def test_fresh_until_requires_offset_and_matching_visible_marker(self):
        self.request["package"]["fresh_until"] = "2026-09-02T10:00:00"
        self.assertEqual(self.execute()["code"], "NAIVE_FRESH_UNTIL")
        self.request["package"]["fresh_until"] = "2026-09-02T12:00:00+08:00"
        self.assertEqual(self.execute()["code"], "PACKAGE_MARKER_MISMATCH")
        self.assertEqual(self.bridge.calls, [])

    def test_protocol_is_independent_from_package_schema(self):
        self.assertEqual(notes.validate_package(package())["schema_version"], 1)
        for value in (True, "2", 3, None):
            self.request["package"] = package()
            self.request["package"]["protocol_version"] = value
            self.assertEqual(self.execute()["code"], "UNSUPPORTED_PHONE_PROTOCOL")
        self.request["package"] = package()
        self.request["package"].pop("protocol_version")
        self.assertEqual(self.execute()["code"], "LEGACY_PROTOCOL_VERIFY_ONLY")
        self.assertEqual(self.execute("verify", False)["code"], "PACKAGE_MARKER_MISMATCH")
        self.assertEqual(self.bridge.calls, [])
        self.assertFalse(self.state.exists())

    def test_phone_validity_fields_must_match_exact_local_midnights(self):
        for key, value in (("valid_from", "2026-09-02T00:00:00"),
                           ("valid_from", "2026-09-02T00:00:00+09:00"),
                           ("valid_from", "2026-09-02T01:00:00+08:00"),
                           ("valid_until", "2026-09-03T06:00:00+08:00"),
                           ("valid_until", "2026-09-02T00:00:00+08:00")):
            with self.subTest(key=key, value=value):
                self.request["package"] = package()
                self.request["package"][key] = value
                self.assertEqual(self.execute()["code"], "INVALID_VALIDITY_BOUNDARY")
        self.request["package"] = package()
        self.request["package"].pop("valid_until")
        self.assertEqual(self.execute()["code"], "INVALID_PACKAGE")
        self.assertEqual(self.bridge.calls, [])

    def test_generated_and_freshness_cannot_exceed_phone_day_validity(self):
        for key, value, code in (("generated_at", "2026-09-01T23:59:59+08:00", "GENERATED_OUTSIDE_VALIDITY"),
                                 ("generated_at", "2026-09-03T00:00:00+08:00", "GENERATED_OUTSIDE_VALIDITY"),
                                 ("fresh_until", "2026-09-03T00:00:01+08:00", "FRESHNESS_EXCEEDS_VALIDITY")):
            with self.subTest(key=key):
                self.request["package"] = package()
                self.request["package"][key] = value
                self.assertEqual(self.execute()["code"], code)
        self.assertEqual(self.bridge.calls, [])

    def test_stale_freshness_before_valid_from_is_honestly_partial(self):
        candidate = package(readiness="PARTIAL")
        old_stamp = candidate["fresh_until"]
        candidate["fresh_until"] = "2026-09-01T23:00:00+08:00"
        candidate["content_text"] = candidate["content_text"].replace(old_stamp, candidate["fresh_until"])
        self.request["package"] = rehash(candidate)
        self.assertEqual(self.execute(apply=False)["readiness"], "PARTIAL")
        self.assertEqual(self.bridge.calls, [])

    def test_phone_validity_marker_is_verified_inside_visible_digest(self):
        candidate = self.request["package"]
        candidate["content_text"] = candidate["content_text"].replace("MB:VALID-FROM=2026-09-02", "MB:VALID-FROM=2026-09-01")
        rehash(candidate)
        self.assertEqual(self.execute()["code"], "PACKAGE_MARKER_MISMATCH")
        self.assertEqual(self.bridge.calls, [])

    def test_title_config_revision_must_match_body_identity(self):
        candidate = self.request["package"]
        candidate["title"] = candidate["title"].replace("c01", "c02")
        rehash(candidate)
        self.assertEqual(self.execute()["code"], "TITLE_VERSION_MISMATCH")
        self.assertEqual(self.bridge.calls, [])

    def test_core_v2_package_roundtrips_through_publisher(self):
        core_spec = importlib.util.spec_from_file_location("core_for_publisher", SCRIPT.with_name("brief_core.py"))
        core = importlib.util.module_from_spec(core_spec)
        core_spec.loader.exec_module(core)
        root = SCRIPT.parents[1]
        config = core.load_json(root / "assets" / "config.example.json")
        candidate = core.load_json(root / "assets" / "candidate.example.json")
        for config_revision in (1, 2):
            config["config_revision"] = candidate["config_revision"] = config_revision
            self.request["package"] = core.build_package(config, candidate, now=candidate["generated_at"])
            self.assertEqual(self.execute()["status"], "local_verified")
        self.assertEqual(self.bridge.create_count, 2)
        config["timezone"] = "America/New_York"
        config["schedule"]["weekdays"] = [1, 2, 3, 4, 5, 6, 7]
        for day, offset in (("2026-03-08", "-04:00"), ("2026-11-01", "-05:00")):
            candidate.update(applicable_date=day, generated_at=day + "T06:10:00" + offset, modules={})
            self.request["package"] = core.build_package(config, candidate, now=candidate["generated_at"])
            self.assertEqual(self.execute()["status"], "local_verified")
        self.assertEqual(self.bridge.create_count, 4)

    def test_package_marker_mismatch_rejected_even_with_rehashed_body(self):
        candidate = self.request["package"]
        candidate["content_text"] = candidate["content_text"].replace("MB:STATUS=READY", "MB:STATUS=PARTIAL")
        candidate["content_sha256"] = notes.digest(candidate["content_text"])
        candidate["body_text"] = (candidate["title"] + "\nMB:BEGIN\nMB:CONTENT-BEGIN\n" + candidate["content_text"] +
                                  "\nMB:CONTENT-END\nMB:CONTENT-SHA256=" + candidate["content_sha256"] + "\nMB:END")
        candidate["body_sha256"] = notes.digest(candidate["body_text"])
        self.assertEqual(self.execute()["code"], "PACKAGE_MARKER_MISMATCH")

    def test_unsafe_note_readback_rejected(self):
        for field in ("shared", "password_protected"):
            with self.subTest(field=field):
                candidate = note_for(self.request["package"])
                candidate[field] = True
                self.bridge.notes = [candidate]
                self.assertEqual(self.execute()["code"], "UNSAFE_NOTE_SCOPE")
        self.assertEqual(self.bridge.create_count, 0)

    def test_unexpected_error_does_not_expose_private_description(self):
        with patch.object(self.bridge, "call", side_effect=RuntimeError("SECRET NOTE BODY")):
            result = self.execute()
        self.assertEqual(result["code"], "PUBLISHER_ERROR")
        self.assertNotIn("SECRET", json.dumps(result))

    def test_private_body_is_not_in_success_output(self):
        result = self.execute()
        self.assertNotIn("合成内容", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("body_sha256", result)


class TextTests(unittest.TestCase):
    def test_html_escaping_links_and_whitespace_roundtrip(self):
        content = 'Title <>&"\n\n  starts with spaces  \nA  B   C\nhttps://example.org/a?x=1&y=2。\nEnd'
        encoded = notes.text_to_html(content)
        self.assertNotIn("<>&", encoded)
        self.assertIn("&lt;&gt;&amp;", encoded)
        self.assertIn('href="https://example.org/a?x=1&amp;y=2"', encoded)
        self.assertEqual(notes.html_to_visible(encoded), content)

    def test_script_shaped_text_is_visible_not_executable(self):
        content = '<script>alert("fixture")</script>\njavascript:alert(1)'
        encoded = notes.text_to_html(content)
        self.assertNotIn("<script>", encoded)
        self.assertNotIn("href=", encoded)
        self.assertEqual(notes.html_to_visible(encoded), content)

    def test_line_endings_and_single_terminal_newline_only(self):
        self.assertEqual(notes.normalize_plaintext("A\r\nB\r\n"), "A\nB")
        self.assertEqual(notes.normalize_plaintext("A\nB\n\n"), "A\nB\n")
        self.assertEqual(notes.normalize_plaintext(" A  B \n"), " A  B ")
        self.assertEqual(notes.normalize_plaintext("A\u200bB\n"), "A\u200bB")

    def test_typical_notes_formatting_does_not_change_text(self):
        source = '<div><b><span style="font-size: 18px; font-weight: bold">Title</span></b></div>\n<div><br></div>\n<div>A&nbsp; B</div>'
        self.assertEqual(notes.html_to_visible(source), "Title\n\nA  B")

    def test_hidden_content_and_changed_link_rejected(self):
        for source in ('<div hidden>Text</div>', '<div style="display:none">Text</div>',
                       '<script>Text</script>', '<div>Text</div><!--payload-->',
                       '<div><a href="https://wrong.example">https://right.example</a></div>'):
            with self.subTest(source=source):
                with self.assertRaises(notes.PublisherError):
                    notes.html_to_visible(source)

    def test_extra_blank_line_and_meaningful_spaces_not_trimmed(self):
        self.assertEqual(notes.html_to_visible("<div>A</div><div><br></div>"), "A\n")
        self.assertNotEqual(notes.html_to_visible("<div>A&nbsp;</div>"), "A")


class NativeBoundaryTests(unittest.TestCase):
    def test_default_backend_is_applescript_with_private_stdin_not_argv(self):
        completed = subprocess.CompletedProcess([], 0, '{"ok":true}', "")
        request = {"account": "PRIVATE_SCOPE", "folder": "Fixture", "package": package()}
        with patch.object(notes.subprocess, "run", return_value=completed) as runner:
            notes.NativeBridge().call("lookup", request)
        arguments = runner.call_args.args[0]
        self.assertEqual(arguments, ["/usr/bin/osascript", str(SCRIPT.with_name("notes_bridge.applescript"))])
        self.assertNotIn("PRIVATE_SCOPE", " ".join(arguments))
        self.assertIn("PRIVATE_SCOPE", runner.call_args.kwargs["input"])

    def test_native_timeout_never_echoes_subprocess_body(self):
        bridge = notes.NativeBridge()
        request = {"account": "Fixture", "folder": "Fixture", "package": package()}
        with patch.object(notes.subprocess, "run", side_effect=subprocess.TimeoutExpired("fixture", 1, output="SECRET")):
            with self.assertRaises(notes.NativeError) as error:
                bridge.call("create", request)
        self.assertTrue(error.exception.uncertain)
        self.assertNotIn("SECRET", str(error.exception))

    def test_timeout_diagnostic_reports_only_fixed_last_native_stage(self):
        error = subprocess.TimeoutExpired("fixture", 1, stderr=b"SECRET\nMB_STAGE:INPUT_PARSED\nMB_STAGE:ACCOUNT_LOOKUP\n")
        with patch.object(notes.subprocess, "run", side_effect=error):
            with self.assertRaises(notes.NativeError) as raised:
                notes.NativeBridge().call("lookup", {"account": "F", "folder": "F", "package": package()})
        self.assertEqual(raised.exception.stage, "ACCOUNT_LOOKUP")
        self.assertEqual(notes._native_diagnostic(raised.exception), {"native_stage": "ACCOUNT_LOOKUP"})
        self.assertIsNone(notes._native_stage("MB_STAGE:SECRET_BODY\n"))

    def test_native_stderr_and_invalid_json_are_sanitized(self):
        completed = subprocess.CompletedProcess([], 1, "SECRET BODY", "SECRET STDERR")
        with patch.object(notes.subprocess, "run", return_value=completed):
            with self.assertRaises(notes.NativeError) as error:
                notes.NativeBridge().call("lookup", {"account": "F", "folder": "F", "package": package()})
        self.assertEqual(str(error.exception), "NATIVE_PROCESS_ERROR")

    def test_cli_invalid_json_has_one_safe_stdout_record(self):
        completed = subprocess.run([sys.executable, str(SCRIPT), "publish"], input="SECRET malformed json",
                                   text=True, capture_output=True, check=False)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(json.loads(completed.stdout)["code"], "INVALID_JSON")
        self.assertNotIn("SECRET", completed.stdout)

    def test_cli_rejects_duplicate_keys_and_nonfinite_numbers(self):
        for content in ('{"authorized":true,"authorized":false}', '{"number":NaN}', '{"number":Infinity}', '{"number":1e999}'):
            completed = subprocess.run([sys.executable, str(SCRIPT), "publish"], input=content,
                                       text=True, capture_output=True, check=False)
            self.assertEqual(completed.returncode, 2)
            self.assertEqual(completed.stderr, "")
            self.assertFalse(json.loads(completed.stdout)["ok"])
            self.assertEqual(json.loads(completed.stdout)["code"], "INVALID_JSON")

    def test_cli_dry_run_success_has_ok_and_no_body_or_side_effect(self):
        with tempfile.TemporaryDirectory(prefix="morning-notes-cli-test-") as directory:
            state = Path(directory) / "untouched-state"
            request = {"authorized": True, "account": "Fixture", "folder": "Fixture",
                       "state_dir": str(state), "package": package()}
            completed = subprocess.run([sys.executable, str(SCRIPT), "publish"], input=json.dumps(request),
                                       text=True, capture_output=True, check=False)
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(completed.stderr, "")
            result = json.loads(completed.stdout)
            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "dry_run")
            self.assertNotIn("合成内容", completed.stdout)
            self.assertFalse(state.exists())

    @unittest.skipUnless(sys.platform == "darwin", "offline AppleScriptObjC test requires macOS")
    def test_applescript_stdin_gate_never_contacts_notes(self):
        for source in ('{"authorized":false}', '{"authorized":1}', '{"authorized":"true"}', "malformed"):
            completed = subprocess.run(["/usr/bin/osascript", str(SCRIPT.with_name("notes_bridge.applescript"))],
                                       input=source, text=True, capture_output=True, timeout=5, check=False)
            self.assertEqual(completed.returncode, 0)
            self.assertFalse(json.loads(completed.stdout)["ok"])
            self.assertIn(json.loads(completed.stdout)["code"], ("AUTHORIZATION_REQUIRED", "INVALID_JSON"))
            self.assertNotIn("ACCOUNT_LOOKUP", completed.stderr)
            self.assertNotIn("PRIVATE", completed.stdout)

    @unittest.skipUnless(sys.platform == "darwin", "offline AppleScriptObjC test requires macOS")
    def test_applescript_compiles_and_pure_selftest_passes(self):
        completed = subprocess.run(["/usr/bin/osascript", str(SCRIPT.with_name("notes_bridge.applescript")), "--self-test"],
                                   text=True, capture_output=True, timeout=10, check=False)
        self.assertEqual(completed.returncode, 0, "offline AppleScript did not compile/execute")
        self.assertEqual(completed.stderr, "")
        result = json.loads(completed.stdout)
        self.assertTrue(result["ok"])
        self.assertTrue(result["fixture_only"])
        self.assertFalse(result["native_app_contacted"])

if __name__ == "__main__":
    unittest.main()
