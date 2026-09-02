#!/usr/bin/env python3
"""Offline contract checks. Never authorizes or reads Calendar/Reminders data."""

from __future__ import annotations

import json
import plistlib
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "Sources" / "main.swift"
PROTOCOL = ROOT / "protocol-v1.json"
PLIST = ROOT / "Info.plist"
BUILD = ROOT / "build.sh"
RUN = ROOT / "run.sh"
VALIDATOR = ROOT / "validate-envelope.py"
PUBLIC_ENTRYPOINT = ROOT.parent / "apple-eventkit-reader.sh"

EXPECTED_COMMANDS = {
    "capabilities",
    "doctor",
    "self-test",
    "authorize",
    "sources list",
    "containers list",
    "events list",
    "reminders list",
}
FORBIDDEN_OUTPUT_FIELDS = {
    "notes",
    "attendees",
    "organizer",
    "url",
    "alarms",
    "recurrence_rules",
}


def assert_protocol() -> None:
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    assert protocol["protocol_version"] == 1
    assert protocol["reader_version"] == "1.0.0"
    assert set(protocol["commands"]) == EXPECTED_COMMANDS
    assert protocol["read_only_contract"]["eventkit_data_mutated"] is False
    assert protocol["read_only_contract"]["permission_prompt_requires"] == {"confirmed": True}
    assert set(protocol["read_only_contract"]["never_returned"]) == FORBIDDEN_OUTPUT_FIELDS
    string_limits = protocol["read_only_contract"]["string_and_output_limits"]
    assert string_limits["input_identifier_utf8_bytes"] == 4096
    assert string_limits["eventkit_identifier_utf8_bytes"] == 4096
    assert string_limits["eventkit_title_utf8_bytes"] == 4096

    events = protocol["commands"]["events list"]
    assert events["input"]["detail"] == "busy | summary"
    assert "limit" in events["input"] and "window" in events["input"] and "calendar_ids" in events["input"]
    assert "title" not in events["busy_item_fields"]
    assert events["summary_additional_item_fields"] == ["title", "created_at", "last_modified_at"]

    reminders = protocol["commands"]["reminders list"]
    assert set(reminders["selection_semantics"]) == {
        "completed_in_window",
        "incomplete_due_in_window",
    }
    assert "completed" in reminders["output_item_fields"]
    assert "completion_at" in reminders["output_item_fields"]
    assert "limit" in reminders["input"] and "window" in reminders["input"] and "list_ids" in reminders["input"]

    for example in protocol["examples"].values():
        window = example["window"]
        assert window["start_at"].endswith("+08:00")
        assert window["end_at"].endswith("+08:00")
        assert 1 <= example["limit"] <= 500


def assert_bundle_identity() -> None:
    plist = plistlib.loads(PLIST.read_bytes())
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    expected_id = "io.github.sylviachenxy.sylvia-agent-skills.weekly-review-eventkit-reader"
    assert plist["CFBundleIdentifier"] == expected_id
    assert protocol["identity"]["bundle_id"] == expected_id
    assert plist["LSMinimumSystemVersion"] == "14.0"
    assert plist["WeeklyReviewSignatureKind"] == "adhoc"
    assert "read-only" in plist["NSCalendarsFullAccessUsageDescription"]
    assert "read-only" in plist["NSRemindersFullAccessUsageDescription"]
    assert "authorization again" in protocol["identity"]["caveat"]


def assert_native_surface_is_read_only() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    expected_id = "io.github.sylviachenxy.sylvia-agent-skills.weekly-review-eventkit-reader"
    assert expected_id in source

    forbidden_patterns = (
        r"\.\s*(?:save|remove|commit|rollback|reset)\s*\(",
        r"\b(?:EKEvent|EKReminder)\s*\(\s*eventStore\s*:",
        r"\brequestWriteOnlyAccessToEvents\b",
        r"\b(?:event|reminder)\.(?:calendar|title|notes|url|alarms|recurrenceRules|isCompleted)\s*=",
        r"\bcommit\s*:",
    )
    for pattern in forbidden_patterns:
        assert re.search(pattern, source) is None, pattern

    for property_name in ("notes", "attendees", "organizer", "url", "alarms"):
        assert re.search(rf"\.{property_name}\b", source) is None

    command_cases = set(re.findall(r'^\s*case "([a-z-]+(?: [a-z-]+)?)":$', source, flags=re.MULTILINE))
    assert EXPECTED_COMMANDS <= command_cases
    mutation_words = {"create", "update", "patch", "delete", "remove", "complete", "save", "claim"}
    assert not any(any(word in command.split() for word in mutation_words) for command in command_cases)
    assert 'guard try requiredBool(input, "confirmed")' in source
    for placeholder_name in (
        "readerVersion",
        "protocolVersion",
        "operation",
        "context",
        "key",
        "field",
        "status.rawValue",
        "type.rawValue",
        "value.rawValue",
    ):
        assert re.search(rf"(?<!\\)\({re.escape(placeholder_name)}\)", source) is None
    assert "private let maximumOutputBytes = 4_194_304" in source
    assert "data.count < maximumOutputBytes" in source
    assert "output_limit_exceeded" in source
    assert "maximumIdentifierUTF8Bytes" in source
    assert "maximumTitleUTF8Bytes" in source
    assert "boundedResultString" in source
    assert source.count("boundedResultString(context.store.eventStoreIdentifier") == 4
    assert "predicateForCompletedReminders" in source
    assert "predicateForIncompleteReminders" in source
    assert "completionDate >= window.start && completionDate < window.end" in source
    assert "dueDate >= window.start && dueDate < window.end" in source


def assert_wrappers() -> None:
    build = BUILD.read_text(encoding="utf-8")
    run = RUN.read_text(encoding="utf-8")
    validator = VALIDATOR.read_text(encoding="utf-8")
    public = PUBLIC_ENTRYPOINT.read_text(encoding="utf-8")
    assert "-apple-macosx14.0" in build
    assert "-framework EventKit" in build
    assert "/usr/bin/codesign --force --sign -" in build
    assert "ad_hoc_rebuild" not in build
    assert "WEEKLY_REVIEW_EVENTKIT_RUN_TOKEN" in public
    assert "WEEKLY_REVIEW_EVENTKIT_SWIFT_TOKEN" in run
    assert "4194304" in run
    assert "operation_timeout_outcome_unknown" not in run
    assert 'emit_failure "reader_build_failed"' in run
    assert 'emit_failure "reader_protocol_error"' in run
    assert "validate-envelope.py" in run
    assert "/usr/bin/python3" in run
    assert "json.loads(" in validator
    assert "object_pairs_hook=unique_object" in validator
    assert "parse_constant=reject_nonfinite" in validator
    assert "parse_float=finite_float" in validator
    assert "math.isfinite" in validator
    assert "require_finite_numbers(value)" in validator
    assert "allow_nan=False" in validator
    assert "top-level value is not an object" in validator
    assert "worker_status != 2" in validator


def main() -> None:
    assert_protocol()
    assert_bundle_identity()
    assert_native_surface_is_read_only()
    assert_wrappers()
    print("Offline EventKit reader contract checks passed; no EventKit data was accessed.")


if __name__ == "__main__":
    main()
