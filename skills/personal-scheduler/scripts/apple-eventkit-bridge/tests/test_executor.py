#!/usr/bin/env python3
"""Deterministic state-machine tests; never instantiate or call EventKit."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import tempfile
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scheduler_executor.py"
SPEC = importlib.util.spec_from_file_location("scheduler_executor_under_test", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def configured_store(root: Path) -> object:
    store = module.StateStore(root_override=root)
    with store.locked() as (root_fd, state):
        for entity, container in (("event", "EVENT_CONTAINER"), ("reminder", "REMINDER_CONTAINER")):
            state["scopes"][entity] = {
                "read_container_ids": [container],
                "write_source_id": entity.upper() + "_SOURCE",
                "write_container_id": container,
                "private_confirmed": True,
            }
        state["timezone"] = "Asia/Shanghai"
        state["event_store_id"] = "STORE-1"
        state["revision"] = 1
        store.save(root_fd, state)
    return store


def reminder_create_request(operation_id: str, schedule_id: str, dry_run: bool) -> dict:
    return {
        "operation_id": operation_id,
        "entity": "reminder",
        "source_id": "REMINDER_SOURCE",
        "container_id": "REMINDER_CONTAINER",
        "confirm_private_container": True,
        "managed": {"schema_version": 1, "schedule_id": schedule_id, "entity": "reminder", "role": "task"},
        "payload": {"title": "CANARY_PRIVATE_TITLE", "due": {"kind": "none"}, "priority": 0},
        "dry_run": dry_run,
    }


def success_bridge(command: str, payload: dict | None = None) -> tuple[int, dict]:
    assert command == "items create"
    assert payload is not None
    if payload.get("dry_run"):
        assert "expected_event_store_id" not in payload
        return 0, {"ok": True, "command": command, "dry_run": True, "event_store_id": "STORE-1", "mutated": False}
    assert payload.get("expected_event_store_id") == "STORE-1"
    return 0, {
        "ok": True,
        "command": command,
        "event_store_id": "STORE-1",
        "item": {
            "item_id": "ITEM-1",
            "external_id": "EXTERNAL-1",
            "fingerprint": "sha256:" + "a" * 64,
        },
        "mutated": True,
    }


def main() -> None:
    assert module.STATE_SCHEMA_VERSION == 2
    assert module.STATE_FILE_NAME == "state-v2.json"

    for unsafe_call in (
        lambda: module.operation_entity("items create", {"entity": {}}),
        lambda: module.verify_read_scope(module.default_state(), "items get", {"entity": {}, "container_id": "C"}),
        lambda: module.handle_operation_resolve(
            {"operation_id": "OP-11111111-1111-4111-8111-111111111111", "resolution": {}, "confirmed": True},
            module.StateStore(root_override=Path("/unused")),
        ),
    ):
        try:
            unsafe_call()
        except module.ExecutorError as error:
            assert error.code in {"validation_error", "confirmation_required"}
        else:
            raise AssertionError("an unhashable public enum value escaped fail-closed validation")

    invalid_schedule_state = module.default_state()
    invalid_schedule_state["schedules"]["PS-10101010-1010-4010-8010-101010101010"] = {
        "entity": {},
        "state": "pending",
        "event_store_id": None,
        "source_id": None,
        "container_id": None,
        "item_id": None,
        "external_id": None,
        "intent_hash": None,
        "last_fingerprint": None,
        "updated_at": None,
    }
    try:
        module.validate_state(invalid_schedule_state)
    except module.ExecutorError as error:
        assert error.code == "unsafe_state"
    else:
        raise AssertionError("an unhashable state enum value escaped fail-closed validation")

    with tempfile.TemporaryDirectory(prefix="personal-scheduler-legacy-state-test-") as legacy_directory:
        legacy_root = Path(legacy_directory) / "state"
        legacy_root.mkdir(mode=0o700)
        legacy_path = legacy_root / "state-v1.json"
        legacy_path.write_text('{"schema_version":1}\n', encoding="utf-8")
        legacy_path.chmod(0o600)
        legacy_store = module.StateStore(root_override=legacy_root)
        for load in (legacy_store.peek, lambda: legacy_store.locked().__enter__()):
            try:
                load()
            except module.ExecutorError as error:
                assert error.code == "legacy_state_requires_manual_audit"
            else:
                raise AssertionError("a pre-first-cut v1 state was silently ignored or migrated")

    expected_find = {
        "entity": "reminder",
        "source_id": "REMINDER_SOURCE",
        "container_id": "REMINDER_CONTAINER",
        "schedule_id": "PS-12121212-1212-4212-8212-121212121212",
    }
    valid_found_item = {
        "entity": "reminder",
        "source_id": "REMINDER_SOURCE",
        "container_id": "REMINDER_CONTAINER",
        "item_id": "ITEM-EXACT",
        "ownership": "personal_scheduler",
        "recurring": False,
        "fingerprint": "sha256:" + "1" * 64,
        "content_hash": "sha256:" + "2" * 64,
        "managed": {
            "schema_version": 1,
            "schedule_id": expected_find["schedule_id"],
            "entity": "reminder",
            "role": "task",
        },
    }
    valid_find_envelope = {
        "ok": True,
        "command": "items find",
        "event_store_id": "STORE-1",
        "count": 1,
        "item": valid_found_item,
        "mutated": False,
    }
    assert module.require_find_success_shape(valid_find_envelope, expected_find, "create") == valid_found_item
    assert module.require_find_success_shape(
        {
            "ok": True,
            "command": "items find",
            "event_store_id": "STORE-1",
            "count": 0,
            "item": None,
            "mutated": False,
        },
        expected_find,
        "create",
    ) is None
    invalid_find_envelopes = (
        dict(valid_find_envelope, count=0),
        {key: value for key, value in valid_find_envelope.items() if key != "item"},
        dict(valid_find_envelope, count=2),
        dict(valid_find_envelope, count=True),
        dict(valid_find_envelope, command="items get"),
        dict(valid_find_envelope, mutated=True),
        dict(valid_find_envelope, item=dict(valid_found_item, item_id="")),
        dict(valid_find_envelope, item=dict(valid_found_item, ownership={})),
        dict(
            valid_find_envelope,
            item=dict(valid_found_item, managed=dict(valid_found_item["managed"], schema_version=999)),
        ),
        dict(
            valid_find_envelope,
            item=dict(valid_found_item, managed=dict(valid_found_item["managed"], role={})),
        ),
    )
    for invalid_find in invalid_find_envelopes:
        try:
            module.require_find_success_shape(invalid_find, expected_find, "create")
        except module.ExecutorError as error:
            assert error.code == "bridge_protocol_error"
        else:
            raise AssertionError("an inconsistent successful items find envelope was accepted")

    expected_get = dict(expected_find, item_id="ITEM-EXACT")
    valid_get_envelope = {
        "ok": True,
        "command": "items get",
        "event_store_id": "STORE-1",
        "item": valid_found_item,
        "mutated": False,
    }
    assert module.require_get_success_shape(valid_get_envelope, expected_get, "patch") == valid_found_item
    try:
        module.require_get_success_shape(
            dict(valid_get_envelope, item=dict(valid_found_item, item_id="ITEM-OTHER")),
            expected_get,
            "patch",
        )
    except module.ExecutorError as error:
        assert error.code == "bridge_protocol_error"
    else:
        raise AssertionError("items get returned a different item_id as exact evidence")

    valid_container_envelope = {
        "ok": True,
        "command": "containers list",
        "event_store_id": "STORE-1",
        "containers": [
            {
                "container_id": "REMINDER_CONTAINER",
                "source_id": "REMINDER_SOURCE",
                "title": "日常安排",
                "writable": True,
                "subscribed": False,
                "immutable": False,
                "source_is_delegate": False,
                "allowed_entities": ["reminder"],
            }
        ],
        "mutated": False,
    }
    assert module.require_containers_success_shape(valid_container_envelope)[0]["container_id"] == "REMINDER_CONTAINER"
    for malformed_container in (
        dict(valid_container_envelope, containers={}),
        dict(valid_container_envelope, containers=[dict(valid_container_envelope["containers"][0], container_id="")]),
        dict(valid_container_envelope, containers=[dict(valid_container_envelope["containers"][0], allowed_entities=[{}])]),
    ):
        try:
            module.require_containers_success_shape(malformed_container)
        except module.ExecutorError as error:
            assert error.code == "bridge_protocol_error"
        else:
            raise AssertionError("a malformed successful containers list envelope was accepted")

    inconsistent = module.normalize_bridge_output(9, {"ok": True, "command": "doctor", "mutated": True})
    assert inconsistent["ok"] is False
    assert inconsistent["error"]["code"] == "bridge_protocol_error"
    assert "mutated" not in inconsistent
    preserved = module.normalize_bridge_output(7, {"ok": True, "error": {"code": "native_failure", "message": "failed"}})
    assert preserved == {"ok": False, "error": {"code": "native_failure", "message": "failed"}}
    explicit_failure = {"ok": False, "error": {"code": "denied", "message": "denied"}}
    assert module.normalize_bridge_output(0, explicit_failure) is explicit_failure

    class InconsistentCompleted:
        returncode = 9
        stdout = b'{"ok":true,"mutated":true}'
        stderr = b""

    original_subprocess_run = module.subprocess.run
    try:
        module.subprocess.run = lambda *args, **kwargs: InconsistentCompleted()
        for bridge_command, payload in (("doctor", None), ("events list", {}), ("items create", {})):
            bridge_status, bridge_output = module.run_bridge(bridge_command, payload)
            assert bridge_status == 9
            assert bridge_output["ok"] is False
            assert bridge_output["error"]["code"] == "bridge_protocol_error"
            assert "mutated" not in bridge_output
    finally:
        module.subprocess.run = original_subprocess_run

    for command, kind, outcome in (
        ("items create", "create", "verified_local"),
        ("unmanaged items patch", "unmanaged_patch", "verified_local"),
        ("containers create", "container_create", "verified_local"),
    ):
        terminal_record = {"kind": kind, "phase": "terminal", "outcome": outcome, "event_store_id": "STORE-1"}
        current_replay = module.terminal_replay_result(command, "OP-REPLAY", terminal_record, "STORE-1")
        assert current_replay["ok"] is True and current_replay["event_store_id"] == "STORE-1"
        stale_replay = module.terminal_replay_result(command, "OP-REPLAY", terminal_record, "STORE-2")
        assert stale_replay["ok"] is False
        assert stale_replay["error"]["code"] == "operation_terminal_stale_epoch"
        assert stale_replay["historical_event_store_id"] == "STORE-1"

    empty_location_request = {
        "payload": {
            "title": "event",
            "location": "",
            "time": {
                "kind": "timed",
                "start_at": "2026-09-08T14:00:00+08:00",
                "end_at": "2026-09-08T15:00:00+08:00",
                "timezone": "Asia/Shanghai",
            },
            "alarms": [],
        }
    }
    try:
        module.desired_content_hash("event", empty_location_request)
    except module.ExecutorError as error:
        assert error.code == "validation_error"
    else:
        raise AssertionError("an empty event location escaped executor validation")

    with tempfile.TemporaryDirectory(prefix="personal-scheduler-executor-test-") as directory:
        root = Path(directory) / "state"
        root.mkdir(mode=0o700)
        store = configured_store(root)
        operation_id = "OP-77777777-7777-4777-8777-777777777777"
        schedule_id = "PS-88888888-8888-4888-8888-888888888888"

        original_run_bridge = module.run_bridge
        try:
            reserved_request = reminder_create_request(operation_id, schedule_id, True)
            reserved_request["expected_event_store_id"] = "STORE-1"
            try:
                module.handle_mutation("items create", reserved_request, store)
            except module.ExecutorError as error:
                assert error.code == "validation_error"
            else:
                raise AssertionError("a public request supplied the executor-owned EventKit epoch")

            module.run_bridge = success_bridge
            preview_request = reminder_create_request(operation_id, schedule_id, True)
            preview = module.handle_mutation("items create", preview_request, store)
            assert preview["ok"] is True and preview["journaled"] is False
            exists, state = store.peek()
            assert exists and state["operations"] == {}

            actual_request = reminder_create_request(operation_id, schedule_id, False)
            actual_request["preview_hash"] = preview["preview_hash"]
            result = module.handle_mutation("items create", actual_request, store)
            assert result["ok"] is True and result["journal_phase"] == "terminal"
            _, state = store.peek()
            assert state["operations"][operation_id]["phase"] == "terminal"
            assert state["operations"][operation_id]["outcome"] == "verified_local"
            assert state["schedules"][schedule_id]["state"] == "verified_local"
            replay = module.handle_mutation("items create", actual_request, store)
            assert replay["ok"] is True and replay["already_terminal"] is True
            assert replay["event_store_id"] == "STORE-1" and replay["mutated"] is False
            raw_state = (root / module.STATE_FILE_NAME).read_text(encoding="utf-8")
            assert "CANARY_PRIVATE_TITLE" not in raw_state

            timeout_operation = "OP-99999999-9999-4999-8999-999999999999"
            timeout_schedule = "PS-AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"
            timeout_preview_request = reminder_create_request(timeout_operation, timeout_schedule, True)
            timeout_preview = module.handle_mutation("items create", timeout_preview_request, store)
            timeout_actual = reminder_create_request(timeout_operation, timeout_schedule, False)
            timeout_actual["preview_hash"] = timeout_preview["preview_hash"]

            def timeout_bridge(command: str, payload: dict | None = None) -> tuple[int, dict]:
                assert payload is not None
                if payload.get("dry_run"):
                    assert "expected_event_store_id" not in payload
                    return 0, {"ok": True, "command": command, "dry_run": True, "event_store_id": "STORE-1", "mutated": False}
                assert payload.get("expected_event_store_id") == "STORE-1"
                return 2, {"ok": False, "error": {"code": "operation_timeout_outcome_unknown", "message": "timeout"}}

            module.run_bridge = timeout_bridge
            timeout_result = module.handle_mutation("items create", timeout_actual, store)
            assert timeout_result["ok"] is False and timeout_result["journal_phase"] == "outcome_unknown"
            _, state = store.peek()
            assert state["operations"][timeout_operation]["phase"] == "outcome_unknown"
            try:
                module.handle_mutation("items create", timeout_actual, store)
            except module.ExecutorError as error:
                assert error.code == "reconciliation_required"
            else:
                raise AssertionError("outcome_unknown operation was replayed")

            def reconciled_create(command: str, payload: dict | None = None) -> tuple[int, dict]:
                assert command == "items find"
                return 0, {
                    "ok": True,
                    "command": command,
                    "event_store_id": "STORE-1",
                    "count": 1,
                    "mutated": False,
                    "item": {
                        "entity": "reminder",
                        "source_id": "REMINDER_SOURCE",
                        "container_id": "REMINDER_CONTAINER",
                        "item_id": "ITEM-TIMEOUT",
                        "external_id": "EXTERNAL-TIMEOUT",
                        "ownership": "personal_scheduler",
                        "managed": {"schema_version": 1, "schedule_id": timeout_schedule, "entity": "reminder", "role": "task"},
                        "recurring": False,
                        "fingerprint": "sha256:" + "d" * 64,
                        "content_hash": module.desired_content_hash("reminder", timeout_actual),
                    },
                }

            module.run_bridge = reconciled_create
            reconciled = module.handle_operation_reconcile(
                {"operation_id": timeout_operation, "command": "items create", "original_request": timeout_actual},
                store,
            )
            assert reconciled["resolution"] == "verified_local"
            _, state = store.peek()
            assert state["operations"][timeout_operation]["phase"] == "terminal"
            assert state["schedules"][timeout_schedule]["state"] == "verified_local"

            expected_managed = timeout_actual["managed"]
            wrong_role_item = reconciled_create("items find", {})[1]["item"]
            wrong_role_item["managed"] = dict(wrong_role_item["managed"], role="deadline")
            assert module.managed_matches(wrong_role_item, timeout_schedule, expected_managed) is False

            duplicate_create_operation = "OP-31313131-3131-4131-8131-313131313131"
            duplicate_create_schedule = "PS-32323232-3232-4232-8232-323232323232"
            module.run_bridge = success_bridge
            duplicate_create_preview_request = reminder_create_request(duplicate_create_operation, duplicate_create_schedule, True)
            duplicate_create_preview = module.handle_mutation("items create", duplicate_create_preview_request, store)
            duplicate_create_actual = reminder_create_request(duplicate_create_operation, duplicate_create_schedule, False)
            duplicate_create_actual["preview_hash"] = duplicate_create_preview["preview_hash"]
            module.run_bridge = timeout_bridge
            module.handle_mutation("items create", duplicate_create_actual, store)

            def duplicate_create_reconcile(command: str, payload: dict | None = None) -> tuple[int, dict]:
                assert command == "items find"
                return 2, {"ok": False, "error": {"code": "schedule_duplicate", "message": "duplicate"}}

            module.run_bridge = duplicate_create_reconcile
            duplicate_create_result = module.handle_operation_reconcile(
                {"operation_id": duplicate_create_operation, "command": "items create", "original_request": duplicate_create_actual},
                store,
            )
            assert duplicate_create_result["resolution"] == "outcome_unknown"
            _, state = store.peek()
            assert state["operations"][duplicate_create_operation]["phase"] == "outcome_unknown"
            assert state["schedules"][duplicate_create_schedule]["state"] == "outcome_unknown"

            patch_operation = "OP-10101010-1010-4010-8010-101010101010"
            patch_request = {
                "operation_id": patch_operation,
                "entity": "reminder",
                "source_id": "REMINDER_SOURCE",
                "container_id": "REMINDER_CONTAINER",
                "confirm_private_container": True,
                "schedule_id": schedule_id,
                "item_id": "ITEM-1",
                "expected_fingerprint": "sha256:" + "a" * 64,
                "managed": {"schema_version": 1, "schedule_id": schedule_id, "entity": "reminder", "role": "task"},
                "payload": {"title": "UPDATED", "due": {"kind": "none"}, "priority": 0},
                "dry_run": True,
            }

            def patch_timeout_bridge(command: str, payload: dict | None = None) -> tuple[int, dict]:
                assert command == "items patch" and payload is not None
                if payload.get("dry_run"):
                    assert "expected_event_store_id" not in payload
                    return 0, {"ok": True, "command": command, "dry_run": True, "event_store_id": "STORE-1", "mutated": False}
                assert payload.get("expected_event_store_id") == "STORE-1"
                return 2, {"ok": False, "error": {"code": "operation_timeout_outcome_unknown", "message": "timeout"}}

            module.run_bridge = patch_timeout_bridge
            patch_preview = module.handle_mutation("items patch", patch_request, store)
            patch_actual = dict(patch_request, dry_run=False, preview_hash=patch_preview["preview_hash"])
            patch_timeout = module.handle_mutation("items patch", patch_actual, store)
            assert patch_timeout["journal_phase"] == "outcome_unknown"

            def duplicate_patch_reconcile(command: str, payload: dict | None = None) -> tuple[int, dict]:
                assert payload is not None
                if command == "items get":
                    return 0, {
                        "ok": True,
                        "command": command,
                        "event_store_id": "STORE-1",
                        "mutated": False,
                        "item": {
                            "entity": "reminder",
                            "source_id": "REMINDER_SOURCE",
                            "container_id": "REMINDER_CONTAINER",
                            "item_id": "ITEM-1",
                            "external_id": "EXTERNAL-1",
                            "ownership": "personal_scheduler",
                            "managed": patch_actual["managed"],
                            "recurring": False,
                            "fingerprint": "sha256:" + "b" * 64,
                            "content_hash": module.desired_content_hash("reminder", patch_actual),
                        },
                    }
                assert command == "items find"
                return 2, {"ok": False, "error": {"code": "schedule_duplicate", "message": "duplicate"}}

            module.run_bridge = duplicate_patch_reconcile
            duplicate_result = module.handle_operation_reconcile(
                {"operation_id": patch_operation, "command": "items patch", "original_request": patch_actual},
                store,
            )
            assert duplicate_result["resolution"] == "outcome_unknown"
            _, state = store.peek()
            assert state["operations"][patch_operation]["phase"] == "outcome_unknown"
            assert state["schedules"][schedule_id]["state"] == "outcome_unknown"

            missing_find_operation = "OP-34343434-3434-4434-8434-343434343434"
            missing_find_schedule = "PS-35353535-3535-4535-8535-353535353535"
            missing_find_request = {
                "operation_id": missing_find_operation,
                "entity": "reminder",
                "source_id": "REMINDER_SOURCE",
                "container_id": "REMINDER_CONTAINER",
                "confirm_private_container": True,
                "schedule_id": missing_find_schedule,
                "item_id": "ITEM-MISSING-NEW",
                "expected_fingerprint": "sha256:" + "9" * 64,
                "managed": {
                    "schema_version": 1,
                    "schedule_id": missing_find_schedule,
                    "entity": "reminder",
                    "role": "task",
                },
                "payload": {"title": "MISSING", "due": {"kind": "none"}, "priority": 0},
                "dry_run": True,
            }

            def missing_find_timeout_bridge(command: str, payload: dict | None = None) -> tuple[int, dict]:
                assert command == "items patch" and payload is not None
                if payload.get("dry_run"):
                    return 0, {
                        "ok": True,
                        "command": command,
                        "dry_run": True,
                        "event_store_id": "STORE-1",
                        "mutated": False,
                    }
                return 2, {"ok": False, "error": {"code": "operation_timeout_outcome_unknown", "message": "timeout"}}

            module.run_bridge = missing_find_timeout_bridge
            missing_find_preview = module.handle_mutation("items patch", missing_find_request, store)
            missing_find_actual = dict(
                missing_find_request,
                dry_run=False,
                preview_hash=missing_find_preview["preview_hash"],
            )
            assert module.handle_mutation("items patch", missing_find_actual, store)["journal_phase"] == "outcome_unknown"
            missing_find_reads: list[str] = []

            def missing_then_conflict_error(command: str, payload: dict | None = None) -> tuple[int, dict]:
                assert payload is not None
                missing_find_reads.append(command)
                if command == "items get":
                    return 2, {"ok": False, "error": {"code": "item_missing", "message": "missing"}}
                assert command == "items find"
                return 2, {"ok": False, "error": {"code": "schedule_duplicate", "message": "duplicate"}}

            module.run_bridge = missing_then_conflict_error
            missing_find_result = module.handle_operation_reconcile(
                {
                    "operation_id": missing_find_operation,
                    "command": "items patch",
                    "original_request": missing_find_actual,
                },
                store,
            )
            assert missing_find_reads == ["items get", "items find"]
            assert missing_find_result["resolution"] == "outcome_unknown"
            _, state = store.peek()
            assert state["operations"][missing_find_operation]["phase"] == "outcome_unknown"
            assert state["schedules"][missing_find_schedule]["state"] == "outcome_unknown"

            preserved_state = {
                "schedules": {
                    schedule_id: {
                        "intent_hash": "sha256:" + "1" * 64,
                        "state": "outcome_unknown",
                    }
                }
            }
            module.checkpoint_reconciled_schedule(
                preserved_state,
                {
                    "kind": "patch",
                    "entity": "reminder",
                    "schedule_id": schedule_id,
                    "source_id": "REMINDER_SOURCE",
                    "container_id": "REMINDER_CONTAINER",
                    "intent_hash": "sha256:" + "2" * 64,
                },
                {"item_id": "ITEM-1", "external_id": "EXTERNAL-1", "fingerprint": "sha256:" + "3" * 64},
                "STORE-1",
                "not_applied",
            )
            assert preserved_state["schedules"][schedule_id]["intent_hash"] == "sha256:" + "1" * 64

            stale_operation = "OP-DDDDDDDD-DDDD-4DDD-8DDD-DDDDDDDDDDDD"
            stale_schedule = "PS-EEEEEEEE-EEEE-4EEE-8EEE-EEEEEEEEEEEE"
            module.run_bridge = success_bridge
            stale_preview_request = reminder_create_request(stale_operation, stale_schedule, True)
            stale_preview = module.handle_mutation("items create", stale_preview_request, store)
            stale_actual = reminder_create_request(stale_operation, stale_schedule, False)
            stale_actual["preview_hash"] = stale_preview["preview_hash"]

            def actual_stale(command: str, payload: dict | None = None) -> tuple[int, dict]:
                assert payload is not None
                if payload.get("dry_run"):
                    assert "expected_event_store_id" not in payload
                    return 0, {"ok": True, "command": command, "dry_run": True, "event_store_id": "STORE-1", "mutated": False}
                assert payload.get("expected_event_store_id") == "STORE-1"
                return 2, {"ok": False, "error": {"code": "stale_object", "message": "could be pre- or post-write"}}

            module.run_bridge = actual_stale
            stale_result = module.handle_mutation("items create", stale_actual, store)
            assert stale_result["journal_phase"] == "outcome_unknown"
            _, state = store.peek()
            assert state["operations"][stale_operation]["outcome"] == "outcome_unknown"
            blocked_operation = "OP-FFFFFFFF-FFFF-4FFF-8FFF-FFFFFFFFFFFF"
            blocked_request = reminder_create_request(blocked_operation, stale_schedule, False)
            blocked_request["preview_hash"] = module.digest(module.public_intent("items create", blocked_request))
            try:
                module.handle_mutation("items create", blocked_request, store)
            except module.ExecutorError as error:
                assert error.code == "reconciliation_required"
            else:
                raise AssertionError("a new operation bypassed an unresolved schedule")

            prepared_operation = "OP-BBBBBBBB-BBBB-4BBB-8BBB-BBBBBBBBBBBB"
            prepared_schedule = "PS-CCCCCCCC-CCCC-4CCC-8CCC-CCCCCCCCCCCC"
            prepared_preview_request = reminder_create_request(prepared_operation, prepared_schedule, True)
            module.run_bridge = success_bridge
            prepared_preview = module.handle_mutation("items create", prepared_preview_request, store)
            prepared_actual = reminder_create_request(prepared_operation, prepared_schedule, False)
            prepared_actual["preview_hash"] = prepared_preview["preview_hash"]

            def preflight_failure(command: str, payload: dict | None = None) -> tuple[int, dict]:
                return 2, {"ok": False, "error": {"code": "stale_object", "message": "changed"}}

            module.run_bridge = preflight_failure
            failed = module.handle_mutation("items create", prepared_actual, store)
            assert failed["ok"] is False
            _, state = store.peek()
            assert state["operations"][prepared_operation]["phase"] == "prepared"
            module.run_bridge = success_bridge
            recovered = module.handle_mutation("items create", prepared_actual, store)
            assert recovered["ok"] is True

            missing_epoch_operation = "OP-45454545-4545-4545-8545-454545454545"
            missing_epoch_schedule = "PS-67676767-6767-4767-8767-676767676767"
            module.run_bridge = success_bridge
            missing_preview_request = reminder_create_request(missing_epoch_operation, missing_epoch_schedule, True)
            missing_preview = module.handle_mutation("items create", missing_preview_request, store)
            missing_actual = reminder_create_request(missing_epoch_operation, missing_epoch_schedule, False)
            missing_actual["preview_hash"] = missing_preview["preview_hash"]
            calls: list[str] = []

            def missing_epoch_preflight(command: str, payload: dict | None = None) -> tuple[int, dict]:
                assert command == "items create" and payload is not None
                calls.append("dry" if payload.get("dry_run") else "actual")
                return 0, {"ok": True, "command": command, "dry_run": payload.get("dry_run"), "mutated": False}

            module.run_bridge = missing_epoch_preflight
            try:
                module.handle_mutation("items create", missing_actual, store)
            except module.ExecutorError as error:
                assert error.code == "bridge_protocol_error"
            else:
                raise AssertionError("a mutation preflight without event_store_id was accepted")
            assert calls == ["dry"]
            _, state = store.peek()
            assert state["operations"][missing_epoch_operation]["phase"] == "prepared"
            assert state["operations"][missing_epoch_operation]["error_code"] == "bridge_protocol_error"
            module.handle_operation_resolve(
                {"operation_id": missing_epoch_operation, "resolution": "not_applied", "confirmed": True},
                store,
            )

            missing_result_operation = "OP-23232323-2323-4323-8323-232323232323"
            missing_result_schedule = "PS-24242424-2424-4424-8424-242424242424"
            module.run_bridge = success_bridge
            missing_result_preview_request = reminder_create_request(missing_result_operation, missing_result_schedule, True)
            missing_result_preview = module.handle_mutation("items create", missing_result_preview_request, store)
            missing_result_actual = reminder_create_request(missing_result_operation, missing_result_schedule, False)
            missing_result_actual["preview_hash"] = missing_result_preview["preview_hash"]

            def missing_result_epoch(command: str, payload: dict | None = None) -> tuple[int, dict]:
                assert payload is not None
                if payload.get("dry_run"):
                    assert "expected_event_store_id" not in payload
                    return 0, {"ok": True, "command": command, "dry_run": True, "event_store_id": "STORE-1", "mutated": False}
                assert payload.get("expected_event_store_id") == "STORE-1"
                return 0, {"ok": True, "command": command, "mutated": True}

            module.run_bridge = missing_result_epoch
            missing_result = module.handle_mutation("items create", missing_result_actual, store)
            assert missing_result["ok"] is False
            assert missing_result["journal_phase"] == "outcome_unknown"
            assert missing_result["mutation_outcome"] == "unknown"
            _, state = store.peek()
            assert state["operations"][missing_result_operation]["phase"] == "outcome_unknown"
            assert state["schedules"][missing_result_schedule]["state"] == "outcome_unknown"

            cancelled_operation = "OP-12121212-1212-4212-8212-121212121212"
            cancelled_schedule = "PS-34343434-3434-4434-8434-343434343434"
            module.run_bridge = success_bridge
            cancelled_preview_request = reminder_create_request(cancelled_operation, cancelled_schedule, True)
            cancelled_preview = module.handle_mutation("items create", cancelled_preview_request, store)
            cancelled_actual = reminder_create_request(cancelled_operation, cancelled_schedule, False)
            cancelled_actual["preview_hash"] = cancelled_preview["preview_hash"]
            module.run_bridge = preflight_failure
            cancelled_preflight = module.handle_mutation("items create", cancelled_actual, store)
            assert cancelled_preflight["journal_phase"] == "prepared"
            resolved = module.handle_operation_resolve(
                {"operation_id": cancelled_operation, "resolution": "not_applied", "confirmed": True},
                store,
            )
            assert resolved["resolution"] == "not_applied"
            _, state = store.peek()
            assert cancelled_schedule not in state["schedules"]
            terminal_retry = module.handle_mutation("items create", cancelled_actual, store)
            assert terminal_retry["ok"] is False
            assert terminal_retry["error"]["code"] == "operation_terminal_without_success"

            try:
                module.verify_read_scope(state, "availability", {"calendar_ids": ["UNCONFIRMED"]})
            except module.ExecutorError as error:
                assert error.code == "read_scope_mismatch"
            else:
                raise AssertionError("an unconfirmed Calendar entered the read scope")
            for command, malformed in (
                ("availability", {"calendar_ids": [{}]}),
                ("reminders list", {"list_ids": [{}]}),
                ("items get", {"entity": "event", "container_id": {}}),
            ):
                try:
                    module.verify_read_scope(state, command, malformed)
                except module.ExecutorError:
                    pass
                else:
                    raise AssertionError("malformed read scope escaped structured validation")
        finally:
            module.run_bridge = original_run_bridge

        with tempfile.TemporaryDirectory(prefix="personal-scheduler-epoch-test-") as epoch_directory:
            epoch_root = Path(epoch_directory) / "state"
            epoch_root.mkdir(mode=0o700)
            epoch_store = configured_store(epoch_root)
            epoch_operation = "OP-89898989-8989-4989-8989-898989898989"
            epoch_schedule = "PS-90909090-9090-4090-8090-909090909090"
            original_run_bridge = module.run_bridge
            try:
                module.run_bridge = success_bridge
                epoch_preview_request = reminder_create_request(epoch_operation, epoch_schedule, True)
                epoch_preview = module.handle_mutation("items create", epoch_preview_request, epoch_store)
                epoch_actual = reminder_create_request(epoch_operation, epoch_schedule, False)
                epoch_actual["preview_hash"] = epoch_preview["preview_hash"]
                epoch_calls: list[str] = []

                def changed_epoch_preflight(command: str, payload: dict | None = None) -> tuple[int, dict]:
                    assert payload is not None
                    epoch_calls.append("dry" if payload.get("dry_run") else "actual")
                    return 0, {"ok": True, "command": command, "dry_run": True, "event_store_id": "STORE-2", "mutated": False}

                module.run_bridge = changed_epoch_preflight
                try:
                    module.handle_mutation("items create", epoch_actual, epoch_store)
                except module.ExecutorError as error:
                    assert error.code == "event_store_changed"
                else:
                    raise AssertionError("a changed EventKit store epoch reached mutation")
                assert epoch_calls == ["dry"]
                _, epoch_state = epoch_store.peek()
                assert epoch_state["event_store_id"] == "STORE-2"
                assert epoch_state["scopes"]["event"]["read_container_ids"] == []
                assert epoch_state["scopes"]["reminder"]["read_container_ids"] == []
            finally:
                module.run_bridge = original_run_bridge

        with tempfile.TemporaryDirectory(prefix="personal-scheduler-store-race-test-") as race_directory:
            race_root = Path(race_directory) / "state"
            race_root.mkdir(mode=0o700)
            race_store = configured_store(race_root)
            race_operation = "OP-51515151-5151-4151-8151-515151515151"
            race_schedule = "PS-52525252-5252-4252-8252-525252525252"
            original_run_bridge = module.run_bridge
            try:
                module.run_bridge = success_bridge
                race_preview_request = reminder_create_request(race_operation, race_schedule, True)
                race_preview = module.handle_mutation("items create", race_preview_request, race_store)
                race_actual = reminder_create_request(race_operation, race_schedule, False)
                race_actual["preview_hash"] = race_preview["preview_hash"]
                race_calls: list[str] = []

                def store_race_bridge(command: str, payload: dict | None = None) -> tuple[int, dict]:
                    assert command == "items create" and payload is not None
                    if payload.get("dry_run"):
                        assert "expected_event_store_id" not in payload
                        race_calls.append("preflight:STORE-1")
                        return 0, {"ok": True, "command": command, "dry_run": True, "event_store_id": "STORE-1", "mutated": False}
                    assert payload.get("expected_event_store_id") == "STORE-1"
                    race_calls.append("actual:STORE-1")
                    return 2, {
                        "ok": False,
                        "error": {
                            "code": "event_store_changed_before_mutation",
                            "message": "store changed; no native effect was attempted",
                        },
                    }

                module.run_bridge = store_race_bridge
                race_result = module.handle_mutation("items create", race_actual, race_store)
                assert race_calls == ["preflight:STORE-1", "actual:STORE-1"]
                assert race_result["journal_phase"] == "outcome_unknown"
                assert race_result["mutation_outcome"] == "unknown"
                assert "mutated" not in race_result
                _, race_state = race_store.peek()
                assert race_state["operations"][race_operation]["phase"] == "outcome_unknown"
                assert race_state["schedules"][race_schedule]["state"] == "outcome_unknown"
            finally:
                module.run_bridge = original_run_bridge

        with tempfile.TemporaryDirectory(prefix="personal-scheduler-reconcile-snapshot-test-") as snapshot_directory:
            snapshot_root = Path(snapshot_directory) / "state"
            snapshot_root.mkdir(mode=0o700)
            snapshot_store = configured_store(snapshot_root)
            original_run_bridge = module.run_bridge
            try:
                initial_operation = "OP-61616161-6161-4161-8161-616161616161"
                managed_schedule = "PS-62626262-6262-4262-8262-626262626262"
                module.run_bridge = success_bridge
                initial_preview_request = reminder_create_request(initial_operation, managed_schedule, True)
                initial_preview = module.handle_mutation("items create", initial_preview_request, snapshot_store)
                initial_actual = reminder_create_request(initial_operation, managed_schedule, False)
                initial_actual["preview_hash"] = initial_preview["preview_hash"]
                assert module.handle_mutation("items create", initial_actual, snapshot_store)["ok"] is True

                drift_operation = "OP-63636363-6363-4363-8363-636363636363"
                drift_request = {
                    "operation_id": drift_operation,
                    "entity": "reminder",
                    "source_id": "REMINDER_SOURCE",
                    "container_id": "REMINDER_CONTAINER",
                    "confirm_private_container": True,
                    "schedule_id": managed_schedule,
                    "item_id": "ITEM-1",
                    "expected_fingerprint": "sha256:" + "a" * 64,
                    "managed": {"schema_version": 1, "schedule_id": managed_schedule, "entity": "reminder", "role": "task"},
                    "payload": {"title": "TARGET", "due": {"kind": "none"}, "priority": 0},
                    "dry_run": True,
                }

                def drift_timeout_bridge(command: str, payload: dict | None = None) -> tuple[int, dict]:
                    assert command == "items patch" and payload is not None
                    if payload.get("dry_run"):
                        return 0, {"ok": True, "command": command, "dry_run": True, "event_store_id": "STORE-1", "mutated": False}
                    assert payload.get("expected_event_store_id") == "STORE-1"
                    return 2, {"ok": False, "error": {"code": "operation_timeout_outcome_unknown", "message": "timeout"}}

                module.run_bridge = drift_timeout_bridge
                drift_preview = module.handle_mutation("items patch", drift_request, snapshot_store)
                drift_actual = dict(drift_request, dry_run=False, preview_hash=drift_preview["preview_hash"])
                assert module.handle_mutation("items patch", drift_actual, snapshot_store)["journal_phase"] == "outcome_unknown"
                drift_reads: list[str] = []

                def drift_reconcile_bridge(command: str, payload: dict | None = None) -> tuple[int, dict]:
                    assert payload is not None
                    base_item = {
                        "entity": "reminder",
                        "source_id": "REMINDER_SOURCE",
                        "container_id": "REMINDER_CONTAINER",
                        "item_id": "ITEM-1",
                        "external_id": "EXTERNAL-1",
                        "ownership": "personal_scheduler",
                        "managed": drift_actual["managed"],
                        "recurring": False,
                    }
                    if command == "items get":
                        drift_reads.append("get:target")
                        return 0, {
                            "ok": True,
                            "command": command,
                            "event_store_id": "STORE-1",
                            "mutated": False,
                            "item": dict(
                                base_item,
                                fingerprint="sha256:" + "b" * 64,
                                content_hash=module.desired_content_hash("reminder", drift_actual),
                            ),
                        }
                    assert command == "items find"
                    drift_reads.append("find:drift")
                    return 0, {
                        "ok": True,
                        "command": command,
                        "event_store_id": "STORE-1",
                        "count": 1,
                        "mutated": False,
                        "item": dict(
                            base_item,
                            fingerprint="sha256:" + "c" * 64,
                            content_hash="sha256:" + "d" * 64,
                        ),
                    }

                module.run_bridge = drift_reconcile_bridge
                drift_result = module.handle_operation_reconcile(
                    {"operation_id": drift_operation, "command": "items patch", "original_request": drift_actual},
                    snapshot_store,
                )
                assert drift_reads == ["get:target", "find:drift"]
                assert drift_result["resolution"] == "conflict"

                claim_operation = "OP-64646464-6464-4464-8464-646464646464"
                claim_schedule = "PS-65656565-6565-4565-8565-656565656565"
                claim_before = "sha256:" + "e" * 64
                claim_request = {
                    "operation_id": claim_operation,
                    "entity": "reminder",
                    "source_id": "REMINDER_SOURCE",
                    "container_id": "REMINDER_CONTAINER",
                    "confirm_private_container": True,
                    "item_id": "UNMANAGED-1",
                    "expected_fingerprint": claim_before,
                    "managed": {"schema_version": 1, "schedule_id": claim_schedule, "entity": "reminder", "role": "task"},
                    "dry_run": True,
                }

                def claim_timeout_bridge(command: str, payload: dict | None = None) -> tuple[int, dict]:
                    assert command == "items claim" and payload is not None
                    if payload.get("dry_run"):
                        return 0, {"ok": True, "command": command, "dry_run": True, "event_store_id": "STORE-1", "mutated": False}
                    assert payload.get("expected_event_store_id") == "STORE-1"
                    return 2, {"ok": False, "error": {"code": "operation_timeout_outcome_unknown", "message": "timeout"}}

                module.run_bridge = claim_timeout_bridge
                claim_preview = module.handle_mutation("items claim", claim_request, snapshot_store)
                claim_actual = dict(claim_request, dry_run=False, preview_hash=claim_preview["preview_hash"])
                assert module.handle_mutation("items claim", claim_actual, snapshot_store)["journal_phase"] == "outcome_unknown"
                claim_get_count = 0
                claim_reads: list[str] = []

                def claim_drift_reconcile(command: str, payload: dict | None = None) -> tuple[int, dict]:
                    nonlocal claim_get_count
                    assert payload is not None
                    if command == "items find":
                        claim_reads.append("find:zero")
                        return 0, {"ok": True, "command": command, "event_store_id": "STORE-1", "count": 0, "mutated": False}
                    assert command == "items get"
                    claim_get_count += 1
                    fingerprint = claim_before if claim_get_count == 1 else "sha256:" + "f" * 64
                    claim_reads.append(f"get:{claim_get_count}")
                    return 0, {
                        "ok": True,
                        "command": command,
                        "event_store_id": "STORE-1",
                        "mutated": False,
                        "item": {
                            "entity": "reminder",
                            "source_id": "REMINDER_SOURCE",
                            "container_id": "REMINDER_CONTAINER",
                            "item_id": "UNMANAGED-1",
                            "external_id": "UNMANAGED-EXTERNAL-1",
                            "ownership": "unmanaged",
                            "recurring": False,
                            "fingerprint": fingerprint,
                        },
                    }

                module.run_bridge = claim_drift_reconcile
                claim_result = module.handle_operation_reconcile(
                    {"operation_id": claim_operation, "command": "items claim", "original_request": claim_actual},
                    snapshot_store,
                )
                assert claim_reads == ["get:1", "find:zero", "get:2"]
                assert claim_result["resolution"] == "conflict"

                unchanged_claim_operation = "OP-66666666-6666-4666-8666-666666666666"
                unchanged_claim_schedule = "PS-67676767-6767-4767-8767-676767676768"
                unchanged_claim_request = dict(
                    claim_request,
                    operation_id=unchanged_claim_operation,
                    item_id="UNMANAGED-2",
                    managed={"schema_version": 1, "schedule_id": unchanged_claim_schedule, "entity": "reminder", "role": "task"},
                    dry_run=True,
                )
                module.run_bridge = claim_timeout_bridge
                unchanged_claim_preview = module.handle_mutation("items claim", unchanged_claim_request, snapshot_store)
                unchanged_claim_actual = dict(
                    unchanged_claim_request,
                    dry_run=False,
                    preview_hash=unchanged_claim_preview["preview_hash"],
                )
                assert module.handle_mutation("items claim", unchanged_claim_actual, snapshot_store)["journal_phase"] == "outcome_unknown"
                unchanged_reads: list[str] = []

                def unchanged_claim_reconcile(command: str, payload: dict | None = None) -> tuple[int, dict]:
                    assert payload is not None
                    if command == "items find":
                        unchanged_reads.append("find:zero")
                        return 0, {"ok": True, "command": command, "event_store_id": "STORE-1", "count": 0, "mutated": False}
                    assert command == "items get"
                    unchanged_reads.append("get")
                    return 0, {
                        "ok": True,
                        "command": command,
                        "event_store_id": "STORE-1",
                        "mutated": False,
                        "item": {
                            "entity": "reminder",
                            "source_id": "REMINDER_SOURCE",
                            "container_id": "REMINDER_CONTAINER",
                            "item_id": "UNMANAGED-2",
                            "external_id": "UNMANAGED-EXTERNAL-2",
                            "ownership": "unmanaged",
                            "recurring": False,
                            "fingerprint": claim_before,
                        },
                    }

                module.run_bridge = unchanged_claim_reconcile
                unchanged_claim_result = module.handle_operation_reconcile(
                    {
                        "operation_id": unchanged_claim_operation,
                        "command": "items claim",
                        "original_request": unchanged_claim_actual,
                    },
                    snapshot_store,
                )
                assert unchanged_reads == ["get", "find:zero", "get"]
                assert unchanged_claim_result["resolution"] == "not_applied"
                _, unchanged_claim_state = snapshot_store.peek()
                assert unchanged_claim_schedule not in unchanged_claim_state["schedules"]

                malformed_claim_operation = "OP-68686868-6868-4868-8686-686868686868"
                malformed_claim_schedule = "PS-69696969-6969-4969-8969-696969696969"
                malformed_claim_request = dict(
                    claim_request,
                    operation_id=malformed_claim_operation,
                    item_id="UNMANAGED-3",
                    managed={
                        "schema_version": 1,
                        "schedule_id": malformed_claim_schedule,
                        "entity": "reminder",
                        "role": "task",
                    },
                    dry_run=True,
                )
                module.run_bridge = claim_timeout_bridge
                malformed_claim_preview = module.handle_mutation("items claim", malformed_claim_request, snapshot_store)
                malformed_claim_actual = dict(
                    malformed_claim_request,
                    dry_run=False,
                    preview_hash=malformed_claim_preview["preview_hash"],
                )
                assert module.handle_mutation("items claim", malformed_claim_actual, snapshot_store)["journal_phase"] == "outcome_unknown"
                malformed_claim_reads: list[str] = []

                def malformed_claim_reconcile(command: str, payload: dict | None = None) -> tuple[int, dict]:
                    assert payload is not None
                    malformed_claim_reads.append(command)
                    if command == "items get":
                        return 0, {
                            "ok": True,
                            "command": command,
                            "event_store_id": "STORE-1",
                            "item": {
                                "entity": "reminder",
                                "source_id": "REMINDER_SOURCE",
                                "container_id": "REMINDER_CONTAINER",
                                "item_id": "UNMANAGED-3",
                                "external_id": "UNMANAGED-EXTERNAL-3",
                                "ownership": "unmanaged",
                                "recurring": False,
                                "fingerprint": claim_before,
                            },
                            "mutated": False,
                        }
                    assert command == "items find"
                    return 0, {
                        "ok": True,
                        "command": command,
                        "event_store_id": "STORE-1",
                        "count": 1,
                        "mutated": False,
                    }

                module.run_bridge = malformed_claim_reconcile
                malformed_claim_result = module.handle_operation_reconcile(
                    {
                        "operation_id": malformed_claim_operation,
                        "command": "items claim",
                        "original_request": malformed_claim_actual,
                    },
                    snapshot_store,
                )
                assert malformed_claim_reads == ["items get", "items find"]
                assert malformed_claim_result["resolution"] == "outcome_unknown"
                assert malformed_claim_result["cause"]["error"]["code"] == "bridge_protocol_error"
                _, malformed_claim_state = snapshot_store.peek()
                assert malformed_claim_state["operations"][malformed_claim_operation]["phase"] == "outcome_unknown"
                assert malformed_claim_state["schedules"][malformed_claim_schedule]["state"] == "outcome_unknown"
            finally:
                module.run_bridge = original_run_bridge

        with tempfile.TemporaryDirectory(prefix="personal-scheduler-result-epoch-test-") as result_epoch_directory:
            result_epoch_root = Path(result_epoch_directory) / "state"
            result_epoch_root.mkdir(mode=0o700)
            result_epoch_store = configured_store(result_epoch_root)
            result_epoch_operation = "OP-71717171-7171-4171-8171-717171717171"
            result_epoch_schedule = "PS-72727272-7272-4272-8272-727272727272"
            original_run_bridge = module.run_bridge
            try:
                module.run_bridge = success_bridge
                result_epoch_preview_request = reminder_create_request(result_epoch_operation, result_epoch_schedule, True)
                result_epoch_preview = module.handle_mutation("items create", result_epoch_preview_request, result_epoch_store)
                result_epoch_actual = reminder_create_request(result_epoch_operation, result_epoch_schedule, False)
                result_epoch_actual["preview_hash"] = result_epoch_preview["preview_hash"]

                def changed_result_epoch_bridge(command: str, payload: dict | None = None) -> tuple[int, dict]:
                    assert command == "items create" and payload is not None
                    if payload.get("dry_run"):
                        return 0, {"ok": True, "command": command, "dry_run": True, "event_store_id": "STORE-1", "mutated": False}
                    assert payload.get("expected_event_store_id") == "STORE-1"
                    return 0, {
                        "ok": True,
                        "command": command,
                        "event_store_id": "STORE-2",
                        "item": {"item_id": "ITEM-NEW", "external_id": "EXTERNAL-NEW", "fingerprint": "sha256:" + "1" * 64},
                        "mutated": True,
                    }

                module.run_bridge = changed_result_epoch_bridge
                result_epoch_result = module.handle_mutation("items create", result_epoch_actual, result_epoch_store)
                assert result_epoch_result["ok"] is False
                assert result_epoch_result["journal_phase"] == "outcome_unknown"
                assert result_epoch_result["mutation_outcome"] == "unknown"
                assert "mutated" not in result_epoch_result
                _, result_epoch_state = result_epoch_store.peek()
                result_epoch_record = result_epoch_state["operations"][result_epoch_operation]
                result_epoch_schedule_state = result_epoch_state["schedules"][result_epoch_schedule]
                assert result_epoch_record["phase"] == "outcome_unknown"
                assert result_epoch_record["event_store_id"] == "STORE-1"
                assert result_epoch_state["event_store_id"] == "STORE-2"
                assert result_epoch_schedule_state["state"] == "outcome_unknown"
                assert result_epoch_schedule_state["source_id"] is None
                assert result_epoch_schedule_state["container_id"] is None
                assert result_epoch_schedule_state["item_id"] is None

                module.handle_settings_set(
                    {
                        "expected_revision": result_epoch_state["revision"],
                        "confirmed": True,
                        "event_store_id": "STORE-2",
                        "timezone": "Asia/Shanghai",
                        "event": {
                            "read_container_ids": ["EVENT_CONTAINER"],
                            "write_source_id": "EVENT_SOURCE",
                            "write_container_id": "EVENT_CONTAINER",
                            "private_confirmed": True,
                        },
                        "reminder": {
                            "read_container_ids": ["REMINDER_CONTAINER"],
                            "write_source_id": "REMINDER_SOURCE",
                            "write_container_id": "REMINDER_CONTAINER",
                            "private_confirmed": True,
                        },
                    },
                    result_epoch_store,
                )
                reconcile_bridge_called = False

                def forbidden_cross_epoch_reconcile(command: str, payload: dict | None = None) -> tuple[int, dict]:
                    nonlocal reconcile_bridge_called
                    reconcile_bridge_called = True
                    raise AssertionError("cross-epoch reconciliation reached EventKit")

                module.run_bridge = forbidden_cross_epoch_reconcile
                try:
                    module.handle_operation_reconcile(
                        {
                            "operation_id": result_epoch_operation,
                            "command": "items create",
                            "original_request": result_epoch_actual,
                        },
                        result_epoch_store,
                    )
                except module.ExecutorError as error:
                    assert error.code == "operation_event_store_changed"
                else:
                    raise AssertionError("an unresolved operation crossed EventKit store epochs")
                assert reconcile_bridge_called is False
            finally:
                module.run_bridge = original_run_bridge

        for index, (epoch_command, epoch_request) in enumerate(
            (
                (
                    "items patch",
                    {
                        "operation_id": "OP-81818181-8181-4181-8181-818181818181",
                        "entity": "reminder",
                        "source_id": "REMINDER_SOURCE",
                        "container_id": "REMINDER_CONTAINER",
                        "confirm_private_container": True,
                        "schedule_id": "PS-84848484-8484-4484-8484-848484848484",
                        "item_id": "ITEM-OLD",
                        "expected_fingerprint": "sha256:" + "a" * 64,
                        "managed": {
                            "schema_version": 1,
                            "schedule_id": "PS-84848484-8484-4484-8484-848484848484",
                            "entity": "reminder",
                            "role": "task",
                        },
                        "payload": {"title": "TARGET", "due": {"kind": "none"}, "priority": 0},
                        "dry_run": True,
                    },
                ),
                (
                    "reminders complete",
                    {
                        "operation_id": "OP-82828282-8282-4282-8282-828282828282",
                        "source_id": "REMINDER_SOURCE",
                        "container_id": "REMINDER_CONTAINER",
                        "confirm_private_container": True,
                        "schedule_id": "PS-85858585-8585-4585-8585-858585858585",
                        "item_id": "ITEM-OLD",
                        "expected_fingerprint": "sha256:" + "b" * 64,
                        "dry_run": True,
                    },
                ),
                (
                    "items delete",
                    {
                        "operation_id": "OP-83838383-8383-4383-8383-838383838383",
                        "entity": "reminder",
                        "source_id": "REMINDER_SOURCE",
                        "container_id": "REMINDER_CONTAINER",
                        "confirm_private_container": True,
                        "schedule_id": "PS-86868686-8686-4686-8686-868686868686",
                        "item_id": "ITEM-OLD",
                        "expected_fingerprint": "sha256:" + "c" * 64,
                        "dry_run": True,
                    },
                ),
            )
        ):
            with tempfile.TemporaryDirectory(prefix=f"personal-scheduler-no-cache-epoch-{index}-") as no_cache_directory:
                no_cache_root = Path(no_cache_directory) / "state"
                no_cache_root.mkdir(mode=0o700)
                no_cache_store = configured_store(no_cache_root)
                schedule_id = epoch_request["schedule_id"]
                operation_id = epoch_request["operation_id"]
                _, before_epoch_state = no_cache_store.peek()
                assert schedule_id not in before_epoch_state["schedules"]
                original_run_bridge = module.run_bridge
                try:
                    def no_cache_changed_epoch_bridge(command: str, payload: dict | None = None) -> tuple[int, dict]:
                        assert command == epoch_command and payload is not None
                        if payload.get("dry_run"):
                            return 0, {
                                "ok": True,
                                "command": command,
                                "dry_run": True,
                                "event_store_id": "STORE-1",
                                "mutated": False,
                            }
                        assert payload.get("expected_event_store_id") == "STORE-1"
                        return 0, {
                            "ok": True,
                            "command": command,
                            "event_store_id": "STORE-2",
                            "item": {
                                "item_id": "ITEM-NEW",
                                "external_id": "EXTERNAL-NEW",
                                "fingerprint": "sha256:" + "d" * 64,
                            },
                            "mutated": True,
                        }

                    module.run_bridge = no_cache_changed_epoch_bridge
                    preview = module.handle_mutation(epoch_command, epoch_request, no_cache_store)
                    _, after_preview_state = no_cache_store.peek()
                    assert schedule_id not in after_preview_state["schedules"]
                    actual_request = dict(epoch_request, dry_run=False, preview_hash=preview["preview_hash"])
                    result = module.handle_mutation(epoch_command, actual_request, no_cache_store)
                    assert result["ok"] is False
                    assert result["journal_phase"] == "outcome_unknown"
                    assert result["error"]["code"] == "event_store_changed_during_mutation"
                    assert "mutated" not in result
                    _, changed_state = no_cache_store.peek()
                    operation = changed_state["operations"][operation_id]
                    schedule = changed_state["schedules"][schedule_id]
                    assert changed_state["event_store_id"] == "STORE-2"
                    assert operation["phase"] == "outcome_unknown"
                    assert operation["event_store_id"] == "STORE-1"
                    assert operation["source_id"] == "REMINDER_SOURCE"
                    assert operation["container_id"] == "REMINDER_CONTAINER"
                    assert operation["item_id"] == "ITEM-OLD"
                    assert schedule["state"] == "outcome_unknown"
                    assert schedule["event_store_id"] == "STORE-2"
                    for locator in ("source_id", "container_id", "item_id", "external_id", "last_fingerprint"):
                        assert schedule[locator] is None
                finally:
                    module.run_bridge = original_run_bridge

        state_path = root / module.STATE_FILE_NAME
        os.chmod(state_path, 0o644)
        try:
            store.peek()
        except module.ExecutorError as error:
            assert error.code == "unsafe_state"
        else:
            raise AssertionError("overly broad state permissions were accepted")

        assert stat.S_IMODE(root.stat().st_mode) == 0o700

        invalid = module.default_state()
        invalid["operations"]["OP-56565656-5656-4656-8656-565656565656"] = {
            "kind": "create",
            "phase": "in_flight",
            "entity": "reminder",
            "schedule_id": "PS-78787878-7878-4878-8878-787878787878",
            "event_store_id": "STORE-1",
            "source_id": "S",
            "container_id": "C",
            "item_id": None,
            "before_fingerprint": None,
            "intent_hash": "sha256:" + "a" * 64,
            "created_at": "2026-09-01T00:00:00Z",
            "started_at": "2026-09-01T00:00:01Z",
            "finished_at": None,
            "outcome": "verified_local",
            "error_code": None,
        }
        try:
            module.validate_state(invalid)
        except module.ExecutorError as error:
            assert error.code == "unsafe_state"
        else:
            raise AssertionError("an inconsistent in-flight outcome was accepted")

    print("Executor state-machine tests passed. EventKit was not called.")


if __name__ == "__main__":
    main()
